"""One writer per workspace at a time (docs/workspace.md § Revisions).

Two layers: an `asyncio.Lock` per user inside this process, and `flock` on a file across
processes — the API and the worker both write to the same directory, and two conversations
editing in parallel must not interleave git's index. The lock files live beside the git
directory, outside the VM's mount: a shell that could unlink the lock could defeat it.

Two locks: the *workspace* lock around every git index operation and every host write,
and the *shell* lock a conversation holds for the whole of a shell command and its
commit, so two shells never have the tree dirty at once (docs/chat.md § Tools).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path

from .layout import git_dir

_process_locks: dict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)

WORKSPACE_SUFFIX = ".lock"
SHELL_SUFFIX = ".shell.lock"


def _lock_path(root: Path, suffix: str) -> Path:
    repo = git_dir(root)
    repo.parent.mkdir(parents=True, exist_ok=True)
    return repo.parent / f"{repo.name}{suffix}"


def _acquire(path: Path) -> int:
    fd = path.open("a+")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd  # type: ignore[return-value]


@contextlib.asynccontextmanager
async def _hold(root: Path, suffix: str) -> AsyncIterator[None]:
    path = _lock_path(root, suffix)
    async with _process_locks[path]:
        handle = await asyncio.to_thread(_acquire, path)
        try:
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                handle.close()


@contextlib.asynccontextmanager
async def workspace_lock(root: Path) -> AsyncIterator[None]:
    """Hold the user's workspace for writing. Re-entrancy is not supported on purpose."""
    async with _hold(root, WORKSPACE_SUFFIX):
        yield


@contextlib.asynccontextmanager
async def shell_lock(root: Path) -> AsyncIterator[None]:
    """Hold the user's shell: one command and its commit at a time, across conversations.
    Taken *before* the workspace lock, never inside it."""
    async with _hold(root, SHELL_SUFFIX):
        yield
