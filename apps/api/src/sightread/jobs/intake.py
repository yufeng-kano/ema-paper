"""Parse intake: everything between "bytes arrive" and "a job exists" (docs/api.md, docs/jobs.md).

`POST /v1/parse` and the web library's `POST /api/library/documents` run this one sequence
— store, hash, probe, dedup, enqueue — whichever credential opened the door (API key, OAuth
token, upload ticket, or the web session). A route keeps only its transport concerns
(multipart vs base64, SSE vs JSON, which folder the file lands in); the shell owns no
business logic (docs/project-structure.md).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import CHECK, PARSE, AgentDefinition
from ..agents import check as check_agent
from ..auth import upload_tickets
from ..billing import ledger
from ..billing.credits import parse_credits
from ..config import Settings
from ..db.models import (
    EFFORT_DEFAULT,
    PARSE_KIND,
    AgentSetting,
    Document,
    Job,
    Result,
    UploadTicket,
    User,
    utcnow,
)
from ..errors import ApiError
from ..parsing import poppler
from ..parsing.images import ACCEPTED_IMAGE_TYPES, ImageError, probe_image
from ..upstream.client import offered_models
from ..workspace import documents as workspace_documents
from ..workspace import safe_io
from ..workspace.filing import document_relative
from ..workspace.trash import read_row
from . import events
from .queue import (
    CheckTarget,
    count_running_jobs,
    enqueue_job,
    find_active_job,
    find_cached_job,
    find_parse_to_retry,
    find_recent_terminal_job,
    hold_dedup_key,
    normalize_pages_spec,
    parse_pages_spec,
    retry_job,
)
from .runner import result_payload

logger = logging.getLogger(__name__)

PDF_MEDIA_TYPE = "application/pdf"
UPLOAD_CHUNK_BYTES = 1024 * 1024
# Room for the multipart envelope around a file at the cap: boundaries, part headers and
# the other fields. The exact cap on the document itself stays in `store_upload`; this is
# the coarse bound that stops a body being spooled to disk without limit.
MULTIPART_ENVELOPE_BYTES = 64 * 1024
# Base64 decodes in whole 4-character groups, so the slice length must stay a multiple of 4.
BASE64_CHUNK_CHARS = 4 * 256 * 1024


@dataclass
class Submission:
    """The outcome of an intake: either a queued job or a cached result, never both.
    `upload` is the fresh file, kept for a caller that retains sources when the answer
    came from the cache (docs/jobs.md § Retention)."""

    job: Job | None = None
    cached: Result | None = None
    upload: Path | None = None


class ChunkReader(Protocol):
    """What a multipart upload has to offer to be streamed: `read(size)`.

    Typed as a protocol rather than as Starlette's `UploadFile` so this layer stays a
    service and not a piece of the web framework — every route that accepts a file hands
    its own reader in (docs/project-structure.md § Boundaries).
    """

    async def read(self, size: int = -1) -> bytes: ...


async def upload_chunks(upload: ChunkReader) -> AsyncIterator[bytes]:
    """Read a multipart upload in slices, so the document never sits in memory whole."""
    while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
        yield chunk


async def capped_stream(
    stream: AsyncIterator[bytes], max_bytes: int, *, cap_name: str = "UPLOAD_MAX_BYTES"
) -> AsyncIterator[bytes]:
    """A request body that stops at a size, for the multipart parser to read.

    The parser spools every file part to disk as it arrives with no cap of its own, and
    `store_upload` only sees the file once the whole body has been parsed. A request that
    declares no length — chunked, or simply lying — could therefore fill the upload
    directory long before anything answered 413. This is where that request stops.
    `cap_name` is the setting the refusal names: an attachment's cap is not a parse's.
    """
    total = 0
    async for chunk in stream:
        total += len(chunk)
        if total > max_bytes:
            raise ApiError(413, "invalid_request", f"The upload exceeds {cap_name}")
        yield chunk


async def base64_chunks(source: str) -> AsyncIterator[bytes]:
    """Decode a base64 `source` in slices, so a large document never sits in memory twice."""
    compact = "".join(source.split())
    for start in range(0, len(compact), BASE64_CHUNK_CHARS):
        piece = compact[start : start + BASE64_CHUNK_CHARS]
        try:
            yield base64.b64decode(piece, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(400, "invalid_request", "'source' is not valid base64") from exc


async def file_chunks(path: Path) -> AsyncIterator[bytes]:
    """Read a file already on disk in slices, off the event loop: a library document's
    original, parsed again (docs/jobs.md § Re-converting a document)."""
    with path.open("rb") as handle:
        while chunk := await asyncio.to_thread(handle.read, UPLOAD_CHUNK_BYTES):
            yield chunk


def resolve_kind(filename: str, media_type: str) -> tuple[str, str]:
    """Decide pdf vs image from the declared type, falling back to the file extension."""
    suffix = Path(filename).suffix.lower()
    if media_type == PDF_MEDIA_TYPE or suffix == ".pdf":
        return "pdf", PDF_MEDIA_TYPE
    if media_type in ACCEPTED_IMAGE_TYPES:
        return "image", media_type
    for accepted, extension in ACCEPTED_IMAGE_TYPES.items():
        if suffix == extension:
            return "image", accepted
    raise ApiError(400, "invalid_request", "Only PDF and jpg/png/webp/heic images are accepted")


@dataclass(frozen=True)
class Target:
    """What a job will run: agent, model, prompt and the upstream it calls.

    `connection_id` is always NULL now — every job runs on the operator's Kano Proxy —
    and `connection_base_url` snapshots `KANO_PROXY_BASE_URL` at enqueue: part of the
    dedup key, so historical OpenRouter and provider-connection results never answer
    (docs/jobs.md § Dedup).
    """

    agent: str
    model: str
    bbox_format: str
    prompt: str
    connection_id: int | None
    connection_base_url: str
    # The fallback a blocked page goes to, only while the agent's switch is on
    # (docs/agents.md § Enqueue rules); not part of the dedup key.
    fallback_model: str | None = None


def checked_parse_model(settings: Settings, model: str | None) -> str:
    """The model a parse, a retry or a re-convert runs on: `PARSE_MODEL`, or the one named
    when it is one of the parse agent's two (docs/agents.md § Enqueue rules). 503 with no
    parse model configured, 400 for any other model."""
    if not settings.parse_model:
        raise ApiError(503, "upstream", "No parse model is configured on this deployment")
    if model is None:
        return settings.parse_model
    if model not in offered_models(settings, PARSE.name):
        raise ApiError(400, "invalid_request", "That model is not one the parse agent runs")
    return model


async def resolve_target(
    db: AsyncSession,
    settings: Settings,
    user: User,
    agent: AgentDefinition,
    model: str | None,
) -> Target:
    """Upstream, model and prompt for a job of `agent` (docs/agents.md § Enqueue rules):
    the operator's Kano Proxy, the deployment's parse model — a per-request `model` must
    be one of the parse agent's — and the user's own prompt, else a legacy preset, else
    the agent's default template (docs/parsing.md § Prompts)."""
    model = checked_parse_model(settings, model)
    if not settings.kano_proxy_configured:
        raise ApiError(503, "upstream", "Kano Proxy is not configured on this deployment")
    row = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user.id, AgentSetting.agent == agent.name
            )
        )
    ).scalar_one_or_none()
    # The user's own prompt, else a legacy preset, else the default — each replacing the
    # template whole (docs/parsing.md § Prompts).
    preset = row.prompt_preset if row else None
    if row is not None and row.prompt:
        prompt = row.prompt
    elif preset is not None:
        prompt = preset.text
    else:
        prompt = agent.default_prompt_template
    fallback = (
        settings.parse_fallback_model
        if row is not None and row.auto_fallback and settings.parse_fallback_model
        else None
    )
    return Target(
        agent.name,
        model,
        agent.bbox_format,
        prompt,
        None,
        settings.kano_proxy_base_url,
        fallback,
    )


