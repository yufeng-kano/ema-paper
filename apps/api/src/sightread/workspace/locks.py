"""One writer per workspace at a time (docs/workspace.md § Revisions).

Two layers: an `asyncio.Lock` per user inside this process, and `flock` on a file under
the user's `.sightread/` across processes — the API and the worker both write to the same
directory, and two conversations editing in parallel must not interleave git's index.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path

from .layout import LOCK_FILE, SIGHTREAD_DIR

_process_locks: dict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)


def _lock_path(root: Path) -> Path:
    path = root / SIGHTREAD_DIR / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _acquire(path: Path) -> int:
    fd = path.open("a+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd  # type: ignore[return-value]


@contextlib.asynccontextmanager
async def workspace_lock(root: Path) -> AsyncIterator[None]:
    """Hold the user's workspace for writing. Re-entrancy is not supported on purpose."""
    async with _process_locks[root.resolve()]:
        handle = await asyncio.to_thread(_acquire, _lock_path(root))
        try:
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()
