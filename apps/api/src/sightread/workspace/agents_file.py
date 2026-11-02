"""The AGENTS.md files on a document's path (docs/chat.md § AGENTS.md): the reader's standing
instructions for the documents of a folder and the folders inside it, read from the tree —
`files/AGENTS.md`, then one per folder down to the document's own — and rendered as the
body a conversation snapshots at its first turn and after a compaction.

Each one is a file of the library like any other (docs/workspace.md § Files): the agent
writes it with its editor tool, the reader sees it on the Files page.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import Settings
from . import safe_io
from .layout import FILES_DIR, user_root
from .locks import workspace_lock

AGENTS_FILE = "AGENTS.md"
VM_ROOT = Path("/workspace")
UNREADABLE = "[This file could not be read as text; it is left out.]"


def agents_file_chain(root: Path, folder_dir: Path, max_bytes: int) -> list[tuple[Path, str]]:
    """Every AGENTS.md on the way from `files/` to `folder_dir` — the folder the document
    sits in, relative to the root (`files/<folders…>`; `files` itself for a document at
    the top of the library, or for the root file alone) — the outermost first, as `(path
    relative to the root, text)`; a file that is not plain UTF-8 text or is larger than the
    API opens has `UNREADABLE` as its text. Nothing inside a document's directory is read:
    the caller passes the document directory's parent."""
    parts = folder_dir.parts
    if not parts or parts[0] != FILES_DIR:
        return []
    chain: list[tuple[Path, str]] = []
    for depth in range(1, len(parts) + 1):
        relative = Path(*parts[:depth]) / AGENTS_FILE
        # A directory or a link of that name is not the file: nothing is said of it.
        if safe_io.entry_kind(root, root / relative) != "file":
            continue
        try:
            text = safe_io.read_text(root, root / relative, max_bytes)
        except (safe_io.EntryTooLarge, safe_io.UnsafeEntry, UnicodeDecodeError, OSError):
            text = UNREADABLE
        if text is None:
            continue
        chain.append((relative, text))
    return chain


def render_chain(chain: list[tuple[Path, str]]) -> str:
    """The body a conversation stores: each file under a heading naming its path in the VM,
    the outermost first; "" when there is none."""
    return "\n\n".join(
        f"## {VM_ROOT / relative.as_posix()}\n\n{text.strip()}" for relative, text in chain
    )


async def agents_file_text(settings: Settings, user_id: int, folder_dir: Path) -> str:
    """The rendered chain as it is now, read off the thread under the workspace lock — a
    shell command of another conversation writing one of the files holds that lock for the
    command's whole run, so no file is read half-written. The caller holds no database row
    lock, the order every workspace mutation keeps (docs/workspace.md § Revisions). A
    standing writing marker is a dead worker's and refuses no read; a workspace that does
    not exist yet has no files."""
    root = user_root(settings.workspaces_dir, user_id)
    if not root.is_dir():
        return ""
    async with workspace_lock(root, recovering=True):
        chain = await asyncio.to_thread(
            agents_file_chain, root, folder_dir, settings.content_max_bytes
        )
    return render_chain(chain)
