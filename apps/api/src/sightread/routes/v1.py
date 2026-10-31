"""Data plane `/v1/*` (docs/api.md).

Thin by design: this module validates the request, puts the bytes on disk, and hands the
work to `jobs`. It never parses a document and never talks to a model itself.

A parse here is the parse a Files upload gets (docs/api.md § POST /v1/parse): checked, its
reading copy returned as `markdown`, its crops served, a failed one retried and one in
flight joined — so the connector, whose commands are these routes, behaves as the web does.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..agents import PARSE
from ..auth.deps import (
    AppSettings,
    DbSession,
    JobReader,
    ReaderUser,
    TicketJobId,
    UploaderCaller,
)
from ..billing import ledger
from ..db.models import Job, Result, User
from ..errors import ApiError
from ..jobs import events
from ..jobs.intake import (
    MULTIPART_ENVELOPE_BYTES,
    PDF_MEDIA_TYPE,
    base64_chunks,
    cached_payload,
    capped_stream,
    submit_parse,
    upload_chunks,
)
from ..jobs.runner import result_payload
from ..upstream.client import agent_models
from .library import figure_crop_response

router = APIRouter(prefix="/v1", tags=["data"])

SSE_MEDIA_TYPE = "text/event-stream"


class ParseJson(BaseModel):
    """JSON fallback for clients that cannot post multipart (docs/api.md § POST /v1/parse)."""

    source: str = Field(min_length=1)
    filename: str = Field(default="upload", max_length=512)
    media_type: str = Field(default=PDF_MEDIA_TYPE, max_length=128)
    model: str | None = Field(default=None, max_length=255)
    pages: str | None = Field(default=None, max_length=255)
    force: bool = False
    retry: bool = False


@router.get("/models")
async def list_models(user: ReaderUser, db: DbSession, settings: AppSettings):
    """The models a parse may run (docs/api.md § GET /v1/models)."""
    return {"data": await agent_models(settings, PARSE.name)}


# --- parse ----------------------------------------------------------------------------


def _form_value(form, field: str) -> str | None:
    value = form.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _cached_response(result: Result, wants_stream: bool) -> Response:
    payload = cached_payload(result)

    async def single_event() -> AsyncIterator[str]:
        yield events.sse(events.DONE, payload)

    if wants_stream:
        return StreamingResponse(single_event(), media_type=SSE_MEDIA_TYPE)
    return JSONResponse(payload)


@router.post("/parse")
async def parse(request: Request, caller: UploaderCaller, db: DbSession, settings: AppSettings):
    """Accept a document, dedup it, and queue the parse (docs/api.md § POST /v1/parse).

    Transport only: reading the body is this route's job, the intake sequence behind it is
    `jobs.intake` (docs/project-structure.md). An upload ticket is spent by that same
    intake transaction, so a request refused before a job exists keeps it usable.
    """
    content_type = request.headers.get("content-type", "")
    wants_stream = SSE_MEDIA_TYPE in request.headers.get("accept", "")
    # Cheap rejection before a byte is read; the streaming copy below enforces the real
    # cap for requests that declare no length. Base64 inflates by a third, hence the slack.
    declared_length = request.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > settings.upload_max_bytes * 2:
        raise ApiError(413, "invalid_request", "The upload exceeds UPLOAD_MAX_BYTES")

    if content_type.startswith("multipart/form-data"):
        # A bounded stream, not `request.form()`'s: the parser spools each file part to
        # disk as it arrives, so a request that declares no length would otherwise be
        # written out in full before `store_upload` could refuse it.
        try:
            form = await MultiPartParser(
                request.headers,
                capped_stream(
                    request.stream(), settings.upload_max_bytes + MULTIPART_ENVELOPE_BYTES
                ),
            ).parse()
        except MultiPartException as exc:
            raise ApiError(400, "invalid_request", "The multipart body could not be read") from exc
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(400, "invalid_request", "Multipart requests need a 'file' part")
        filename = upload.filename or "upload"
        media_type = upload.content_type or ""
        model, pages, force, retry = (
            _form_value(form, "model"),
            _form_value(form, "pages"),
            _form_value(form, "force") in ("1", "true", "yes"),
            _form_value(form, "retry") in ("1", "true", "yes"),
        )
        chunks = upload_chunks(upload)
    else:
        try:
            body = ParseJson.model_validate(await request.json())
        except ValueError as exc:
            raise ApiError(
                400, "invalid_request", "Body must be multipart/form-data or JSON with 'source'"
            ) from exc
        filename, media_type = body.filename, body.media_type
        model, pages, force, retry = body.model, body.pages, body.force, body.retry
        chunks = base64_chunks(body.source)

    submission = await submit_parse(
        db,
        settings,
        user=caller.user,
        chunks=chunks,
        media_type=media_type,
        filename=filename,
        model=model,
        pages=pages,
        force=force,
        ticket=caller.ticket,
        # As the web library's upload: the same bytes in flight are joined, never bought
        # twice, and the parse is checked (docs/jobs.md § Dedup cache, docs/parsing.md
        # § Format check).
        reuse_in_flight=True,
        check=True,
        retry=retry,
    )
    if submission.cached is not None:
        return _cached_response(submission.cached, wants_stream)

    job = submission.job
    if wants_stream:
        return StreamingResponse(
            events.stream_job_events(request.app.state.sessionmaker, settings.database_url, job.id),
            media_type=SSE_MEDIA_TYPE,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(
        {"job_id": str(job.id), "status": job.status}, status_code=status.HTTP_202_ACCEPTED
    )


# --- job reads ------------------------------------------------------------------------


async def _owned_job(db: DbSession, job_id: uuid.UUID, user: User) -> Job:
    job = (
        await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user.id))
    ).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return job


async def _status_payload(db: DbSession, job: Job) -> dict:
    return {
        "job_id": str(job.id),
        "status": job.status,
        "page_count": job.page_count,
        "pages_done": job.pages_done,
        "error": job.error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        # What the parse cost, once settled (docs/billing.md § Routes).
        "credits": (await ledger.credits_by_job(db, [job.id])).get(job.id),
    }


async def _stored_result(db: DbSession, job_id: uuid.UUID) -> Result:
    result = (await db.execute(select(Result).where(Result.job_id == job_id))).scalar_one_or_none()
    if result is None:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job is not None and job.status == "failed":
            # Final, not pending: "yet" would have an agent wait on a parse that is over.
            raise ApiError(
                404,
                "invalid_request",
                f"This parse failed ({job.error or 'no reason recorded'}) and has no result",
            )
        raise ApiError(404, "invalid_request", "This job has no result yet")
    return result


def _markdown_response(result: Result) -> PlainTextResponse:
    """The document as the web reader opens it: the reading copy of a checked parse."""
    payload = result_payload(result, reading_copy=True)
    return PlainTextResponse(payload["markdown"], media_type="text/markdown; charset=utf-8")


# The `last` aliases resolve an upload ticket to the job it created (docs/api.md). They
# are declared before the `{job_id}` routes so the literal path wins the match.


@router.get("/jobs/last")
async def last_job_status(job_id: TicketJobId, db: DbSession):
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return await _status_payload(db, job)


@router.get("/jobs/last/result")
async def last_job_result(job_id: TicketJobId, db: DbSession):
    return result_payload(await _stored_result(db, job_id), reading_copy=True)


@router.get("/jobs/last/result.md")
async def last_job_markdown(job_id: TicketJobId, db: DbSession):
    return _markdown_response(await _stored_result(db, job_id))


@router.get("/jobs/last/figures/{page}/{bbox}")
async def last_job_figure(
    page: int, bbox: str, job_id: TicketJobId, db: DbSession, settings: AppSettings
):
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return await figure_crop_response(settings, job, page, bbox)


@router.get("/jobs/{job_id}")
async def job_status(job_id: uuid.UUID, user: JobReader, db: DbSession):
    return await _status_payload(db, await _owned_job(db, job_id, user))


@router.get("/jobs/{job_id}/result")
async def job_result(job_id: uuid.UUID, user: JobReader, db: DbSession):
    await _owned_job(db, job_id, user)
    return result_payload(await _stored_result(db, job_id), reading_copy=True)


@router.get("/jobs/{job_id}/result.md")
async def job_markdown(job_id: uuid.UUID, user: JobReader, db: DbSession):
    await _owned_job(db, job_id, user)
    return _markdown_response(await _stored_result(db, job_id))


@router.get("/jobs/{job_id}/figures/{page}/{bbox}")
async def job_figure(
    job_id: uuid.UUID,
    page: int,
    bbox: str,
    user: JobReader,
    db: DbSession,
    settings: AppSettings,
):
    """One figure's crop, the one the web reader shows (docs/api.md § GET
    /v1/jobs/{id}/figures/…): the job's own, never another parse's."""
    return await figure_crop_response(settings, await _owned_job(db, job_id, user), page, bbox)


@router.get("/jobs/{job_id}/events")
async def job_events(
    job_id: uuid.UUID, request: Request, user: JobReader, db: DbSession, settings: AppSettings
):
    """Progress stream; a terminal job replays its final event at once (docs/api.md)."""
    await _owned_job(db, job_id, user)
    return StreamingResponse(
        events.stream_job_events(request.app.state.sessionmaker, settings.database_url, job_id),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
