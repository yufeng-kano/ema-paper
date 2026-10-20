"""The conversation's event stream (docs/chat.md § Conversations, turns, events).

The worker appends `chat_events` rows while a turn runs; the SSE route replays what a
client missed and tails what comes next. Same shape as `events.py`: the database is the
truth, `NOTIFY` only wakes a stream up, and a stream that never hears one still progresses
by polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator

import asyncpg
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import ChatEvent, ChatTurn, Conversation

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 1.0

TURN_START = "turn_start"
TEXT_DELTA = "text_delta"
THINKING = "thinking"
TOOL_USE = "tool_use"
TOOL_RESULT = "tool_result"
EDIT = "edit"
# A `memory` call that saved, changed or forgot one of the reader's memories (docs/chat.md
# § Memory): the thread draws it as a quiet line with Undo.
MEMORY = "memory"
RETRY = "retry"
TURN_DONE = "turn_done"
TURN_ERROR = "turn_error"


def channel_for(conversation_id: int) -> str:
    return f"sr_conv_{conversation_id}"


def sse(event: str, seq: int, data: dict) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def render(row: ChatEvent) -> str:
    """One stored event as an SSE frame; the turn rides along so a client can pair the
    stream with the turns it lists."""
    return sse(row.event, row.seq, {**row.data, "turn_id": str(row.turn_id)})


async def append(db: AsyncSession, conversation_id: int, turn_id, event: str, data: dict) -> int:
    """Add one event to the session and queue the wake-up, without committing: for a
    caller that must publish the event in the same transaction as a status change."""
    # The conversation's row, held until the caller commits: two tools finishing at once
    # must not both take `max(seq) + 1` (PostgreSQL; SQLite writes one at a time).
    await db.execute(
        select(Conversation.id).where(Conversation.id == conversation_id).with_for_update()
    )
    last = (
        await db.execute(
            select(func.max(ChatEvent.seq)).where(ChatEvent.conversation_id == conversation_id)
        )
    ).scalar_one()
    seq = int(last or 0) + 1
    db.add(
        ChatEvent(conversation_id=conversation_id, turn_id=turn_id, seq=seq, event=event, data=data)
    )
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_notify(:channel, '')"), {"channel": channel_for(conversation_id)}
        )
    return seq


async def emit(db: AsyncSession, conversation_id: int, turn_id, event: str, data: dict) -> int:
    """Append one event and wake the streams. The turn is the only writer of its
    conversation, so `max(seq) + 1` cannot race itself. Commits."""
    seq = await append(db, conversation_id, turn_id, event, data)
    await db.commit()
    return seq


async def _open_listener(database_url: str, conversation_id: int, wakeup: asyncio.Event):
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        return None
    connection = None
    try:
        connection = await asyncpg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        await connection.add_listener(channel_for(conversation_id), lambda *_: wakeup.set())
    except (OSError, asyncpg.PostgresError):
        logger.warning("conversation event listener unavailable; falling back to polling")
        if connection is not None:
            # Opened, then refused LISTEN (a proxy in between): closed, or every stream
            # opened under that condition would hold one connection for nothing.
            with contextlib.suppress(Exception):
                await connection.close()
        return None
    return connection


# Rows read per query when replaying a conversation's events: a long thread is streamed
# batch by batch, never held whole in the API process (docs/api.md).
REPLAY_BATCH_SIZE = 200


async def _events_after(
    sessionmaker: async_sessionmaker[AsyncSession], conversation_id: int, after: int
) -> list[ChatEvent]:
    """One batch of events after `after`; a batch shorter than the size is the last."""
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                select(ChatEvent)
                .where(ChatEvent.conversation_id == conversation_id, ChatEvent.seq > after)
                .order_by(ChatEvent.seq)
                .limit(REPLAY_BATCH_SIZE)
            )
        ).scalars()
        return list(rows)


async def _replay(
    sessionmaker: async_sessionmaker[AsyncSession], conversation_id: int, after: int
) -> AsyncIterator[ChatEvent]:
    """Every event after `after`, batch by batch, each yielded before the next is read."""
    cursor = after
    while True:
        rows = await _events_after(sessionmaker, conversation_id, cursor)
        for row in rows:
            cursor = row.seq
            yield row
        if len(rows) < REPLAY_BATCH_SIZE:
            return


async def _turn_running(
    sessionmaker: async_sessionmaker[AsyncSession], conversation_id: int
) -> bool:
    async with sessionmaker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(ChatTurn)
                .where(
                    ChatTurn.conversation_id == conversation_id,
                    # A cancelled turn is still the worker's until it winds down and
                    # writes the terminal event; the stream waits for that.
                    ChatTurn.finished_at.is_(None),
                )
            )
        ).scalar_one()
        return int(count) > 0


async def stream_conversation_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    database_url: str,
    conversation_id: int,
    after: int,
) -> AsyncIterator[str]:
    """Replay everything after `after`, then tail. Ends once no turn is queued or running
    and the replay is complete — the client reopens the stream when it sends again."""
    wakeup = asyncio.Event()
    listener = await _open_listener(database_url, conversation_id, wakeup)
    cursor = after
    silent_for = 0.0
    try:
        while True:
            async for row in _replay(sessionmaker, conversation_id, cursor):
                cursor = row.seq
                silent_for = 0.0
                yield render(row)
            if not await _turn_running(sessionmaker, conversation_id):
                # One more read: a turn can finish between the replay and this check.
                async for row in _replay(sessionmaker, conversation_id, cursor):
                    cursor = row.seq
                    yield render(row)
                return
            wakeup.clear()
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=POLL_INTERVAL_SECONDS)
                silent_for = 0.0
            except TimeoutError:
                silent_for += POLL_INTERVAL_SECONDS
                if silent_for >= KEEPALIVE_SECONDS:
                    silent_for = 0.0
                    yield ": keepalive\n\n"
    finally:
        if listener is not None:
            await listener.close()
