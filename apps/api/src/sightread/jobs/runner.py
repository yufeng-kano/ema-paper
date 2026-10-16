"""Job execution: one document, page by page (docs/jobs.md, docs/parsing.md).

This is the `parse` agent's runner (docs/agents.md). Shape of a run: read the job, resolve
the user's key, work out the pages, then fan the pages out. Poppler work is bounded by the
worker-wide render semaphore; upstream calls are bounded by this job's own vision budget,
which halves itself when the upstream says 429.

Page failures are page-scoped by design: an unreadable page becomes an entry in `errors`
and the rest of the document still parses. Only a document where no page survived, a dead
key, or exhausted credits ends the whole job.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import threading
import uuid
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
from ..parsing.figures import discard_job_figures, save_page_figures
from ..parsing.images import ImageError, normalize_image
from ..parsing.markdown import PageMarkdown, assemble
from ..upstream.client import (
    Connection,
    EmptyCompletion,
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
    keep_source,
    materialize_document,
    original_of,
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


@dataclass
class PageOutcome:
    page: int
    width_pt: float
    height_pt: float
    method: str | None = None
    error: str | None = None
    markdown: str = ""
    usage: list[Usage] = field(default_factory=list)


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
        "pages": [{"page": row.page_no, "method": row.method, "error": row.error} for row in rows],
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


async def _guarded_call(state: RunState, outcome: PageOutcome, call):
    """Apply the upstream failure policy of docs/parsing.md to one page.

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
    except UpstreamError as exc:
        if exc.fatal:
            raise JobAborted("the upstream rejected the stored key") from None
        # The message is a status code or a fixed phrase — never credentials or content.
        logger.warning("job %s page %d upstream call failed: %s", state.job_id, outcome.page, exc)
        outcome.error = "upstream call failed"
        return None

    state.payment_failures = 0
    outcome.usage.append(result.usage)
    return result


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
        await _record_page(sessionmaker, job, outcome)
        return outcome

    convention = convention_for_job(job.model, job.pipeline_version)
    # Pillow work, so it runs beside the in-flight HTTP calls rather than under them.
    sent = await asyncio.to_thread(prepare_image, image, convention)
    try:
        transcription = await _guarded_call(
            state,
            outcome,
            lambda: transcribe_page(
                connection,
                job.model,
                job_prompt(job),
                sent.path,
                page_no,
                convention=convention,
                max_response_bytes=state.max_response_bytes,
            ),
        )
        if transcription is not None:
            outcome.markdown = transcription.markdown
        # Billing before the optional raster work: a SIGTERM landing mid-crop cancels this
        # task, and the requeued reparse bills the page again — the call that already ran
        # must be in `usage_log` first.
        await _record_page(sessionmaker, job, outcome)
        if transcription is not None:
            await _save_figures(state, outcome.markdown, image, page_no)
    finally:
        # The rendered page has served its purpose; nothing may outlive the call.
        sent.cleanup()
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
        await _record_page(sessionmaker, job, outcome)
        return outcome

    outcome = PageOutcome(
        page=1,
        width_pt=float(normalized.width_px),
        height_pt=float(normalized.height_px),
        method=VISION,
    )
    convention = convention_for_job(job.model, job.pipeline_version)
    sent = await asyncio.to_thread(prepare_image, normalized.path, convention)
    try:
        transcription = await _guarded_call(
            state,
            outcome,
            lambda: transcribe_page(
                connection,
                job.model,
                job_prompt(job),
                sent.path,
                1,
                convention=convention,
                max_response_bytes=state.max_response_bytes,
            ),
        )
        if transcription is not None:
            outcome.markdown = transcription.markdown
        # Same ordering as the PDF path: the billed call is persisted before crop work.
        await _record_page(sessionmaker, job, outcome)
        if transcription is not None:
            await _save_figures(state, outcome.markdown, normalized.path, 1)
    finally:
        sent.cleanup()
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


