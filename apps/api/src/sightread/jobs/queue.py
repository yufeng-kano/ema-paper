"""The job queue (docs/jobs.md). The only module that knows the queue is PostgreSQL.

A job row *is* its queue entry, so creating a job and enqueueing it are one transaction —
there is no broker and no dual write. Claiming is `FOR UPDATE SKIP LOCKED`, FIFO, with the
per-user cap applied at claim time so one user cannot monopolise the workers.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import get_agent
from ..db.models import ChatTurn, Job, JobPage, utcnow
from . import chat_events

# PostgreSQL claim (docs/jobs.md). The correlated count applies the per-user running cap
# to the same scan that picks the oldest queued job.
CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', started_at = now()
    WHERE id = (
      SELECT j.id FROM jobs j
      WHERE j.status = 'queued'
        AND (
          SELECT count(*) FROM jobs running
          WHERE running.user_id = j.user_id AND running.status = 'running'
        ) < :max_jobs_per_user
      ORDER BY j.created_at
      FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id
    """
)

# SQLite has neither SKIP LOCKED nor row locks. The fallback keeps the whole test suite
# runnable with no services; the guarded UPDATE still makes a double claim impossible.
CLAIM_SQL_SQLITE_SELECT = text(
    """
    SELECT j.id FROM jobs j
    WHERE j.status = 'queued'
      AND (
        SELECT count(*) FROM jobs running
        WHERE running.user_id = j.user_id AND running.status = 'running'
      ) < :max_jobs_per_user
    ORDER BY j.created_at LIMIT 1
    """
)

_PAGES_SPEC_RE = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


def parse_pages_spec(spec: str, page_count: int) -> list[int]:
    """Expand `"1-5,8"` into page numbers. Empty means every page.

    Raises `ValueError` for malformed input or a selection outside the document.
    """
    if not spec:
        return list(range(1, page_count + 1))
    if not _PAGES_SPEC_RE.match(spec):
        raise ValueError("pages must look like '1-5,8'")

    pages: set[int] = set()
    for part in spec.split(","):
        first, _, last = part.partition("-")
        start, end = int(first), int(last or first)
        if start < 1 or end < start or end > page_count:
            raise ValueError(f"pages selects a page outside 1-{page_count}")
        pages.update(range(start, end + 1))
    return sorted(pages)


def normalize_pages_spec(spec: str | None) -> str:
    """Canonical form stored on the job and used in the dedup key: no whitespace."""
    return re.sub(r"\s+", "", spec or "")


async def count_running_jobs(db: AsyncSession, user_id: int) -> int:
    """Running jobs for one user — the cap `POST /v1/parse` enforces (docs/jobs.md)."""
    return (
        await db.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.user_id == user_id, Job.status == "running")
        )
    ).scalar_one()


def dedup_key(
    *,
    user_id: int,
    agent: str,
    sha256: str,
    model: str,
    connection_id: int | None,
    connection_base_url: str | None,
    pages_spec: str,
    prompt_sha256: str,
) -> list:
    """Everything that has to match for two uploads to be the same parse.

    Same user, agent, bytes, model, upstream (id *and* endpoint snapshot — editing a
    connection's URL repoints what its id means, and the built-in upstream's snapshot is
    KANO_PROXY_BASE_URL, which no historical OpenRouter row has), pages, prompt, and the
    agent's pipeline version (docs/jobs.md § Dedup). Status is the caller's: a finished
    parse answers the cache, an unfinished one answers a retry.
    """
    return [
        Job.user_id == user_id,
        Job.agent == agent,
        Job.sha256 == sha256,
        Job.model == model,
        Job.connection_id.is_(None)
        if connection_id is None
        else Job.connection_id == connection_id,
        Job.connection_base_url.is_(None)
        if connection_base_url is None
        else Job.connection_base_url == connection_base_url,
        Job.pages_spec == pages_spec,
        Job.prompt_sha256 == prompt_sha256,
        Job.pipeline_version == get_agent(agent).pipeline_version,
    ]


