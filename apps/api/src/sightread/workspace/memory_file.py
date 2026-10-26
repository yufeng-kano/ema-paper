"""The chat agent's memory file, `memory.md` at the root of the user's workspace
(docs/chat.md § Memory, *The memory file*): what a conversation snapshots at its first turn
and after a compaction, and what the Agent page reads and writes.

Outside `files/`, so it is no entry of the library; a revision like any text, so the
agent's edits have diff cards and the reader's save is a revision of their own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import Settings
from ..errors import ApiError
from . import git, safe_io
from .layout import user_root

MEMORY_FILE = "memory.md"
NOT_PLAIN = "The memory file is not a plain text file"


def read_memory_file(root: Path, max_bytes: int) -> str:
    """The file's text, "" when there is none; 409 when it is not a plain UTF-8 file or is
    larger than the API opens."""
    try:
        return safe_io.read_text(root, root / MEMORY_FILE, max_bytes) or ""
    except FileNotFoundError:
        return ""
    except (safe_io.EntryTooLarge, safe_io.UnsafeEntry, UnicodeDecodeError, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc


async def memory_file_text(settings: Settings, user_id: int) -> str:
    """The file as it is now, read off the thread; a workspace that does not exist yet has
    none."""
    root = user_root(settings.workspaces_dir, user_id)
    if not root.is_dir():
        return ""
    return await asyncio.to_thread(read_memory_file, root, settings.content_max_bytes)


def write_memory_file(root: Path, text: str, max_bytes: int) -> None:
    """Write the reader's text as a revision of their own. Under the workspace lock, which
    the caller holds, having refused the write while a turn of the reader is unfinished."""
    git.ensure_repo(root)
    path = root / MEMORY_FILE
    existed = path.exists()
    current = read_memory_file(root, max_bytes) if existed else ""
    if existed and current == text:
        return
    try:
        safe_io.write_text(root, path, text)
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    try:
        git.commit_paths(root, git.USER_AUTHOR, f"memory {MEMORY_FILE}", [Path(MEMORY_FILE)])
    except git.GitError as exc:
        # The file as it was: a save with no revision is not a save (docs/workspace.md
        # § Revisions).
        if existed:
            safe_io.write_text(root, path, current)
        else:
            path.unlink(missing_ok=True)
        raise ApiError(
            409, "invalid_request", "The save could not be recorded; the file is as it was"
        ) from exc
