"""Upload retention sweeper (docs/jobs.md § Retention).

Deleting the source at terminal state is the optimisation; this sweeper is the guarantee.
It runs inside the worker and trashes anything under the upload directory older than
`UPLOAD_MAX_AGE_SECONDS` (a day), whatever left it there — a crashed job, an abandoned
upload, or a bug.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

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


async def sweep_forever(upload_dir: Path, stop: asyncio.Event, *, max_age: float) -> None:
    """Sweep now and every 15 minutes until the worker shuts down."""
    while not stop.is_set():
        removed = await asyncio.to_thread(sweep_uploads, upload_dir, max_age=max_age)
        if removed:
            logger.info("sweeper removed %d stale upload entries", removed)
        try:
            await asyncio.wait_for(stop.wait(), timeout=SWEEP_INTERVAL_SECONDS)
        except TimeoutError:
            continue