async def _record_page(
    sessionmaker: async_sessionmaker[AsyncSession], job: Job, outcome: PageOutcome
) -> None:
    """Persist one finished page: its row, the job's progress and the calls it billed."""
    async with sessionmaker() as db:
        db.add(
            JobPage(
                job_id=job.id,
                page_no=outcome.page,
                method=outcome.method,
                status="failed" if outcome.error else "succeeded",
                error=outcome.error,
                # What the partial result reads while the job runs (docs/jobs.md).
                markdown=outcome.markdown if not outcome.error else None,
            )
        )
        for usage in outcome.usage:
            db.add(
                UsageLog(
                    user_id=job.user_id,
                    job_id=job.id,
                    model=job.model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    cost=usage.cost,
                    cost_source=usage.cost_source,
                )
            )
        await db.execute(update(Job).where(Job.id == job.id).values(pages_done=Job.pages_done + 1))
        await events.notify(db, job.id)
        await db.commit()


async def fail_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    job_id: uuid.UUID,
    error: str,
) -> None:
    """A job whose task raised past `run_job` (a database error while the terminal state
    was being written, say): failed, never requeued — the parse ran and billed the user's
    key, and a fault of ours is no reason to run it again. A job no longer `running` (its
    result committed before what raised) is left as it is (docs/jobs.md). With settings,
    like every other terminal path: a library row keeps its original here too."""
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status != "running":
            return
    await _finish(sessionmaker, job_id, status="failed", error=error, settings=settings)


async def _to_the_end(coro) -> None:
    """Await `coro` so that a cancel of the caller waits for it instead of interrupting
    it; the cancel is raised once it is done. For the finalization of a committed result
    (docs/jobs.md § Retention): the row is `succeeded`, so a shutdown's requeue would
    not take it back, and what is half-copied under a lock that a cancel released would
    be nobody's to finish."""
    task = asyncio.ensure_future(coro)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            logger.error("finalization failed during shutdown", exc_info=task.exception())
        raise


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    result: dict | None = None,
    settings: Settings | None = None,
) -> None:
    """Write the terminal state and delete the source document (docs/jobs.md § Retention).

    A succeeded parse behind a library document keeps its original instead: the source
    moves into the document's workspace directory (docs/workspace.md). A failed job's crop
    directory goes too — `run_job`'s outermost finally discards it (through the crop
    guard), so a late crop thread cannot write it back.
    """
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return
        user_id = job.user_id
    if settings is None:
        await _finish_held(sessionmaker, job_id, status=status, error=error, result=result)
        return
    # The rows that say the parse is done, and the original and copies they describe,
    # land in one backup snapshot: the backup lock is held shared from before the commit
    # until the files are in place, since the upload directory they come from is not
    # backed up (docs/workspace.md § Backups).
    async with backup_shared(user_root(settings.workspaces_dir, user_id)):
        await _finish_held(
            sessionmaker, job_id, status=status, error=error, result=result, settings=settings
        )


