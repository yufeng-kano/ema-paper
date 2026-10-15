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
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Document, Folder, User
from . import git, safe_io
from .layout import (
    FILES_DIR,
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


def _walk(root: Path, relative: Path):
    """Every directory under `relative`, top down, as `(relative dir, subdir names,
    file names)` — read through descriptors opened beneath the user root with nothing
    followed (`safe_io.list_dir`): a `files/` the shell replaced with a link, or any
    directory under it, is an empty tree to this walk, never another user's. A caller
    may trim the subdirectory list in place to prune the walk."""
    # An explicit stack: a shell can nest directories past the interpreter's limit.
    stack = [relative]
    while stack:
        current = stack.pop()
        try:
            entries = safe_io.list_dir(root, root / current)
        except (safe_io.UnsafeEntry, OSError):
            continue
        subdirs = sorted(name for name, is_dir in entries if is_dir)
        files = [name for name, is_dir in entries if not is_dir]
        yield current, subdirs, files
        stack.extend(current / name for name in reversed(subdirs))


def _canonicalize(root: Path) -> int:
    """Move every directory under files/ whose name is not what `path_segment` would make
    of it — `.report`, `_report` — to the segment that name encodes to, so the row the
    scan records finds its directory again (docs/workspace.md § The tree is the truth).
    One commit for all of them; returns how many moved."""
    moved: list[Path] = []
    for current, dirs, _files in _walk(root, Path(FILES_DIR)):
        kept: list[str] = []
        for name in dirs:
            source = current / name
            if is_canonical_segment(name):
                kept.append(name)
                continue
            target = current / path_segment(name)
            if safe_io.entry_kind(root, root / target) != "none":
                logger.warning("reconcile: %s stays, %s is taken", source, target)
                continue  # left alone, and not descended into: its rows stay as they were
            try:
                git.move(root, root / source, root / target)
            except safe_io.UnsafeEntry:
                logger.warning("reconcile: %s could not be moved; left alone", source)
                continue
            moved += [source, target]
            kept.append(target.name)
        dirs[:] = kept
    if moved:
        # Only the moved paths: a shell may be running, with its own dirt in the tree.
        git.commit_paths(root, git.USER_AUTHOR, "canonicalize names from the shell", moved)
    return len(moved) // 2


def _scan(root: Path) -> list[Found]:
    """Every document directory under files/, by the id its `.meta.json` carries. Symlinks
    are not followed and not read: the tree the host trusts is made of directories."""
    found: list[Found] = []
    for current, dirs, files in _walk(root, Path(FILES_DIR)):
        dirs[:] = [name for name in dirs if is_canonical_segment(name)]
        if META_FILE not in files:
            continue
        meta = root / current / META_FILE
        try:
            # A plain, small file or nothing: the shell can put anything under that name,
            # and the open follows nothing (docs/workspace.md § Revisions).
            data = safe_io.read_bytes(root, meta, META_MAX_BYTES)
            payload = json.loads(data or b"")
            document_id = int(payload["document_id"])
        except (
            safe_io.UnsafeEntry,
            safe_io.EntryTooLarge,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ):
            continue
        parts = current.relative_to(FILES_DIR).parts
        if not parts:
            continue
        found.append(Found(document_id, tuple(parts[:-1]), parts[-1]))
    return found


def _canonicalize_and_scan(root: Path) -> list[Found]:
    git.ensure_repo(root)
    _canonicalize(root)
    return _scan(root)


class NestedInDocument(Exception):
    """The path runs through a document's directory: no folder row may share it."""


async def _folder_for(
    db: AsyncSession, user_id: int, segments: tuple[str, ...], cache: dict
) -> int | None:
    """The folder row at this path, created when the shell made the directory — unless
    a segment is a document's: a folder under a document's name would share its
    directory (docs/workspace.md § The tree is the truth)."""
    parent: int | None = None
    for segment in segments:
        key = (parent, segment)
        if key in cache:
            parent = cache[key]
            continue
        taken = (
            (
                await db.execute(
                    select(Document.name).where(
                        Document.user_id == user_id,
                        Document.folder_id.is_(None)
                        if parent is None
                        else Document.folder_id == parent,
                    )
                )
            )
            .scalars()
            .all()
        )
        if any(path_segment(name) == segment for name in taken):
            raise NestedInDocument(segment)
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
    # Held through the commit: a rename on the Files page holds the same lock across its
    # row write and its move, so neither can apply a path the other just changed.
    async with workspace_lock(root):
        return await reconcile_locked(db, settings, user_id)


async def reconcile_locked(db: AsyncSession, settings: Settings, user_id: int) -> int:
    """`reconcile_user` for a caller that already holds the workspace lock."""
    root = user_root(settings.workspaces_dir, user_id)
    if not files_root(root).is_dir():
        return 0
    return await _reconcile_locked(db, user_id, root)


async def _reconcile_locked(db: AsyncSession, user_id: int, root: Path) -> int:
    found = await asyncio.to_thread(_canonicalize_and_scan, root)
    if not found:
        return 0
    # The user's row, under the workspace lock — the order every mutation takes: a
    # folder row added here shares the namespace the Files page's name decisions lock
    # (docs/workspace.md § Revisions).
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    documents = {
        row.id: row
        for row in (await db.execute(select(Document).where(Document.user_id == user_id))).scalars()
    }
    # One id in two places — `cp -r` carries `.meta.json` along — names no winner: the
    # row stays where it is, and the copies are the shell's to sort out.
    seen: dict[int, int] = {}
    for entry in found:
        seen[entry.document_id] = seen.get(entry.document_id, 0) + 1
    cache: dict = {}
    changed = 0
    for entry in found:
        row = documents.get(entry.document_id)
        if row is None:
            continue
        if seen[entry.document_id] > 1:
            logger.warning(
                "reconcile for user %d: document %d is in %d places; row unchanged",
                user_id,
                entry.document_id,
                seen[entry.document_id],
            )
            continue
        try:
            folder_id = await _folder_for(db, user_id, entry.folder_segments, cache)
        except NestedInDocument as nested:
            logger.warning(
                "reconcile for user %d: document %d sits under document directory %s; "
                "row unchanged",
                user_id,
                entry.document_id,
                nested,
            )
            continue
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
