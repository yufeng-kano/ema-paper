"""Job execution: one document, page by page (docs/jobs.md, docs/parsing.md).

This is the `parse` agent's runner (docs/agents.md). Shape of a run: read the job, resolve
the user's key, work out the pages, then fan the pages out. Poppler work is bounded by the
worker-wide render semaphore; upstream calls are bounded by this job's own vision budget,
which halves itself when the upstream says 429.

A page fails on its own: the other pages still run to their end and are kept on their rows.
But a document is every page or nothing — a job with a failed page fails whole, and its
failed pages are retried alone later, the kept ones never transcribed again (docs/jobs.md
§ Retrying failed pages). A dead key or exhausted credits end the job at once.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
import threading
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents import get_agent
from ..agents.bbox import convention_for_job, prepare_image
from ..config import Settings
from ..db.models import Document, Job, JobPage, Result, UsageLog, utcnow
from ..errors import ApiError
from ..parsing import poppler
from ..parsing.figures import discard_job_figures, publish_job_figures, save_page_figures
from ..parsing.images import ImageError, normalize_image
from ..parsing.markdown import PLACEHOLDER_RE, PageMarkdown, assemble
from ..upstream.client import (
    Connection,
    EmptyCompletion,
    IncompleteCompletion,
    PageTranscription,
    PaymentRequired,
    RateLimited,
    UpstreamError,
    Usage,
    load_connection,
    transcribe_page,
)
from ..workspace.documents import (
    copy_original,
    ensure_within_quota,
    folder_names,
    is_live,
    keep_source,
    materialize_document,
    original_of,
    user_folders,
)
from ..workspace.layout import document_relative_dir, user_root
from ..workspace.locks import backup_shared, workspace_lock
from . import events
from .queue import parse_pages_spec

logger = logging.getLogger(__name__)

# Retry policy for one upstream call — 429s and transient failures alike
# (docs/parsing.md § Upstream usage).
VISION_MAX_ATTEMPTS = 4
VISION_BACKOFF_BASE_SECONDS = 1.0

# A second 402 without an intervening success means the key is dead, not the page.
PAYMENT_FAILURES_BEFORE_ABORT = 2


class JobAborted(Exception):
    """A failure that ends the whole job rather than one page."""


class VisionBudget:
    """Per-job concurrency limit that can shrink while the job runs.

    A plain semaphore cannot give permits back to nobody, and 429 handling requires
    exactly that: after a rate limit the job must keep running, more slowly.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._in_flight = 0
        self._condition = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @asynccontextmanager
    async def slot(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self._limit)
            self._in_flight += 1
        try:
            yield
        finally:
            async with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()

    async def halve(self) -> None:
        async with self._condition:
            self._limit = max(1, self._limit // 2)


# The one conversion method (docs/parsing.md § Vision-only conversion).
VISION = "vision"

# The failure reason of a page its model blocked: the one a fallback model is tried on
# (docs/parsing.md § Fallback model).
BLOCKED = "blocked"


@dataclass
class PageOutcome:
    page: int
    width_pt: float
    height_pt: float
    method: str | None = None
    error: str | None = None
    markdown: str = ""
    # The model whose answer the page holds, or that failed it last; None when no model
    # saw the page (a render failure).
    model: str | None = None
    # Every call the page billed, with the model it went to.
    usage: list[tuple[str, Usage]] = field(default_factory=list)


@dataclass(frozen=True)
class KeptPage:
    """A page an earlier attempt of the job finished: kept, never transcribed again
    (docs/jobs.md § Retrying failed pages)."""

    markdown: str
    method: str | None
    model: str | None


@dataclass
class RunState:
    """Cross-page state for one run: what the fan-out has to agree about."""

    budget: VisionBudget
    # For log lines only: page tasks see the state, not the job row.
    job_id: str = ""
    # UPSTREAM_RESPONSE_MAX_BYTES, carried here so page tasks need no Settings.
    max_response_bytes: int = 33_554_432
    payment_failures: int = 0
    # This job's own directory under FIGURES_DIR; None turns crop persistence off
    # (docs/parsing.md § Figure crops).
    figures_dir: Path | None = None
    # The worker's generation, the fence on every write to the job's row; None when the
    # caller claimed without one (docs/jobs.md § Queue).
    generation: int | None = None
    # FIGURES_PER_PAGE_MAX — the crop cap one page may spend (docs/parsing.md).
    figures_per_page: int = 40
    # Crop threads cannot be cancelled from the event loop, so writing and discarding
    # serialize on this guard; once `figures_discarded` is set, a late crop thread writes
    # nothing and cannot recreate a directory the cleanup just removed.
    figures_guard: threading.Lock = field(default_factory=threading.Lock)
    figures_discarded: bool = False


def result_payload(result: Result) -> dict:
    """The result shape both planes return (docs/api.md § GET /v1/jobs/{id}/result)."""
    return {
        "markdown": result.markdown,
        "pages": result.pages,
        "figures": result.figures,
        "errors": result.errors,
        "meta": result.meta,
    }


def partial_result_payload(job: Job, rows: list[JobPage]) -> dict:
    """A running job's finished pages, in the result's own shape (docs/api.md § Partial).

    The stored per-page markdown goes through the same `assemble` the final result gets —
    markers, renumbered placeholders, cleaned boxes. Figure ids are per-snapshot: a page
    finishing out of order renumbers them, which is why nothing may cache this payload.
    `pages` carries only what is known so far — no page dimensions, which only the final
    outcome holds.
    """
    # `is not None`, not truthiness: a blank page stores "" with no error, and it must
    # count as finished here exactly as it does in the final assembly (which drops empty
    # bodies itself either way).
    finished = [row for row in rows if not row.error and row.markdown is not None]
    document = assemble(
        [PageMarkdown(page=row.page_no, markdown=row.markdown or "") for row in finished]
    )
    return {
        "markdown": document.markdown,
        "pages": [
            {"page": row.page_no, "method": row.method, "model": row.model, "error": row.error}
            for row in rows
        ],
        "figures": document.figures,
        "errors": [{"page": row.page_no, "reason": row.error} for row in rows if row.error],
        "meta": {
            "job_id": str(job.id),
            "agent": job.agent,
            "model": job.model,
            "bbox_format": job.bbox_format,
            "pipeline_version": job.pipeline_version,
            "sha256": job.sha256,
            "cached": False,
            "partial": True,
            "page_count": job.page_count,
            "pages_done": job.pages_done,
        },
    }


def job_prompt(job: Job) -> str:
    """The transcription prompt template this job runs.

    The effective template is stored on the row at enqueue time (docs/parsing.md §
    Prompts); jobs from before that column exist fall back to the agent's default.
    """
    return job.prompt or get_agent(job.agent).default_prompt_template


async def _call_upstream(state: RunState, call):
    """Run one upstream call under the job's budget, backing off on 429 and on transient
    failures.

    Each rate limit also halves this job's concurrency, so a job that is being throttled
    stops fighting the throttle. A transient failure does not: a flaky upstream is not
    asking for less traffic. The backoff sleeps outside the slot, so a waiting page never
    holds up one that could run.
    """
    for attempt in range(VISION_MAX_ATTEMPTS):
        last = attempt == VISION_MAX_ATTEMPTS - 1
        async with state.budget.slot():
            try:
                return await call()
            except RateLimited as exc:
                if last:
                    raise
                throttled = True
                delay = exc.retry_after or VISION_BACKOFF_BASE_SECONDS * (2**attempt)
            except UpstreamError as exc:
                if last or not exc.transient:
                    raise
                throttled = False
                delay = VISION_BACKOFF_BASE_SECONDS * (2**attempt)
        if throttled:
            await state.budget.halve()
        await asyncio.sleep(delay)
    raise RateLimited()


async def _guarded_call(state: RunState, outcome: PageOutcome, model: str, call):
    """Apply the upstream failure policy of docs/parsing.md to one page's call to `model`.

    Returns the call's result, or None when the page failed — the failure is already
    recorded on the outcome. Failures that will repeat for every page abort the job.
    """
    try:
        result = await _call_upstream(state, call)
    except PaymentRequired:
        state.payment_failures += 1
        if state.payment_failures >= PAYMENT_FAILURES_BEFORE_ABORT:
            raise JobAborted("the upstream reported exhausted credits") from None
        outcome.error = "payment"
        return None
    except RateLimited:
        outcome.error = "rate limited"
        return None
    except EmptyCompletion as exc:
        # Empty through every attempt: the page has nothing on it, which is a result and
        # not a failure (docs/parsing.md § Upstream usage).
        result = PageTranscription(markdown="", usage=exc.usage)
    except IncompleteCompletion as exc:
        # The model stopped its own answer: an answered call, billed like one, and the
        # page's failure at once (docs/parsing.md § Upstream usage).
        logger.warning("job %s page %d %s: %s", state.job_id, outcome.page, exc.reason, model)
        state.payment_failures = 0
        outcome.usage.append((model, exc.usage))
        outcome.error = exc.reason
        return None
    except UpstreamError as exc:
        if exc.fatal:
            raise JobAborted("the upstream rejected the stored key") from None
        # The message is a status code or a fixed phrase — never credentials or content.
        logger.warning("job %s page %d upstream call failed: %s", state.job_id, outcome.page, exc)
        outcome.error = "upstream call failed"
        return None

    state.payment_failures = 0
    outcome.usage.append((model, result.usage))
    return result


async def _transcribe(
    job: Job,
    state: RunState,
    connection: Connection,
    outcome: PageOutcome,
    image: Path,
    page_no: int,
) -> PageTranscription | None:
    """The page's transcription, or None when it failed — the outcome says why. The run's
    model first (a retry's, else the job's); a page it blocked goes once more to the
    job's fallback, when it has one that is not that model (docs/parsing.md § Fallback
    model). The outcome names the model that answered, or failed, last."""
    first = job.retry_model or job.model
    models = [first]
    if job.fallback_model and job.fallback_model != first:
        models.append(job.fallback_model)
    transcription = None
    for model in models:
        outcome.error = None
        outcome.model = model
        convention = convention_for_job(model, job.pipeline_version)
        # Pillow work, so it runs beside the in-flight HTTP calls rather than under them —
        # and settled under a cancel, so no thread writes the work directory after the
        # requeue has removed it. Prepared per model: each family is sent its own image.
        sent = await _to_the_end(asyncio.to_thread(prepare_image, image, convention))
        try:
            transcription = await _guarded_call(
                state,
                outcome,
                model,
                functools.partial(
                    transcribe_page,
                    connection,
                    model,
                    job_prompt(job),
                    sent.path,
                    page_no,
                    convention=convention,
                    max_response_bytes=state.max_response_bytes,
                ),
            )
        finally:
            sent.cleanup()
        if transcription is not None or outcome.error != BLOCKED:
            break
    return transcription


async def _process_pdf_page(
    job: Job,
    state: RunState,
    connection: Connection,
    info: poppler.PdfInfo,
    page_no: int,
    source: Path,
    work_dir: Path,
    render_slots: asyncio.Semaphore,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> PageOutcome:
    size = info.page_size(page_no)
    outcome = PageOutcome(
        page=page_no, width_pt=size.width_pt, height_pt=size.height_pt, method=VISION
    )

    try:
        async with render_slots:
            image = await poppler.render_page(source, page_no, size, cwd=work_dir)
    except poppler.PopplerError as exc:
        # The Poppler detail is diagnostic (its stderr), never document content.
        logger.warning("job %s page %d render failed: %s", job.id, page_no, exc)
        outcome.error = "render failed"
        await _record_page(sessionmaker, job, outcome, state.generation)
        return outcome

    try:
        transcription = await _transcribe(job, state, connection, outcome, image, page_no)
        if transcription is not None:
            outcome.markdown = transcription.markdown
        # Billing before the optional raster work: a SIGTERM landing mid-crop cancels this
        # task, and the calls that already ran must be in `usage_log` first.
        await _record_page(sessionmaker, job, outcome, state.generation)
        if transcription is not None:
            await _save_figures(state, outcome.markdown, image, page_no)
    finally:
        # The rendered page has served its purpose; nothing may outlive the call.
        image.unlink(missing_ok=True)
    return outcome


async def _restore_pdf_page(
    job: Job,
    state: RunState,
    info: poppler.PdfInfo,
    page_no: int,
    kept: KeptPage,
    source: Path,
    work_dir: Path,
    render_slots: asyncio.Semaphore,
) -> PageOutcome:
    """A page an earlier attempt finished: its stored transcription, and its crops made
    again from it on a fresh render — no upstream call, and no render for a page without
    figures. An attempt's crops are its own (docs/parsing.md § Figure crops); a render that
    fails costs the page its crops, never the page."""
    size = info.page_size(page_no)
    outcome = PageOutcome(
        page=page_no,
        width_pt=size.width_pt,
        height_pt=size.height_pt,
        method=kept.method or VISION,
        markdown=kept.markdown,
        model=kept.model or job.model,
    )
    if state.figures_dir is None or not PLACEHOLDER_RE.search(kept.markdown):
        return outcome
    try:
        async with render_slots:
            image = await poppler.render_page(source, page_no, size, cwd=work_dir)
    except poppler.PopplerError as exc:
        logger.warning("job %s page %d render for its crops failed: %s", job.id, page_no, exc)
        return outcome
    try:
        await _save_figures(state, outcome.markdown, image, page_no)
    finally:
        image.unlink(missing_ok=True)
    return outcome


async def _process_image(
    job: Job,
    state: RunState,
    connection: Connection,
    source: Path,
    work_dir: Path,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> PageOutcome:
    try:
        normalized = normalize_image(source, work_dir)
    except ImageError:
        outcome = PageOutcome(
            page=1, width_pt=0, height_pt=0, error="the image could not be decoded"
        )
        await _record_page(sessionmaker, job, outcome, state.generation)
        return outcome

    outcome = PageOutcome(
        page=1,
        width_pt=float(normalized.width_px),
        height_pt=float(normalized.height_px),
        method=VISION,
    )
    try:
        transcription = await _transcribe(job, state, connection, outcome, normalized.path, 1)
        if transcription is not None:
            outcome.markdown = transcription.markdown
        # Same ordering as the PDF path: the billed call is persisted before crop work.
        await _record_page(sessionmaker, job, outcome, state.generation)
        if transcription is not None:
            await _save_figures(state, outcome.markdown, normalized.path, 1)
    finally:
        normalized.path.unlink(missing_ok=True)
    return outcome


async def _restore_image(
    job: Job, state: RunState, source: Path, work_dir: Path, kept: KeptPage
) -> PageOutcome:
    """The image's one page, finished by an earlier attempt: kept as `_restore_pdf_page`
    keeps a PDF page, its size and its crops taken from the image again."""
    outcome = PageOutcome(
        page=1,
        width_pt=0,
        height_pt=0,
        method=kept.method or VISION,
        markdown=kept.markdown,
        model=kept.model or job.model,
    )
    try:
        normalized = normalize_image(source, work_dir)
    except ImageError:
        logger.warning("job %s image no longer decodes; its kept page has no crops", job.id)
        return outcome
    outcome.width_pt = float(normalized.width_px)
    outcome.height_pt = float(normalized.height_px)
    try:
        await _save_figures(state, outcome.markdown, normalized.path, 1)
    finally:
        normalized.path.unlink(missing_ok=True)
    return outcome


def _locked_save_figures(state: RunState, markdown: str, image: Path, page_no: int) -> None:
    """Thread body: crop under the guard, unless this job's figures were discarded."""
    with state.figures_guard:
        if state.figures_discarded or state.figures_dir is None:
            return
        save_page_figures(markdown, image, page_no, state.figures_dir, state.figures_per_page)


async def _save_figures(state: RunState, markdown: str, image: Path, page_no: int) -> None:
    """Persist the page's figure crops before retention deletes its render.

    Pillow work runs off the event loop; the fan-out must not stall behind an image
    decode. The guard it runs under is what lets `_discard_figures` wait it out.
    """
    if state.figures_dir is None:
        return
    await asyncio.to_thread(_locked_save_figures, state, markdown, image, page_no)


def _publish_figures(state: RunState, figures_root: Path, job_id: uuid.UUID) -> None:
    """The attempt's crops under the job's name, once its result is committed. Under the
    guard, so no crop thread writes into a directory being moved; the flag then turns
    away one that has not started (docs/parsing.md § Figure crops)."""
    if state.figures_dir is None:
        return
    with state.figures_guard:
        state.figures_discarded = True
        publish_job_figures(figures_root, job_id, state.figures_dir)


def _discard_figures(state: RunState) -> None:
    """Drop an abandoned attempt's crops, waiting out any in-flight crop thread.

    Synchronous on purpose: it runs from `run_job`'s outermost finally, which executes
    even under cancellation — where a fresh await could be interrupted again. Taking the
    guard means a crop thread mid-write finishes first, and the discard flag means one
    that has not started yet writes nothing (docs/parsing.md § Figure crops).
    """
    if state.figures_dir is None:
        return
    with state.figures_guard:
        state.figures_discarded = True
        shutil.rmtree(state.figures_dir, ignore_errors=True)


def _owned(generation: int | None) -> list:
    """The fence: the row still holds the caller's generation (docs/jobs.md § Queue)."""
    return [Job.worker_generation == generation] if generation is not None else []


async def _record_page(
    sessionmaker: async_sessionmaker[AsyncSession],
    job: Job,
    outcome: PageOutcome,
    generation: int | None = None,
) -> None:
    """Persist one finished page: its row, the job's progress and the calls it billed.
    The progress goes first, fenced on the generation: a row another worker has taken
    (this one lost the worker lock) gets no page from here — the other's pages are its
    own — but the calls are logged whatever happened, since the user's key paid for
    them (docs/jobs.md § Queue)."""
    async with sessionmaker() as db:
        owned = (
            await db.execute(
                update(Job)
                .where(Job.id == job.id, *_owned(generation))
                .values(pages_done=Job.pages_done + 1)
            )
        ).rowcount == 1
        if owned:
            db.add(
                JobPage(
                    job_id=job.id,
                    page_no=outcome.page,
                    method=outcome.method,
                    status="failed" if outcome.error else "succeeded",
                    error=outcome.error,
                    # What the partial result reads while the job runs, and what a later
                    # attempt keeps (docs/jobs.md § Progress); none on a failed page.
                    markdown=outcome.markdown if not outcome.error else None,
                    model=outcome.model,
                )
            )
        else:
            logger.warning(
                "job %s is no longer this worker's; page %d is not recorded", job.id, outcome.page
            )
        for model, usage in outcome.usage:
            db.add(
                UsageLog(
                    user_id=job.user_id,
                    job_id=job.id,
                    model=model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cost=usage.cost,
                    cost_source=usage.cost_source,
                )
            )
        if owned:
            await events.notify(db, job.id)
        await db.commit()


async def fail_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_id: uuid.UUID,
    error: str,
    generation: int | None = None,
) -> None:
    """A job whose task raised past `run_job` (a database error while the terminal state
    was being written, say): failed, never requeued — the parse ran and billed the user's
    key, and a fault of ours is no reason to run it again. A job no longer `running` (its
    result committed before what raised), or no longer this worker's (docs/jobs.md
    § Queue), is left as it is. With settings, like every other terminal path: a library
    row keeps its original here too."""
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status != "running":
            return
        if generation is not None and job.worker_generation != generation:
            return
    await _finish(
        sessionmaker,
        job_id,
        status="failed",
        error=error,
        settings=settings,
        generation=generation,
    )


async def _to_the_end(coro):
    """Await `coro` so that a cancel of the caller waits for it instead of interrupting
    it; the cancel is raised once it is done. For the finalization of a committed result
    (docs/jobs.md § Retention): the row is `succeeded`, so a shutdown's requeue would
    not take it back, and what is half-copied under a lock that a cancel released would
    be nobody's to finish. And for a page's image preparation in a thread, which a
    cancelled await does not stop: the work directory is removed and the job requeued
    only once nothing writes into it any more."""
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            logger.error("work interrupted by a cancel failed", exc_info=task.exception())
        raise


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    result: dict | None = None,
    settings: Settings | None = None,
    generation: int | None = None,
    publish: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Write the terminal state and delete the source document (docs/jobs.md § Retention).
    False when nothing was written: the job is gone, or no longer this worker's.
    `publish` runs right after a terminal state that was written, under the same shared
    hold of the backup lock and before the documents behind the job are materialized:
    the crops of a succeeded parse land under the job's name before a backup can see the
    result, so no snapshot carries one without them, and before the document copies are
    made, which take their crops from there (docs/workspace.md § Layout).

    A succeeded parse behind a library document keeps its original instead: the source
    moves into the document's workspace directory (docs/workspace.md). A failed job's crop
    directory goes too — `run_job`'s outermost finally discards it (through the crop
    guard), so a late crop thread cannot write it back.
    """
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return False
        user_id = job.user_id
    if settings is None:
        return await _finish_held(
            sessionmaker,
            job_id,
            status=status,
            error=error,
            result=result,
            generation=generation,
            publish=publish,
        )
    # The rows that say the parse is done, and the original and copies they describe,
    # land in one backup snapshot: the backup lock is held shared from before the commit
    # until the files are in place, since the upload directory they come from is not
    # backed up (docs/workspace.md § Backups).
    async with backup_shared(user_root(settings.workspaces_dir, user_id)):
        return await _finish_held(
            sessionmaker,
            job_id,
            status=status,
            error=error,
            result=result,
            settings=settings,
            generation=generation,
            publish=publish,
        )


async def _finish_held(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    result: dict | None = None,
    settings: Settings | None = None,
    generation: int | None = None,
    publish: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return False
        # The job's row lock before anything is written or the recipients are read:
        # filing takes it before it inserts, so a row filed meanwhile is either here or
        # filed after, as a finished parse — never filed in the gap and left without its
        # original (docs/jobs.md). And the fence under it: a row another worker has
        # taken gets neither this result nor this failure (docs/jobs.md § Queue).
        await db.execute(select(Job.id).where(Job.id == job.id).with_for_update())
        await db.refresh(job)
        if generation is not None and job.worker_generation != generation:
            logger.warning(
                "job %s is no longer this worker's; its terminal state is not written", job_id
            )
            return False
        if result is not None:
            db.add(
                Result(
                    job_id=job.id,
                    markdown=result["markdown"],
                    pages=result["pages"],
                    figures=result["figures"],
                    errors=result["errors"],
                    meta=result["meta"],
                )
            )
        job.status = status
        job.error = error
        job.finished_at = utcnow()
        documents: list[Document] = []
        if settings is not None:
            # Whatever the terminal state: a failed parse's row keeps its original as a
            # finished one does, so the upload is not lost with the error (docs/jobs.md).
            documents = list(
                (
                    await db.execute(
                        select(Document).where(Document.job_id == job.id).order_by(Document.id)
                    )
                ).scalars()
            )
        source = Path(job.source_path) if job.source_path else None
        if source is not None and not documents:
            if job.retain_source and source.is_file():
                # A library upload whose row has not landed yet: the upload stays where
                # it is for that row to take (docs/jobs.md § Retention).
                pass
            else:
                # Nothing keeps it: deleted now, with the terminal state.
                source.unlink(missing_ok=True)
                job.source_deleted_at = utcnow()
        await events.notify(db, job.id)
        await db.commit()
    if publish is not None:
        # The crops under the job's name now, before the copies below are made from them.
        await publish()
    if source is not None and documents:
        # The terminal state is committed and billed; keeping the original is best effort
        # from here (docs/jobs.md § Retention): a copy that fails is logged, never a
        # reason to leave the job `running`. Whether the upload is still there is not
        # checked here: a row filed meanwhile may have taken it as its own original, and
        # retention then copies from that one for the recipients found above.
        try:
            await _retain_source(sessionmaker, settings, job_id, source)
        except Exception:  # noqa: BLE001 - retention must not undo a finished parse
            logger.exception("could not keep the source of job %s", job_id)
    if settings is not None and documents and status == "succeeded":
        # The result is committed: the document directories can take their copies now.
        async with sessionmaker() as db:
            for document in documents:
                try:
                    # The rows again, in this session: the ones above belong to the one
                    # that committed the result, and a detached row cannot be refreshed.
                    row = await db.get(Document, document.id)
                    # A document in the Trash gets its directory when it is put back
                    # (docs/workspace.md § The Trash); materialization refuses it anyway.
                    if row is not None and is_live(row, await user_folders(db, row.user_id)):
                        await materialize_document(db, settings, row)
                except Exception:  # noqa: BLE001 - the parse succeeded; the copy can wait
                    logger.exception("could not materialize document %d", document.id)
    return True


async def _retain_source(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_id: uuid.UUID,
    source: Path,
) -> None:
    """One parse filed in several places: every copy keeps the original, the last one
    by moving it (docs/jobs.md § Retention). The user's workspace lock first, then the
    job's row — the one order every holder of both takes — and the recipients are read
    under that row lock: filing takes it before it inserts, so a row filed while this
    runs is either here or filed afterwards, as a finished parse. Each copy is checked
    against the quota and taken where the document is at that moment. A recipient that
    holds an original already is left alone, and when the upload is gone — a row filed
    between the terminal commit and this lock found no original yet and took the upload
    as its own — the rest are copied from the original any recipient holds."""
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return
        root = user_root(settings.workspaces_dir, job.user_id)
        async with workspace_lock(root):
            await db.refresh(job, with_for_update=True)
            documents = list(
                (
                    await db.execute(
                        select(Document)
                        .where(Document.job_id == job_id, Document.user_id == job.user_id)
                        .order_by(Document.id)
                    )
                ).scalars()
            )
            donor: int | None = None
            pending: list[Document] = []
            for row in documents:
                if original_of(root, row.id) is None:
                    pending.append(row)
                elif donor is None:
                    donor = row.id
            if not source.is_file() and donor is None and pending:
                logger.warning(
                    "the source of job %s is gone and no document of it holds an original; "
                    "%d document(s) keep none",
                    job_id,
                    len(pending),
                )
                pending = []
            folders = await user_folders(db, job.user_id)
            for index, row in enumerate(pending):
                # A document in the Trash keeps its original without the shell's copy,
                # which would make a directory under `files/` for a file that is not
                # there (docs/workspace.md § The Trash).
                relative = None
                if is_live(row, folders):
                    relative = document_relative_dir(
                        await folder_names(db, row.user_id, row.folder_id), row.name
                    )
                try:
                    # Every copy counts: a full workspace keeps no more of them.
                    await ensure_within_quota(settings, root)
                except ApiError:
                    logger.warning(
                        "workspace of user %d is at its quota; "
                        "the source of document %d is not kept",
                        row.user_id,
                        row.id,
                    )
                    continue
                if source.is_file():
                    await asyncio.to_thread(
                        keep_source,
                        root,
                        relative,
                        source,
                        document_id=row.id,
                        kind=job.kind,
                        copy=index < len(pending) - 1,
                    )
                else:
                    # The shell's copy comes with the next materialization, which
                    # `_finish_held` runs right after this.
                    await asyncio.to_thread(copy_original, root, donor, row.id)
            # Not kept anywhere (none with room): deleted, as for a parse outside the
            # library.
            source.unlink(missing_ok=True)
            await db.execute(update(Job).where(Job.id == job_id).values(source_deleted_at=utcnow()))
            await db.commit()


async def run_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_id: uuid.UUID,
    render_slots: asyncio.Semaphore,
    generation: int | None = None,
) -> None:
    """Drive one claimed job to a terminal state. Never raises for a document's own faults.
    `generation` is the one the job was claimed with: every write to its row is fenced
    on it (docs/jobs.md § Queue)."""
    async with sessionmaker() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job is None or job.status != "running":
            return
        if generation is not None and job.worker_generation != generation:
            return
        connection = await load_connection(db, settings, job.user_id, job.connection_id)
        if connection is not None and job.connection_base_url != connection.snapshot:
            # The connection's URL (or KANO_PROXY_BASE_URL) changed while the job sat
            # queued. The dedup key must name the endpoint that actually produced the
            # result, not the one selected at submission (docs/jobs.md § Dedup). Fenced
            # like every write to the row: a replacement that took the job meanwhile
            # keeps its own snapshot, and this run ends here (docs/jobs.md § Queue).
            moved = await db.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == "running", *_owned(generation))
                .values(connection_base_url=connection.snapshot)
            )
            if moved.rowcount != 1:
                return
            job.connection_base_url = connection.snapshot
            await db.commit()
        # What an earlier attempt finished — before a requeue, or before the failure a
        # retry follows — is kept, never transcribed again (docs/jobs.md § Retrying
        # failed pages). A row with markdown is a page that succeeded; a failed one has
        # none, and the requeue or the retry removed those already.
        kept = {
            row.page_no: KeptPage(markdown=row.markdown, method=row.method, model=row.model)
            for row in (
                await db.execute(
                    select(JobPage).where(JobPage.job_id == job_id, JobPage.markdown.is_not(None))
                )
            ).scalars()
        }

    figures_root = Path(settings.figures_dir)

    if connection is None:
        error = (
            "Kano Proxy is not connected"
            if job.connection_id is None
            else "the provider connection for this job no longer exists"
        )
        # No crop thread ever started for this run; a plain discard covers leftovers.
        discard_job_figures(figures_root, job_id)
        await _finish(
            sessionmaker,
            job_id,
            status="failed",
            error=error,
            settings=settings,
            generation=generation,
        )
        return

    source = Path(job.source_path or "")
    # This attempt's own directories, for its renders and its crops: a worker that lost
    # the lock goes on for seconds beside the replacement's attempt, and must neither
    # write into the replacement's directories nor remove them. The crops are published
    # under the job's name only by the finish that passed the fence (docs/jobs.md).
    attempt = f"{job.id}.{uuid.uuid4().hex[:12]}"
    work_dir = Path(settings.upload_dir) / attempt
    work_dir.mkdir(parents=True, exist_ok=True)
    state = RunState(
        budget=VisionBudget(settings.vision_concurrency_per_job),
        job_id=str(job.id),
        max_response_bytes=settings.upstream_response_max_bytes,
        figures_dir=figures_root / attempt,
        figures_per_page=settings.figures_per_page_max,
        generation=generation,
    )

    succeeded = False
    try:
        try:
            outcomes = await _process_job(
                job, state, connection, source, work_dir, render_slots, sessionmaker, kept
            )
        except JobAborted as exc:
            await _finish(
                sessionmaker,
                job_id,
                status="failed",
                error=str(exc),
                settings=settings,
                generation=generation,
            )
            return
        except (poppler.PopplerError, ImageError, ValueError):
            await _finish(
                sessionmaker,
                job_id,
                status="failed",
                error="the document is unreadable",
                settings=settings,
                generation=generation,
            )
            return
        except Exception:
            # A worker must not die on one job. The traceback goes to the log; the job row
            # carries no internals and no document content.
            logger.exception("job %s failed unexpectedly", job_id)
            await _finish(
                sessionmaker,
                job_id,
                status="failed",
                error="internal error",
                settings=settings,
                generation=generation,
            )
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        failed = [outcome for outcome in outcomes if outcome.error]
        if failed:
            # Every page or nothing: a document with a page missing is no result
            # (docs/parsing.md § Vision-only conversion). The pages that succeeded stay
            # on their rows for a retry to keep, and the caller learns what killed each
            # page that did not.
            reasons = "; ".join(f"page {o.page}: {o.error}" for o in failed[:5])
            if len(failed) > 5:
                reasons += "; …"
            await _finish(
                sessionmaker,
                job_id,
                status="failed",
                error=f"{len(failed)} of {len(outcomes)} pages failed ({reasons})",
                settings=settings,
                generation=generation,
            )
            return

        document = assemble(
            [PageMarkdown(page=outcome.page, markdown=outcome.markdown) for outcome in outcomes]
        )
        if document.dropped_figures:
            # A count only: how badly the chosen model follows the coordinate contract is
            # worth knowing, what it said about the document is not.
            logger.info("job %s dropped %d unusable figure boxes", job_id, document.dropped_figures)
        # Set before the commit below, deliberately: a SIGTERM can land after PostgreSQL
        # has durably committed the result but before this coroutine resumes, and a
        # committed result must keep its crops. The trade is the right way round — if the
        # commit itself failed, the crops leak (bounded, harmless) instead of a succeeded
        # job losing its images. A finish the fence turned away is no result of this
        # attempt's: its crops are discarded below like a failed attempt's.
        succeeded = True

        async def publish() -> None:
            # Under the job's name only now: the result is this attempt's — and still
            # under the backup lock the commit held, so no snapshot sees one without
            # the other (docs/jobs.md § Queue).
            await asyncio.to_thread(_publish_figures, state, figures_root, job_id)

        async def finish_and_publish() -> bool:
            return await _finish(
                sessionmaker,
                job_id,
                status="succeeded",
                settings=settings,
                generation=generation,
                result=result,
                publish=publish,
            )

        result = {
            "markdown": document.markdown,
            "pages": [
                {
                    "page": outcome.page,
                    "width_pt": round(outcome.width_pt, 2),
                    "height_pt": round(outcome.height_pt, 2),
                    "method": outcome.method,
                    "model": outcome.model,
                    "error": None,
                }
                for outcome in outcomes
            ],
            "figures": document.figures,
            # Empty since a failed page fails the job; kept for the shape older results
            # and their readers have (docs/api.md).
            "errors": [],
            "meta": {
                "job_id": str(job.id),
                "agent": job.agent,
                "model": job.model,
                "bbox_format": job.bbox_format,
                "pipeline_version": job.pipeline_version,
                "sha256": job.sha256,
                "cached": False,
            },
        }
        # Run to its end whatever the worker does: a cancel landing after the commit
        # would otherwise drop the workspace and backup locks under a copy still being
        # made, leave the crops unpublished, and hand the worker a job to requeue that
        # is already done.
        if not await _to_the_end(finish_and_publish()):
            succeeded = False
    finally:
        if not succeeded:
            # Whatever ended this run without a result — a failure above, or the worker
            # cancelling on shutdown — takes the attempt's crops with it (docs/jobs.md
            # § Retention). Runs even under cancellation, and waits out any crop thread
            # still writing before removing the directory.
            _discard_figures(state)


async def _process_job(
    job: Job,
    state: RunState,
    connection: Connection,
    source: Path,
    work_dir: Path,
    render_slots: asyncio.Semaphore,
    sessionmaker: async_sessionmaker[AsyncSession],
    kept: dict[int, KeptPage],
) -> list[PageOutcome]:
    """Fan the job's pages out; each page records itself the moment its billed call ran.
    A page in `kept` is restored instead of transcribed."""
    if job.kind == "image":
        if 1 in kept:
            return [await _restore_image(job, state, source, work_dir, kept[1])]
        return [await _process_image(job, state, connection, source, work_dir, sessionmaker)]

    info = await poppler.pdf_info(source, cwd=work_dir)
    page_numbers = parse_pages_spec(job.pages_spec, info.page_count)

    outcomes: dict[int, PageOutcome] = {}

    async def process(page_no: int) -> None:
        if page_no in kept:
            outcomes[page_no] = await _restore_pdf_page(
                job, state, info, page_no, kept[page_no], source, work_dir, render_slots
            )
            return
        outcomes[page_no] = await _process_pdf_page(
            job, state, connection, info, page_no, source, work_dir, render_slots, sessionmaker
        )

    try:
        async with asyncio.TaskGroup() as group:
            for page_no in page_numbers:
                group.create_task(process(page_no))
    except* JobAborted as aborted:
        # One page hit a job-ending failure; the group has already cancelled the rest.
        raise JobAborted(str(aborted.exceptions[0])) from None
    return [outcomes[page_no] for page_no in page_numbers if page_no in outcomes]
