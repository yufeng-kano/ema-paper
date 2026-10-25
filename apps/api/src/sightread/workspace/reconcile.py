"""The tree is the truth (docs/workspace.md § The tree is the truth).

After a chat turn the workspace may differ from the library: a shell command moved a
document directory, renamed a folder, or created one. Reconciliation reads `.meta.json`
under `files/` and updates the rows to match. It never deletes a row: a document whose
directory vanished is materialized again on its next read, and deleting is done from the
page, where it is deliberate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import FILE_KIND, Document, Folder, User
from . import git, safe_io
from .documents import (
    ORIGIN_SUFFIX,
    aside_origin,
    file_media_type,
    folder_names,
    recover_trash,
    sweep_aside,
    sweep_orphaned_images,
    sweep_orphaned_originals,
    trashed_folder_ids,
    user_folders,
)
from .layout import (
    FILES_DIR,
    META_FILE,
    META_MAX_BYTES,
    document_relative_dir,
    files_root,
    is_canonical_segment,
    path_segment,
    segment_name,
    user_root,
)
from .locks import workspace_lock

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Found:
    document_id: int
    folder_segments: tuple[str, ...]
    name: str


@dataclass(frozen=True)
class FoundFile:
    """A plain file directly in a folder — a file of the library (docs/workspace.md § Files):
    where it sits, its canonical name and its size."""

    folder_segments: tuple[str, ...]
    name: str
    size: int


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
    for current, dirs, files in _walk(root, Path(FILES_DIR)):
        if META_FILE in files:
            # A document's directory: what a command made inside it is the document's
            # own, under the names the command chose (docs/workspace.md).
            dirs[:] = []
            continue
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
        for name in files:
            # A file of the library under a name the shell gave it, by the same rule — a
            # write in progress (`.sr-tmp-*`) is nobody's yet and stays (docs/workspace.md
            # § Files).
            if name.startswith(safe_io.TEMP_PREFIX) or is_canonical_segment(name):
                continue
            source = current / name
            target = current / path_segment(name)
            if safe_io.entry_kind(root, root / target) != "none":
                logger.warning("reconcile: %s stays, %s is taken", source, target)
                continue
            try:
                git.move(root, root / source, root / target)
            except (safe_io.UnsafeEntry, OSError):
                logger.warning("reconcile: %s could not be moved; left alone", source)
                continue
            moved += [source, target]
    if moved:
        # Only the moved paths: a shell may be running, with its own dirt in the tree.
        try:
            git.commit_paths(root, git.USER_AUTHOR, "canonicalize names from the shell", moved)
        except git.GitError:
            # Back under the names the shell gave: a move without its revision would
            # strand the directory beside a row that still names the old path. The
            # rows stay as they were; the next reconciliation tries again.
            logger.exception("reconcile: canonicalization could not be recorded; undone")
            for source, target in reversed(list(zip(moved[::2], moved[1::2], strict=True))):
                with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                    safe_io.rename(root, root / target, root / source)
            return 0
    return len(moved) // 2


def _scan(root: Path) -> tuple[list[Found], list[tuple[str, ...]], list[FoundFile]]:
    """Every document directory under files/, by the id its `.meta.json` carries; every
    other directory — a folder, empty or not, which gets a row of its own whether or not a
    document sits in it; and every plain file directly in a folder, a file of the library
    (docs/workspace.md § Files). Symlinks are not followed and not read: the tree the
    host trusts is made of directories and plain files. A write in progress (`.sr-tmp-*`)
    is nobody's yet."""
    found: list[Found] = []
    folders: list[tuple[str, ...]] = []
    plain: list[FoundFile] = []
    for current, dirs, files in _walk(root, Path(FILES_DIR)):
        dirs[:] = [name for name in dirs if is_canonical_segment(name)]
        if META_FILE not in files:
            parts = current.relative_to(FILES_DIR).parts
            if parts:
                folders.append(parts)
            for name in files:
                if name.startswith(safe_io.TEMP_PREFIX) or not is_canonical_segment(name):
                    continue
                size = safe_io.file_size(root, root / current / name)
                if size is not None:
                    plain.append(FoundFile(parts, name, size))
            continue
        dirs[:] = []  # nothing beneath a document is a folder or a document
        document_id = _meta_id(root, current)
        if document_id is None:
            continue
        parts = current.relative_to(FILES_DIR).parts
        if not parts:
            continue
        found.append(Found(document_id, tuple(parts[:-1]), parts[-1]))
    return found, folders, plain


