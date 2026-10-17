"""Control plane `/api/attachments` — the chat composer's images and files, uploaded one
request each before the message that carries them (docs/chat.md § What a user message
carries; docs/api.md).

Transport only, like the library's upload: the bytes stream to disk under their own cap,
`parsing/attachments.py` prepares them, and a row holds the result for the send, which
embeds it in the message and deletes both. One never sent is swept with the upload
directory after a day (docs/jobs.md § Retention).
"""

from __future__ import annotations

import asyncio
import contextlib
import unicodedata
import uuid
from datetime import timedelta
from pathlib import Path, PurePath

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import delete, select
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..db.models import ChatAttachment, utcnow
from ..errors import ApiError
from ..jobs.intake import MULTIPART_ENVELOPE_BYTES, capped_stream, store_upload, upload_chunks
from ..jobs.sweeper import MAX_AGE_SECONDS
from ..parsing.attachments import (
    FILE_PREFIX,
    AttachmentRefused,
    prepare_attachment,
    prepared_path,
)

router = APIRouter(prefix="/api", tags=["attachments"])

NAME_MAX = 255
CAP_NAME = "CHAT_ATTACHMENT_MAX_BYTES"


def attachment_name(raw: str | None) -> str:
    """The file's own name, as the thread shows it: the last path segment, NFC, without
    control characters, bounded; `attachment` when nothing is left."""
    base = PurePath((raw or "").replace("\\", "/")).name
    cleaned = "".join(
        char for char in unicodedata.normalize("NFC", base) if unicodedata.category(char)[0] != "C"
    ).strip()
    return cleaned[:NAME_MAX] or "attachment"


def attachment_payload(row: ChatAttachment) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "media_type": row.media_type,
        "size_bytes": row.size_bytes,
        "chars": row.chars,
    }


@router.post("/attachments", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    request: Request, user: SessionUser, db: DbSession, settings: AppSettings
):
    user_id = user.id
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ApiError(400, "invalid_request", "The upload must be multipart/form-data")
    cap = settings.chat_attachment_max_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap + MULTIPART_ENVELOPE_BYTES:
        raise ApiError(413, "invalid_request", f"The upload exceeds {CAP_NAME}")
    # Bounded before the parser spools a byte: a chunked request without a length would
    # otherwise fill the disk before anything answered 413 (jobs/intake.capped_stream).
    try:
        form = await MultiPartParser(
            request.headers,
            capped_stream(request.stream(), cap + MULTIPART_ENVELOPE_BYTES, cap_name=CAP_NAME),
        ).parse()
    except MultiPartException as exc:
        raise ApiError(400, "invalid_request", "The multipart body could not be read") from exc
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(400, "invalid_request", "Multipart requests need a 'file' part")
        name = attachment_name(upload.filename)
        upload_dir = Path(settings.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        attachment_id = uuid.uuid4()
        staging = upload_dir / f"{FILE_PREFIX}{attachment_id}.upload"
        try:
            await store_upload(upload_chunks(upload), staging, cap, cap_name=CAP_NAME)
            prepared = await prepare_attachment(
                staging,
                name=name,
                media_type=upload.content_type or "",
                out_dir=upload_dir,
                attachment_id=attachment_id,
                max_pixels=settings.chat_page_image_max_pixels,
                long_edge=settings.chat_attachment_image_long_edge,
                text_max_chars=settings.chat_attachment_text_max_chars,
            )
        except AttachmentRefused as exc:
            raise ApiError(exc.status, "invalid_request", exc.message) from exc
        finally:
            staging.unlink(missing_ok=True)
    finally:
        await form.close()

    # The caller's rows the sweeper has taken the files of are no attachments any more:
    # they go with this upload, so the never-sent ones do not pile up (docs/jobs.md
    # § Retention).
    await db.execute(
        delete(ChatAttachment).where(
            ChatAttachment.user_id == user_id,
            ChatAttachment.created_at < utcnow() - timedelta(seconds=MAX_AGE_SECONDS),
        )
    )
    row = ChatAttachment(
        id=attachment_id,
        user_id=user_id,
        name=name,
        kind=prepared.kind,
        media_type=prepared.media_type,
        size_bytes=prepared.size_bytes,
        chars=prepared.chars,
    )
    db.add(row)
    try:
        await db.commit()
    except BaseException:
        prepared.path.unlink(missing_ok=True)
        raise
    return attachment_payload(row)


@router.delete(
    "/attachments/{attachment_id}",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_attachment(
    attachment_id: str, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Drop an attachment not yet sent: the chip's remove control."""
    try:
        wanted = uuid.UUID(attachment_id)
    except ValueError as exc:
        raise ApiError(404, "invalid_request", "No such attachment") from exc
    row = (
        await db.execute(
            select(ChatAttachment).where(
                ChatAttachment.id == wanted, ChatAttachment.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such attachment")
    path = prepared_path(Path(settings.upload_dir), row.id, row.media_type)
    await db.delete(row)
    await db.commit()
    with contextlib.suppress(OSError):
        await asyncio.to_thread(path.unlink, missing_ok=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
