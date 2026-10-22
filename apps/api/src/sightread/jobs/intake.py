"""Parse intake: everything between "bytes arrive" and "a job exists" (docs/api.md, docs/jobs.md).

`POST /v1/parse` and the web library's `POST /api/library/documents` run this one sequence
— store, hash, probe, dedup, enqueue — whichever credential opened the door (API key, OAuth
token, upload ticket, or the web session). A route keeps only its transport concerns
(multipart vs base64, SSE vs JSON, which folder the file lands in); the shell owns no
business logic (docs/project-structure.md).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
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
from ..config import Settings
from ..db.models import (
    EFFORT_DEFAULT,
    AgentSetting,
    Job,
    KanoProxyKey,
    Result,
    UploadTicket,
    User,
    utcnow,
)
from ..errors import ApiError
from ..parsing import poppler
from ..parsing.images import ACCEPTED_IMAGE_TYPES, ImageError, probe_image
from .queue import (
    CheckTarget,
    count_running_jobs,
    enqueue_job,
    find_active_job,
    find_cached_job,
    find_recent_terminal_job,
    hold_dedup_key,
    normalize_pages_spec,
    parse_pages_spec,
)
from .runner import result_payload

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

    `connection_base_url` snapshots the endpoint at enqueue time — part of the dedup key,
    so editing a connection's URL invalidates the old endpoint's cached results, and the
    built-in upstream's snapshot (KANO_PROXY_BASE_URL) keeps historical OpenRouter results
    from ever answering (docs/jobs.md § Dedup).
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


async def resolve_target(
    db: AsyncSession,
    settings: Settings,
    user: User,
    agent: AgentDefinition,
    model: str | None,
) -> Target:
    """Upstream, model and prompt for a job of `agent`, from that agent's settings
    (docs/agents.md § Enqueue rules). A per-request `model` overrides the stored one; the
    user's own prompt — or a legacy preset — replaces the agent's default template
    (docs/parsing.md § Prompts)."""
    row = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user.id, AgentSetting.agent == agent.name
            )
        )
    ).scalar_one_or_none()
    connection = row.connection if row else None

    if connection is None:
        if not settings.kano_proxy_configured:
            raise ApiError(
                503,
                "upstream",
                f"Kano Proxy is not configured on this deployment: choose a provider "
                f"connection for the {agent.name} agent through the API",
            )
        connected = (
            await db.execute(select(KanoProxyKey.user_id).where(KanoProxyKey.user_id == user.id))
        ).first()
        if connected is None:
            raise ApiError(
                400,
                "invalid_request",
                f"Kano Proxy is not connected: connect it on the Agent page or choose a provider "
                f"connection for the {agent.name} agent",
            )
        connection_id, snapshot = None, settings.kano_proxy_base_url
    else:
        connection_id, snapshot = connection.id, connection.base_url

    model = model or (row.model if row else None)
    if not model:
        raise ApiError(
            400,
            "invalid_request",
            f"No model chosen for the {agent.name} agent: pick one on the Agent page or "
            f"pass 'model'",
        )
    # The user's own prompt, else a legacy preset, else the default — each replacing the
    # template whole (docs/parsing.md § Prompts).
    preset = row.prompt_preset if row else None
    if row is not None and row.prompt:
        prompt = row.prompt
    elif preset is not None:
        prompt = preset.text
    else:
        prompt = agent.default_prompt_template
    fallback = row.fallback_model if row is not None and row.auto_fallback else None
    return Target(agent.name, model, agent.bbox_format, prompt, connection_id, snapshot, fallback)


async def resolve_check(
    db: AsyncSession, settings: Settings, user_id: int, parse_model: str
) -> CheckTarget | None:
    """The check a Files upload or a retry takes, from the `check` agent's settings as they
    are now; None while its switch is off or Kano Proxy is not connected, the one upstream
    it speaks (docs/parsing.md § Format check)."""
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
    connected = (
        await db.execute(select(KanoProxyKey.user_id).where(KanoProxyKey.user_id == user_id))
    ).first()
    if connected is None:
        return None
    if row is None:
        return CheckTarget(parse_model, EFFORT_DEFAULT, CHECK.default_prompt_template)
    # The user's own prompt, else a legacy preset, else the default, as for the parse
    # agent (docs/agents.md § Enqueue rules).
    if row.prompt:
        prompt = row.prompt
    elif row.prompt_preset is not None:
        prompt = row.prompt_preset.text
    else:
        prompt = CHECK.default_prompt_template
    return CheckTarget(
        row.model or parse_model,
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
) -> Submission:
    """Store the document, dedup it and queue the parse. Commits before it returns.

    An upload ticket is bound and spent by that same commit, so nothing rejected on the way
    here (413, 400, 429) costs the caller its one upload (docs/auth.md § 5).

    `reuse_in_flight` additionally answers with a job that is still queued or running for
    these exact bytes instead of starting a second one. Only the web library asks for it: a
    browser cannot tell a lost response from a lost request, so its retry would otherwise
    buy the user a second parse of a document they already paid for (docs/jobs.md § Dedup).

    `check` asks for the format check, which only a Files upload takes; the check is no
    part of the dedup key, so a cached result or a parse already on its way is answered
    as it is (docs/parsing.md § Format check).
    """
    kind, media_type = resolve_kind(filename, media_type)
    # An upload is always the parse agent's input (docs/agents.md).
    target = await resolve_target(db, settings, user, PARSE, model)
    prompt_sha256 = hashlib.sha256(target.prompt.encode()).hexdigest()

    if await count_running_jobs(db, user.id) >= settings.max_jobs_per_user:
        raise ApiError(
            429,
            "rate_limit",
            "Too many running jobs; retry when one finishes",
            headers={"Retry-After": str(settings.parse_retry_after_seconds)},
        )

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
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

    if not force:
        cached = await find_cached_job(db, **key)
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
                    await db.commit()
                return Submission(job=active, upload=stored if retain_source else None)

    check_target = await resolve_check(db, settings, user.id, target.model) if check else None
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
    )
    if ticket is not None:
        upload_tickets.spend(ticket, job.id)
    await db.commit()
    return Submission(job=job)


def cached_payload(result: Result) -> dict:
    """The result payload of a dedup hit, flagged `meta.cached` (docs/jobs.md § Dedup)."""
    payload = result_payload(result)
    payload["meta"] = {**payload["meta"], "cached": True}
    return payload
