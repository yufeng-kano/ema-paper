"""The tree is the truth (docs/workspace.md § The tree is the truth).

After a chat turn the workspace may differ from the library: a shell command moved a
document directory, renamed a folder, or created one. Reconciliation reads `.meta.json`
under `files/` and updates the rows to match. It never deletes a row: a document whose
directory vanished is materialized again on its next read, and deleting is done from the
page, where it is deliberate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Document, Folder
from . import git
from .layout import (
    META_FILE,
    files_root,
    is_canonical_segment,
    path_segment,
    segment_name,
    user_root,
)
from .locks import workspace_lock

logger = logging.getLogger(__name__)

# A document's metadata is a few hundred bytes; anything larger is not ours.
META_MAX_BYTES = 65_536


@dataclass(frozen=True)
class Found:
    document_id: int
    folder_segments: tuple[str, ...]
    name: str


def _canonicalize(root: Path) -> int:
    """Move every directory under files/ whose name is not what `path_segment` would make
    of it — `.report`, `_report` — to the segment that name encodes to, so the row the
    scan records finds its directory again (docs/workspace.md § The tree is the truth).
    One commit for all of them; returns how many moved."""
    base = files_root(root)
    if not base.is_dir():
        return 0
    moved = 0
    for current, dirs, _files in os.walk(base, topdown=True, followlinks=False):
        kept: list[str] = []
        for name in dirs:
            source = Path(current) / name
            if source.is_symlink():
                continue  # never descended into, never recorded
            if is_canonical_segment(name):
                kept.append(name)
                continue
            target = Path(current) / path_segment(name)
            if target.exists():
                logger.warning("reconcile: %s stays, %s is taken", source, target)
                continue  # left alone, and not descended into: its rows stay as they were
            git.move(root, source, target)
            moved += 1
            kept.append(target.name)
        dirs[:] = kept
    if moved:
        git.commit_all(root, git.USER_AUTHOR, "canonicalize names from the shell")
    return moved


def _scan(root: Path) -> list[Found]:
    """Every document directory under files/, by the id its `.meta.json` carries. Symlinks
    are not followed and not read: the tree the host trusts is made of directories."""
    base = files_root(root)
    found: list[Found] = []
    if not base.is_dir():
        return found
    for current, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not (Path(current) / name).is_symlink() and is_canonical_segment(name)
        ]
        if META_FILE not in files:
            continue
        meta = Path(current) / META_FILE
        try:
            # A plain, small file or nothing: the shell can put anything under that name.
            info = os.lstat(meta)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > META_MAX_BYTES:
            continue
        try:
            payload = json.loads(meta.read_text())
            document_id = int(payload["document_id"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        parts = meta.parent.relative_to(base).parts
        if not parts:
            continue
        found.append(Found(document_id, tuple(parts[:-1]), parts[-1]))
    return found


def _canonicalize_and_scan(root: Path) -> list[Found]:
    git.ensure_repo(root)
    _canonicalize(root)
    return _scan(root)


async def _folder_for(
    db: AsyncSession, user_id: int, segments: tuple[str, ...], cache: dict
) -> int | None:
    """The folder row at this path, created when the shell made the directory."""
    parent: int | None = None
    for segment in segments:
        key = (parent, segment)
        if key in cache:
            parent = cache[key]
            continue
        rows = (
            (
                await db.execute(
                    select(Folder).where(
                        Folder.user_id == user_id,
                        Folder.parent_id.is_(None)
                        if parent is None
                        else Folder.parent_id == parent,
                    )
                )
            )
            .scalars()
            .all()
        )
        match = next((row for row in rows if path_segment(row.name) == segment), None)
        if match is None:
            match = Folder(user_id=user_id, parent_id=parent, name=segment_name(segment))
            db.add(match)
            await db.flush()
        cache[key] = match.id
        parent = match.id
    return parent


async def reconcile_user(db: AsyncSession, settings: Settings, user_id: int) -> int:
    """Bring the user's rows in line with their tree. Returns how many rows changed."""
    root = user_root(settings.workspaces_dir, user_id)
    if not files_root(root).is_dir():
        return 0
    # Held through the commit: a rename on the Files page holds the same lock across its
    # row write and its move, so neither can apply a path the other just changed.
    async with workspace_lock(root):
        return await _reconcile_locked(db, user_id, root)


async def _reconcile_locked(db: AsyncSession, user_id: int, root: Path) -> int:
    found = await asyncio.to_thread(_canonicalize_and_scan, root)
    if not found:
        return 0
    documents = {
        row.id: row
        for row in (await db.execute(select(Document).where(Document.user_id == user_id))).scalars()
    }
    cache: dict = {}
    changed = 0
    for entry in found:
        row = documents.get(entry.document_id)
        if row is None:
            continue
        folder_id = await _folder_for(db, user_id, entry.folder_segments, cache)
        # The row's name when the segment is still its encoding; otherwise the name the
        # (now canonical) segment decodes to — what the shell typed.
        name = row.name if path_segment(row.name) == entry.name else segment_name(entry.name)
        if row.folder_id != folder_id or row.name != name:
            row.folder_id = folder_id
            row.name = name[:512]
            changed += 1
    if changed:
        try:
            await db.commit()
        except IntegrityError:
            # A unique clash from the shell is reported, never fatal.
            await db.rollback()
            logger.warning("reconcile for user %d hit a name conflict; rows unchanged", user_id)
            return 0
    return changed
