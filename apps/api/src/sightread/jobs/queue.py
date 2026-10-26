"""The job queue (docs/jobs.md). The only module that knows the queue is PostgreSQL.

A job row *is* its queue entry, so creating a job and enqueueing it are one transaction —
there is no broker and no dual write. Claiming is `FOR UPDATE SKIP LOCKED`, FIFO, with the
per-user cap applied at claim time so one user cannot monopolise the workers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import (
    ColumnElement,
    LargeBinary,
    Text,
    bindparam,
    cast,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm import aliased

from ..agents import get_agent
from ..billing import ledger
from ..billing.plans import PAID_STATUSES
from ..config import Settings
from ..db.models import (
    MESSAGE_TURN,
    WORKER_GENERATION_SEQ,
    ChatTurn,
    Conversation,
    Document,
    Job,
    JobPage,
    Message,
    UploadTicket,
    utcnow,
)
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..workspace import git
from ..workspace.layout import REPOS_DIR, git_dir, user_root
from ..workspace.locks import (
    WRITING_SUFFIX,
    clear_writing,
    read_writing,
    shell_lock,
    workspace_lock,
    writing_marker,
)
from ..workspace.reconcile import reconcile_locked
from . import chat_events

logger = logging.getLogger(__name__)

# The revision an orphaned turn's unrecorded changes become (docs/jobs.md § Chat turns).
ORPHAN_REVISION_MESSAGE = "bash: interrupted by a worker restart"
# The revision a command the runner could not confirm over leaves, once a restart has.
UNSETTLED_REVISION_MESSAGE = "bash: recovered after an unconfirmed command"

# PostgreSQL claim (docs/jobs.md). The correlated count applies the per-user running cap
# to the same scan that picks the oldest queued job: the user's plan's parses at once —
# their paid `subscriptions` row as it stands now, joined to `:plan_jobs` (`{plan id:
# maxJobs}`), Free's `:free_max_jobs` otherwise — under the deployment's
# `:max_jobs_per_user` (docs/billing.md § Plans). The generation is the claimer's fence
# on every later write to the row.
CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', started_at = now(), worker_generation = :generation
    WHERE id = (
      SELECT j.id FROM jobs j
      WHERE j.status = 'queued'
        AND j.user_id NOT IN :excluded_users
        AND (
          SELECT count(*) FROM jobs running
          WHERE running.user_id = j.user_id AND running.status = 'running'
        ) < LEAST(
          :max_jobs_per_user,
          COALESCE(
            (
              SELECT (CAST(:plan_jobs AS jsonb) ->> s.plan_id)::int FROM subscriptions s
              WHERE s.user_id = j.user_id AND s.status IN :paid_statuses
                AND s.period_start <= :now AND :now < s.period_end
            ),
            :free_max_jobs
          )
        )
      ORDER BY j.created_at
      FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id
    """
).bindparams(
    bindparam("excluded_users", expanding=True), bindparam("paid_statuses", expanding=True)
)

