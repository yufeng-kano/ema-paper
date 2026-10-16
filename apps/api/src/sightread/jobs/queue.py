"""The job queue (docs/jobs.md). The only module that knows the queue is PostgreSQL.

A job row *is* its queue entry, so creating a job and enqueueing it are one transaction —
there is no broker and no dual write. Claiming is `FOR UPDATE SKIP LOCKED`, FIFO, with the
per-user cap applied at claim time so one user cannot monopolise the workers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import get_agent
from ..config import Settings
from ..db.models import ChatTurn, Conversation, Job, JobPage, utcnow
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..workspace import git
from ..workspace.layout import git_dir, user_root
from ..workspace.locks import shell_lock, workspace_lock
from ..workspace.reconcile import reconcile_locked
from . import chat_events

logger = logging.getLogger(__name__)

# The revision an orphaned turn's unrecorded changes become (docs/jobs.md § Chat turns).
ORPHAN_REVISION_MESSAGE = "bash: interrupted by a worker restart"

# PostgreSQL claim (docs/jobs.md). The correlated count applies the per-user running cap
# to the same scan that picks the oldest queued job.
CLAIM_SQL = text(
    """
    UPDATE jobs SET status = 'running', started_at = now()
    WHERE id = (
      SELECT j.id FROM jobs j
      WHERE j.status = 'queued'
        AND j.user_id NOT IN :excluded_users
        AND (
          SELECT count(*) FROM jobs running
          WHERE running.user_id = j.user_id AND running.status = 'running'
        ) < :max_jobs_per_user
      ORDER BY j.created_at
      FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id
    """
).bindparams(bindparam("excluded_users", expanding=True))

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
      ) < :max_jobs_per_user
    ORDER BY j.created_at LIMIT 1
    """
).bindparams(bindparam("excluded_users", expanding=True))

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


async def claim_next_job(
    db: AsyncSession, max_jobs_per_user: int, excluded_users: set[int] | None = None
) -> uuid.UUID | None:
    """Claim the oldest queued job whose user is under the running cap, or None. A user
    in `excluded_users` — one whose orphaned turn the worker could not stop yet — gets
    no parse run, since its finish writes that user's tree (docs/jobs.md § Chat turns)."""
    # No user id is 0: the sentinel keeps the clause well-formed when nobody is excluded.
    params = {
        "max_jobs_per_user": max_jobs_per_user,
        "excluded_users": sorted(excluded_users or ()) or [0],
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
        if settings is not None and not await _record_orphaned_changes(
            db, settings, user_id, conversation_id
        ):
            left.add(user_id)
            continue
        await fail_turn(db, turn_id, "worker restarted")
        finished += 1
    return finished, left


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
    async with shell_lock(root), workspace_lock(root):
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
    await chat_events.append(
        db,
        turn.conversation_id,
        turn.id,
        chat_events.TURN_ERROR,
        {"status": turn.status, "error": turn.error or ""},
    )
    await db.commit()