def _meta_id(root: Path, directory: Path) -> int | None:
    """The document id a directory's `.meta.json` names, or None: a plain, small file or
    nothing — the shell can put anything under that name — and the open follows nothing
    (docs/workspace.md § Revisions)."""
    try:
        data = safe_io.read_bytes(root, root / directory / META_FILE, META_MAX_BYTES)
        return int(json.loads(data or b"")["document_id"])
    except (
        safe_io.UnsafeEntry,
        safe_io.EntryTooLarge,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        OverflowError,  # an id of 1e400
        RecursionError,  # arrays nested deeper than the decoder goes
    ):
        return None


def _aside_documents(root: Path) -> dict[str, list[tuple[Path, int]]]:
    """What a delete set aside and a crash left: each aside directory with the document
    directories inside it and their ids. A document whose row survived the crash (the
    removal was committed to git, the rows' delete was not) is restored by
    reconciliation rather than swept (docs/workspace.md § Revisions)."""
    asides: dict[str, list[tuple[Path, int]]] = {}
    try:
        entries = safe_io.list_dir(root, root)
    except (safe_io.UnsafeEntry, OSError):
        return asides
    for name, is_dir in entries:
        if not name.startswith(safe_io.TEMP_PREFIX) or name.endswith(ORIGIN_SUFFIX):
            continue
        inside: list[tuple[Path, int]] = []
        # A plain file aside is a library file a delete moved out of the tree: nothing
        # inside it, its origin beside it says where it stood (docs/workspace.md § Files).
        for current, dirs, files in _walk(root, Path(name)) if is_dir else ():
            if META_FILE not in files:
                continue
            dirs[:] = []
            document_id = _meta_id(root, current)
            if document_id is not None:
                inside.append((current, document_id))
        asides[name] = inside
    return asides


def _restore_aside(root: Path, aside_dir: Path, target: Path) -> bool:
    """Move a document a delete set aside back to where its surviving row says, as a
    revision; False when that place is taken (a fresh copy was materialized there) or
    the move cannot be recorded, in which case the directory stays aside."""
    if safe_io.entry_kind(root, root / target) != "none":
        logger.warning("reconcile: %s stays aside, %s is taken", aside_dir, target)
        return False
    try:
        safe_io.ensure_dir(root, root / target.parent)
        safe_io.rename(root, root / aside_dir, root / target)
    except (safe_io.UnsafeEntry, OSError):
        logger.exception("reconcile: %s could not be restored to %s", aside_dir, target)
        return False
    try:
        git.commit_paths(root, git.USER_AUTHOR, f"restore {target}", [target])
    except git.GitError:
        logger.exception("reconcile: the restore of %s could not be recorded; undone", target)
        with contextlib.suppress(OSError, safe_io.UnsafeEntry):
            safe_io.rename(root, root / target, root / aside_dir)
        return False
    return True


async def _row_names(db: AsyncSession, user_id: int, origin: Path) -> bool:
    """Whether a live folder or document row still places itself at `origin` (`files/…`):
    what decides that an aside holding no document metadata goes back there rather than
    being swept (docs/workspace.md § Revisions). A row in the Trash places nothing under
    `files/`."""
    segments = origin.parts[1:]
    if origin.parts[:1] != (FILES_DIR,) or not segments:
        return False
    parent: int | None = None
    for index, segment in enumerate(segments):
        at_parent = Folder.parent_id.is_(None) if parent is None else Folder.parent_id == parent
        folders = (
            await db.execute(
                select(Folder.id, Folder.name).where(
                    Folder.user_id == user_id, at_parent, Folder.trashed_at.is_(None)
                )
            )
        ).all()
        match = next((row.id for row in folders if path_segment(row.name) == segment), None)
        if match is not None:
            parent = match
            continue
        if index != len(segments) - 1:
            return False
        in_folder = Document.folder_id.is_(None) if parent is None else Document.folder_id == parent
        names = (
            await db.execute(
                select(Document.name).where(
                    Document.user_id == user_id, in_folder, Document.trashed_at.is_(None)
                )
            )
        ).scalars()
        return any(path_segment(name) == segment for name in names)
    return True


