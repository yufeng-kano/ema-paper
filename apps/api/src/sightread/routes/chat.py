"""Control plane `/api/conversations/*` and `/api/sandbox` — the chat agent's threads and
the user's sandbox (docs/chat.md, docs/sandbox.md).

Thin like every route module: a message is stored and a turn is queued here; the worker
does the rest. The event stream is `jobs/chat_events.py`; the runner's status is the
sandbox client's.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import CHAT
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..db.models import (
    AgentSetting,
    ChatTurn,
    Conversation,
    Document,
    KanoProxyKey,
    Message,
    User,
    utcnow,
)
from ..errors import ApiError
from ..jobs import chat_events
from ..jobs.queue import count_active_turns, enqueue_turn
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..workspace.documents import (
    current_revision,
    directory_size,
    materialize_document,
    workspace_size,
)
from ..workspace.layout import CACHE_DIR, FILES_DIR, SIGHTREAD_DIR, git_dir, sources_dir, user_root

router = APIRouter(prefix="/api", tags=["chat"])

SSE_MEDIA_TYPE = "text/event-stream"
RETRY_AFTER_SECONDS = 15
TITLE_MAX_CHARS = 80


# --- conversations -------------------------------------------------------------------------


def conversation_payload(row: Conversation) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "document_id": row.document_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def owned_conversation(db: AsyncSession, user_id: int, conversation_id: int) -> Conversation:
    row = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such conversation")
    return row


async def owned_document(db: AsyncSession, user_id: int, document_id: int) -> Document:
    row = (
        await db.execute(
            select(Document).where(Document.id == document_id, Document.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such document")
    return row


@router.get("/library/documents/{document_id}/conversations")
async def list_conversations(document_id: int, user: SessionUser, db: DbSession):
    document = await owned_document(db, user.id, document_id)
    rows = (
        await db.execute(
            select(Conversation)
            .where(Conversation.document_id == document.id)
            .order_by(Conversation.updated_at.desc())
        )
    ).scalars()
    return {"conversations": [conversation_payload(row) for row in rows]}


@router.post(
    "/library/documents/{document_id}/conversations",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    document = await owned_document(db, user.id, document_id)
    # The document's directory exists from here on: the thread is about its files.
    await materialize_document(db, settings, document)
    # The user's row, the lock a delete takes before it decides, then the document
    # again under it: a delete racing this ends with a clean 404 here, or with a thread
    # the delete then takes along with the document — never a 500 (docs/api.md).
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    still = (
        await db.execute(
            select(Document.id).where(Document.id == document.id, Document.user_id == user.id)
        )
    ).first()
    if still is None:
        raise ApiError(404, "invalid_request", "No such document")
    row = Conversation(
        user_id=user.id,
        document_id=document.id,
        title="",
        # The shell this thread holds in the user's VM (docs/sandbox.md).
        session=secrets.token_hex(8),
    )
    db.add(row)
    await db.commit()
    return conversation_payload(row)


# What the stored selection block says before the passage (docs/chat.md § Prompt caching).
SELECTION_PREFIX = "The passage selected in the document:\n\n"
# The per-turn context block (docs/chat.md § Prompt caching): what the model must know
# about this message and may not learn from the cached prefix.
TURN_CONTEXT = "turn"


def turn_note(revision: str | None, locale: str) -> str:
    return (
        f"Context for this message:\n- document revision: {revision or 'unknown'}\n"
        f"- reader locale: {locale}"
    )


def display_content(content: list) -> list:
    """The blocks as the panel draws them: an image — a page or a figure a tool returned,
    stored for the model's replay — is sent without its bytes, which the panel never
    renders and a long thread would otherwise carry by the hundred megabytes; the
    selection block is sent as the passage alone, without the line that introduces it
    to the model, so the note the panel shows counts the selection and nothing else
    (docs/api.md § Chat)."""
    shown = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            shown.append({"type": "image"})
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            shown.append({**block, "content": display_content(block["content"])})
        elif block.get("context") == "selection" and isinstance(block.get("text"), str):
            shown.append({**block, "text": block["text"].removeprefix(SELECTION_PREFIX)})
        elif block.get("context") == TURN_CONTEXT:
            continue  # the model's context, nothing the thread shows
        else:
            shown.append(block)
    return shown


@router.get("/conversations/{conversation_id}")
async def read_conversation(conversation_id: int, user: SessionUser, db: DbSession):
    row = await owned_conversation(db, user.id, conversation_id)
    messages = (
        await db.execute(
            select(Message).where(Message.conversation_id == row.id).order_by(Message.id)
        )
    ).scalars()
    turns = (
        await db.execute(
            select(ChatTurn).where(ChatTurn.conversation_id == row.id).order_by(ChatTurn.created_at)
        )
    ).scalars()
    return {
        "conversation": conversation_payload(row),
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": display_content(message.content),
                "turn_id": str(message.turn_id) if message.turn_id else None,
                "created_at": message.created_at,
            }
            for message in messages
        ],
        "turns": [
            {
                "id": str(turn.id),
                "status": turn.status,
                "error": turn.error,
                # Null while the turn is unfinished, whatever its status says: a cancel
                # marks the status first and the writer winds down after (docs/chat.md).
                "finished_at": turn.finished_at,
            }
            for turn in turns
        ],
    }


@router.delete(
    "/conversations/{conversation_id}",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation(conversation_id: int, user: SessionUser, db: DbSession) -> Response:
    row = await owned_conversation(db, user.id, conversation_id)
    # Under the same row lock a send takes: the worker must not be driving a turn whose
    # rows vanish under it (docs/chat.md § Conversations, turns, events). A cancelled turn
    # counts until the worker has wound it down — only `finished_at` says it has.
    await db.execute(select(Conversation.id).where(Conversation.id == row.id).with_for_update())
    if await _active_turn(db, row.id) is not None:
        raise ApiError(
            409,
            "invalid_request",
            "This conversation is still answering; cancel it and wait for it to stop",
        )
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MessageCreate(BaseModel):
    # The caps are the deployment's (`CHAT_MESSAGE_MAX_CHARS`, `CHAT_SELECTION_MAX_CHARS`),
    # checked in the route where the settings are.
    text: str = Field(min_length=1)
    # The passage the reader had selected when they sent — context, appended as its own
    # block of the user message (docs/chat.md § Prompt caching).
    selection: str | None = None
    # The web app's locale: the language the turn answers in (docs/chat.md § System prompt).
    locale: str = Field(default="en", max_length=16, pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")


async def _active_turn(db: AsyncSession, conversation_id: int) -> uuid.UUID | None:
    """A turn the worker may still be driving: queued, running, or cancelled but not
    yet wound down — anything without `finished_at` (docs/chat.md)."""
    return (
        (
            await db.execute(
                select(ChatTurn.id).where(
                    ChatTurn.conversation_id == conversation_id, ChatTurn.finished_at.is_(None)
                )
            )
        )
        .scalars()
        .first()
    )


async def _ready_to_chat(
    db: AsyncSession, settings: AppSettings, user_id: int, *, needs_model: bool
) -> AgentSetting | None:
    """The enqueue rules of docs/agents.md, applied to the chat agent. A conversation
    that has its model already needs none chosen on the Agent page: later turns use
    the stored one, whatever the page says now (docs/chat.md § Conversations)."""
    if not settings.kano_proxy_configured:
        raise ApiError(503, "upstream", "Kano Proxy is not configured on this deployment")
    setting = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user_id, AgentSetting.agent == CHAT.name
            )
        )
    ).scalar_one_or_none()
    if needs_model and (setting is None or not setting.model):
        raise ApiError(
            400, "invalid_request", "No model chosen for the chat agent: pick one on the Agent page"
        )
    key = (
        await db.execute(select(KanoProxyKey).where(KanoProxyKey.user_id == user_id))
    ).scalar_one_or_none()
    if key is None:
        raise ApiError(
            400, "invalid_request", "Kano Proxy is not connected: connect it on the Agent page"
        )
    return setting


@router.post(
    "/conversations/{conversation_id}/messages",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_message(
    conversation_id: int,
    body: MessageCreate,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    if len(body.text) > settings.chat_message_max_chars:
        raise ApiError(400, "invalid_request", "The message is too long")
    if body.selection is not None and len(body.selection) > settings.chat_selection_max_chars:
        raise ApiError(400, "invalid_request", "The selection is too long")
    row = await owned_conversation(db, user.id, conversation_id)
    # Enforced where the decisions are made, under row locks held until this transaction
    # commits (PostgreSQL; the SQLite fallback runs one request at a time): the user's row
    # for the per-user cap, so two sends to different conversations cannot both slip under
    # it, then the conversation's for one turn at a time — always in that order.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    locked = (
        await db.execute(select(Conversation.id).where(Conversation.id == row.id).with_for_update())
    ).first()
    if locked is None:
        # Deleted while this send waited for the lock: a clean 404, not rows inserted
        # for a conversation that is gone (docs/chat.md § Conversations).
        raise ApiError(404, "invalid_request", "No such conversation")
    setting = await _ready_to_chat(db, settings, user.id, needs_model=row.model is None)
    if row.model is None and setting is not None and setting.model:
        # The model the message was sent under, fixed as it is accepted: a change on the
        # Agent page while the turn waits in the queue does not reach it (docs/chat.md).
        row.model = setting.model
    if await count_active_turns(db, user.id) >= settings.max_chat_turns_per_user:
        raise ApiError(
            429,
            "rate_limit",
            "Too many running conversations; wait for one to finish",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    if await _active_turn(db, row.id) is not None:
        raise ApiError(409, "invalid_request", "This conversation is still answering")

    content: list[dict] = [{"type": "text", "text": body.text}]
    selection = (body.selection or "").strip()
    if selection:
        # Marked as ours: the runner strips the marker before the block goes upstream,
        # and the thread shows a note rather than the scaffolding (docs/chat.md).
        content.append(
            {
                "type": "text",
                "text": f"{SELECTION_PREFIX}{selection}",
                "context": "selection",
            }
        )
    # The document's revision as the message is accepted, and the reader's locale: with
    # the message rather than in the system prompt, so an edit made meanwhile reaches the
    # model and a change of language costs no cache (docs/chat.md § Prompt caching).
    document = await db.get(Document, row.document_id)
    revision = None if document is None else await current_revision(db, settings, document)
    content.append(
        {"type": "text", "text": turn_note(revision, body.locale), "context": TURN_CONTEXT}
    )
    message = Message(conversation_id=row.id, role="user", content=content)
    db.add(message)
    await db.flush()
    if not row.title:
        first_line = body.text.strip().splitlines()[0] if body.text.strip() else ""
        row.title = first_line[:TITLE_MAX_CHARS]
    turn = await enqueue_turn(
        db,
        user_id=user.id,
        conversation_id=row.id,
        message_id=message.id,
        locale=body.locale,
    )
    # The message belongs to the turn that answers it: a reloaded thread groups by turn.
    message.turn_id = turn.id
    await db.commit()
    return {"turn_id": str(turn.id), "message_id": message.id}


@router.post(
    "/conversations/{conversation_id}/cancel",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_turn(conversation_id: int, user: SessionUser, db: DbSession) -> Response:
    """Mark the active turn cancelled. A running one is wound down by the worker, which
    sets `finished_at`; a queued one is never claimed, so it is finished here and now."""
    row = await owned_conversation(db, user.id, conversation_id)
    await db.execute(select(Conversation.id).where(Conversation.id == row.id).with_for_update())
    # Locked, and still queued once held: a worker claiming the turn at this moment
    # (FOR UPDATE SKIP LOCKED) either took it already — then it is a running
    # cancellation below, wound down by the worker — or skips it (docs/chat.md).
    queued = (
        (
            await db.execute(
                select(ChatTurn.id)
                .where(ChatTurn.conversation_id == row.id, ChatTurn.status == "queued")
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for turn_id in queued:
        # Finished here, with the event a worker would have written: the stream sees the
        # turn end instead of waiting for a start that never comes.
        await db.execute(
            update(ChatTurn)
            .where(ChatTurn.id == turn_id)
            .values(status="cancelled", error="cancelled", finished_at=utcnow())
        )
        await chat_events.append(
            db,
            row.id,
            turn_id,
            chat_events.TURN_ERROR,
            {"status": "cancelled", "error": "cancelled"},
        )
    await db.execute(
        update(ChatTurn)
        .where(ChatTurn.conversation_id == row.id, ChatTurn.status == "running")
        .values(status="cancelled")
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations/{conversation_id}/events")
async def conversation_events(
    conversation_id: int,
    request: Request,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
    after: int = Query(default=0, ge=0),
):
    row = await owned_conversation(db, user.id, conversation_id)
    # The request's session is done: the stream opens its own, and a tab holding this
    # response for a whole turn must not hold a pool connection with it (docs/chat.md).
    await db.close()
    return StreamingResponse(
        chat_events.stream_conversation_events(
            request.app.state.sessionmaker, settings.database_url, row.id, after
        ),
        media_type=SSE_MEDIA_TYPE,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- the sandbox --------------------------------------------------------------------------


def _workspace_usage(root: Path, quota: int) -> dict:
    breakdown = []
    for name in (FILES_DIR, f"{SIGHTREAD_DIR}/results", CACHE_DIR):
        size = directory_size(root, root / name)
        if size:
            breakdown.append({"path": f"{name}/", "bytes": size})
    history = directory_size(git_dir(root))
    if history:
        breakdown.append({"path": "history/", "bytes": history})
    originals = directory_size(sources_dir(root))
    if originals:
        breakdown.append({"path": "originals/", "bytes": originals})
    return {
        "bytes": workspace_size(root),
        "quota_bytes": quota,
        "breakdown": breakdown,
    }


@router.get("/sandbox")
async def sandbox_status(user: SessionUser, settings: AppSettings):
    client = SandboxClient(settings)
    try:
        runner = await client.status(user.id)
        state = {
            "state": runner.state,
            "last_used_at": runner.last_used_at,
            "shells": runner.shells,
            "uptime_seconds": runner.uptime_seconds,
        }
    except SandboxUnavailable:
        state = {"state": "unavailable", "last_used_at": None, "shells": 0, "uptime_seconds": 0}
    root = user_root(settings.workspaces_dir, user.id)
    workspace = await asyncio.to_thread(_workspace_usage, root, settings.workspace_quota_bytes)
    return {**state, "workspace": workspace}


@router.post("/sandbox/stop", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def sandbox_stop(user: SessionUser, settings: AppSettings) -> Response:
    try:
        await SandboxClient(settings).stop(user.id)
    except SandboxUnavailable as exc:
        raise ApiError(503, "upstream", "The sandbox is unavailable") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sandbox/reset", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def sandbox_reset(user: SessionUser, settings: AppSettings) -> Response:
    try:
        await SandboxClient(settings).reset(user.id)
    except SandboxUnavailable as exc:
        raise ApiError(503, "upstream", "The sandbox is unavailable") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def turn_id_of(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ApiError(404, "invalid_request", "No such turn") from exc
