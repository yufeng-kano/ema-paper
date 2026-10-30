"""Versions of a sent message: the conversation as a tree of turns (docs/chat.md § History
is append-only, *An edited message starts a version*).

Every `chat_turns` row names its `parent_id`, the turn before it on its branch, and the
conversation its `head_id`, the turn whose branch it shows and continues. Turns with one
parent are the versions of one message. What a turn replays is its own ancestry; what the
panel shows is the head's. The helpers here read the turns of one conversation once and
walk them in Python: a conversation holds tens of turns, not thousands.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import COMPACTION_TURNS, MESSAGE_TURN, ChatTurn, Message


async def conversation_turns(db: AsyncSession, conversation_id: int) -> list[ChatTurn]:
    """Every turn of the conversation, oldest first."""
    return list(
        (
            await db.execute(
                select(ChatTurn)
                .where(ChatTurn.conversation_id == conversation_id)
                .order_by(ChatTurn.created_at, ChatTurn.id)
            )
        ).scalars()
    )


def branch(turns: list[ChatTurn], head_id: uuid.UUID | None) -> list[ChatTurn]:
    """The turns from the root to `head_id`, oldest first. With no head among `turns` —
    a conversation with no turn yet, or rows written before versions that no migration
    reached — every turn in order: one branch, the thread as it was before versions."""
    by_id = {turn.id: turn for turn in turns}
    if head_id is None or head_id not in by_id:
        return list(turns)
    path: list[ChatTurn] = []
    cursor: uuid.UUID | None = head_id
    while cursor is not None and cursor in by_id and by_id[cursor] not in path:
        path.append(by_id[cursor])
        cursor = by_id[cursor].parent_id
    path.reverse()
    return path


def newest_leaf(turns: list[ChatTurn], start_id: uuid.UUID) -> uuid.UUID:
    """The turn a version switch shows: from `start_id` down, at each step the newest turn
    that continued the current one, until none did."""
    children: dict[uuid.UUID | None, list[ChatTurn]] = {}
    for turn in turns:
        children.setdefault(turn.parent_id, []).append(turn)
    cursor = start_id
    seen: set[uuid.UUID] = set()
    while cursor not in seen and (below := children.get(cursor)):
        seen.add(cursor)
        cursor = below[-1].id  # `turns` is oldest first
    return cursor


def versions_of(turns: list[ChatTurn], turn: ChatTurn) -> list[uuid.UUID] | None:
    """The ids of the `message` turns that are versions of one message — the siblings under
    `turn`'s parent, itself among them, oldest first — or None when it is the only one."""
    if turn.kind != MESSAGE_TURN:
        return None
    siblings = [
        sibling.id
        for sibling in turns
        if sibling.parent_id == turn.parent_id and sibling.kind == MESSAGE_TURN
    ]
    return siblings if len(siblings) > 1 else None


async def branch_boundary(db: AsyncSession, path: list[ChatTurn]) -> int | None:
    """The summary row the branch's replay starts from: the last message of the newest
    succeeded compaction on `path` — the summary is the row a compaction stores last —
    or None when the branch has none (docs/chat.md § Compaction)."""
    for turn in reversed(path):
        if turn.kind not in COMPACTION_TURNS or turn.status != "succeeded":
            continue
        summary_id = (
            await db.execute(select(func.max(Message.id)).where(Message.turn_id == turn.id))
        ).scalar_one_or_none()
        if summary_id is not None:
            return summary_id
    return None