def _aside_origin(aside: Path, targets: dict[Path, Path]) -> Path | None:
    """Where an aside directory stood, as its surviving documents place it: each one sat
    `inner` below the aside and belongs at `target`, so the aside stood where `target`
    minus `inner` says; None unless every document agrees and that is under `files/`."""
    origins: set[Path] = set()
    for aside_dir, target in targets.items():
        inner = aside_dir.relative_to(aside)
        depth = len(target.parts) - len(inner.parts)
        if depth < 2:
            return None
        origins.add(Path(*target.parts[:depth]))
    origin = origins.pop() if len(origins) == 1 else None
    return origin if origin is not None and origin.parts[0] == FILES_DIR else None


def _canonicalize_and_scan(
    root: Path,
) -> tuple[list[Found], list[tuple[str, ...]], list[FoundFile]]:
    git.ensure_repo(root)
    _canonicalize(root)
    return _scan(root)


class NestedInDocument(Exception):
    """The path runs through a document's directory: no folder row may share it."""


async def _folder_for(
    db: AsyncSession, user_id: int, segments: tuple[str, ...], cache: dict, created: list[int]
) -> int | None:
    """The folder row at this path, created when the shell made the directory — unless
    a segment is a document's: a folder under a document's name would share its
    directory (docs/workspace.md § The tree is the truth). Live rows only: a folder in
    the Trash is matched to no directory, so one the shell makes under its name gets a
    row of its own (§ The Trash). `created` collects the ids of the rows made here."""
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
                        Document.trashed_at.is_(None),
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
                        Folder.trashed_at.is_(None),
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
            created.append(match.id)
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
    # What an interrupted move to or from the Trash left goes where its row says first,
    # so the scan below finds it there (docs/workspace.md § The Trash).
    restored = await recover_trash(db, settings, user_id)
    if not files_root(root).is_dir():
        return restored
    return restored + await _reconcile_locked(db, user_id, root)


