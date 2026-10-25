"""The worker's sweep (docs/jobs.md § Retention).

Deleting the source at terminal state is the optimisation; this sweep is the guarantee.
Every 15 minutes, inside the worker, it trashes anything under the upload directory older
than `UPLOAD_MAX_AGE_SECONDS` (a day), whatever left it there — a crashed job, an
abandoned upload, or a bug — packs the workspaces' git histories (docs/workspace.md
§ Revisions) and removes from each workspace the staging file of a file upload the API
process died in (§ Files). It deletes for good what has been in the Trash longer than
`TRASH_RETENTION_DAYS` (§ The Trash), deletes the finished parses the retention no longer
keeps once they are `PARSE_RETENTION_DAYS` old, and finishes the account deletions a
request left half done, from their markers (docs/auth.md § Deleting the account).

The Trash's and the parses' passes go user by user, each under the user's workspace
lock: a user whose pass is refused, or fails, is logged and tried again at the next
sweep, and the others still go.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections.abc import Collection
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, not_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import Document, Folder, Job, User, utcnow
from ..errors import ApiError
from ..parsing.attachments import FILE_PREFIX
from ..parsing.figures import discard_job_figures
from ..workspace import accounts, git, safe_io, trash
from ..workspace import documents as workspace_documents
from ..workspace.layout import user_root
from ..workspace.locks import workspace_lock
from .queue import parse_kept

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 900.0


def sweep_uploads(upload_dir: Path, *, max_age: float, now: float | None = None) -> int:
    """Delete upload-directory entries older than `max_age` seconds; returns how many were
    removed."""
    now = time.time() if now is None else now
    if not upload_dir.is_dir():
        return 0

    removed = 0
    for entry in upload_dir.iterdir():
        try:
            if now - entry.stat().st_mtime < max_age:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            logger.warning("sweeper could not remove an upload entry")
    return removed


def discard_uploads(upload_dir: Path, stems: Collection[str]) -> bool:
    """Remove every upload-directory entry named for one of `stems` — the stem itself, or
    the stem and a dot and anything after: a job's upload (`{job_id}.pdf`), its retry
    copies and its render directories (`{job_id}.<attempt>`), a chat attachment's file
    and its staging (`chat-attachment-{id}.png`, `.upload`). False when one could not be
    removed, logged."""
    try:
        names = os.listdir(upload_dir)
    except FileNotFoundError:
        return True
    removed = True
    for name in names:
        if name.partition(".")[0] not in stems:
            continue
        try:
            safe_io.remove_tree(upload_dir, upload_dir / name)
        except (OSError, safe_io.UnsafeEntry):
            logger.exception("sweeper could not remove the upload entry %s", name)
            removed = False
    return removed


def workspace_roots(workspaces_dir: Path) -> list[Path]:
    """Every user's workspace root under `workspaces_dir`, by user id."""
    try:
        names = sorted((name for name in os.listdir(workspaces_dir) if name.isdigit()), key=int)
    except OSError:
        return []
    return [workspaces_dir / name for name in names]


async def sweep_staged_uploads(workspaces_dir: Path) -> int:
    """Remove from each workspace the staging file of a file upload the API process died
    in (docs/workspace.md § Files); returns how many went. No workspace lock: the file's
    own lock tells an upload still streaming from one nobody holds."""
    swept = 0
    for root in workspace_roots(workspaces_dir):
        swept += await asyncio.to_thread(workspace_documents.sweep_staged_uploads, root)
    return swept


async def pack_histories(workspaces_dir: Path, *, loose_objects: int) -> int:
    """Pack each workspace's git history holding `loose_objects` loose objects or more,
    under that workspace's lock (docs/workspace.md § Revisions); returns how many were
    packed. One whose lock is refused — a shell command still being recovered — or whose
    git fails waits for the next sweep."""
    packed = 0
    for root in workspace_roots(workspaces_dir):
        try:
            if await asyncio.to_thread(git.loose_objects, root) < loose_objects:
                continue
            async with workspace_lock(root):
                await asyncio.to_thread(git.pack, root)
        except (ApiError, git.GitError, OSError):
            logger.warning("sweeper could not pack the history of workspace %s", root.name)
            continue
        packed += 1
    return packed


