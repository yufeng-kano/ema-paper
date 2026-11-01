"""Data plane `/v1/*` (docs/api.md).

Thin by design: this module validates the request, puts the bytes on disk, and hands the
work to `jobs`. It never parses a document and never talks to a model itself.

A parse here is the parse a Files upload gets (docs/api.md § POST /v1/parse): checked, its
reading copy returned as `markdown`, its crops served, a failed one retried and one in
flight joined — so the connector, whose commands are these routes, behaves as the web does.

The library here is the control plane's reads and its two saves on a data-plane credential
(docs/api.md § The library on the data plane): the same functions, a different door.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from sqlalchemy import select
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..agents import PARSE
from ..auth.deps import (
    AppSettings,
    DbSession,
    DocumentReader,
    DocumentWriter,
    JobReader,
    LibraryUser,
    ReaderUser,
    TicketJobId,
    UploaderCaller,
)
from ..billing import ledger
from ..db.models import FILE_KIND, MARKDOWN_KIND, Document, Folder, Job, Result, User
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
from ..workspace import documents as workspace_documents
from ..workspace import git
from ..workspace.layout import user_root
from . import library
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


def _form_flag(form, field: str) -> bool:
    """A bool form field read as the JSON body reads it; anything else is a 400, never a
    quiet false that would start a parse the caller did not ask for (docs/api.md)."""
    value = _form_value(form, field)
    if value is None:
        return False
    try:
        return TypeAdapter(bool).validate_python(value)
    except ValidationError as exc:
        raise ApiError(400, "invalid_request", f"'{field}' must be true or false") from exc


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
            _form_flag(form, "force"),
            _form_flag(form, "retry"),
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
        # The request's session is done: the stream opens its own, and one that lasts the
        # whole parse must not hold a pool connection with it (docs/jobs.md § Progress).
        await db.close()
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
    # As the parse's own stream: no pool connection held for its length (docs/jobs.md
    # § Progress).
    await db.close()
    return StreamingResponse(
        events.stream_job_events(request.app.state.sessionmaker, settings.database_url, job_id),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- the library (docs/api.md § The library on the data plane) ----------------------------


def library_entry(row: Document, job: Job | None, path: str) -> dict:
    """One document as the data plane lists it: what a caller picks a document by, not
    the Files page's progress state."""
    kind = FILE_KIND if row.kind == FILE_KIND else (MARKDOWN_KIND if job is None else job.kind)
    return {
        "id": row.id,
        "folder_id": row.folder_id,
        "name": row.name,
        "path": path,
        "kind": kind,
        "status": None if job is None else job.status,
        "media_type": row.media_type if row.kind == FILE_KIND else None,
        "page_count": None if job is None else job.page_count,
        "size_bytes": (
            row.size_bytes if row.kind == FILE_KIND else (None if job is None else job.size_bytes)
        ),
        "created_at": row.created_at,
        "finished_at": None if job is None else job.finished_at,
    }


def entry_path(folders: dict[int, Folder], folder_id: int | None, name: str) -> str:
    """The folder names and the name joined with `/`, as the Files page shows them."""
    chain = workspace_documents.folder_chain(folders, folder_id)
    return "/".join([*(folder.name for folder in chain), name])


async def library_index(db, user_id: int) -> dict:
    """The live library from one snapshot, without the Trash: `GET /v1/library`, and the
    rows the connector's `list_library` filters (docs/mcp.md § The library tools).

    One snapshot as `GET /api/library` takes it — the transaction that authenticated the
    caller ended first, `REPEATABLE READ` asked for on the next where PostgreSQL offers it
    — so no document is listed under a folder the same answer does not hold."""
    await db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    folders = {row.id: row for row in await library.read_folders(db, user_id)}
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    rows = (
        await db.execute(
            select(Document, Job)
            .outerjoin(Job, Job.id == Document.job_id)
            .where(Document.user_id == user_id, Document.trashed_at.is_(None))
        )
    ).all()
    live_folders = [row for row in folders.values() if row.id not in trashed]
    documents = [
        library_entry(row, job, entry_path(folders, row.folder_id, row.name))
        for row, job in rows
        if row.folder_id not in trashed
    ]
    return {
        "folders": sorted(
            (
                {
                    "id": row.id,
                    "parent_id": row.parent_id,
                    "name": row.name,
                    "path": entry_path(folders, row.parent_id, row.name),
                }
                for row in live_folders
            ),
            key=lambda entry: entry["path"],
        ),
        "documents": sorted(documents, key=lambda entry: (entry["path"], entry["id"])),
    }


@router.get("/library")
async def read_library(user: LibraryUser, db: DbSession):
    return await library_index(db, user.id)


