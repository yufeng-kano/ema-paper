"""Compaction (docs/chat.md § Compaction): the message a compaction turn carries, what makes
a conversation compactable, and the worker's idle pass that compacts a conversation before
its prompt cache expires.

The reader's `/compact` goes through the send route, which builds the same message; the
idle pass takes the send's checks and locks itself, since no request stands behind it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents import CHAT
from ..agents import chat as chat_agent
from ..billing import ledger
from ..config import Settings
from ..db.models import (
    IDLE_COMPACT_TURN,
    AgentSetting,
    ChatTurn,
    Conversation,
    Document,
    Message,
    User,
    utcnow,
)
from ..errors import ApiError
from ..runtime_settings import RuntimeSettings
from ..workspace.documents import current_revision, ensure_within_quota, is_live, user_folders
from ..workspace.layout import user_root
from . import chat_versions
from .queue import count_active_turns, enqueue_turn, history_bytes

logger = logging.getLogger(__name__)

# The markers of a compaction turn's blocks (docs/chat.md § What a user message carries):
# the instruction the model follows, and the summary row the replay starts from.
COMPACT_CONTEXT = "compact"
SUMMARY_CONTEXT = "summary"


def is_summary(content: object) -> bool:
    """Whether a stored message is the summary row a compaction left."""
    return (
        isinstance(content, list)
        and len(content) > 0
        and isinstance(content[0], dict)
        and content[0].get("context") == SUMMARY_CONTEXT
    )


def compact_message_content(*, visible: str | None, focus: str | None, note: str) -> list[dict]:
    """The compaction turn's user message: the reader's line as typed when there is one,
    the instruction, the turn block (docs/chat.md § Compaction)."""
    content: list[dict] = []
    if visible and visible.strip():
        content.append({"type": "text", "text": visible})
    content.append(
        {"type": "text", "text": chat_agent.compact_instruction(focus), "context": COMPACT_CONTEXT}
    )
    content.append({"type": "text", "text": note, "context": chat_agent.TURN_CONTEXT})
    return content


async def compactable_ids(db: AsyncSession, conversation_ids: list[int]) -> set[int]:
    """The conversations among `conversation_ids` with an assistant message since their last
    summary on the branch they show — something to compact (docs/chat.md § Compaction;
    § History is append-only, *An edited message starts a version*)."""
    if not conversation_ids:
        return set()
    rows = (
        await db.execute(
            select(Message.conversation_id, Message.turn_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.conversation_id.in_(conversation_ids),
                Message.role == "assistant",
                Message.id > func.coalesce(Conversation.compacted_message_id, 0),
            )
            .distinct()
        )
    ).all()
    answered: dict[int, set] = {}
    for conversation_id, turn_id in rows:
        answered.setdefault(conversation_id, set()).add(turn_id)
    if not answered:
        return set()
    heads = dict(
        (
            await db.execute(
                select(Conversation.id, Conversation.head_id).where(
                    Conversation.id.in_(list(answered))
                )
            )
        ).all()
    )
    turns: dict[int, list[ChatTurn]] = {}
    for turn in (
        await db.execute(
            select(ChatTurn)
            .where(ChatTurn.conversation_id.in_(list(answered)))
            .order_by(ChatTurn.created_at, ChatTurn.id)
        )
    ).scalars():
        turns.setdefault(turn.conversation_id, []).append(turn)
    compactable: set[int] = set()
    for conversation_id, turn_ids in answered.items():
        on_branch = {
            turn.id
            for turn in chat_versions.branch(
                turns.get(conversation_id, []), heads.get(conversation_id)
            )
        }
        # An answer of no turn precedes every turn and is on every branch.
        if None in turn_ids or turn_ids & on_branch:
            compactable.add(conversation_id)
    return compactable


async def compactable(db: AsyncSession, conversation_id: int) -> bool:
    return conversation_id in await compactable_ids(db, [conversation_id])


async def unfinished_turn(db: AsyncSession, conversation_id: int) -> bool:
    """A turn the worker may still be driving: anything without `finished_at`."""
    return (
        await db.execute(
            select(ChatTurn.id).where(
                ChatTurn.conversation_id == conversation_id, ChatTurn.finished_at.is_(None)
            )
        )
    ).first() is not None


async def _last_locale(db: AsyncSession, conversation_id: int) -> str:
    locale = (
        await db.execute(
            select(ChatTurn.locale)
            .where(ChatTurn.conversation_id == conversation_id)
            .order_by(ChatTurn.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return locale or "en"


async def try_idle_compaction(
    db: AsyncSession, settings: Settings, conversation_id: int, *, now=None
) -> bool:
    """Enqueue an idle compaction of one conversation, under the send's locks and checks
    (docs/chat.md § Compaction); False when one of them refuses it. The cache clock is
    cleared either way, so the conversation is not tried again before its next turn."""
    now = now or utcnow()
    lead = timedelta(seconds=settings.chat_idle_compact_lead_seconds)
    row = await db.get(Conversation, conversation_id)
    if row is None:
        return False
    # The user's row, then the conversation's: the send's order (docs/chat.md).
    await db.execute(select(User.id).where(User.id == row.user_id).with_for_update())
    # Read again under the lock, the row's columns included (`populate_existing`: the
    # session would otherwise hand back the identity it loaded above, as it was): a turn
    # or a move to another model may have cleared the clock meanwhile.
    row = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if (
        row.cache_warm_until is None
        or row.cache_warm_until > now + lead
        or (row.context_tokens or 0) < settings.chat_idle_compact_min_tokens
    ):
        return False
    # Cleared with `updated_at` kept: the list orders by it, and nothing the reader did
    # moved this conversation (docs/chat.md § Compaction, *Idle compaction*).
    await db.execute(
        update(Conversation)
        .where(Conversation.id == row.id)
        .values(cache_warm_until=None, updated_at=Conversation.updated_at)
    )
    enqueued = await _enqueue_idle_compaction(db, settings, row)
    await db.commit()
    return enqueued


async def _enqueue_idle_compaction(db: AsyncSession, settings: Settings, row: Conversation) -> bool:
    if not settings.kano_proxy_configured or not row.model:
        return False
    if row.model not in settings.model_prices:
        return False
    setting = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == row.user_id, AgentSetting.agent == CHAT.name
            )
        )
    ).scalar_one_or_none()
    if setting is not None and not setting.idle_compact:
        return False
    document = await db.get(Document, row.document_id)
    if document is None or not is_live(document, await user_folders(db, row.user_id)):
        return False
    try:
        await ensure_within_quota(
            db, settings, row.user_id, user_root(settings.workspaces_dir, row.user_id)
        )
    except ApiError:
        return False
    if await count_active_turns(db, row.user_id) >= settings.max_chat_turns_per_user:
        return False
    if await unfinished_turn(db, row.id):
        return False
    # The branch the conversation shows is what the compaction continues (docs/chat.md
    # § History is append-only, *An edited message starts a version*).
    shown = chat_versions.branch(await chat_versions.conversation_turns(db, row.id), row.head_id)
    if (
        await history_bytes(
            db, row.id, since=row.compacted_message_id, path=[turn.id for turn in shown]
        )
        > settings.chat_history_max_bytes
    ):
        return False
    if not await compactable(db, row.id):
        return False
    locale = await _last_locale(db, row.id)
    revision = await current_revision(db, settings, document)
    note = chat_agent.turn_note(
        revision,
        locale,
        setting.answer_language if setting else None,
        setting.translate_language if setting else None,
    )
    # The message, the turn and its credit entry, or none of them: the balance is read as
    # the entry is reserved, and a refusal leaves only the cleared clock behind.
    try:
        async with db.begin_nested():
            message = Message(
                conversation_id=row.id,
                role="user",
                content=compact_message_content(visible=None, focus=None, note=note),
            )
            db.add(message)
            await db.flush()
            turn = await enqueue_turn(
                db,
                user_id=row.user_id,
                conversation_id=row.id,
                message_id=message.id,
                locale=locale,
                kind=IDLE_COMPACT_TURN,
                parent_id=shown[-1].id if shown else None,
            )
            await ledger.reserve(
                db,
                settings,
                user_id=row.user_id,
                kind=ledger.CHAT,
                amount=0,
                turn_id=turn.id,
                model=row.model,
                minimum=settings.chat_min_credits,
            )
            message.turn_id = turn.id
            # The compaction continues the branch, and the head follows it — written with
            # `updated_at` kept, as every write of the idle pass is.
            await db.execute(
                update(Conversation)
                .where(Conversation.id == row.id)
                .values(head_id=turn.id, updated_at=Conversation.updated_at)
            )
    except ledger.InsufficientCredits:
        return False
    return True


async def idle_compact_once(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """One pass: every conversation whose cache expires within the lead and whose context
    has reached the minimum, each tried on its own, so one that fails does not stop the
    rest. Returns how many compactions were enqueued."""
    now = utcnow()
    async with sessionmaker() as db:
        candidates = list(
            (
                await db.execute(
                    select(Conversation.id)
                    .where(
                        Conversation.cache_warm_until.is_not(None),
                        Conversation.cache_warm_until
                        <= now + timedelta(seconds=settings.chat_idle_compact_lead_seconds),
                        Conversation.context_tokens >= settings.chat_idle_compact_min_tokens,
                    )
                    .order_by(Conversation.cache_warm_until)
                )
            ).scalars()
        )
    enqueued = 0
    for conversation_id in candidates:
        try:
            async with sessionmaker() as db:
                if await try_idle_compaction(db, settings, conversation_id, now=now):
                    enqueued += 1
        except Exception:  # noqa: BLE001 - the next conversation still goes
            logger.exception("idle compaction of conversation %d failed", conversation_id)
    return enqueued


async def idle_compact_forever(
    sessionmaker: async_sessionmaker[AsyncSession],
    runtime: RuntimeSettings,
    stop: asyncio.Event,
) -> None:
    """Look every `CHAT_IDLE_COMPACT_SWEEP_SECONDS` until the worker shuts down, each pass on
    the settings in effect as it starts (docs/admin.md § Settings); a pass that raises past
    its own guards is logged, and the next runs on schedule."""
    while not stop.is_set():
        settings = await runtime.current()
        try:
            enqueued = await idle_compact_once(sessionmaker, settings)
            if enqueued:
                logger.info("enqueued %d idle compaction(s)", enqueued)
        except Exception:  # noqa: BLE001 - the next pass tries again
            logger.exception("the idle compaction pass failed; the next one runs on schedule")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.chat_idle_compact_sweep_seconds)
        except TimeoutError:
            continue
