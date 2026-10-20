"""Control plane `/api/memories` — the chat agent's memories of the reader (docs/chat.md
§ Memory; docs/api.md).

The Agent page lists, edits, pins and deletes them; a thread's Undo puts back what the
agent's `memory` tool changed through the same routes. A text is cleaned and capped like
the tool's, and the count is decided under the reader's row lock, as the tool decides it.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..agents import memory as memory_defs
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..db.models import Conversation, Document, Memory, User, utcnow
from ..errors import ApiError
from .chat import owned_conversation, owned_document

router = APIRouter(prefix="/api", tags=["memories"])


class MemoryCreate(BaseModel):
    kind: str
    text: str
    # NULL: every document.
    document_id: int | None = None
    pinned: bool = False
    # Where an Undo found it: the conversation and the `tool_use_id` that saved it.
    conversation_id: int | None = None
    source_ref: str | None = Field(default=None, max_length=255)


class MemoryUpdate(BaseModel):
    """Partial: only the fields present change; `document_id: null` is every document."""

    kind: str | None = None
    text: str | None = None
    document_id: int | None = None
    pinned: bool | None = None


def _checked_kind(kind: str | None) -> str:
    if kind not in memory_defs.KINDS:
        raise ApiError(
            400, "invalid_request", "kind must be one of " + ", ".join(memory_defs.KINDS)
        )
    return kind


def _checked_text(text: str | None, settings: AppSettings) -> str:
    """One line, cleaned like the tool's, not empty and within `CHAT_MEMORY_MAX_CHARS`."""
    cleaned = memory_defs.clean_text(text or "", single_line=True)
    if not cleaned:
        raise ApiError(400, "invalid_request", "A memory needs text")
    if len(cleaned) > settings.chat_memory_max_chars:
        raise ApiError(
            400,
            "invalid_request",
            f"A memory is at most {settings.chat_memory_max_chars} characters",
        )
    return cleaned


def _payload(row: Memory, document_name: str | None, conversation_title: str | None) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "text": row.text,
        "document_id": row.document_id,
        "document_name": document_name,
        "pinned": bool(row.pinned),
        "conversation_id": row.conversation_id,
        "conversation_title": conversation_title,
        "source_ref": row.source_ref,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _listed():
    """The memories with the names of their scope and source, while those exist."""
    return (
        select(Memory, Document.name, Conversation.title)
        .outerjoin(Document, Document.id == Memory.document_id)
        .outerjoin(Conversation, Conversation.id == Memory.conversation_id)
    )


async def _memory_payload(db: DbSession, memory_id: int) -> dict:
    row = (await db.execute(_listed().where(Memory.id == memory_id))).one()
    return _payload(*row)


async def _owned_memory(db: DbSession, user_id: int, memory_id: int) -> Memory:
    row = (
        await db.execute(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such memory")
    return row


async def _lock_reader(db: DbSession, user_id: int) -> None:
    """The reader's row, held until the commit — the lock the `memory` tool takes before it
    reads whether memory is paused. Every write to the reader's memories takes it first, so
    they apply in one order: an edit or a delete lands after an agent call already deciding,
    never under it (docs/chat.md § Memory)."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


async def _commit(db: DbSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        # The document or the conversation was deleted between the ownership check and
        # this commit: an ordinary race, not a 500.
        await db.rollback()
        raise ApiError(
            409, "invalid_request", "The document or conversation was just deleted"
        ) from exc


@router.get("/memories")
async def list_memories(user: SessionUser, db: DbSession, settings: AppSettings):
    rows = (
        await db.execute(
            _listed()
            .where(Memory.user_id == user.id)
            .order_by(Memory.updated_at.desc(), Memory.id.desc())
        )
    ).all()
    return {"memories": [_payload(*row) for row in rows], "max": settings.chat_memories_max}


@router.post("/memories", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_memory(
    body: MemoryCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    kind = _checked_kind(body.kind)
    text = _checked_text(body.text, settings)
    if body.document_id is not None:
        await owned_document(db, user.id, body.document_id)
    if body.conversation_id is not None:
        await owned_conversation(db, user.id, body.conversation_id)
    # Two saves near the cap — this and the agent's tool — must not both count below it.
    await _lock_reader(db, user.id)
    count = (
        await db.execute(select(func.count()).select_from(Memory).where(Memory.user_id == user.id))
    ).scalar_one()
    if count >= settings.chat_memories_max:
        raise ApiError(
            409,
            "invalid_request",
            f"You can keep at most {settings.chat_memories_max} memories",
        )
    row = Memory(
        user_id=user.id,
        kind=kind,
        text=text,
        document_id=body.document_id,
        pinned=body.pinned,
        conversation_id=body.conversation_id,
        source_ref=(body.source_ref or "").strip() or None,
    )
    db.add(row)
    await _commit(db)
    return await _memory_payload(db, row.id)


@router.put("/memories/{memory_id}", dependencies=[CsrfGuard])
async def update_memory(
    memory_id: int, body: MemoryUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    await _lock_reader(db, user.id)
    row = await _owned_memory(db, user.id, memory_id)
    provided = body.model_fields_set
    for field in ("kind", "text", "pinned"):
        if field in provided and getattr(body, field) is None:
            raise ApiError(400, "invalid_request", f"{field} cannot be null")
    if "kind" in provided:
        row.kind = _checked_kind(body.kind)
    if "text" in provided:
        row.text = _checked_text(body.text, settings)
    if "document_id" in provided:
        if body.document_id is not None:
            await owned_document(db, user.id, body.document_id)
        row.document_id = body.document_id
    if "pinned" in provided:
        row.pinned = bool(body.pinned)
    row.updated_at = utcnow()
    await _commit(db)
    return await _memory_payload(db, row.id)


@router.delete(
    "/memories/{memory_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_memory(memory_id: int, user: SessionUser, db: DbSession) -> Response:
    await _lock_reader(db, user.id)
    row = await _owned_memory(db, user.id, memory_id)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memories", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def clear_memories(user: SessionUser, db: DbSession) -> Response:
    """*Clear all memories*: every row goes; the switches on the agent's settings stay."""
    await _lock_reader(db, user.id)
    await db.execute(delete(Memory).where(Memory.user_id == user.id))
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
