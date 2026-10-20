"""The chat agent's memories in a turn (docs/chat.md § Memory): the snapshot a conversation
takes at its first turn and renders at every turn, and the `memory` tool.

The block's text, the cleaning and the selection are `agents/memory.py`; this module reads
and writes the rows. The tool never holds the conversation's row lock — the events take it
— only the reader's, under which their memories are counted, in a transaction of its own.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents import CHAT
from ..agents import memory as memory_defs
from ..config import Settings
from ..db.models import AgentSetting, Conversation, Memory, User, utcnow
from .chat_tools import ToolOutcome

PAUSED = "Memory is paused: the user turned it off. Nothing was saved or changed; go on without it."
ACTIONS = {"save": "saved", "update": "updated", "forget": "forgotten"}
SCOPES = ("all", "document")
# How the block names a memory, and what the tool takes back: `m12`, or the bare number.
_ID = re.compile(r"^\[?m?(\d{1,18})\]?$", re.IGNORECASE)


# --- the snapshot ------------------------------------------------------------------------


async def applicable_memories(
    db: AsyncSession, user_id: int, document_id: int | None
) -> list[Memory]:
    """The reader's memories a conversation about `document_id` may load: every
    document's, and this one's. Without a document, every document's alone."""
    query = select(Memory).where(Memory.user_id == user_id)
    if document_id is None:
        query = query.where(Memory.document_id.is_(None))
    else:
        query = query.where(or_(Memory.document_id.is_(None), Memory.document_id == document_id))
    return list((await db.execute(query)).scalars().all())


def first_turn_selection(rows: Sequence[Memory], settings: Settings) -> list[Memory]:
    """What a first turn loads, in the order the block renders it — the order its ids are
    stored in (docs/chat.md § Memory)."""
    budget = memory_defs.items_budget(settings.chat_memory_block_max_chars)
    return memory_defs.render_order(memory_defs.select_memories(rows, budget))


async def snapshot_rows(
    db: AsyncSession, user_id: int, document_id: int, ids: Sequence[object]
) -> list[Memory]:
    """The snapshot's memories as they are now, in its order: one the reader deleted is
    gone, and one whose scope no longer takes in this document is dropped."""
    wanted = [value for value in ids if isinstance(value, int) and not isinstance(value, bool)]
    if not wanted:
        return []
    rows = {
        row.id: row
        for row in (
            await db.execute(select(Memory).where(Memory.user_id == user_id, Memory.id.in_(wanted)))
        ).scalars()
    }
    return [
        rows[memory_id]
        for memory_id in wanted
        if memory_id in rows and rows[memory_id].document_id in (None, document_id)
    ]


@dataclass(frozen=True)
class TurnMemory:
    """The memory block one turn sends, and the ids it rendered (the `turn_start` event's
    `memory_ids`)."""

    block: str
    ids: list[int]


async def load_turn_memory(
    db: AsyncSession,
    settings: Settings,
    conversation: Conversation,
    *,
    auto: bool,
    paused: bool,
) -> TurnMemory:
    """The memory block of one turn. Paused: the line saying so, and no snapshot taken —
    the conversation takes one at its first turn after the reader resumes. Otherwise the
    conversation's snapshot, taken now when it has none (the caller commits it with the
    load), rendered from the rows as they are now: an edit applies, a deletion drops the
    item, and a memory saved since waits for the next conversation."""
    if paused:
        return TurnMemory(memory_defs.render_memory_block([], auto=auto, paused=True), [])
    if conversation.memory_ids is None:
        rows = await applicable_memories(db, conversation.user_id, conversation.document_id)
        conversation.memory_ids = [row.id for row in first_turn_selection(rows, settings)]
    current = await snapshot_rows(
        db, conversation.user_id, conversation.document_id, conversation.memory_ids
    )
    # Fitted again: an item the reader lengthened since must not carry the block past its
    # cap. The same rows fit the same way, so the cached prefix holds.
    budget = memory_defs.items_budget(settings.chat_memory_block_max_chars)
    items = memory_defs.render_order(memory_defs.fit(current, budget))
    return TurnMemory(
        memory_defs.render_memory_block(items, auto=auto, paused=False),
        [item.id for item in items],
    )


# --- the tool ------------------------------------------------------------------------------


def snapshot(row: Memory) -> dict:
    """A memory as a `memory` event carries it."""
    return {
        "id": row.id,
        "kind": row.kind,
        "text": row.text,
        "document_id": row.document_id,
        "pinned": bool(row.pinned),
    }