# SQLite has neither SKIP LOCKED nor row locks. The fallback keeps the whole test suite
# runnable with no services; the guarded UPDATE still makes a double claim impossible.
CLAIM_SQL_SQLITE_SELECT = text(
    """
    SELECT j.id FROM jobs j
    WHERE j.status = 'queued'
      AND j.user_id NOT IN :excluded_users
      AND (
        SELECT count(*) FROM jobs running
        WHERE running.user_id = j.user_id AND running.status = 'running'
      ) < MIN(
        :max_jobs_per_user,
        COALESCE(
          (
            SELECT json_extract(:plan_jobs, '$."' || s.plan_id || '"') FROM subscriptions s
            WHERE s.user_id = j.user_id AND s.status IN :paid_statuses
              AND s.period_start <= :now AND :now < s.period_end
          ),
          :free_max_jobs
        )
      )
    ORDER BY j.created_at LIMIT 1
    """
).bindparams(
    bindparam("excluded_users", expanding=True), bindparam("paid_statuses", expanding=True)
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


def parse_kept(retention_days: int, margin: timedelta = timedelta(0)) -> ColumnElement[bool]:
    """The parse retention's rule, written once (docs/jobs.md § Retention): a job is kept
    while it is queued or running, while it finished less than `retention_days` days ago
    (a row from before `finished_at` was always written counts from its creation), while
    any document of its user — live or in the Trash — names a parse (its `job_id` or its
    `reconvert_job_id`) of the same original, or while an upload ticket that has not
    expired names it. The document clause keeps a document's own parse, its re-convert,
    and the earlier parses of its original the figure route falls back on; the ticket
    clause keeps the job a spent ticket still reads, a cache hit's included.

    `margin`: kept for at least that much longer — the age and the ticket clauses then
    ask for that much more time left. The purge deletes the finished jobs kept with no
    margin; the dedup cache asks with a day (`find_cached_job`)."""
    named = aliased(Job)
    now = utcnow()
    return or_(
        Job.status.in_(("queued", "running")),
        func.coalesce(Job.finished_at, Job.created_at)
        > now - timedelta(days=retention_days) + margin,
        select(Document.id)
        .join(named, or_(named.id == Document.job_id, named.id == Document.reconvert_job_id))
        .where(Document.user_id == Job.user_id, named.sha256 == Job.sha256)
        .exists(),
        select(UploadTicket.id)
        .where(UploadTicket.job_id == Job.id, UploadTicket.expires_at > now + margin)
        .exists(),
    )


async def find_cached_job(db: AsyncSession, *, retention_days: int, **key) -> Job | None:
    """The dedup lookup: a succeeded job for this exact parse (docs/jobs.md § Dedup) that
    the retention keeps for at least another day — named by a document's clause,
    finished less than `retention_days - 1` days ago (never by its age alone at 1), or
    named by an upload ticket with a day left, which one living `UPLOAD_TICKET_TTL_SECONDS`
    (an hour) never has — so a result handed out is never purged between the lookup and
    its use (§ Dedup margin). With `retention_days` 0 nothing is purged, and any
    succeeded job answers."""
    query = select(Job).where(*dedup_key(**key), Job.status == "succeeded")
    if retention_days > 0:
        query = query.where(parse_kept(retention_days, margin=timedelta(days=1)))
    return (await db.execute(query.order_by(Job.created_at.desc()).limit(1))).scalar_one_or_none()


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


async def find_recent_terminal_job(
    db: AsyncSession, *, finished_since: datetime, **key
) -> Job | None:
    """The same parse, finished since `finished_since` — succeeded with page errors, which
    the cache never answers, or failed. For the web library's retry of an upload whose
    answer was lost, and nothing else: within the window the same bytes are one upload,
    whatever became of it (docs/jobs.md § Dedup)."""
    return (
        await db.execute(
            select(Job)
            .where(
                *dedup_key(**key),
                Job.status.in_(("succeeded", "failed")),
                Job.finished_at.is_not(None),
                Job.finished_at >= finished_since,
            )
            .order_by(Job.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class CheckTarget:
    """The format check a job is queued or retried with, snapshotted on its row: model,
    effort, and the system prompt with the reader's instructions after it (docs/parsing.md
    § Format check)."""

    model: str
    effort: str
    prompt: str


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
    fallback_model: str | None = None,
    check: CheckTarget | None = None,
    job_id: uuid.UUID | None = None,
) -> Job:
    """Create the queued job row. The caller commits — creation and enqueue are one act.
    `job_id` is the id the upload was named for, when it was."""
    job = Job(
        id=job_id if job_id is not None else uuid.uuid4(),
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
        fallback_model=fallback_model,
        check_model=check.model if check is not None else None,
        check_effort=check.effort if check is not None else None,
        check_prompt=check.prompt if check is not None else None,
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


async def claim_next_job(
    db: AsyncSession,
    max_jobs_per_user: int,
    excluded_users: set[int] | None = None,
    generation: int | None = None,
    *,
    plan_jobs: dict[str, int] | None = None,
    free_max_jobs: int | None = None,
) -> uuid.UUID | None:
    """Claim the oldest queued job whose user is under their running cap, or None: the
    plan's parses at once (`plan_jobs` by plan id for a paid period, `free_max_jobs`
    otherwise; the ceiling alone when neither is given) under `max_jobs_per_user`. A
    user in `excluded_users` — one whose orphaned turn the worker could not stop yet —
    gets no parse run, since its finish writes that user's tree (docs/jobs.md § Chat
    turns). `generation` is the claimer's (`next_worker_generation`), written on the
    row: the fence every write it makes to the job carries, so a worker that lost the
    worker lock cannot write over the one that took the job (docs/jobs.md § Queue)."""
    # No user id is 0: the sentinel keeps the clause well-formed when nobody is excluded.
    params = {
        "max_jobs_per_user": max_jobs_per_user,
        "excluded_users": sorted(excluded_users or ()) or [0],
        "generation": generation,
        "plan_jobs": json.dumps(plan_jobs or {}),
        "paid_statuses": sorted(PAID_STATUSES),
        "free_max_jobs": max_jobs_per_user if free_max_jobs is None else free_max_jobs,
        "now": utcnow(),
    }
    if db.get_bind().dialect.name == "postgresql":
        claimed = (await db.execute(CLAIM_SQL, params)).scalar_one_or_none()
        await db.commit()
        return claimed

    candidate = (await db.execute(CLAIM_SQL_SQLITE_SELECT, params)).scalar_one_or_none()
    if candidate is None:
        return None
    job_id = uuid.UUID(str(candidate))
    updated = await db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="running", started_at=utcnow(), worker_generation=generation)
    )
    await db.commit()
    return job_id if updated.rowcount == 1 else None


async def next_worker_generation(db: AsyncSession) -> int:
    """The generation a worker takes with the worker lock and writes on every job it
    claims (docs/jobs.md § Queue). PostgreSQL: the next value of a sequence, so no two
    workers ever hold one generation — the stale one keeps making paid calls for the
    seconds before it notices its lock is gone, and the replacement has requeued and
    claimed its job by then. The SQLite fallback runs one worker: past the highest on
    record."""
    if db.get_bind().dialect.name == "postgresql":
        return int((await db.execute(select(WORKER_GENERATION_SEQ.next_value()))).scalar_one())
    highest = (await db.execute(select(func.max(Job.worker_generation)))).scalar_one()
    return int(highest or 0) + 1


# The advisory lock one worker holds for its lifetime: any fixed bigint (docs/jobs.md).
WORKER_LOCK_KEY = 7_312_024_001


async def try_worker_lock(engine: AsyncEngine) -> AsyncConnection | None:
    """One worker per deployment, enforced: the connection that holds the session-level
    advisory lock, or None while another worker holds it. The recovery at start treats
    every `running` row as a dead worker's, which is only true of a singleton; a second
    worker process waits for the lock rather than taking the first one's jobs back
    (docs/jobs.md). Not PostgreSQL (the SQLite fallback): no lock, one worker assumed."""
    if engine.dialect.name != "postgresql":
        return None
    connection = await engine.connect()
    try:
        # Autocommit: a session-level lock outlives transactions, and a connection
        # left idle in one would hold a snapshot for the worker's whole life.
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        held = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK_KEY}
            )
        ).scalar_one()
    except BaseException:
        await connection.close()
        raise
    if held:
        return connection
    await connection.close()
    return None