async def resolve_check(
    db: AsyncSession, settings: Settings, user_id: int, parse_model: str
) -> CheckTarget | None:
    """The check a Files upload or a retry takes, from the `check` agent's settings as they
    are now; None while its switch is off or Kano Proxy is not configured, the one
    upstream it speaks (docs/parsing.md § Format check). Its model is `CHECK_MODEL`, else
    the parse's."""
    row = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user_id, AgentSetting.agent == CHECK.name
            )
        )
    ).scalar_one_or_none()
    if row is not None and not row.auto_check:
        return None
    if not settings.kano_proxy_configured:
        return None
    model = settings.check_model or parse_model
    if row is None:
        return CheckTarget(model, EFFORT_DEFAULT, CHECK.default_prompt_template)
    # The user's own prompt, else a legacy preset, else the default, as for the parse
    # agent (docs/agents.md § Enqueue rules).
    if row.prompt:
        prompt = row.prompt
    elif row.prompt_preset is not None:
        prompt = row.prompt_preset.text
    else:
        prompt = CHECK.default_prompt_template
    return CheckTarget(
        model,
        row.effort or EFFORT_DEFAULT,
        check_agent.system_prompt(prompt, row.instructions),
    )


async def store_upload(
    chunks: AsyncIterator[bytes],
    destination: Path,
    max_bytes: int,
    *,
    cap_name: str = "UPLOAD_MAX_BYTES",
) -> tuple[int, str]:
    """Stream an upload to disk, hashing as it goes; the whole file never sits in memory."""
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("wb") as handle:
            async for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise ApiError(413, "invalid_request", f"The upload exceeds {cap_name}")
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


