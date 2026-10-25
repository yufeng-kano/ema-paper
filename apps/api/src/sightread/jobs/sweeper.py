"""Upload retention sweeper (docs/jobs.md § Retention).

Deleting the source at terminal state is the optimisation; this sweeper is the guarantee.
It runs inside the worker and trashes anything under the upload directory older than
`UPLOAD_MAX_AGE_SECONDS` (a day), whatever left it there — a crashed job, an abandoned
upload, or a bug. The same pass packs the workspaces' git histories
(docs/workspace.md § Revisions).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from ..errors import ApiError
from ..workspace import git
from ..workspace.locks import workspace_lock

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


async def pack_histories(workspaces_dir: Path, *, loose_objects: int) -> int:
    """Pack each workspace's git history holding `loose_objects` loose objects or more,
    under that workspace's lock (docs/workspace.md § Revisions); returns how many were
    packed. One whose lock is refused — a shell command still being recovered — or whose
    git fails waits for the next sweep."""
    try:
        names = sorted((name for name in os.listdir(workspaces_dir) if name.isdigit()), key=int)
    except OSError:
        return 0
    packed = 0
    for name in names:
        root = workspaces_dir / name
        try:
            if await asyncio.to_thread(git.loose_objects, root) < loose_objects:
                continue
            async with workspace_lock(root):
                await asyncio.to_thread(git.pack, root)
        except (ApiError, git.GitError, OSError):
            logger.warning("sweeper could not pack the history of workspace %s", name)
            continue
        packed += 1
    return packed


async def sweep_forever(
    upload_dir: Path,
    workspaces_dir: Path,
    stop: asyncio.Event,
    *,
    max_age: float,
    pack_loose_objects: int,
) -> None:
    """Sweep now and every 15 minutes until the worker shuts down."""
    while not stop.is_set():
        removed = await asyncio.to_thread(sweep_uploads, upload_dir, max_age=max_age)
        if removed:
            logger.info("sweeper removed %d stale upload entries", removed)
        packed = await pack_histories(workspaces_dir, loose_objects=pack_loose_objects)
        if packed:
            logger.info("sweeper packed the history of %d workspaces", packed)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_SECONDS)
        except TimeoutError:
            continue