async def find_cached_job(db: AsyncSession, **key) -> Job | None:
    """The dedup lookup: a succeeded job for this exact parse (docs/jobs.md § Dedup)."""
    return (
        await db.execute(
            select(Job)
            .where(*dedup_key(**key), Job.status == "succeeded")
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def hold_dedup_key(db: AsyncSession, **key) -> None:
    """Serialize everything that would enqueue this exact parse, until this commit.

    Reading for an in-flight duplicate is not enough on its own: two uploads of the same
    bytes can both find nothing and both enqueue, which is the user's key charged twice for
    one document. A transaction-level advisory lock closes that — the second request waits
    and then sees the first's committed job.

    Keyed on a hash of the dedup key, so it only ever blocks a request that was about to
    parse the same document for the same account; a hash collision costs two unrelated
    uploads a few milliseconds and nothing else. PostgreSQL only, like the claim query: the
    SQLite fallback runs one process at a time and has nothing to serialize.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    fingerprint = "\x1f".join(f"{name}={value!r}" for name, value in sorted(key.items()))
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:fingerprint))"),
        {"fingerprint": fingerprint},
    )


async def find_active_job(db: AsyncSession, **key) -> Job | None:
    """The same parse, already queued or running.

    Not part of the dedup cache — an unfinished job has no result to serve. It exists for
    one caller: a browser that lost the response to its upload and retried, which must not
    buy the user a second parse of the same bytes (docs/jobs.md § Dedup).
    """
    return (
        await db.execute(
            select(Job)
            .where(*dedup_key(**key), Job.status.in_(("queued", "running")))
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def enqueue_job(
    db: AsyncSession,
    *,
    user_id: int,
    agent: str,
    kind: str,
    filename: str,
    media_type: str,
    size_bytes: int,
    sha256: str,
    pages_spec: str,
    model: str,
    connection_id: int | None,
    connection_base_url: str | None,
    bbox_format: str,
    prompt: str,
    prompt_sha256: str,
    page_count: int,
    source_path: str,
    retain_source: bool = False,
) -> Job:
    """Create the queued job row. The caller commits — creation and enqueue are one act."""
    job = Job(
        id=uuid.uuid4(),
        user_id=user_id,
        agent=agent,
        kind=kind,
        filename=filename,
        media_type=media_type,
        size_bytes=size_bytes,
        sha256=sha256,
        pages_spec=pages_spec,
        model=model,
        connection_id=connection_id,
        connection_base_url=connection_base_url,
        pipeline_version=get_agent(agent).pipeline_version,
        bbox_format=bbox_format,
        prompt=prompt,
        prompt_sha256=prompt_sha256,
        status="queued",
        page_count=page_count,
        pages_done=0,
        source_path=source_path,
        retain_source=retain_source,
        created_at=utcnow(),
    )
    db.add(job)
    await db.flush()
    return job


async def claim_next_job(db: AsyncSession, max_jobs_per_user: int) -> uuid.UUID | None:
    """Claim the oldest queued job whose user is under the running cap, or None."""
    if db.get_bind().dialect.name == "postgresql":
        claimed = (
            await db.execute(CLAIM_SQL, {"max_jobs_per_user": max_jobs_per_user})
        ).scalar_one_or_none()
        await db.commit()
        return claimed

    candidate = (
        await db.execute(CLAIM_SQL_SQLITE_SELECT, {"max_jobs_per_user": max_jobs_per_user})
    ).scalar_one_or_none()
    if candidate is None:
        return None
    job_id = uuid.UUID(str(candidate))
    updated = await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="running", started_at=utcnow())
    )
    await db.commit()
    return job_id if updated.rowcount == 1 else None


async def requeue_job(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Put an interrupted job back on the queue and forget its partial progress.

    Shutdown mid-run reparses from scratch: pages are not individually resumable, and a
    reparse is cheaper to reason about than a half-written result.
    """
    await db.execute(delete(JobPage).where(JobPage.job_id == job_id))
    await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "running")
        .values(status="queued", started_at=None, pages_done=0)
    )
    await db.commit()


# --- chat turns (docs/jobs.md § Chat turns) ----------------------------------------------

CLAIM_TURN_SQL = text(
    """
    UPDATE chat_turns SET status = 'running', started_at = now()
    WHERE id = (
      SELECT t.id FROM chat_turns t
      WHERE t.status = 'queued'
        AND (
          SELECT count(*) FROM chat_turns running
          WHERE running.user_id = t.user_id AND running.status = 'running'
        ) < :max_turns_per_user
      ORDER BY t.created_at
      FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id
    """
)

CLAIM_TURN_SQL_SQLITE_SELECT = text(
    """
    SELECT t.id FROM chat_turns t
    WHERE t.status = 'queued'
      AND (
        SELECT count(*) FROM chat_turns running
        WHERE running.user_id = t.user_id AND running.status = 'running'
      ) < :max_turns_per_user
    ORDER BY t.created_at LIMIT 1
    """
)


async def count_active_turns(db: AsyncSession, user_id: int) -> int:
    """Queued or running turns for one user — the cap `POST …/messages` enforces."""
    return (
        await db.execute(
            select(func.count())
            .select_from(ChatTurn)
            .where(ChatTurn.user_id == user_id, ChatTurn.status.in_(("queued", "running")))
        )
    ).scalar_one()


async def enqueue_turn(
    db: AsyncSession,
    *,
    user_id: int,
    conversation_id: int,
    message_id: int,
    locale: str = "en",
) -> ChatTurn:
    """Create the queued turn row. The caller commits, with the user message it answers."""
    turn = ChatTurn(
        id=uuid.uuid4(),
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        status="queued",
        locale=locale,
        created_at=utcnow(),
    )
    db.add(turn)
    await db.flush()
    return turn


async def claim_next_turn(db: AsyncSession, max_turns_per_user: int) -> uuid.UUID | None:
    """Claim the oldest queued turn whose user is under the running cap, or None."""
    if db.get_bind().dialect.name == "postgresql":
        claimed = (
            await db.execute(CLAIM_TURN_SQL, {"max_turns_per_user": max_turns_per_user})
        ).scalar_one_or_none()
        await db.commit()
        return claimed

    candidate = (
        await db.execute(CLAIM_TURN_SQL_SQLITE_SELECT, {"max_turns_per_user": max_turns_per_user})
    ).scalar_one_or_none()
    if candidate is None:
        return None
    turn_id = uuid.UUID(str(candidate))
    updated = await db.execute(
        update(ChatTurn)
        .where(ChatTurn.id == turn_id, ChatTurn.status == "queued")
        .values(status="running", started_at=utcnow())
    )
    await db.commit()
    return turn_id if updated.rowcount == 1 else None


async def fail_turn(db: AsyncSession, turn_id: uuid.UUID, error: str) -> None:
    """A turn the worker could not finish. Never requeued: its tool calls may have run.
    A turn the user had cancelled keeps `cancelled`; either way it is finished now, with
    the terminal event a stream waits for (docs/jobs.md § Chat turns)."""
    turn = await db.get(ChatTurn, turn_id)
    if turn is None or turn.finished_at is not None:
        return
    if turn.status == "cancelled":
        turn.error = "cancelled"
    else:
        turn.status = "failed"
        turn.error = error
    turn.finished_at = utcnow()
    await chat_events.append(
        db,
        turn.conversation_id,
        turn.id,
        chat_events.TURN_ERROR,
        {"status": turn.status, "error": turn.error or ""},
    )
    await db.commit()