async def expire_trash(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    """Delete for good every item whose own `trashed_at` is older than
    `TRASH_RETENTION_DAYS`, as *Empty Trash* deletes it — the same function, with a
    cutoff (docs/workspace.md § The Trash); returns how many items went. Nothing at 0."""
    if settings.trash_retention_days == 0:
        return 0
    cutoff = utcnow() - timedelta(days=settings.trash_retention_days)
    async with sessionmaker() as db:
        users = sorted(
            (
                await db.execute(
                    select(Folder.user_id)
                    .where(Folder.trashed_at < cutoff)
                    .union(select(Document.user_id).where(Document.trashed_at < cutoff))
                )
            ).scalars()
        )
    deleted = 0
    for user_id in users:
        try:
            async with sessionmaker() as db:
                deleted += await trash.empty_trash(db, settings, user_id, trashed_before=cutoff)
        except ApiError as exc:
            # A conversation about a file it would delete still answering, or the
            # workspace recovering from an unconfirmed shell command: the next sweep.
            logger.warning("the Trash of user %d waits for the next sweep: %s", user_id, exc)
        except Exception:  # noqa: BLE001 - one user's failure never stops the others'
            logger.exception("could not empty the expired Trash of user %d", user_id)
    return deleted


async def purge_parses(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    """Delete the finished parses the retention no longer keeps (docs/jobs.md
    § Retention); returns how many went. Nothing at `PARSE_RETENTION_DAYS` 0.

    Per user, under the workspace lock every filing, copy, retry and re-convert takes, so
    none of them can name a job while it goes: the candidates' rows are locked, the rule
    read again under the lock, and the files removed before the rows — nothing reads a
    parse past its retention, so a commit that fails leaves rows the next sweep deletes,
    never crops no row can find. Results and pages cascade with the job; usage rows,
    upload tickets and ledger entries keep theirs with `job_id` NULL."""
    if settings.parse_retention_days == 0:
        return 0
    unkept = (
        Job.status.in_(("succeeded", "failed")),
        not_(parse_kept(settings.parse_retention_days)),
    )
    async with sessionmaker() as db:
        users = sorted((await db.execute(select(Job.user_id).where(*unkept).distinct())).scalars())
    purged = 0
    for user_id in users:
        try:
            async with (
                workspace_lock(user_root(settings.workspaces_dir, user_id)),
                sessionmaker() as db,
            ):
                candidates = (
                    await db.execute(
                        select(Job.id)
                        .where(Job.user_id == user_id, *unkept)
                        .order_by(Job.id)
                        .with_for_update()
                    )
                ).scalars()
                doomed = list(
                    (
                        await db.execute(
                            select(Job.id).where(Job.id.in_(list(candidates)), *unkept)
                        )
                    ).scalars()
                )
                # A job whose crops will not go stays, for the next sweep to try again: its
                # row is the only thing that names them.
                gone = [
                    job_id
                    for job_id in doomed
                    if await asyncio.to_thread(
                        discard_job_figures, Path(settings.figures_dir), job_id
                    )
                ]
                if not gone:
                    continue
                await workspace_documents.discard_results(settings, user_id, gone)
                await asyncio.to_thread(
                    discard_uploads, Path(settings.upload_dir), {str(job_id) for job_id in gone}
                )
                await db.execute(delete(Job).where(Job.id.in_(gone)))
                await db.commit()
            purged += len(gone)
        except ApiError as exc:
            # The workspace recovering from an unconfirmed shell command.
            logger.warning("the parses of user %d wait for the next sweep: %s", user_id, exc)
        except Exception:  # noqa: BLE001 - one user's failure never stops the others'
            logger.exception("could not delete the expired parses of user %d", user_id)
    return purged


async def discard_account(
    settings: Settings,
    user_id: int,
    job_ids: Collection[uuid.UUID],
    attachment_ids: Collection[uuid.UUID] = (),
) -> bool:
    """Everything of a deleted account outside the database, once its rows are gone
    (docs/auth.md § Deleting the account): the user directory, the git directory, the
    originals, the images and the lock files, the crop directories and the
    upload-directory files of its jobs and of its chat attachments — and then the marker,
    once all of that went. A failure is logged and leaves the marker for the next sweep;
    returns whether everything went. The caller holds the user's workspace lock."""
    removed = await asyncio.to_thread(accounts.discard_workspace, settings.workspaces_dir, user_id)
    for job_id in job_ids:
        crops = await asyncio.to_thread(discard_job_figures, Path(settings.figures_dir), job_id)
        removed = crops and removed
    stems = {str(job_id) for job_id in job_ids} | {
        f"{FILE_PREFIX}{attachment_id}" for attachment_id in attachment_ids
    }
    removed = await asyncio.to_thread(discard_uploads, Path(settings.upload_dir), stems) and removed
    if removed:
        accounts.discard_marker(settings.workspaces_dir, user_id)
    return removed


async def finish_deletions(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings
) -> int:
    """The account deletions a request left half done: for each marker whose user row is
    gone, what the request would have removed, the crops and uploads of the job ids the
    marker names included; a marker whose user row still stands — the commit never
    landed — is removed alone (docs/auth.md § Deleting the account). Nothing else here
    deletes a user's files by inference. Returns how many deletions it finished."""
    finished = 0
    for user_id in accounts.marked_users(settings.workspaces_dir):
        try:
            # The lock the deleting request holds from before the marker until after its
            # files: a marker is never read in the middle of a deletion. Taken as the
            # recovery: a writing marker of a deleted account is one more file to remove.
            async with workspace_lock(user_root(settings.workspaces_dir, user_id), recovering=True):
                async with sessionmaker() as db:
                    standing = await db.get(User, user_id) is not None
                if standing:
                    logger.warning(
                        "the deletion of user %d never committed; marker removed", user_id
                    )
                    accounts.discard_marker(settings.workspaces_dir, user_id)
                    continue
                try:
                    job_ids = accounts.read_marker(settings.workspaces_dir, user_id)
                except FileNotFoundError:
                    # The deleting request removed its lock file with the rest, so another
                    # process can take a new one while it finishes: finished meanwhile.
                    continue
                if await discard_account(settings, user_id, job_ids):
                    finished += 1
        except Exception:  # noqa: BLE001 - one account's failure never stops the others'
            logger.exception("could not finish the deletion of user %d", user_id)
    return finished


async def sweep_once(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """One pass of every sweep, each logged by what it did."""
    upload_dir = Path(settings.upload_dir)
    removed = await asyncio.to_thread(
        sweep_uploads, upload_dir, max_age=settings.upload_max_age_seconds
    )
    if removed:
        logger.info("sweeper removed %d stale upload entries", removed)
    packed = await pack_histories(
        Path(settings.workspaces_dir), loose_objects=settings.workspace_git_pack_loose_objects
    )
    if packed:
        logger.info("sweeper packed the history of %d workspaces", packed)
    staged = await sweep_staged_uploads(Path(settings.workspaces_dir))
    if staged:
        logger.info("sweeper removed the staging files of %d uploads that died", staged)
    expired = await expire_trash(sessionmaker, settings)
    if expired:
        logger.info("sweeper deleted %d items past their time in the Trash", expired)
    purged = await purge_parses(sessionmaker, settings)
    if purged:
        logger.info("sweeper deleted %d parses past their retention", purged)
    finished = await finish_deletions(sessionmaker, settings)
    if finished:
        logger.info("sweeper finished %d account deletions", finished)


async def sweep_forever(
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, stop: asyncio.Event
) -> None:
    """Sweep now and every 15 minutes until the worker shuts down. A pass that raises
    past its own guards — the database away while it lists its users, say — is logged,
    and the next one runs on schedule: one failing pass never ends the sweeper."""
    while not stop.is_set():
        try:
            await sweep_once(sessionmaker, settings)
        except Exception:  # noqa: BLE001 - the next pass tries again
            logger.exception("the sweep failed; the next one runs on schedule")
        try:
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_SECONDS)
        except TimeoutError:
            continue