def _field(tool_input: dict, name: str) -> str:
    value = tool_input.get(name)
    return value.strip() if isinstance(value, str) else ""


def _error(message: str) -> ToolOutcome:
    return ToolOutcome(message, is_error=True)


async def run_memory_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    user_id: int,
    conversation_id: int,
    document_id: int,
    tool_use_id: str,
    tool_input: dict,
) -> ToolOutcome:
    """One `memory` call: save, update or forget one of the reader's memories (docs/chat.md
    § Memory). Every refusal is an error result the model can correct; a success carries
    what its `memory` event reports, and a save of what is already saved carries nothing."""
    action = _field(tool_input, "action")
    if action not in ACTIONS:
        return _error("`action` is save, update or forget.")
    kind = _field(tool_input, "kind")
    if kind and kind not in memory_defs.KINDS:
        return _error("`kind` is term, preference, about or document.")
    scope = _field(tool_input, "scope")
    if scope and scope not in SCOPES:
        return _error("`scope` is all or document.")
    raw_text = tool_input.get("text")
    text = memory_defs.clean_text(raw_text, single_line=True) if isinstance(raw_text, str) else ""
    if len(text) > settings.chat_memory_max_chars:
        return _error(
            f"A memory is one line of at most {settings.chat_memory_max_chars} characters; "
            "keep it shorter."
        )
    memory_id: int | None = None
    if action != "save":
        matched = _ID.fullmatch(_field(tool_input, "id"))
        if matched is None:
            return _error(f"`{action}` needs the memory's id as the Memory section shows it.")
        memory_id = int(matched.group(1))
    if action == "save" and not (kind and text and scope):
        return _error("`save` needs `kind`, `text` and `scope`.")
    if action == "update" and not (kind or text or scope):
        return _error("`update` changes `kind`, `text` or `scope`; none was given.")

    async with sessionmaker() as db:
        # The reader's row, held until the commit: the count below must not race another
        # conversation's save past the cap (docs/chat.md § Memory).
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
        paused = (
            await db.execute(
                select(AgentSetting.memory_paused).where(
                    AgentSetting.user_id == user_id, AgentSetting.agent == CHAT.name
                )
            )
        ).scalar_one_or_none()
        if paused:
            return _error(PAUSED)
        row: Memory | None = None
        if memory_id is not None:
            row = (
                await db.execute(
                    select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return _error(f"No memory m{memory_id}: give an id the Memory section shows.")
        previous: dict | None = None
        forgotten: dict | None = None
        if action == "save":
            scoped = document_id if scope == "document" else None
            # A memory saved again as it is — a save the turn's end cut off before its
            # result was stored reads as interrupted on the replay — is answered with the
            # row there is, and adds none (docs/chat.md § Memory).
            same = (
                await db.execute(
                    select(Memory.id)
                    .where(
                        Memory.user_id == user_id,
                        Memory.kind == kind,
                        Memory.text == text,
                        Memory.document_id.is_not_distinct_from(scoped),
                    )
                    .order_by(Memory.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if same is not None:
                return ToolOutcome(f"Already saved as m{same}.")
            count = (
                await db.execute(
                    select(func.count()).select_from(Memory).where(Memory.user_id == user_id)
                )
            ).scalar_one()
            if count >= settings.chat_memories_max:
                return _error(
                    f"The user's memory is full ({settings.chat_memories_max} memories): "
                    "update one that says something close, or tell the user."
                )
            row = Memory(
                user_id=user_id,
                kind=kind,
                text=text,
                document_id=scoped,
                conversation_id=conversation_id,
                source_ref=tool_use_id[:255],
            )
            db.add(row)
        elif action == "update":
            assert row is not None
            previous = snapshot(row)
            if kind:
                row.kind = kind
            if text:
                row.text = text
            if scope:
                row.document_id = document_id if scope == "document" else None
            row.updated_at = utcnow()
        else:
            assert row is not None
            # A forgotten memory is reported as it was, so Undo can save it again.
            forgotten = snapshot(row)
            await db.delete(row)
        try:
            await db.flush()
            reported = forgotten if forgotten is not None else snapshot(row)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return _error("The memory could not be written; try again.")
    done = {"save": "Saved as", "update": "Updated", "forget": "Forgot"}[action]
    return ToolOutcome(
        f"{done} m{reported['id']}.",
        memory={"action": ACTIONS[action], "memory": reported, "previous": previous},
    )