async def watch_worker_lock(
    connection: AsyncConnection, stop: asyncio.Event, interval_seconds: float
) -> None:
    """Keep asking the lock's session whether it is alive, until `stop`. PostgreSQL
    releases a session-level advisory lock with the session: a restart or a dropped
    connection hands it to a waiting worker, which then requeues every `running` job,
    so this one must stop and cancel its work the moment its session is gone, rather
    than finish a parse beside the worker that took it (docs/jobs.md)."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        if stop.is_set():
            return
        try:
            (await connection.execute(text("SELECT 1"))).scalar_one()
        except Exception:  # noqa: BLE001 - any failure means the session is not ours to trust
            logger.error("the worker lock's session is gone; stopping, the lock may be another's")
            stop.set()
            return


async def release_worker_lock(connection: AsyncConnection) -> None:
    with contextlib.suppress(Exception):
        await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK_KEY})
    with contextlib.suppress(Exception):
        await connection.close()


async def requeue_orphaned_jobs(db: AsyncSession) -> list[uuid.UUID]:
    """Every job still `running` when a worker starts goes back to the queue: the
    deployment has one worker, so a job nobody is driving is one a dead worker held,
    and it would otherwise stay `running` for good, against its user's cap (docs/jobs.md).
    Returns the ids, whose crops the caller discards with the partial progress."""
    orphans = list((await db.execute(select(Job.id).where(Job.status == "running"))).scalars())
    for job_id in orphans:
        await requeue_job(db, job_id)
    return orphans


async def _keep_succeeded_pages(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Forget a requeued job's failed pages and count the ones it keeps: a row with
    markdown is a page that succeeded (a failed one stores none), and the next attempt
    transcribes only what has no such row (docs/jobs.md § Retrying failed pages)."""
    await db.execute(delete(JobPage).where(JobPage.job_id == job_id, JobPage.markdown.is_(None)))
    await db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            pages_done=select(func.count())
            .select_from(JobPage)
            .where(JobPage.job_id == job_id)
            .scalar_subquery()
        )
    )


