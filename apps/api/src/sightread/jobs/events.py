"""SSE progress streams (docs/api.md § events, docs/jobs.md § Progress).

The database is the only source of truth: `NOTIFY` merely wakes a stream up, and a stream
that never hears a notification still makes progress by polling. That keeps the contract
identical on a backend without LISTEN/NOTIFY, and makes a missed notification a latency
problem rather than a correctness one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator

import asyncpg
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Job, JobPage, Result

logger = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 1.0
TERMINAL_STATUSES = ("succeeded", "failed")

# Event names. `keepalive` carries no data and exists only to hold an idle HTTP connection
# open; consumers that are not SSE (the MCP tools) ignore it.
PROGRESS = "progress"
DONE = "done"
ERROR = "error"
KEEPALIVE = "keepalive"


def channel_for(job_id: uuid.UUID) -> str:
    """One channel per job, so a stream is only woken by its own job."""
    return f"sr_job_{job_id.hex}"


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def notify_channel(db: AsyncSession, channel: str) -> None:
    """Wake every stream listening on `channel`, when the backend can do it. Sent in the
    caller's transaction, so it is delivered when that commits."""
    if db.get_bind().dialect.name != "postgresql":
        return
    await db.execute(text("SELECT pg_notify(:channel, '')"), {"channel": channel})


async def notify(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Wake every stream watching this job."""
    await notify_channel(db, channel_for(job_id))


async def open_listener(database_url: str, channel: str, wakeup: asyncio.Event, what: str):
    """A dedicated PostgreSQL connection that sets `wakeup` on each NOTIFY of `channel`,
    or None, and the stream polls. `what` names the stream in the log line.

    Deliberately outside the SQLAlchemy pool: a stream lives as long as its client, and
    long-lived listeners must not eat the connections the API needs to answer requests.
    Shared by the job streams and the conversation streams (`chat_events`).
    """
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        return None
    connection = None
    try:
        connection = await asyncpg.connect(
            url.set(drivername="postgresql").render_as_string(hide_password=False)
        )
        await connection.add_listener(channel, lambda *_: wakeup.set())
    except (OSError, asyncpg.PostgresError):
        logger.warning("%s event listener unavailable; falling back to polling", what)
        if connection is not None:
            # Opened, then refused LISTEN (a proxy in between): closed, or every stream
            # opened under that condition would hold one connection for nothing.
            with contextlib.suppress(Exception):
                await connection.close()
        return None
    return connection


async def _snapshot(sessionmaker: async_sessionmaker[AsyncSession], job_id: uuid.UUID):
    """Job row plus the number and method of each page finished so far — never a page's
    markdown or figures, which the stream does not send and a long parse would re-read on
    every wake-up."""
    async with sessionmaker() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job is None:
            return None, [], None
        pages = (
            await db.execute(
                select(JobPage.page_no, JobPage.method)
                .where(JobPage.job_id == job_id)
                .order_by(JobPage.page_no)
            )
        ).all()
        result = None
        if job.status in TERMINAL_STATUSES:
            result = (
                await db.execute(select(Result).where(Result.job_id == job_id))
            ).scalar_one_or_none()
        return job, list(pages), result


async def stream_job_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    database_url: str,
    job_id: uuid.UUID,
) -> AsyncIterator[str]:
    """The SSE rendering of `iter_job_events` (docs/api.md § events)."""
    async for event, data in iter_job_events(sessionmaker, database_url, job_id):
        yield ": keepalive\n\n" if event == KEEPALIVE else sse(event, data)


async def iter_job_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    database_url: str,
    job_id: uuid.UUID,
) -> AsyncIterator[tuple[str, dict]]:
    """Yield `(event, data)` pairs until the job reaches a terminal state.

    A job that is already terminal replays its final event immediately, so a client that
    reconnects late still gets its result. Two consumers: the SSE routes and the MCP tools,
    which turn `progress` into MCP progress notifications (docs/mcp.md).
    """
    # Imported here: the runner imports this module for `notify`, and the result shape
    # belongs to the runner that writes it.
    from .runner import result_payload

    wakeup = asyncio.Event()
    listener = await open_listener(database_url, channel_for(job_id), wakeup, "job")
    emitted: set[int] = set()
    silent_for = 0.0

    try:
        while True:
            # Cleared before the read, so a NOTIFY that lands during it or while the pairs
            # below are consumed wakes the next wait at once.
            wakeup.clear()
            job, pages, result = await _snapshot(sessionmaker, job_id)
            if job is None:
                yield ERROR, {"error": {"type": "invalid_request", "message": "No such job"}}
                return

            for page in pages:
                if page.page_no in emitted:
                    continue
                emitted.add(page.page_no)
                silent_for = 0.0
                yield (
                    PROGRESS,
                    {
                        "job_id": str(job.id),
                        "pages_done": job.pages_done,
                        "page_count": job.page_count,
                        "page": page.page_no,
                        "method": page.method,
                    },
                )

            if job.status in TERMINAL_STATUSES:
                if result is not None:
                    # The data plane's stream: the text the web reader opens.
                    yield DONE, result_payload(result, reading_copy=True)
                else:
                    yield (
                        ERROR,
                        {"error": {"type": "internal", "message": job.error or "Job failed"}},
                    )
                return

            try:
                await asyncio.wait_for(wakeup.wait(), timeout=POLL_INTERVAL_SECONDS)
                silent_for = 0.0
            except TimeoutError:
                silent_for += POLL_INTERVAL_SECONDS
                if silent_for >= KEEPALIVE_SECONDS:
                    silent_for = 0.0
                    yield KEEPALIVE, {}
    finally:
        if listener is not None:
            await listener.close()
