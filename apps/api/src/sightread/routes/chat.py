"""Control plane `/api/conversations/*` and `/api/sandbox` — the chat agent's threads and
the user's sandbox (docs/chat.md, docs/sandbox.md).

Thin like every route module: a message is stored and a turn is queued here; the worker
does the rest. The event stream is `jobs/chat_events.py`; the runner's status is the
sandbox client's.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import secrets
import uuid
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import CHAT
from ..agents import chat as chat_agent
from ..agents import memory as memory_defs
from ..agents import skills as skill_defs
from ..agents.chat import (  # noqa: F401 - re-exported: the context route and the tests read them here
    ANSWER_LANGUAGE_RULE,
    TRANSLATION_LANGUAGE_RULE,
    TURN_CONTEXT,
    turn_note,
)
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..billing import ledger
from ..billing.credits import credits_for_usd
from ..db.models import (
    COMPACT_TURN,
    FILE_KIND,
    MESSAGE_TURN,
    AgentSetting,
    ChatAttachment,
    ChatTurn,
    Conversation,
    Document,
    Message,
    Skill,
    User,
    utcnow,
)
from ..errors import ApiError
from ..jobs import chat_compact, chat_events, chat_versions
from ..jobs.intake import insufficient_credits
from ..jobs.queue import HISTORY_FULL, count_active_turns, enqueue_turn, history_bytes
from ..parsing.attachments import prepared_path
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..upstream.client import agent_models
from ..workspace.documents import (
    StorageUsage,
    current_revision,
    ensure_within_quota,
    is_live,
    materialize_document,
    measure_storage,
    user_folders,
)
from ..workspace.layout import user_root

router = APIRouter(prefix="/api", tags=["chat"])

SSE_MEDIA_TYPE = "text/event-stream"
TITLE_MAX_CHARS = 80


# --- conversations -------------------------------------------------------------------------


def conversation_payload(row: Conversation, settings: AppSettings, *, compactable: bool) -> dict:
    """The conversation as the list and the detail give it, with what the panel's ring
    reads: the context the last call replayed, the model's window, when the cache
    expires and what a cold re-read would cost (docs/chat.md § Compaction)."""
    price = settings.model_prices.get(row.model) if row.model else None
    recache = None
    if price is not None and row.context_tokens:
        recache = credits_for_usd(
            price.usd(prompt_tokens=0, completion_tokens=0, cache_write_tokens=row.context_tokens),
            settings.credit_usd,
        )
    return {
        "id": row.id,
        "title": row.title,
        "document_id": row.document_id,
        "model": row.model,
        "compacted_message_id": row.compacted_message_id,
        "compactable": compactable,
        "context_tokens": row.context_tokens,
        "context_window": (
            price.context
            if price is not None and price.context
            else settings.chat_context_window_tokens
        ),
        "cache_warm_until": row.cache_warm_until,
        "recache_credits": recache,
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
    """A document of the caller's that is not in the Trash: a trashed file is not talked
    about until it is put back (docs/api.md)."""
    row = (
        await db.execute(
            select(Document).where(Document.id == document_id, Document.user_id == user_id)
        )
    ).scalar_one_or_none()
    # A file of the library has no conversations (docs/workspace.md § Files).
    if row is None or row.kind == FILE_KIND or not is_live(row, await user_folders(db, user_id)):
        raise ApiError(404, "invalid_request", "No such document")
    return row


@router.get("/library/documents/{document_id}/conversations")
async def list_conversations(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    document = await owned_document(db, user.id, document_id)
    # Only threads with a message: an empty row is a first send that was refused, kept for
    # its retry, and never a conversation to pick (docs/chat.md § Conversations).
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.document_id == document.id,
                    exists().where(Message.conversation_id == Conversation.id),
                )
                .order_by(Conversation.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    compactable = await chat_compact.compactable_ids(db, [row.id for row in rows])
    return {
        "conversations": [
            conversation_payload(row, settings, compactable=row.id in compactable) for row in rows
        ]
    }


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
    # The same lock a move to the Trash takes: one meanwhile is a 404 here too.
    await owned_document(db, user.id, document.id)
    row = Conversation(
        user_id=user.id,
        document_id=document.id,
        title="",
        # The shell this thread holds in the user's VM (docs/sandbox.md).
        session=secrets.token_hex(8),
    )
    db.add(row)
    await db.commit()
    return conversation_payload(row, settings, compactable=False)


# What the stored selection block says before the passage (docs/chat.md § Prompt caching).
SELECTION_PREFIX = "The passage selected in the document:\n\n"
# And the reply's quote before its passage (docs/chat.md § What a user message carries).
QUOTE_PREFIX = "The user is replying to this passage of your earlier answer:\n\n"
ATTACHMENT_EXPIRED = "An attachment is no longer available; attach it again"


SECTION_CONTEXT = "section"


def section_text(heading: str, text: str | None) -> str:
    """A whole section the reader added, introduced by its heading; one too long to send
    names the heading and points the agent at the document rather than passing a cut
    section for a whole one (docs/chat.md § What a user message carries)."""
    if text is None:
        return (
            f'The user added the whole section "{heading}" of the document to this message. '
            "It is too long to include here: read it from the document."
        )
    return f'The whole section "{heading}" of the document:\n\n{text}'


def display_content(content: list) -> list:
    """The blocks as the panel draws them: an image — a page or a figure a tool returned,
    stored for the model's replay — is sent without its bytes, which the panel never
    renders and a long thread would otherwise carry by the hundred megabytes, and an
    attached image without them too, the panel asking for them by the block's index; the
    selection and the quote are sent as the passage alone, without the line that
    introduces it to the model, so the note the panel shows counts the passage and
    nothing else; a command's skill, an attached file and a whole section are sent as what
    they name, not the text the model reads (docs/api.md § Chat)."""
    shown = []
    for block in content:
        if not isinstance(block, dict):
            continue
        marker = block.get("context")
        if block.get("type") == "image":
            image: dict = {"type": "image"}
            if isinstance(block.get("attachment"), dict):
                image["attachment"] = block["attachment"]
            shown.append(image)
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            shown.append({**block, "content": display_content(block["content"])})
        elif marker == "selection" and isinstance(block.get("text"), str):
            shown.append({**block, "text": block["text"].removeprefix(SELECTION_PREFIX)})
        elif marker == "quote" and isinstance(block.get("text"), str):
            shown.append({**block, "text": block["text"].removeprefix(QUOTE_PREFIX)})
        elif marker == "skill":
            shown.append({"type": "text", "context": "skill", "skill": block.get("skill")})
        elif marker == SECTION_CONTEXT:
            # The heading the thread draws as a chip, never the section's text.
            shown.append(
                {"type": "text", "context": SECTION_CONTEXT, "section": block.get("section")}
            )
        elif marker == "attachment":
            shown.append(
                {"type": "text", "context": "attachment", "attachment": block.get("attachment")}
            )
        elif marker in (TURN_CONTEXT, chat_compact.COMPACT_CONTEXT):
            continue  # the model's context and the compaction instruction: nothing the thread shows
        else:
            shown.append(block)
    return shown


@router.get("/conversations/{conversation_id}")
async def read_conversation(
    conversation_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await owned_conversation(db, user.id, conversation_id)
    # The branch the conversation shows, root to head, and the rows of no turn, which
    # precede every turn: the rows of a version the reader edited away stay unsent until a
    # switch brings its branch back (docs/chat.md § History is append-only, *An edited
    # message starts a version*).
    turns = await chat_versions.conversation_turns(db, row.id)
    shown = chat_versions.branch(turns, row.head_id)
    on_branch = [turn.id for turn in shown]
    messages = (
        await db.execute(
            select(Message)
            .where(
                Message.conversation_id == row.id,
                or_(Message.turn_id.is_(None), Message.turn_id.in_(on_branch)),
            )
            .order_by(Message.id)
        )
    ).scalars()
    credits = await ledger.credits_by_turn(db, on_branch)
    return {
        "conversation": conversation_payload(
            row, settings, compactable=await chat_compact.compactable(db, row.id)
        ),
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
                # A user message's, or a compaction's (docs/chat.md § Compaction).
                "kind": turn.kind,
                "status": turn.status,
                "error": turn.error,
                # Null while the turn is unfinished, whatever its status says: a cancel
                # marks the status first and the writer winds down after (docs/chat.md).
                "finished_at": turn.finished_at,
                # What the turn cost, once it is settled (docs/billing.md § Routes).
                "credits": credits.get(turn.id),
                # The turns that are versions of one message, this one among them, oldest
                # first — null for a message with one version (docs/chat.md § History is
                # append-only, *An edited message starts a version*).
                "versions": (
                    [str(version) for version in versions]
                    if (versions := chat_versions.versions_of(turns, turn))
                    else None
                ),
            }
            for turn in shown
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


class ConversationUpdate(BaseModel):
    """A rename, a move to another model, a switch to another version of a message, or any
    of them together (docs/api.md)."""

    title: str | None = None
    model: str | None = Field(default=None, max_length=255)
    # The id of a `message` turn: the conversation then shows its branch (docs/chat.md
    # § History is append-only, *An edited message starts a version*).
    version: uuid.UUID | None = None


@router.put("/conversations/{conversation_id}", dependencies=[CsrfGuard])
async def update_conversation(
    conversation_id: int,
    body: ConversationUpdate,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """The reader's own title, one line like a memory's text, the model the conversation's
    later turns run on, and the version of a message it shows. Written with `updated_at`
    kept as it was: the list orders by it, and none of them is a thread continued. A running
    turn does not refuse the title — the runner never writes it — and refuses the model and
    the version (**409**): the turn in flight replays and stores blocks under the model and
    on the branch it started on (docs/chat.md § History is append-only)."""
    provided = body.model_fields_set
    if not provided & {"title", "model", "version"}:
        raise ApiError(400, "invalid_request", "Send a title, a model or a version")
    row = await owned_conversation(db, user.id, conversation_id)
    values: dict = {"updated_at": Conversation.updated_at}
    if "title" in provided:
        title = memory_defs.clean_text(body.title or "", single_line=True)[:TITLE_MAX_CHARS].strip()
        if not title:
            raise ApiError(400, "invalid_request", "A conversation needs a title")
        values["title"] = title
    model = None
    if "model" in provided:
        model = (body.model or "").strip()
        # The offered list, read live (docs/billing.md § Models).
        if model not in {entry["id"] for entry in await agent_models(settings, CHAT.name)}:
            raise ApiError(400, "invalid_request", "That model is not one the chat agent offers")
    version = None
    turns: list[ChatTurn] = []
    if "version" in provided:
        turns = await chat_versions.conversation_turns(db, row.id)
        version = next(
            (turn for turn in turns if turn.id == body.version and turn.kind == MESSAGE_TURN),
            None,
        )
        if version is None:
            raise ApiError(
                400, "invalid_request", "That is not a version of a message of this conversation"
            )
    if model is not None or version is not None:
        # Under the conversation's lock, as a send decides: no turn may start under the
        # move or the switch.
        await db.execute(select(Conversation.id).where(Conversation.id == row.id).with_for_update())
        if await _active_turn(db, row.id) is not None:
            raise ApiError(
                409,
                "invalid_request",
                "This conversation is still answering; wait for it to finish",
            )
    if model is not None and model != row.model:
        # The new model has no cache of this history (docs/chat.md § Compaction).
        values["model"] = model
        values["cache_warm_until"] = None
    if version is not None:
        # The branch under the chosen version, down to the turn that continued it last;
        # with it the summary row its replay starts from, and the context size and the
        # cache clock cleared — what the next call replays only that call reports
        # (docs/chat.md § History is append-only, *An edited message starts a version*).
        head_id = chat_versions.newest_leaf(turns, version.id)
        if head_id != row.head_id:
            values.update(
                head_id=head_id,
                compacted_message_id=await chat_versions.branch_boundary(
                    db, chat_versions.branch(turns, head_id)
                ),
                context_tokens=None,
                cache_warm_until=None,
            )
    await db.execute(update(Conversation).where(Conversation.id == row.id).values(**values))
    await db.commit()
    await db.refresh(row)
    return conversation_payload(
        row, settings, compactable=await chat_compact.compactable(db, row.id)
    )


class SectionIn(BaseModel):
    """A whole section the reader added by its heading: its markdown, or null for one past
    the selection cap, which the agent then reads from the document."""

    heading: str
    text: str | None = None


class MessageCreate(BaseModel):
    # The caps are the deployment's (`CHAT_MESSAGE_MAX_CHARS`, `CHAT_SELECTION_MAX_CHARS`,
    # `CHAT_SECTIONS_PER_MESSAGE`, `CHAT_ATTACHMENTS_PER_MESSAGE`), checked in the route
    # where the settings are. Empty text is a message only with a command or an attachment;
    # with a command it is the command's arguments (docs/chat.md § What a user message
    # carries).
    text: str = ""
    # The passage the reader had selected when they sent — context, appended as its own
    # block of the user message (docs/chat.md § Prompt caching).
    selection: str | None = None
    # Whole sections the reader added, each its own block after the selection.
    sections: list[SectionIn] = Field(default_factory=list)
    # The passage of an earlier answer the reader is replying to.
    quote: str | None = None
    # A skill the reader runs as `/name` (docs/chat.md § Skills).
    command: str | None = Field(default=None, max_length=64)
    # Ids `POST /api/attachments` returned.
    attachments: list[uuid.UUID] = Field(default_factory=list)
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
) -> tuple[AgentSetting | None, str | None]:
    """The enqueue rules of docs/agents.md, applied to the chat agent: its settings, and
    the model the conversation starts on, read from the offered list as it is now
    (docs/billing.md § Models). A conversation that has its model already needs none:
    later turns use the stored one, whatever the page says now (docs/chat.md
    § Conversations)."""
    if not settings.kano_proxy_configured:
        raise ApiError(503, "upstream", "Kano Proxy is not configured on this deployment")
    setting = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user_id, AgentSetting.agent == CHAT.name
            )
        )
    ).scalar_one_or_none()
    if not needs_model:
        return setting, None
    # The live list, as the picker shows it: a model Kano Proxy no longer serves must not
    # be fixed on a conversation for every later turn to fail on (docs/billing.md § Models).
    offered = [entry["id"] for entry in await agent_models(settings, CHAT.name)]
    picked = setting.model if setting is not None else None
    if picked is not None and picked not in offered:
        raise ApiError(
            400,
            "invalid_request",
            "That model is no longer served: pick another on the Agent page",
        )
    if not offered:
        raise ApiError(503, "upstream", "No chat model is offered on this deployment")
    return setting, picked or offered[0]


async def _admitted(
    db: AsyncSession, settings: AppSettings, user: SessionUser, row: Conversation
) -> tuple:
    """The admission a message and an edit share (docs/chat.md § Conversations, turns,
    events): the workspace's quota, the locks, the live-document check, the model fixed on
    a first send, the per-user cap and one turn at a time. Returns the conversation's row
    as locked — its title, head and summary row, read under the lock — the document, the
    chat agent's settings and the conversation's turns, read under the lock too: the branch
    a message joins is decided where no other send can move it."""
    # A full workspace runs no turn (docs/workspace.md § Quota): refused here, where the
    # reader is told at once, rather than as a turn that fails after it was accepted.
    await ensure_within_quota(db, settings, user.id, user_root(settings.workspaces_dir, user.id))
    # Enforced where the decisions are made, under row locks held until this transaction
    # commits (PostgreSQL; the SQLite fallback runs one request at a time): the user's row
    # for the per-user cap, so two sends to different conversations cannot both slip under
    # it, then the conversation's for one turn at a time — always in that order.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    # The title read under the lock too: a rename that landed after `row` was read must
    # not be overwritten by the first line of a first message.
    locked = (
        await db.execute(
            select(
                Conversation.id,
                Conversation.title,
                Conversation.head_id,
                Conversation.compacted_message_id,
            )
            .where(Conversation.id == row.id)
            .with_for_update()
        )
    ).first()
    if locked is None:
        # Deleted while this send waited for the lock: a clean 404, not rows inserted
        # for a conversation that is gone (docs/chat.md § Conversations).
        raise ApiError(404, "invalid_request", "No such conversation")
    # Under the user's row lock, which a move to the Trash takes before it marks the row:
    # no turn starts on a file in the Trash, and none queued here is missed by its check
    # for unfinished turns (docs/workspace.md § The Trash).
    document = await db.get(Document, row.document_id)
    if document is None or not is_live(document, await user_folders(db, user.id)):
        raise ApiError(409, "invalid_request", "This file is in the Trash; put it back to continue")
    setting, model = await _ready_to_chat(db, settings, user.id, needs_model=row.model is None)
    if row.model is None and model is not None:
        # The model the message was sent under, fixed as it is accepted: a change on the
        # Agent page while the turn waits in the queue does not reach it (docs/chat.md).
        row.model = model
    if await count_active_turns(db, user.id) >= settings.max_chat_turns_per_user:
        raise ApiError(
            429,
            "rate_limit",
            "Too many running conversations; wait for one to finish",
            headers={"Retry-After": str(settings.chat_retry_after_seconds)},
        )
    if await _active_turn(db, row.id) is not None:
        raise ApiError(409, "invalid_request", "This conversation is still answering")
    return locked, document, setting, await chat_versions.conversation_turns(db, row.id)


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
    if body.quote is not None and len(body.quote) > settings.chat_selection_max_chars:
        raise ApiError(400, "invalid_request", "The quoted passage is too long")
    sections = _checked_sections(body.sections, settings)
    attachment_ids = list(dict.fromkeys(body.attachments))
    if len(attachment_ids) > settings.chat_attachments_per_message:
        raise ApiError(
            400,
            "invalid_request",
            f"At most {settings.chat_attachments_per_message} attachments go with one message",
        )
    command = (body.command or "").strip() or None
    if not body.text.strip() and command is None and not attachment_ids:
        raise ApiError(400, "invalid_request", "The message needs text, a command or a file")
    row = await owned_conversation(db, user.id, conversation_id)
    locked, document, setting, turns = await _admitted(db, settings, user, row)
    # The message continues the branch the conversation shows, measured from that branch's
    # summary row, both as locked (docs/chat.md § History is append-only, *An edited
    # message starts a version*).
    shown = chat_versions.branch(turns, locked.head_id)
    path = [turn.id for turn in shown]
    boundary = locked.compacted_message_id
    # Under the conversation's lock: a history past the cap takes no further message, or
    # capped-size sends would grow it without end while every turn on it failed
    # (docs/chat.md § History is append-only). What a turn replays: from the last
    # compaction's summary on (docs/chat.md § Compaction).
    if await history_bytes(db, row.id, since=boundary, path=path) > settings.chat_history_max_bytes:
        raise ApiError(409, "invalid_request", HISTORY_FULL)

    # The reader's `/compact`: the runner's own command, never a skill's name
    # (docs/chat.md § Compaction).
    compact = command == chat_agent.COMPACT_COMMAND
    if compact:
        if attachment_ids:
            raise ApiError(400, "invalid_request", "/compact takes no attachments")
        if not await chat_compact.compactable(db, row.id):
            raise ApiError(
                400, "nothing_to_compact", "There is nothing to compact in this conversation"
            )

    skill = (
        await _invocable_skill(db, user.id, command)
        if command is not None and not compact
        else None
    )

    # The attachments first, images before files, as the Messages API advises; each was
    # prepared at its upload, and the send embeds and deletes it (docs/chat.md § What a
    # user message carries). Under the user's row lock: two sends cannot take one.
    attachments = await _claimed_attachments(db, settings, user.id, attachment_ids)
    content: list[dict] = [block for _, block in attachments]

    if skill is not None:
        arguments = body.text.strip()
        visible = f"/{skill.name}" + (f" {arguments}" if arguments else "")
    elif compact:
        focus = body.text.strip()
        visible = f"/{chat_agent.COMPACT_COMMAND}" + (f" {focus}" if focus else "")
    else:
        visible = body.text
    if visible.strip():
        content.append({"type": "text", "text": visible})
    # A compaction carries the instruction and the turn block, nothing else: the selection
    # the composer read as the command was sent is not what it is about.
    selection = (body.selection or "").strip() if not compact else ""
    if compact:
        sections = []
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
    for heading, text in sections:
        content.append(
            {
                "type": "text",
                "text": section_text(heading, text),
                "context": SECTION_CONTEXT,
                "section": heading,
            }
        )
    quote = (body.quote or "").strip() if not compact else ""
    if quote:
        content.append({"type": "text", "text": f"{QUOTE_PREFIX}{quote}", "context": "quote"})
    if compact:
        content.append(
            {
                "type": "text",
                "text": chat_agent.compact_instruction(body.text),
                "context": chat_compact.COMPACT_CONTEXT,
            }
        )
    if skill is not None:
        content.append(
            _skill_block(skill, body.text, bool(selection), [heading for heading, _ in sections])
        )
    # The document's revision as the message is accepted, the reader's locale and the
    # languages they chose: with the message rather than in the system prompt, so an edit
    # made meanwhile reaches the model and a change of language costs no cache (docs/chat.md
    # § Prompt caching, § Instructions).
    revision = await current_revision(db, settings, document)
    note = turn_note(
        revision,
        body.locale,
        setting.answer_language if setting else None,
        setting.translate_language if setting else None,
    )
    content.append({"type": "text", "text": note, "context": TURN_CONTEXT})
    message = Message(conversation_id=row.id, role="user", content=content)
    db.add(message)
    await db.flush()
    # Again with the message in, measured as the worker measures it: attached images can
    # carry a history from under the cap to past it, and the turn would then fail before
    # its first call with the attachments spent. Refused here instead, so the rollback
    # keeps them for a new conversation.
    if await history_bytes(db, row.id, since=boundary, path=path) > settings.chat_history_max_bytes:
        raise ApiError(409, "invalid_request", HISTORY_FULL)
    if not locked.title and not compact:
        named = visible.strip() or (attachments[0][0].name if attachments else "")
        first_line = named.splitlines()[0] if named else ""
        # Trimmed after the cut, as a rename is: a line cut at a space keeps none.
        row.title = first_line[:TITLE_MAX_CHARS].strip()
    turn = await enqueue_turn(
        db,
        user_id=user.id,
        conversation_id=row.id,
        message_id=message.id,
        locale=body.locale,
        kind=COMPACT_TURN if compact else MESSAGE_TURN,
        parent_id=path[-1] if path else None,
    )
    # The turn's entry, under the user's row lock taken above: a send needs the minimum
    # balance, and what the turn costs is settled on it as it runs (docs/billing.md
    # § A chat turn).
    try:
        await ledger.reserve(
            db,
            settings,
            user_id=user.id,
            kind=ledger.CHAT,
            amount=0,
            turn_id=turn.id,
            model=row.model,
            minimum=settings.chat_min_credits,
        )
    except ledger.InsufficientCredits as exc:
        raise insufficient_credits(exc) from exc
    # The conversation list orders by it: the thread continued last is listed first.
    row.updated_at = utcnow()
    # The turn continues the branch, and the conversation shows it (docs/chat.md § History
    # is append-only, *An edited message starts a version*).
    row.head_id = turn.id
    # The message belongs to the turn that answers it: a reloaded thread groups by turn.
    message.turn_id = turn.id
    await db.commit()
    # Embedded now: the prepared files have nothing left to wait for.
    for attachment, _ in attachments:
        prepared = prepared_path(Path(settings.upload_dir), attachment.id, attachment.media_type)
        with contextlib.suppress(OSError):
            await asyncio.to_thread(prepared.unlink, missing_ok=True)
    return {"turn_id": str(turn.id), "message_id": message.id}


class MessageEdit(BaseModel):
    """The new text of a sent message — the arguments, for a command — and the web app's
    locale (docs/api.md)."""

    text: str = ""
    locale: str = Field(default="en", max_length=16, pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/edit",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_202_ACCEPTED,
)
async def edit_message(
    conversation_id: int,
    message_id: int,
    body: MessageEdit,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """A sent message sent again with new text, as a version of its own (docs/chat.md
    § History is append-only, *An edited message starts a version*): a new user message and
    a new turn, the edited turn's sibling, which the conversation then shows. The old
    message, its turn and its answer stay where they are, and nothing stored is rewritten:
    the branch the new version joins is the edited turn's ancestry, whose prefix the model
    has cached and whose thinking blocks stay valid."""
    if len(body.text) > settings.chat_message_max_chars:
        raise ApiError(400, "invalid_request", "The message is too long")
    row = await owned_conversation(db, user.id, conversation_id)
    original = await db.get(Message, message_id)
    if original is None or original.conversation_id != row.id or original.role != "user":
        raise ApiError(404, "invalid_request", "No such message")
    locked, document, setting, turns = await _admitted(db, settings, user, row)
    edited = next((turn for turn in turns if turn.id == original.turn_id), None)
    # Only a `message` turn's own user message: not a compaction's instruction, nor the
    # summary it left (docs/chat.md § Compaction).
    if edited is None or edited.kind != MESSAGE_TURN or edited.message_id != original.id:
        raise ApiError(404, "invalid_request", "That message cannot be edited")
    # The branch the new version joins — the turns before the edited one — and its own
    # summary row, which the history cap is measured from and the conversation keeps.
    joined = [] if edited.parent_id is None else chat_versions.branch(turns, edited.parent_id)
    path = [turn.id for turn in joined]
    boundary = await chat_versions.branch_boundary(db, joined)
    if await history_bytes(db, row.id, since=boundary, path=path) > settings.chat_history_max_bytes:
        raise ApiError(409, "invalid_request", HISTORY_FULL)
    # What the message carried, as stored: its attachments, its selection, sections and
    # quote; the text is the reader's new one, the skill's body is rendered again from the
    # skill as it is now, and the turn block is this send's (docs/chat.md § What a user
    # message carries).
    carried = [block for block in original.content if isinstance(block, dict)]
    attachments = [
        block
        for block in carried
        if block.get("type") == "image" or block.get("context") == "attachment"
    ]
    selection = [block for block in carried if block.get("context") == "selection"]
    sections = [block for block in carried if block.get("context") == SECTION_CONTEXT]
    quote = [block for block in carried if block.get("context") == "quote"]
    command = next((block for block in carried if block.get("context") == "skill"), None)
    skill = (
        await _invocable_skill(db, user.id, str(command.get("skill") or ""))
        if command is not None
        else None
    )
    if skill is not None:
        arguments = body.text.strip()
        visible = f"/{skill.name}" + (f" {arguments}" if arguments else "")
    else:
        visible = body.text
    if not visible.strip() and not attachments:
        raise ApiError(400, "invalid_request", "The message needs text, a command or a file")
    content: list[dict] = list(attachments)
    if visible.strip():
        content.append({"type": "text", "text": visible})
    content += selection + sections + quote
    if skill is not None:
        content.append(
            _skill_block(
                skill,
                body.text,
                bool(selection),
                [str(block.get("section") or "") for block in sections],
            )
        )
    revision = await current_revision(db, settings, document)
    note = turn_note(
        revision,
        body.locale,
        setting.answer_language if setting else None,
        setting.translate_language if setting else None,
    )
    content.append({"type": "text", "text": note, "context": TURN_CONTEXT})
    message = Message(conversation_id=row.id, role="user", content=content)
    db.add(message)
    await db.flush()
    # Again with the message in, measured as the worker measures it (the send does the same).
    if await history_bytes(db, row.id, since=boundary, path=path) > settings.chat_history_max_bytes:
        raise ApiError(409, "invalid_request", HISTORY_FULL)
    turn = await enqueue_turn(
        db,
        user_id=user.id,
        conversation_id=row.id,
        message_id=message.id,
        locale=body.locale,
        parent_id=edited.parent_id,
    )
    try:
        await ledger.reserve(
            db,
            settings,
            user_id=user.id,
            kind=ledger.CHAT,
            amount=0,
            turn_id=turn.id,
            model=row.model,
            minimum=settings.chat_min_credits,
        )
    except ledger.InsufficientCredits as exc:
        raise insufficient_credits(exc) from exc
    # The new version is what the conversation shows and continues, with the summary row,
    # the context size and the cache clock its branch's: what the next call replays only
    # that call reports (docs/chat.md § History is append-only, *An edited message starts
    # a version*); `updated_at` moved as a send moves it. Written outright rather than
    # through `row`, which was read before the lock: a switch that landed in between left
    # another branch's summary row in the column, and a value `row` still holds — None,
    # for a branch without one — would not be written again over it.
    await db.execute(
        update(Conversation)
        .where(Conversation.id == row.id)
        .values(
            updated_at=utcnow(),
            head_id=turn.id,
            compacted_message_id=boundary,
            context_tokens=None,
            cache_warm_until=None,
        )
    )
    message.turn_id = turn.id
    await db.commit()
    return {"turn_id": str(turn.id), "message_id": message.id}


async def _invocable_skill(
    db: AsyncSession, user_id: int, command: str
) -> skill_defs.ResolvedSkill:
    """The skill a command names, as the caller can run it now (docs/chat.md § Skills);
    **400** for a name that is no such skill — or no longer is."""
    rows = (await db.execute(select(Skill).where(Skill.user_id == user_id))).scalars().all()
    skill = next(
        (
            candidate
            for candidate in skill_defs.resolve_skills(rows)
            if candidate.name == command and candidate.user_invocable
        ),
        None,
    )
    if skill is None:
        raise ApiError(400, "invalid_request", f"No command /{command}")
    return skill


def _skill_block(
    skill: skill_defs.ResolvedSkill, arguments: str, has_selection: bool, headings: list[str]
) -> dict:
    """The block that carries a command's skill to the model: its body rendered with the
    arguments, kept to the selection and the sections the message carries."""
    rendered = skill_defs.render_skill(skill.body, arguments)
    # Sent with a selection or sections, the command runs on them alone. The range is
    # this line's, not the body's: a body can be older than the range it is sent with
    # (docs/chat.md § Skills, *A command keeps to what it was sent with*).
    ranges = (["the selection above"] if has_selection else []) + [
        f'the section "{heading}"' for heading in headings
    ]
    if ranges:
        *rest, last = ranges
        named = f"{', '.join(rest)} and {last}" if rest else last
        rendered += f"\n\nRun it on {named} only: the rest of the document is outside its range."
    return {
        "type": "text",
        "text": f"The user ran the skill /{skill.name}. Follow its instructions:\n\n{rendered}",
        "context": "skill",
        "skill": skill.name,
    }


def _checked_sections(
    sections: list[SectionIn], settings: AppSettings
) -> list[tuple[str, str | None]]:
    """The sections as `(heading, text)`, refused past the deployment's caps: their number
    (`CHAT_SECTIONS_PER_MESSAGE`), a heading empty or past `CHAT_SECTION_HEADING_MAX_CHARS`,
    a text past `CHAT_SELECTION_MAX_CHARS` — a section that long is sent with no text
    (docs/api.md). The heading is one line, cleaned like a memory: it is shown and quoted
    back."""
    if len(sections) > settings.chat_sections_per_message:
        raise ApiError(
            400,
            "invalid_request",
            f"At most {settings.chat_sections_per_message} sections go with one message",
        )
    checked: list[tuple[str, str | None]] = []
    for section in sections:
        heading = memory_defs.clean_text(section.heading, single_line=True)
        if not heading or len(heading) > settings.chat_section_heading_max_chars:
            raise ApiError(
                400,
                "invalid_request",
                f"A section's heading is 1 to {settings.chat_section_heading_max_chars} characters",
            )
        text = section.text.strip() if section.text is not None else None
        if text is not None and len(text) > settings.chat_selection_max_chars:
            raise ApiError(400, "invalid_request", "A section is too long; send it without text")
        checked.append((heading, text))
    return checked


async def _claimed_attachments(
    db: AsyncSession, settings: AppSettings, user_id: int, ids: list[uuid.UUID]
) -> list[tuple[ChatAttachment, dict]]:
    """The caller's pending attachments as the message's blocks, images first, their rows
    deleted in the send's transaction. 409 for one that is not the caller's, already sent,
    or whose prepared file the sweeper took (docs/api.md)."""
    if not ids:
        return []
    rows = {
        row.id: row
        for row in (
            await db.execute(
                select(ChatAttachment).where(
                    ChatAttachment.id.in_(ids), ChatAttachment.user_id == user_id
                )
            )
        ).scalars()
    }
    if len(rows) != len(ids):
        raise ApiError(409, "invalid_request", ATTACHMENT_EXPIRED)
    upload_dir = Path(settings.upload_dir)
    claimed: list[tuple[ChatAttachment, dict]] = []
    for attachment_id in ids:
        row = rows[attachment_id]
        path = prepared_path(upload_dir, row.id, row.media_type)
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise ApiError(409, "invalid_request", ATTACHMENT_EXPIRED) from exc
        if row.kind == "image":
            block = {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": row.media_type,
                    "data": base64.b64encode(data).decode("ascii"),
                },
                "attachment": {"name": row.name},
            }
        else:
            block = {
                "type": "text",
                "text": f"Attached file: {row.name}\n\n{data.decode('utf-8', 'replace')}",
                "context": "attachment",
                "attachment": {"name": row.name, "kind": row.kind, "chars": row.chars},
            }
        claimed.append((row, block))
    # A remove of one of them racing this send: whichever commits first wins it.
    removed = await db.execute(
        delete(ChatAttachment).where(ChatAttachment.id.in_(ids), ChatAttachment.user_id == user_id)
    )
    if removed.rowcount != len(ids):
        raise ApiError(409, "invalid_request", ATTACHMENT_EXPIRED)
    claimed.sort(key=lambda item: item[0].kind != "image")
    return claimed


@router.get("/conversations/{conversation_id}/messages/{message_id}/attachments/{index}")
async def message_attachment(
    conversation_id: int,
    message_id: int,
    index: int,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
) -> Response:
    """The bytes of an image the reader attached to one of their messages, from the stored
    block itself — the record, and the only copy kept (docs/chat.md § What a user message
    carries)."""
    row = await owned_conversation(db, user.id, conversation_id)
    message = (
        await db.execute(
            select(Message).where(Message.id == message_id, Message.conversation_id == row.id)
        )
    ).scalar_one_or_none()
    content = message.content if message is not None and message.role == "user" else None
    block = content[index] if isinstance(content, list) and 0 <= index < len(content) else None
    source = block.get("source") if isinstance(block, dict) else None
    if (
        not isinstance(block, dict)
        or block.get("type") != "image"
        or not isinstance(block.get("attachment"), dict)
        or not isinstance(source, dict)
        or source.get("media_type") not in ("image/png", "image/jpeg")
        or not isinstance(source.get("data"), str)
    ):
        raise ApiError(404, "invalid_request", "No such attachment")
    try:
        data = base64.b64decode(source["data"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(404, "invalid_request", "No such attachment") from exc
    return Response(
        content=data,
        media_type=source["media_type"],
        # Only the reader's browser may keep it, and for as long as the deployment says: a
        # stored message never changes (docs/api.md).
        headers={
            "Cache-Control": f"private, max-age={settings.chat_attachment_cache_seconds}, immutable"
        },
    )


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


def workspace_payload(usage: StorageUsage, quota: int) -> dict:
    """The workspace's storage as the status route answers with it (docs/api.md)."""
    return {
        "bytes": usage.total,
        "quota_bytes": quota,
        "files_bytes": usage.files,
        "trash_bytes": usage.trash,
        "history_bytes": usage.history,
    }


@router.get("/sandbox")
async def sandbox_status(user: SessionUser, db: DbSession, settings: AppSettings):
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
    plan = await ledger.plan_of(db, settings, user.id)
    workspace = workspace_payload(
        await measure_storage(db, settings, user.id, root), ledger.storage_limit(settings, plan)
    )
    # Whether the plan gives the chat agent a sandbox at all; the runner's state above
    # says whether this deployment has one to give (docs/billing.md § Plans).
    return {**state, "granted": plan.sandbox, "workspace": workspace}


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