async def requeue_job(db: AsyncSession, job_id: uuid.UUID, generation: int | None = None) -> bool:
    """Put an interrupted job back on the queue, keeping the pages it finished.

    The next attempt transcribes only the pages without a succeeded row — a page the
    user's key already paid for is not transcribed again — and crops every page anew,
    so the caller discards this attempt's crops (docs/jobs.md § Queue). False when the
    job was no longer `running` — its result committed before the cancel landed — and so
    stays as it is, its pages and crops with it; or, with `generation`, when the row no
    longer holds the caller's: a worker that lost the lock must not put back a job the
    replacement has taken (docs/jobs.md § Queue). A worker starting requeues without
    one, since every `running` row is then a dead worker's.
    """
    owned = [Job.worker_generation == generation] if generation is not None else []
    requeued = (
        await db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "running", *owned)
            .values(status="queued", started_at=None, worker_generation=None)
        )
    ).rowcount > 0
    if requeued:
        await _keep_succeeded_pages(db, job_id)
    await db.commit()
    return requeued


async def retry_job(
    db: AsyncSession,
    job_id: uuid.UUID,
    *,
    source_path: str,
    model: str | None,
    check: CheckTarget | None,
) -> bool:
    """Put a failed parse back on the queue to run its failed pages, on `model` (None: the
    job's own), from `source_path` — a copy of its document's original — and checked as
    `check` says, the settings of this moment (None: unchecked). The caller holds the job's
    row lock and commits. False when the job is no longer `failed` (docs/jobs.md
    § Retrying failed pages)."""
    retried = (
        await db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "failed")
            .values(
                status="queued",
                error=None,
                started_at=None,
                finished_at=None,
                worker_generation=None,
                retry_model=model,
                check_model=check.model if check is not None else None,
                check_effort=check.effort if check is not None else None,
                check_prompt=check.prompt if check is not None else None,
                source_path=source_path,
                source_deleted_at=None,
            )
        )
    ).rowcount > 0
    if retried:
        await _keep_succeeded_pages(db, job_id)
    return retried


# --- chat turns (docs/jobs.md § Chat turns) ----------------------------------------------

CLAIM_TURN_SQL = text(
    """
    UPDATE chat_turns SET status = 'running', started_at = now()
    WHERE id = (
      SELECT t.id FROM chat_turns t
      WHERE t.status = 'queued'
        AND (
          SELECT count(*) FROM chat_turns running
          WHERE running.user_id = t.user_id
            AND running.status <> 'queued' AND running.finished_at IS NULL
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
        WHERE running.user_id = t.user_id
          AND running.status <> 'queued' AND running.finished_at IS NULL
      ) < :max_turns_per_user
    ORDER BY t.created_at LIMIT 1
    """
)


# What the reader is told, at the send and in the turn alike, once a conversation's
# stored history is past `CHAT_HISTORY_MAX_BYTES` (docs/chat.md § History is append-only).
HISTORY_FULL = (
    "This conversation's history is too large to continue; start a new conversation about "
    "this document"
)


async def history_bytes(db: AsyncSession, conversation_id: int, *, since: int | None = None) -> int:
    """The stored size of a conversation's messages, in bytes as the cap is: PostgreSQL's
    `length` counts characters, and a history of non-ASCII text would reach four times
    the cap before refusal (docs/chat.md § History is append-only). With `since`, the
    summary row a compaction left, only the rows from it on — what a turn replays
    (docs/chat.md § Compaction)."""
    as_text = cast(Message.content, Text)
    size = (
        func.octet_length(as_text)
        if db.get_bind().dialect.name == "postgresql"
        else func.length(cast(as_text, LargeBinary))
    )
    query = select(func.coalesce(func.sum(size), 0)).where(
        Message.conversation_id == conversation_id
    )
    if since is not None:
        query = query.where(Message.id >= since)
    return (await db.execute(query)).scalar_one()