async def _reconcile_locked(db: AsyncSession, user_id: int, root: Path) -> int:
    # The originals and images of rows a committed delete removed before its cleanup ran go
    # first: every writer of either holds this lock (docs/workspace.md § Revisions).
    live_ids = set(
        (await db.execute(select(Document.id).where(Document.user_id == user_id))).scalars()
    )
    swept_originals = await asyncio.to_thread(sweep_orphaned_originals, root, live_ids)
    if swept_originals:
        logger.warning(
            "reconcile for user %d: removed %d original(s) whose document rows are gone",
            user_id,
            swept_originals,
        )
    swept_images = await asyncio.to_thread(sweep_orphaned_images, root, live_ids)
    if swept_images:
        logger.warning(
            "reconcile for user %d: removed the images of %d document(s) whose rows are gone",
            user_id,
            swept_images,
        )
    found, folders, plain = await asyncio.to_thread(_canonicalize_and_scan, root)
    asides = await asyncio.to_thread(_aside_documents, root)
    # A file row whose file is gone goes with it (docs/workspace.md § Files): while any
    # stands, the tree is compared to the rows even when it holds nothing.
    file_rows = await db.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            Document.user_id == user_id,
            Document.kind == FILE_KIND,
            Document.trashed_at.is_(None),
        )
    )
    if not found and not folders and not asides and not plain and not file_rows:
        # Nothing to record and nothing aside: nothing to do, under the same lock a
        # delete holds, so none in flight is touched.
        await asyncio.to_thread(sweep_aside, root)
        return 0
    # The user's row, under the workspace lock — the order every mutation takes: a
    # folder row added here shares the namespace the Files page's name decisions lock
    # (docs/workspace.md § Revisions).
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    # Live rows only: a `.meta.json` under `files/` that names a document in the Trash —
    # copied out of the read-only Trash, brought back by a revert — is not that document,
    # and its row stays in the Trash (docs/workspace.md § The Trash).
    trashed = trashed_folder_ids((await user_folders(db, user_id)).values())
    documents = {
        row.id: row
        for row in (await db.execute(select(Document).where(Document.user_id == user_id))).scalars()
        if row.trashed_at is None and (row.folder_id is None or row.folder_id not in trashed)
    }
    # A document set aside by a delete whose rows survived (the process died between the
    # removal's revision and the rows' commit) goes back where its row says; an aside
    # that still holds one that could not go back is kept, the rest are swept.
    keep: set[str] = set()
    restored = 0
    for name, inside in asides.items():
        targets = {}
        for aside_dir, document_id in inside:
            row = documents.get(document_id)
            if row is not None:
                targets[aside_dir] = document_relative_dir(
                    await folder_names(db, user_id, row.folder_id), row.name
                )
        if not targets:
            # No document metadata inside — a folder of shell-made files, a document whose
            # `.meta.json` a command removed: the origin written beside the aside says
            # where it stood, and a row that still names that path brings it back whole
            # (docs/workspace.md § Revisions). Without one, the sweep takes it.
            origin = await asyncio.to_thread(aside_origin, root, name)
            if origin is not None and await _row_names(db, user_id, origin):
                if await asyncio.to_thread(_restore_aside, root, Path(name), origin):
                    restored += 1
                else:
                    keep.add(name)
            continue
        # The aside is a folder's directory whole when its surviving rows all place it at
        # one path: it goes back whole, with whatever else the folder held — files and
        # directories the shell made, which no row names — rather than document by
        # document with the rest swept (docs/workspace.md § Revisions). Only when every
        # document in it still has its row: a document deleted since must not come back
        # under `files/` with the folder, readable from the shell and named by nothing.
        whole = _aside_origin(Path(name), targets) if len(targets) == len(inside) else None
        if whole is not None and await asyncio.to_thread(_restore_aside, root, Path(name), whole):
            restored += 1
            continue
        for aside_dir, target in targets.items():
            if await asyncio.to_thread(_restore_aside, root, aside_dir, target):
                restored += 1
            else:
                keep.add(name)
    await asyncio.to_thread(sweep_aside, root, keep)
    # One id in two places — `cp -r` carries `.meta.json` along — names no winner: the
    # row stays where it is, and the copies are the shell's to sort out.
    live_files = {row.id: row for row in documents.values() if row.kind == FILE_KIND}
    seen: dict[int, int] = {}
    for entry in found:
        seen[entry.document_id] = seen.get(entry.document_id, 0) + 1
    cache: dict = {}
    created: list[int] = []
    # The names the folder rows hold at each parent, as segments: a document may not
    # take a sibling folder's name, as on disk the two would be one directory.
    folder_segments: dict[int | None, set[str]] = {}

    async def taken_by_folders(parent: int | None) -> set[str]:
        if parent not in folder_segments:
            rows = await db.execute(
                select(Folder.name).where(
                    Folder.user_id == user_id,
                    Folder.parent_id.is_(None) if parent is None else Folder.parent_id == parent,
                    Folder.trashed_at.is_(None),
                )
            )
            folder_segments[parent] = {path_segment(name) for name in rows.scalars()}
        return folder_segments[parent]

    # Every directory that is not a document gets its row, a document in it or not: a
    # `mkdir` from the shell shows on the Files page (docs/workspace.md).
    for segments in folders:
        try:
            await _folder_for(db, user_id, segments, cache, created)
        except NestedInDocument as nested:
            logger.warning(
                "reconcile for user %d: directory %s sits under document directory %s; "
                "not recorded",
                user_id,
                "/".join(segments),
                nested,
            )
    # The names the live document rows hold at each parent, as segments: a plain file
    # under a document's name sits where its directory belongs, and is not a file of the
    # library (docs/workspace.md § Files).
    document_segments: dict[int | None, set[str]] = {}

    async def taken_by_documents(parent: int | None) -> set[str]:
        if parent not in document_segments:
            rows = await db.execute(
                select(Document.name).where(
                    Document.user_id == user_id,
                    Document.folder_id.is_(None)
                    if parent is None
                    else Document.folder_id == parent,
                    Document.trashed_at.is_(None),
                    Document.kind != FILE_KIND,
                )
            )
            document_segments[parent] = {path_segment(name) for name in rows.scalars()}
        return document_segments[parent]

    # A plain file directly in a folder is a file of the library (docs/workspace.md
    # § Files): its row is found by its place and name — a file carries no metadata with
    # an id — made when there is none, its size refreshed when there is one, and removed
    # when the file is gone, the one row reconciliation deletes.
    by_place = {(row.folder_id, path_segment(row.name)): row for row in live_files.values()}
    present: set[int] = set()
    file_changes = 0
    for plain_file in plain:
        where = "/".join((*plain_file.folder_segments, plain_file.name))
        try:
            folder_id = await _folder_for(db, user_id, plain_file.folder_segments, cache, created)
        except NestedInDocument as nested:
            logger.warning(
                "reconcile for user %d: file %s sits under document directory %s; not recorded",
                user_id,
                where,
                nested,
            )
            continue
        row = by_place.get((folder_id, plain_file.name))
        if row is not None:
            present.add(row.id)
            if row.size_bytes != plain_file.size:
                row.size_bytes = plain_file.size
                file_changes += 1
            continue
        if plain_file.name in await taken_by_folders(folder_id) or plain_file.name in (
            await taken_by_documents(folder_id)
        ):
            logger.warning(
                "reconcile for user %d: file %s took a name a row holds there; not recorded",
                user_id,
                where,
            )
            continue
        db.add(
            Document(
                user_id=user_id,
                folder_id=folder_id,
                job_id=None,
                kind=FILE_KIND,
                name=segment_name(plain_file.name)[:512],
                media_type=file_media_type(plain_file.name),
                size_bytes=plain_file.size,
            )
        )
        file_changes += 1
    for row in live_files.values():
        if row.id not in present:
            await db.delete(row)
            file_changes += 1
    updates: list[tuple[Document, int | None, str]] = []
    for entry in found:
        row = documents.get(entry.document_id)
        if row is None or row.kind == FILE_KIND:
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
            folder_id = await _folder_for(db, user_id, entry.folder_segments, cache, created)
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
        if row.folder_id == folder_id and row.name == name:
            continue
        if entry.name in await taken_by_folders(folder_id):
            logger.warning(
                "reconcile for user %d: document %d took a folder's name %s; row unchanged",
                user_id,
                entry.document_id,
                entry.name,
            )
            continue
        updates.append((row, folder_id, name[:512]))
    if not updates:
        if created or file_changes:
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                logger.warning("reconcile for user %d hit a name conflict; rows unchanged", user_id)
                return restored
        return len(created) + restored + file_changes
    # Two phases, so rows that trade places (a shell swapped two directories) never
    # meet the unique constraint on their way: every changing row first takes a name
    # nothing else holds, then its own (docs/workspace.md § The tree is the truth).
    for row, _folder_id, _name in updates:
        row.name = f"~{uuid.uuid4().hex}"
    try:
        await db.flush()
        for row, folder_id, name in updates:
            row.folder_id = folder_id
            row.name = name
        await db.commit()
    except IntegrityError:
        # A unique clash from the shell is reported, never fatal.
        await db.rollback()
        logger.warning("reconcile for user %d hit a name conflict; rows unchanged", user_id)
        return restored
    return len(updates) + len(created) + restored + file_changes