async def _finish_held(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    result: dict | None = None,
    settings: Settings | None = None,
) -> None:
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return
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
        # The job's row lock before the recipients are read: filing takes it before it
        # inserts, so a row filed meanwhile is either here or filed after, as a finished
        # parse — never filed in the gap and left without its original (docs/jobs.md).
        await db.execute(select(Job.id).where(Job.id == job.id).with_for_update())
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
                    if row is not None:
                        await materialize_document(db, settings, row)
                except Exception:  # noqa: BLE001 - the parse succeeded; the copy can wait
                    logger.exception("could not materialize document %d", document.id)


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
            for index, row in enumerate(pending):
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
) -> None:
    """Drive one claimed job to a terminal state. Never raises for a document's own faults."""
    async with sessionmaker() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job is None or job.status != "running":
            return
        connection = await load_connection(db, settings, job.user_id, job.connection_id)
        if connection is not None and job.connection_base_url != connection.snapshot:
            # The connection's URL (or KANO_PROXY_BASE_URL) changed while the job sat
            # queued. The dedup key must name the endpoint that actually produced the
            # result, not the one selected at submission (docs/jobs.md § Dedup).
            job.connection_base_url = connection.snapshot
            await db.commit()

    figures_root = Path(settings.figures_dir)

    if connection is None:
        error = (
            "Kano Proxy is not connected"
            if job.connection_id is None
            else "the provider connection for this job no longer exists"
        )
        # No crop thread ever started for this run; a plain discard covers leftovers.
        discard_job_figures(figures_root, job_id)
        await _finish(sessionmaker, job_id, status="failed", error=error, settings=settings)
        return

    source = Path(job.source_path or "")
    work_dir = Path(settings.upload_dir) / str(job.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    state = RunState(
        budget=VisionBudget(settings.vision_concurrency_per_job),
        job_id=str(job.id),
        max_response_bytes=settings.upstream_response_max_bytes,
        figures_dir=figures_root / str(job.id),
        figures_per_page=settings.figures_per_page_max,
    )

    succeeded = False
    try:
        try:
            outcomes = await _process_job(
                job, state, connection, source, work_dir, render_slots, sessionmaker
            )
        except JobAborted as exc:
            await _finish(sessionmaker, job_id, status="failed", error=str(exc), settings=settings)
            return
        except (poppler.PopplerError, ImageError, ValueError):
            await _finish(
                sessionmaker,
                job_id,
                status="failed",
                error="the document is unreadable",
                settings=settings,
            )
            return
        except Exception:
            # A worker must not die on one job. The traceback goes to the log; the job row
            # carries no internals and no document content.
            logger.exception("job %s failed unexpectedly", job_id)
            await _finish(
                sessionmaker, job_id, status="failed", error="internal error", settings=settings
            )
            return
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        if all(outcome.error for outcome in outcomes):
            # Partial markdown from a document nothing could read is worse than nothing,
            # but the caller still deserves to know what killed each page.
            reasons = "; ".join(f"page {o.page}: {o.error}" for o in outcomes[:5])
            if len(outcomes) > 5:
                reasons += "; …"
            await _finish(
                sessionmaker,
                job_id,
                status="failed",
                error=f"no page could be parsed ({reasons})",
                settings=settings,
            )
            return

        document = assemble(
            [
                PageMarkdown(page=outcome.page, markdown=outcome.markdown)
                for outcome in outcomes
                if not outcome.error
            ]
        )
        if document.dropped_figures:
            # A count only: how badly the chosen model follows the coordinate contract is
            # worth knowing, what it said about the document is not.
            logger.info("job %s dropped %d unusable figure boxes", job_id, document.dropped_figures)
        # Set before the commit below, deliberately: a SIGTERM can land after PostgreSQL
        # has durably committed the result but before this coroutine resumes, and a
        # committed result must keep its crops. The trade is the right way round — if the
        # commit itself failed, the crops leak (bounded, harmless) instead of a succeeded
        # job losing its images.
        succeeded = True
        # And run to its end whatever the worker does: a cancel landing after the commit
        # would otherwise drop the workspace and backup locks under a copy still being
        # made, and hand the worker a job to requeue that is already done.
        await _to_the_end(
            _finish(
                sessionmaker,
                job_id,
                status="succeeded",
                settings=settings,
                result={
                    "markdown": document.markdown,
                    "pages": [
                        {
                            "page": outcome.page,
                            "width_pt": round(outcome.width_pt, 2),
                            "height_pt": round(outcome.height_pt, 2),
                            "method": outcome.method,
                            "error": outcome.error,
                        }
                        for outcome in outcomes
                    ],
                    "figures": document.figures,
                    "errors": [
                        {"page": outcome.page, "reason": outcome.error}
                        for outcome in outcomes
                        if outcome.error
                    ],
                    "meta": {
                        "job_id": str(job.id),
                        "agent": job.agent,
                        "model": job.model,
                        "bbox_format": job.bbox_format,
                        "pipeline_version": job.pipeline_version,
                        "sha256": job.sha256,
                        "cached": False,
                    },
                },
            )
        )
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
) -> list[PageOutcome]:
    """Fan the job's pages out; each page records itself the moment its billed call ran."""
    if job.kind == "image":
        return [await _process_image(job, state, connection, source, work_dir, sessionmaker)]

    info = await poppler.pdf_info(source, cwd=work_dir)
    page_numbers = parse_pages_spec(job.pages_spec, info.page_count)

    outcomes: dict[int, PageOutcome] = {}

    async def process(page_no: int) -> None:
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