async def count_active_turns(db: AsyncSession, user_id: int) -> int:
    """Unfinished turns for one user — queued, running, or cancelled but not yet wound
    down — the cap `POST …/messages` enforces."""
    return (
        await db.execute(
            select(func.count())
            .select_from(ChatTurn)
            .where(ChatTurn.user_id == user_id, ChatTurn.finished_at.is_(None))
        )
    ).scalar_one()


async def enqueue_turn(
    db: AsyncSession,
    *,
    user_id: int,
    conversation_id: int,
    message_id: int,
    locale: str = "en",
    kind: str = MESSAGE_TURN,
) -> ChatTurn:
    """Create the queued turn row — a user message's, or a compaction's by its `kind`
    (docs/chat.md § Compaction). The caller commits, with the user message it answers."""
    turn = ChatTurn(
        id=uuid.uuid4(),
        user_id=user_id,
        conversation_id=conversation_id,
        message_id=message_id,
        kind=kind,
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


async def fail_orphaned_turns(
    db: AsyncSession, sandbox: SandboxClient | None = None, settings: Settings | None = None
) -> tuple[int, set[int]]:
    """Finish every turn a worker that died left behind: `running`, or cancelled and not
    yet wound down. Run when a worker starts, and again until it goes through — the
    deployment has one worker, so a turn nobody is driving is exactly one a dead worker
    held (docs/jobs.md § Chat turns). Each turn's shell is restarted on the runner first,
    which kills the command the dead worker may have left running inside the VM; a
    runner that does not answer leaves that turn for the next pass rather than finishing
    it over a command that may still write. Then whatever the turn changed and never
    recorded — the worker died between a tool's write and its commit — becomes a revision
    of its own, under the same locks the tool held, so the next revision carries only
    its own changes; a tree that cannot be committed leaves the turn for the next pass
    too. Returns (finished, the users whose turns are left for later)."""
    orphans = (
        await db.execute(
            select(ChatTurn.id, ChatTurn.user_id, Conversation.id, Conversation.session)
            .join(Conversation, Conversation.id == ChatTurn.conversation_id)
            .where(
                ChatTurn.status.in_(("running", "cancelled")),
                ChatTurn.finished_at.is_(None),
            )
        )
    ).all()
    finished = 0
    left: set[int] = set()
    for turn_id, user_id, conversation_id, session in orphans:
        if user_id in left:
            continue
        if sandbox is not None and sandbox.configured:
            try:
                await sandbox.restart(user_id, session)
            except SandboxUnavailable:
                left.add(user_id)
                continue
        elif (
            settings is not None
            and writing_marker(user_root(settings.workspaces_dir, user_id)).exists()
        ):
            # A shell command was writing when the worker died, and this worker has no
            # runner to stop it with (SANDBOX_URL gone from its environment): the VM may
            # still be running the command, so the tree is not declared safe here. The
            # marker standing is what counts, readable or not.
            logger.warning(
                "an orphaned shell command of user %d cannot be stopped: no sandbox runner "
                "is configured; the workspace stays closed",
                user_id,
            )
            left.add(user_id)
            continue
        if settings is not None and not await _record_orphaned_changes(
            db, settings, user_id, conversation_id
        ):
            left.add(user_id)
            continue
        await fail_turn(db, turn_id, "worker restarted")
        finished += 1
    return finished, left


async def recover_unsettled_commands(settings: Settings, sandbox: SandboxClient | None) -> set[int]:
    """Every writing marker still standing after the orphaned turns are finished names a
    shell command nobody could confirm over — one the runner could not vouch for in a
    turn that then ended. Its shell is restarted on the runner, which answers only once
    the command is dead, and the tree is recorded in the conversation's name; a runner
    that does not answer leaves the marker, and the user's host writes refused, for the
    next pass. Returns the users left for later (docs/workspace.md § Revisions)."""
    left: set[int] = set()
    repos = Path(settings.workspaces_dir) / REPOS_DIR
    for marker in sorted(repos.glob(f"*{WRITING_SUFFIX}")) if repos.is_dir() else []:
        try:
            user_id = int(marker.name.removesuffix(WRITING_SUFFIX))
        except ValueError:
            continue
        root = user_root(settings.workspaces_dir, user_id)
        named = read_writing(root)
        if sandbox is None or not sandbox.configured:
            # Nothing to stop the shell with: unconfirmed, as a restart that failed.
            logger.warning(
                "an unconfirmed shell command of user %d cannot be stopped: no sandbox "
                "runner is configured; the workspace stays closed",
                user_id,
            )
            left.add(user_id)
            continue
        try:
            if named is not None:
                await sandbox.restart(user_id, named[1])
            else:
                # The marker stands but names no shell (a hand on it, a worker killed
                # before the rename that writes it whole): the command it stood for may
                # still run, in a shell unknown — so every shell of the user goes, the
                # VM stopped, before the tree is declared the command's and recorded.
                logger.warning(
                    "the writing marker of user %d names no shell; stopping the sandbox",
                    user_id,
                )
                await sandbox.stop(user_id)
        except SandboxUnavailable:
            left.add(user_id)
            continue
        author = f"{git.AGENT_AUTHOR_PREFIX}{named[0]}" if named else git.USER_AUTHOR
        async with shell_lock(root), workspace_lock(root, recovering=True):
            try:
                if git_dir(root).is_dir():
                    await asyncio.to_thread(
                        git.commit_all, root, author, UNSETTLED_REVISION_MESSAGE
                    )
            except git.GitError:
                logger.exception("could not record an unconfirmed command of user %d", user_id)
                left.add(user_id)
                continue
            clear_writing(root)
            logger.warning(
                "recovered the workspace of user %d after an unconfirmed command", user_id
            )
    return left


async def _record_orphaned_changes(
    db: AsyncSession, settings: Settings, user_id: int, conversation_id: int
) -> bool:
    """The dirty tree an orphaned turn left, as one revision in the turn's name; True
    when the tree is clean or recorded. The shell lock before the workspace lock, as
    the bash tool takes them; the library follows the tree when something was recorded,
    as after a command (docs/workspace.md § Revisions)."""
    root = user_root(settings.workspaces_dir, user_id)
    if not git_dir(root).is_dir():
        return True
    author = f"{git.AGENT_AUTHOR_PREFIX}{conversation_id}"
    # `recovering`: the mark a command the dead worker was running left stands until
    # the tree is recorded here, and this is the one write allowed under it.
    async with shell_lock(root), workspace_lock(root, recovering=True):
        try:
            commit = await asyncio.to_thread(git.commit_all, root, author, ORPHAN_REVISION_MESSAGE)
        except git.GitError:
            logger.exception(
                "could not record the changes an orphaned turn of user %d left", user_id
            )
            return False
        if commit is not None:
            logger.warning(
                "recorded the changes an orphaned turn of user %d left as %s", user_id, commit
            )
            await reconcile_locked(db, settings, user_id)
        # The shell is stopped and the tree recorded: host writes may resume.
        clear_writing(root)
    return True


async def fail_turn(db: AsyncSession, turn_id: uuid.UUID, error: str) -> None:
    """A turn the worker could not finish. Never requeued: its tool calls may have run.
    A turn the user had cancelled keeps `cancelled`; either way it is finished now, with
    the terminal event a stream waits for (docs/jobs.md § Chat turns)."""
    turn = await db.get(ChatTurn, turn_id)
    if turn is None:
        return
    # The conversation's lock before the turn is touched, the order the cancel route
    # takes (docs/chat.md § Conversations): no deadlock, and a cancel just committed
    # is seen on the refresh.
    await db.execute(
        select(Conversation.id).where(Conversation.id == turn.conversation_id).with_for_update()
    )
    await db.refresh(turn)
    if turn.finished_at is not None:
        return
    if turn.status == "cancelled":
        turn.error = "cancelled"
    else:
        turn.status = "failed"
        turn.error = error
    turn.finished_at = utcnow()
    # Charged for what ran: the entry closes with the turn whichever path ends it
    # (docs/billing.md § A chat turn).
    await ledger.close_chat(db, turn.id)
    await chat_events.append(
        db,
        turn.conversation_id,
        turn.id,
        chat_events.TURN_ERROR,
        {"status": turn.status, "error": turn.error or ""},
    )
    await db.commit()