async def count_pages(kind: str, path: Path, upload_dir: Path, page_cap: int) -> int:
    """Page count for a stored upload, rejecting documents we cannot read or must not run."""
    if kind == "image":
        try:
            probe_image(path)
        except ImageError as exc:
            path.unlink(missing_ok=True)
            raise ApiError(400, "invalid_request", "The image could not be decoded") from exc
        return 1

    try:
        info = await poppler.pdf_info(path, cwd=upload_dir)
    except poppler.PopplerError as exc:
        path.unlink(missing_ok=True)
        raise ApiError(400, "invalid_request", "The PDF could not be read") from exc
    if info.page_count > page_cap:
        path.unlink(missing_ok=True)
        raise ApiError(400, "invalid_request", f"The PDF exceeds the {page_cap} page cap")
    return info.page_count


async def submit_parse(
    db: AsyncSession,
    settings: Settings,
    *,
    user: User,
    chunks: AsyncIterator[bytes],
    media_type: str,
    filename: str,
    model: str | None = None,
    pages: str | None = None,
    force: bool = False,
    ticket: UploadTicket | None = None,
    reuse_in_flight: bool = False,
    retain_source: bool = False,
    check: bool = False,
    reconvert: Document | None = None,
    retry: bool = False,
) -> Submission:
    """Store the document, dedup it and queue the parse. Commits before it returns.

    An upload ticket is bound and spent by that same commit, so nothing rejected on the way
    here (413, 400, 429) costs the caller its one upload (docs/auth.md § 5).

    `reuse_in_flight` additionally answers with a job that is still queued or running for
    these exact bytes instead of starting a second one. The web library and the data plane
    ask for it: a browser cannot tell a lost response from a lost request, and the
    connector re-runs a command its shell cut off, so either would otherwise buy the user
    a second parse of a document they already paid for (docs/jobs.md § Dedup).

    `check` asks for the format check, which every parse takes — a Files upload and a
    data-plane parse alike; the check is no part of the dedup key, so a cached result or a
    parse already on its way is answered as it is (docs/parsing.md § Format check).

    `retry` answers with the newest parse of the same bytes and pages instead of starting
    one: a failed data-plane parse goes back on the queue, its succeeded pages kept and
    its failed ones run on `model` when one is named, else on its own; one in flight or
    succeeded is answered as it stands (docs/jobs.md § Retrying failed pages).

    `reconvert` is the library document this parse re-converts, with `force`: it points at
    the job in the commit that queues it, so no re-convert runs that its document would
    not take (docs/jobs.md § Re-converting a document).
    """
    if retry and force:
        raise ApiError(400, "invalid_request", "'retry' resumes a failed parse; 'force' starts one")
    kind, media_type = resolve_kind(filename, media_type)
    # An upload is always the parse agent's input (docs/agents.md).
    target = await resolve_target(db, settings, user, PARSE, model)
    prompt_sha256 = hashlib.sha256(target.prompt.encode()).hexdigest()

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # Named for the job it may become, so whatever removes a job's uploads by its id — the
    # parse retention, deleting the account — finds it (docs/jobs.md § Retention).
    job_id = uuid.uuid4()
    stored = upload_dir / f"{job_id}{Path(filename).suffix.lower()[:16]}"
    size_bytes, sha256 = await store_upload(chunks, stored, settings.upload_max_bytes)

    page_count = await count_pages(kind, stored, upload_dir, settings.page_cap)
    pages_spec = normalize_pages_spec(pages)
    try:
        parse_pages_spec(pages_spec, page_count)
    except ValueError as exc:
        stored.unlink(missing_ok=True)
        raise ApiError(400, "invalid_request", str(exc)) from exc

    key = {
        "user_id": user.id,
        "agent": target.agent,
        "sha256": sha256,
        "model": target.model,
        "connection_id": target.connection_id,
        "connection_base_url": target.connection_base_url,
        "pages_spec": pages_spec,
        "prompt_sha256": prompt_sha256,
    }

    if retry:
        # The dedup key's hold a plain upload of these bytes takes, so a retry and an upload
        # racing it never both start a parse; then the user's row before the job's, the one
        # order the library's retry and every charge take (docs/billing.md § A parse),
        # which also keeps two retries of one parse from both putting it back.
        await hold_dedup_key(db, **key)
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        found = await find_parse_to_retry(
            db,
            user_id=user.id,
            agent=target.agent,
            sha256=sha256,
            pages_spec=pages_spec,
            retention_days=settings.parse_retention_days,
        )
        job = (
            None
            if found is None
            else (
                await db.execute(
                    select(Job)
                    .where(Job.id == found[0].id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        )
        if job is None:
            stored.unlink(missing_ok=True)
            raise ApiError(
                409,
                "invalid_request",
                "No parse of this file with these pages to retry: send the pages its upload "
                "sent, or upload the file instead",
            )
        if job.status == "failed" and found[1]:
            # A Files document's parse: the library retries it, from its original.
            stored.unlink(missing_ok=True)
            raise ApiError(
                409,
                "invalid_request",
                "The latest parse of this file is a Files document's; retry it from the Files page",
            )
        if job.status == "failed":
            retry_model = target.model if model is not None else None
            # The check as the settings say now, following the model this run sends the
            # failed pages to (docs/jobs.md § Retrying failed pages).
            check_target = (
                await resolve_check(db, settings, user.id, retry_model or job.model)
                if check
                else None
            )
            try:
                # The credits the failure released, taken again: the document is charged
                # once, by the run that delivers it (docs/billing.md § A parse).
                await ledger.reserve_again(db, settings, job.id)
            except ledger.InsufficientCredits as exc:
                stored.unlink(missing_ok=True)
                raise insufficient_credits(exc) from exc
            # Under the job's own name, so whatever removes a job's uploads by its id —
            # the retention, deleting the account — finds it (docs/jobs.md § Retention).
            source = stored.with_name(f"{job.id}.retry-{uuid.uuid4().hex[:12]}{stored.suffix}")
            stored.rename(source)
            try:
                await retry_job(
                    db, job.id, source_path=str(source), model=retry_model, check=check_target
                )
                await events.notify(db, job.id)
            except BaseException:
                source.unlink(missing_ok=True)
                raise
        else:
            # Back on the queue already, never off it, or parsed again since the failure:
            # that parse, answered as it stands, nothing charged.
            stored.unlink(missing_ok=True)
        if ticket is not None:
            upload_tickets.spend(ticket, job.id)
        # A commit that raises may still have landed, so the source stays: a retry that
        # did runs on it, and the upload sweeper takes it otherwise (docs/jobs.md).
        await db.commit()
        await db.refresh(job)
        return Submission(job=job)

    if not force:
        cached = await find_cached_job(db, retention_days=settings.parse_retention_days, **key)
        result = (
            None
            if cached is None
            else (
                await db.execute(select(Result).where(Result.job_id == cached.id))
            ).scalar_one_or_none()
        )
        if result is not None and result.errors:
            # A degraded result (failed pages) answers its own job, never a new upload —
            # a transient upstream failure must not be replayed forever (docs/jobs.md).
            result = None
        if result is not None:
            # The cache already holds this exact parse; the fresh copy is dead weight —
            # unless the caller keeps originals, whose row has nothing else to keep.
            if not retain_source:
                stored.unlink(missing_ok=True)
            if ticket is not None:
                upload_tickets.spend(ticket, result.job_id)
            await db.commit()
            return Submission(cached=result, upload=stored if retain_source else None)

        if reuse_in_flight:
            # Held until this transaction commits, so a second upload of the same bytes
            # waits here rather than racing past the check below into its own parse.
            await hold_dedup_key(db, **key)
            active = await find_active_job(db, **key)
            if active is None:
                # The first request's job may have finished already — with page errors,
                # which the cache never answers, or failed: within the retry window the
                # same bytes are still one upload, not a second parse to bill.
                active = await find_recent_terminal_job(
                    db,
                    finished_since=utcnow()
                    - timedelta(seconds=settings.upload_retry_window_seconds),
                    **key,
                )
            if active is not None:
                # The same parse is already on its way, or just finished. Starting
                # another would spend the user's key twice over for one document. A
                # caller that keeps originals keeps this fresh copy for its own row: the
                # worker may finish and hand the job's upload to the rows that exist
                # before this one does.
                if retain_source:
                    if not active.retain_source:
                        active.retain_source = True
                else:
                    stored.unlink(missing_ok=True)
                if ticket is not None:
                    upload_tickets.spend(ticket, active.id)
                # Committed here as on every other way out, so the dedup key's hold is
                # released before a data-plane caller streams the job's progress.
                await db.commit()
                return Submission(job=active, upload=stored if retain_source else None)

    # The plan's parses at once (docs/billing.md § Plans), under the deployment's cap,
    # asked only of a parse about to start: a cache hit or a join starts none (docs/jobs.md
    # § Concurrency caps). A retry is no upload: the claim applies the cap to it like to
    # any queued job.
    plan = await ledger.plan_of(db, settings, user.id)
    if await count_running_jobs(db, user.id) >= ledger.jobs_limit(settings, plan):
        stored.unlink(missing_ok=True)
        raise ApiError(
            429,
            "rate_limit",
            "Too many running jobs; retry when one finishes",
            headers={"Retry-After": str(settings.parse_retry_after_seconds)},
        )
    check_target = await resolve_check(db, settings, user.id, target.model) if check else None
    # The user's row lock that every charge takes (docs/billing.md § A parse), before the
    # job is inserted: the insert's foreign-key check holds a share lock on this row, and
    # two uploads taking the exclusive lock after it would deadlock (docs/jobs.md
    # § Credits).
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    job = await enqueue_job(
        db,
        user_id=user.id,
        agent=target.agent,
        kind=kind,
        filename=filename,
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
        pages_spec=pages_spec,
        model=target.model,
        connection_id=target.connection_id,
        connection_base_url=target.connection_base_url,
        bbox_format=target.bbox_format,
        prompt=target.prompt,
        prompt_sha256=prompt_sha256,
        page_count=page_count,
        source_path=str(stored),
        retain_source=retain_source,
        fallback_model=target.fallback_model,
        check=check_target,
        job_id=job_id,
    )
    # The job's credits, reserved in the commit that queues it: a job the balance cannot
    # cover is never queued, and the upload is not kept for it.
    try:
        await ledger.reserve(
            db,
            settings,
            user_id=user.id,
            kind=ledger.PARSE,
            amount=parse_credits(
                len(parse_pages_spec(pages_spec, page_count)), settings.parse_credits_per_page
            ),
            job_id=job.id,
            model=target.model,
        )
    except ledger.InsufficientCredits as exc:
        stored.unlink(missing_ok=True)
        raise insufficient_credits(exc) from exc
    if ticket is not None:
        upload_tickets.spend(ticket, job.id)
    if reconvert is not None:
        reconvert.reconvert_job_id = job.id
    await db.commit()
    return Submission(job=job)


def insufficient_credits(exc: ledger.InsufficientCredits) -> ApiError:
    """The 402 of docs/billing.md § Refusals, for a parse or a chat send alike."""
    return ApiError(
        402,
        "insufficient_credits",
        f"Not enough credits: this needs {exc.need}, {exc.remaining} left until "
        f"{exc.period_end.isoformat()}",
        details={
            "need": exc.need,
            "remaining": exc.remaining,
            "period_end": exc.period_end.isoformat(),
        },
    )


async def fd_chunks(entry: int) -> AsyncIterator[bytes]:
    """An open file in chunks, as intake streams an upload."""
    while chunk := await asyncio.to_thread(os.read, entry, UPLOAD_CHUNK_BYTES):
        yield chunk


async def convert_kept_file(
    db: AsyncSession,
    settings: Settings,
    *,
    user: User,
    row: Document,
    model: str | None = None,
    view: str | None = None,
) -> Document:
    """A PDF or an image kept as a file becomes a document (docs/jobs.md § Converting a
    file) — the route's conversion and the chat agent's `convert_file` alike. The parse is
    queued from the file as a Files upload's is — the credits reserved, the per-user cap
    and the dedup cache applied, the file read in place and copied into the upload
    directory as the job's source — then, under the same hold of the workspace lock, the
    file moves aside out of the tree and the row becomes a parse naming the job, its id,
    name and place kept. A row that does not commit brings the file back; the worker files
    the source as the document's original when the parse ends (§ Retention). The caller
    holds the workspace lock and a shared hold of the backup lock, and `row` is a live
    file of the user's. Answers the row as the parse's document, its job on it; raises
    `ApiError` as the route answers it (409 `not_convertible`, 413, 429, 402, …) with
    nothing changed."""
    user_id = user.id
    root = workspace_documents.user_root(settings.workspaces_dir, user_id)
    try:
        _kind, media_type = resolve_kind(row.name, row.media_type or "")
    except ApiError as exc:
        raise ApiError(
            409, "not_convertible", "Only a PDF or an image can be converted into a document"
        ) from exc
    relative = await document_relative(db, row)
    try:
        entry = await asyncio.to_thread(
            safe_io.open_for_read, root, root / relative, settings.upload_max_bytes
        )
    except safe_io.EntryTooLarge as exc:
        raise ApiError(413, "invalid_request", "The file exceeds UPLOAD_MAX_BYTES") from exc
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", workspace_documents.NOT_PLAIN) from exc
    if entry is None:
        raise ApiError(409, "invalid_request", "The file is missing from the workspace")
    try:
        submission = await submit_parse(
            db,
            settings,
            user=user,
            chunks=fd_chunks(entry),
            media_type=media_type,
            filename=row.name,
            model=model,
            reuse_in_flight=True,
            retain_source=True,
            check=True,
        )
    finally:
        os.close(entry)
    job_id = submission.job.id if submission.job is not None else submission.cached.job_id
    if (
        submission.cached is not None
        and await db.scalar(select(Job.id).where(Job.id == job_id)) is None
    ):
        # The purged-cache race a Files upload meets (docs/jobs.md § Dedup margin).
        if submission.upload is not None:
            submission.upload.unlink(missing_ok=True)
        raise ApiError(
            409, "invalid_request", "The earlier parse of this file was just deleted; try again"
        )
    # The parse is committed; the row, read again, becomes its document.
    await db.refresh(row)
    row_id = row.id
    row.job_id = job_id
    row.kind = PARSE_KIND
    row.media_type = None
    row.size_bytes = None
    if view is not None:
        row.view = view
    await db.flush()
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
    # The file out of the tree, aside, as a delete moves a directory: back should the
    # row not commit, gone once it has (docs/workspace.md § Files).
    removal = await workspace_documents.remove_path(
        settings, user_id, relative, message=f"convert {relative}"
    )
    adopted = False
    try:
        # A cache hit or a parse joined in flight hands this row the fresh copy as its
        # original now; a parse just queued has the worker file it (§ Retention).
        await workspace_documents.adopt_source(
            db, settings, row, job, upload=submission.upload, locked=True
        )
        adopted = True
        await db.commit()
    except BaseException as exc:
        readable, landed = await read_row(db, Document, row_id)
        if readable and isinstance(landed, Document) and landed.job_id == job_id:
            logger.warning("the conversion of document %d was committed despite the error", row_id)
            row = landed
        else:
            with contextlib.suppress(Exception):
                await db.rollback()
            if adopted:
                with contextlib.suppress(Exception):
                    await workspace_documents.discard_adopted(settings, user_id, row_id, relative)
            if removal is not None:
                try:
                    await workspace_documents.restore_removed(settings, user_id, relative, removal)
                except Exception:  # noqa: BLE001 - the file waits aside, whole
                    logger.exception("could not bring %s back after a failed conversion", relative)
            if isinstance(exc, ApiError):
                raise
            logger.exception("document %d could not be converted", row_id)
            raise ApiError(
                409, "invalid_request", "The file could not be converted; nothing was changed"
            ) from exc
    if removal is not None:
        try:
            await workspace_documents.discard_removed(settings, user_id, removal)
        except Exception:  # noqa: BLE001 - the next reconciliation sweeps it
            logger.exception("could not discard %s set aside by a conversion", relative)
    return row


def cached_payload(result: Result) -> dict:
    """The data plane's result payload of a dedup hit, flagged `meta.cached` (docs/jobs.md
    § Dedup), with the reading copy as its `markdown` (docs/api.md)."""
    payload = result_payload(result, reading_copy=True)
    payload["meta"] = {**payload["meta"], "cached": True}
    return payload