async def document_entry(db, settings, user_id: int, document_id: int) -> dict:
    """One live document of the caller's as the data plane lists it, plus whether its
    original is kept: `GET /v1/library/documents/{id}`, and the `document` the connector's
    `read_document` and `write_document` answer with (docs/mcp.md § The library tools)."""
    row = await library.live_document(db, user_id, document_id)
    job = await library.job_of(db, row.job_id) if row.job_id is not None else None
    folders = await workspace_documents.user_folders(db, user_id)
    root = user_root(settings.workspaces_dir, row.user_id)
    original = job is not None and (
        await asyncio.to_thread(workspace_documents.original_of, root, row.id) is not None
    )
    return {
        **library_entry(row, job, entry_path(folders, row.folder_id, row.name)),
        "original": original,
    }


@router.get("/library/documents/{document_id}")
async def read_library_document(
    document_id: int, user: DocumentReader, db: DbSession, settings: AppSettings
):
    return await document_entry(db, settings, user.id, document_id)


def _text_response(text: str, revision: str) -> PlainTextResponse:
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"ETag": f'"{revision}"', "Cache-Control": "private, no-store"},
    )


@router.get("/library/documents/{document_id}/doc.md")
async def read_document_text(
    document_id: int, user: DocumentReader, db: DbSession, settings: AppSettings
):
    content = await library.document_content(document_id, user, db, settings)
    return _text_response(content["markdown"], content["revision"])


@router.get("/library/documents/{document_id}/notes.md")
async def read_document_notes(
    document_id: int, user: DocumentReader, db: DbSession, settings: AppSettings
):
    content = await library.document_content(document_id, user, db, settings)
    return _text_response(content["notes"], content["notes_revision"])


async def _edited_text(request: Request, settings) -> tuple[str, str]:
    """What a save sends (docs/api.md § The library on the data plane): the revision its
    `If-Match` names — the ETag's opaque value, `W/` and quotes dropped; `*` names none,
    since a save must say which copy it was made from — and the body as raw UTF-8 under
    `CONTENT_MAX_BYTES`, refused as it arrives past the cap."""
    base = request.headers.get("if-match", "").strip().removeprefix("W/").strip('"').strip()
    if base in ("", "*"):
        raise ApiError(
            428,
            "invalid_request",
            "Send If-Match with the ETag of the copy you edited, so nothing is overwritten unseen",
        )
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.content_max_bytes:
            raise ApiError(413, "invalid_request", "The content is too large to save")
    try:
        return body.decode("utf-8"), base
    except UnicodeDecodeError as exc:
        raise ApiError(400, "invalid_request", "The body is not UTF-8 text") from exc


@router.put("/library/documents/{document_id}/doc.md")
async def save_document_text(
    document_id: int,
    request: Request,
    user: DocumentWriter,
    db: DbSession,
    settings: AppSettings,
):
    text, base = await _edited_text(request, settings)
    row = await library.live_text_document(db, user.id, document_id)
    revision = await workspace_documents.write_content(
        db, settings, row, markdown=text, base_revision=base, author=git.API_AUTHOR
    )
    return JSONResponse({"revision": revision}, headers={"ETag": f'"{revision}"'})


@router.put("/library/documents/{document_id}/notes.md")
async def save_document_notes(
    document_id: int,
    request: Request,
    user: DocumentWriter,
    db: DbSession,
    settings: AppSettings,
):
    text, base = await _edited_text(request, settings)
    row = await library.live_text_document(db, user.id, document_id)
    revision = await workspace_documents.write_notes(
        db, settings, row, text=text, base_revision=base, author=git.API_AUTHOR
    )
    return JSONResponse({"revision": revision}, headers={"ETag": f'"{revision}"'})


@router.get("/library/documents/{document_id}/figures/{page}/{bbox}")
async def read_document_figure(
    document_id: int,
    page: int,
    bbox: str,
    user: DocumentReader,
    db: DbSession,
    settings: AppSettings,
):
    return await library.document_figure(document_id, page, bbox, user, db, settings)


@router.get("/library/documents/{document_id}/images/{name}")
async def read_document_image(
    document_id: int, name: str, user: DocumentReader, db: DbSession, settings: AppSettings
):
    return await library.document_image(document_id, name, user, db, settings)


@router.get("/library/documents/{document_id}/original")
async def read_document_original(
    document_id: int,
    request: Request,
    user: DocumentReader,
    db: DbSession,
    settings: AppSettings,
):
    return await library.document_original(document_id, request, user, db, settings)


@router.get("/library/documents/{document_id}/file")
async def read_document_file(
    document_id: int,
    request: Request,
    user: DocumentReader,
    db: DbSession,
    settings: AppSettings,
    download: bool = Query(default=False),
):
    return await library.document_file(document_id, request, user, db, settings, download)
