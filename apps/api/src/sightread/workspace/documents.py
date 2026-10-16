"""A document's files in the user's workspace (docs/workspace.md).

Materialization writes what is missing and never what exists; every write commits a
revision of the paths it wrote; every write happens under the user's workspace lock.
Paths come from the library's names through `layout.py`, never from request text, and
every file is opened through `safe_io`, which follows nothing the shell could have planted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Document, Folder, Job, Result, utcnow
from ..errors import ApiError
from . import git, safe_io
from .layout import (
    DOC_FILE,
    FIGURES_DIR,
    META_FILE,
    NOTES_FILE,
    SOURCE_STEM,
    document_relative_dir,
    files_root,
    git_dir,
    results_root,
    sources_dir,
    user_root,
)
from .locks import workspace_lock

logger = logging.getLogger(__name__)

NOT_PLAIN = "The document's files are not regular files"
TOO_LARGE = "The document file is too large to open"


@dataclass(frozen=True)
class DocumentPaths:
    root: Path
    relative: Path  # files/<folders…>/<name>

    @property
    def directory(self) -> Path:
        return self.root / self.relative

    @property
    def doc(self) -> Path:
        return self.directory / DOC_FILE

    @property
    def notes(self) -> Path:
        return self.directory / NOTES_FILE

    @property
    def meta(self) -> Path:
        return self.directory / META_FILE

    @property
    def figures(self) -> Path:
        return self.directory / FIGURES_DIR

    def source(self) -> Path | None:
        """`source.<ext>` when the original upload was kept, as a plain file."""
        try:
            names = sorted(name for name, is_dir in safe_io.list_dir(self.root, self.directory))
        except (safe_io.UnsafeEntry, OSError):
            return None
        for name in names:
            candidate = self.directory / name
            if (
                name.startswith(f"{SOURCE_STEM}.")
                and safe_io.entry_kind(self.root, candidate) == "file"
            ):
                return candidate
        return None

    def check(self) -> None:
        """Refuse the directory when the shell turned it, or any fixed file in it, into
        something the host must not open as itself — a symlink, a FIFO, a device, a file
        where a directory belongs or the reverse (docs/workspace.md § Revisions). What
        is checked here is checked again at the open, through the descriptor."""
        expected = (
            (self.directory, "dir"),
            (self.figures, "dir"),
            (self.doc, "file"),
            (self.notes, "file"),
            (self.meta, "file"),
        )
        for path, kind in expected:
            if safe_io.entry_kind(self.root, path) not in (kind, "none"):
                raise ApiError(409, "invalid_request", NOT_PLAIN)

    @property
    def doc_relative(self) -> Path:
        return self.relative / DOC_FILE

    @property
    def notes_relative(self) -> Path:
        return self.relative / NOTES_FILE


@dataclass(frozen=True)
class Content:
    markdown: str
    revision: str
    notes: str
    notes_revision: str
    has_source: bool


def revision_of(text: str) -> str:
    """The revision a client saves against: a hash of the content, not a commit id."""
    return hashlib.sha256(text.encode()).hexdigest()


def _read_text(root: Path, path: Path, max_bytes: int) -> str:
    """The file's text, "" when there is none; 409 when it is not a plain file or is
    larger than the API opens (docs/workspace.md § Revisions)."""
    try:
        return safe_io.read_text(root, path, max_bytes) or ""
    except safe_io.EntryTooLarge as exc:
        raise ApiError(409, "invalid_request", TOO_LARGE) from exc
    except UnicodeDecodeError as exc:
        raise ApiError(409, "invalid_request", "The document file is not UTF-8 text") from exc
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc


def _write_text(root: Path, path: Path, text: str) -> None:
    try:
        safe_io.write_text(root, path, text)
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc


# --- where a document lives ---------------------------------------------------------------


async def folder_names(db: AsyncSession, user_id: int, folder_id: int | None) -> list[str]:
    """The folder chain from the root down to `folder_id`, as names."""
    if folder_id is None:
        return []
    rows = (await db.execute(select(Folder).where(Folder.user_id == user_id))).scalars().all()
    by_id = {row.id: row for row in rows}
    chain: list[str] = []
    cursor = by_id.get(folder_id)
    seen: set[int] = set()
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        chain.append(cursor.name)
        cursor = by_id.get(cursor.parent_id) if cursor.parent_id is not None else None
    chain.reverse()
    return chain


async def document_paths(db: AsyncSession, settings: Settings, document: Document) -> DocumentPaths:
    root = user_root(settings.workspaces_dir, document.user_id)
    names = await folder_names(db, document.user_id, document.folder_id)
    return DocumentPaths(root=root, relative=document_relative_dir(names, document.name))


# --- materialization ----------------------------------------------------------------------


def _write_if_missing(root: Path, path: Path, text: str) -> bool:
    if safe_io.entry_kind(root, path) != "none":
        return False
    try:
        safe_io.write_text(root, path, text, exist_ok=False)
    except FileExistsError:
        return False
    return True


def _materialize_files(
    paths: DocumentPaths,
    *,
    document_id: int,
    job: Job,
    result: Result,
    figures_dir: Path,
) -> list[Path]:
    """Write what is missing. Returns the tracked files written (they need a commit)."""
    git.ensure_repo(paths.root)
    paths.check()
    try:
        safe_io.ensure_dir(paths.root, paths.directory)
    except safe_io.UnsafeEntry as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    written: list[Path] = []
    if _write_if_missing(paths.root, paths.doc, result.markdown):
        written.append(paths.doc)
    if _write_if_missing(paths.root, paths.notes, ""):
        written.append(paths.notes)
    meta = {
        "document_id": document_id,
        "job_id": str(job.id),
        "model": job.model,
        "bbox_format": job.bbox_format,
        "page_count": job.page_count,
        "kind": job.kind,
    }
    if _write_if_missing(paths.root, paths.meta, json.dumps(meta, indent=2) + "\n"):
        written.append(paths.meta)
    parse = results_root(paths.root) / str(job.id) / "result.md"
    _write_if_missing(paths.root, parse, result.markdown)  # excluded from history
    original = original_of(paths.root, document_id)
    if original is not None and paths.source() is None:
        # The shell's copy, made again from the original when a command removed it.
        _copy_source(paths.root, paths.directory, original)  # excluded from history
    crops = figures_dir / str(job.id)
    if crops.is_dir():
        # The crops directory is the service's own; the document directory is shared with
        # the shell, so each crop lands through a descriptor like every workspace file.
        # Each missing one, every time: a copy that failed halfway is finished later.
        for crop in crops.iterdir():
            target = paths.figures / crop.name
            if crop.is_file() and safe_io.entry_kind(paths.root, target) == "none":
                with crop.open("rb") as reader:
                    safe_io.write_from(paths.root, target, reader)
    return written


async def materialize_document(
    db: AsyncSession, settings: Settings, document: Document, *, locked: bool = False
) -> DocumentPaths:
    """Make sure the document's directory exists with its files; idempotent. The path is
    resolved under the workspace lock, after the row is re-read: a rename on the Files
    page holds the same lock across its row write and its move (docs/workspace.md).
    `locked`: the caller already holds that lock (it is not reentrant)."""
    job = (await db.execute(select(Job).where(Job.id == document.job_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    result = (
        await db.execute(select(Result).where(Result.job_id == document.job_id))
    ).scalar_one_or_none()
    if result is None:
        # An empty doc.md written now would never be replaced by the parse result
        # (materialization writes only what is missing), so there is nothing to edit yet.
        raise ApiError(409, "invalid_request", "The document has no parse result to edit yet")
    root = user_root(settings.workspaces_dir, document.user_id)
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        try:
            await db.refresh(document)
        except InvalidRequestError as exc:
            raise ApiError(404, "invalid_request", "No such document") from exc
        paths = await document_paths(db, settings, document)
        if safe_io.entry_kind(root, paths.directory) == "none":
            await ensure_within_quota(settings, root)
        written = await asyncio.to_thread(
            _materialize_files,
            paths,
            document_id=document.id,
            job=job,
            result=result,
            figures_dir=Path(settings.figures_dir),
        )
        if written:
            try:
                await asyncio.to_thread(
                    git.commit_paths,
                    paths.root,
                    git.USER_AUTHOR,
                    f"materialize {paths.relative}",
                    [paths.relative],
                )
            except git.GitError as exc:
                # What was written goes again: files left in place would never be
                # committed, since materialization writes only what is missing, and a
                # shell's next commit would absorb them (docs/workspace.md § Revisions).
                for path in written:
                    with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                        safe_io.remove_file(paths.root, path)
                raise ApiError(
                    409, "invalid_request", "The document could not be recorded; try again"
                ) from exc
    return paths


async def ensure_within_quota(settings: Settings, root: Path) -> None:
    """Refuse new material once the workspace is at its quota (docs/workspace.md § Quota).
    A walk of the tree: cheap at the sizes a quota allows, and only on growth."""
    used = await asyncio.to_thread(workspace_size, root)
    if used >= settings.workspace_quota_bytes:
        raise ApiError(
            409, "invalid_request", "The workspace is at its quota; free some space first"
        )


def _source_suffix(source: Path, kind: str) -> str:
    return ".pdf" if kind == "pdf" else (source.suffix.lower()[:16] or ".img")


def original_of(root: Path, document_id: int) -> Path | None:
    """The document's original upload, kept outside the mount, as a plain file; None when
    none was kept (docs/workspace.md § Layout)."""
    directory = sources_dir(root)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    for name in names:
        stem, dot, _ext = name.partition(".")
        if stem == str(document_id) and dot:
            candidate = directory / name
            if candidate.is_file() and not candidate.is_symlink():
                return candidate
    return None


def _copy_source(root: Path, directory: Path, original: Path) -> Path:
    """The shell's copy of the original, `source.<ext>` in the document directory —
    written through a descriptor opened without following anything, like every
    workspace file; excluded from history."""
    try:
        safe_io.ensure_dir(root, directory)
    except safe_io.UnsafeEntry as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    target = directory / f"{SOURCE_STEM}{original.suffix}"
    if safe_io.entry_kind(root, target) == "dir":
        safe_io.remove_tree(root, target)
    with original.open("rb") as reader:
        safe_io.write_from(root, target, reader)
    return target


def keep_source(
    root: Path,
    relative: Path,
    source: Path,
    *,
    document_id: int,
    kind: str = "pdf",
    copy: bool = False,
) -> Path:
    """File a finished parse's upload as the document's original — `{document_id}.pdf`
    (or `.<ext>` for an image: the parse's kind decides, never the upload's name) beside
    the git directory, outside the mount — then make the shell's copy of it in the
    document directory, and unless `copy`, remove the upload (docs/jobs.md § Retention).
    The original lands first and whole: a copy the tree refuses is made again on the
    next materialization. Returns the original's path."""
    git.ensure_repo(root)
    canonical = sources_dir(root) / f"{document_id}{_source_suffix(source, kind)}"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    # A name `original_of` never picks: its stem is not the id.
    staging = canonical.with_name(f".tmp-{canonical.name}")
    shutil.copyfile(source, staging)
    os.replace(staging, canonical)
    _copy_source(root, root / relative, canonical)
    if not copy:
        source.unlink(missing_ok=True)
    return canonical


def _copy_original(root: Path, source_id: int, target_id: int) -> bool:
    original = original_of(root, source_id)
    if original is None:
        return False
    canonical = original.with_name(f"{target_id}{original.suffix}")
    staging = canonical.with_name(f".tmp-{canonical.name}")
    shutil.copyfile(original, staging)
    os.replace(staging, canonical)
    return True


async def inherit_original(
    db: AsyncSession, settings: Settings, document: Document, job: Job
) -> bool:
    """A parse filed again from the History page: its upload went to the first document
    filed from it, so the new one takes a copy of that original — from any document of
    the parse that holds one — and, when none does but the upload is still there (the
    worker left it for a row that had not landed), takes the upload as `adopt_source`
    does (docs/workspace.md § Layout). False when the row is gone by the time the lock
    is held: another tab deleted it, and no original is kept under an id with no row."""
    root = user_root(settings.workspaces_dir, document.user_id)
    siblings = list(
        (
            await db.execute(
                select(Document.id)
                .where(Document.job_id == job.id, Document.id != document.id)
                .order_by(Document.id)
            )
        ).scalars()
    )
    if siblings:
        # The quota and the copy under the workspace lock, as retention takes them: two
        # filings at once must not both measure the room and both take it (§ Quota).
        async with workspace_lock(root):
            try:
                await db.refresh(document)
            except InvalidRequestError:
                return False  # deleted since it was filed; a delete holds this lock
            try:
                await ensure_within_quota(settings, root)
            except ApiError:
                logger.warning(
                    "workspace of user %d is at its quota; document %d keeps no original",
                    document.user_id,
                    document.id,
                )
                return True
            for sibling_id in siblings:
                if await asyncio.to_thread(_copy_original, root, sibling_id, document.id):
                    return True
    try:
        await adopt_source(db, settings, document, job)
    except InvalidRequestError:
        return False
    return True


def _discard_originals(root: Path, document_ids: list[int]) -> None:
    for document_id in document_ids:
        while (original := original_of(root, document_id)) is not None:
            original.unlink(missing_ok=True)


async def discard_originals(settings: Settings, user_id: int, document_ids: list[int]) -> None:
    """The originals of documents whose rows are gone (docs/workspace.md § Layout)."""
    if document_ids:
        root = user_root(settings.workspaces_dir, user_id)
        await asyncio.to_thread(_discard_originals, root, list(document_ids))


async def adopt_source(
    db: AsyncSession,
    settings: Settings,
    document: Document,
    job: Job,
    *,
    upload: Path | None = None,
) -> None:
    """The library row arrived after its parse finished, so the worker found no document
    to keep the upload for and left it in place — or the parse was answered from the
    cache, or joined one already on its way, and `upload` is the fresh file this row keeps
    for itself (docs/jobs.md § Retention): take it now."""
    if upload is not None:
        source = upload
    else:
        if job.status != "succeeded" or not job.source_path or job.source_deleted_at is not None:
            return
        source = Path(job.source_path)
    if not source.is_file():
        return
    root = user_root(settings.workspaces_dir, document.user_id)
    async with workspace_lock(root):
        await db.refresh(document)
        relative = document_relative_dir(
            await folder_names(db, document.user_id, document.folder_id), document.name
        )
        try:
            await ensure_within_quota(settings, root)
        except ApiError:
            source.unlink(missing_ok=True)
        else:
            await asyncio.to_thread(
                keep_source, root, relative, source, document_id=document.id, kind=job.kind
            )
    if upload is None:
        job.source_deleted_at = utcnow()
        await db.commit()


# --- content ------------------------------------------------------------------------------


def _read(paths: DocumentPaths, max_bytes: int, *, document_id: int) -> Content:
    paths.check()
    markdown = _read_text(paths.root, paths.doc, max_bytes)
    notes = _read_text(paths.root, paths.notes, max_bytes)
    return Content(
        markdown=markdown,
        revision=revision_of(markdown),
        notes=notes,
        notes_revision=revision_of(notes),
        has_source=original_of(paths.root, document_id) is not None or paths.source() is not None,
    )


async def read_content(
    db: AsyncSession, settings: Settings, document: Document, *, locked: bool = False
) -> Content:
    """The lock is held from the materialization through the read: a shell command
    taking it in between could move the directory or rewrite `doc.md` under the read
    (docs/workspace.md § Revisions)."""
    root = user_root(settings.workspaces_dir, document.user_id)
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        paths = await materialize_document(db, settings, document, locked=True)
        return await asyncio.to_thread(
            _read, paths, settings.content_max_bytes, document_id=document.id
        )


def _write_file(
    paths: DocumentPaths,
    path: Path,
    text: str,
    base_revision: str,
    author: str,
    message: str,
    max_bytes: int,
) -> str:
    paths.check()
    if len(text.encode()) > max_bytes:
        raise ApiError(409, "invalid_request", "The content is too large to save")
    current = _read_text(paths.root, path, max_bytes)
    if revision_of(current) != base_revision:
        raise ApiError(409, "invalid_request", "The file changed since it was loaded")
    if text != current:
        _write_text(paths.root, path, text)
        try:
            git.commit_paths(paths.root, author, message, [path.relative_to(paths.root)])
        except git.GitError as exc:
            # The file as it was: a save with no revision is not a save
            # (docs/workspace.md § Revisions).
            _write_text(paths.root, path, current)
            raise ApiError(
                409, "invalid_request", "The save could not be recorded; the file is as it was"
            ) from exc
    return revision_of(text)


async def write_content(
    db: AsyncSession,
    settings: Settings,
    document: Document,
    *,
    markdown: str,
    base_revision: str,
    author: str = git.USER_AUTHOR,
) -> str:
    # The path is resolved under the same lock as the write: a rename landing in between
    # must not send the text to a place the document has left (docs/workspace.md).
    async with workspace_lock(user_root(settings.workspaces_dir, document.user_id)):
        paths = await materialize_document(db, settings, document, locked=True)
        return await asyncio.to_thread(
            _write_file,
            paths,
            paths.doc,
            markdown,
            base_revision,
            author,
            f"edit {paths.doc_relative}",
            settings.content_max_bytes,
        )


async def write_notes(
    db: AsyncSession,
    settings: Settings,
    document: Document,
    *,
    text: str,
    base_revision: str,
    author: str = git.USER_AUTHOR,
) -> str:
    async with workspace_lock(user_root(settings.workspaces_dir, document.user_id)):
        paths = await materialize_document(db, settings, document, locked=True)
        return await asyncio.to_thread(
            _write_file,
            paths,
            paths.notes,
            text,
            base_revision,
            author,
            f"notes {paths.notes_relative}",
            settings.content_max_bytes,
        )


# --- revisions ----------------------------------------------------------------------------


def _history(paths: DocumentPaths, limit: int = 100) -> list[git.Revision]:
    """The directory's own log, merged with each fixed file's log followed through the
    renames git detects: a document moved since keeps the revisions made before the move
    (docs/workspace.md § Revisions). Newest first, one entry per commit."""
    seen: dict[str, git.Revision] = {}
    for relative, follow in (
        (paths.relative, False),
        (paths.doc_relative, True),
        (paths.notes_relative, True),
    ):
        for revision in git.log(paths.root, relative, limit, follow=follow):
            known = seen.get(revision.commit)
            if known is None:
                seen[revision.commit] = revision
            elif set(revision.files) - set(known.files):
                seen[revision.commit] = replace(
                    known, files=known.files + tuple(set(revision.files) - set(known.files))
                )
    merged = sorted(seen.values(), key=lambda revision: revision.created_at, reverse=True)
    return merged[:limit]


async def revisions(db: AsyncSession, settings: Settings, document: Document) -> list[git.Revision]:
    paths = await materialize_document(db, settings, document)
    return await asyncio.to_thread(_history, paths)


def _prune_empty(root: Path, directory: Path) -> None:
    """Remove the directories a moved-back file left empty, up to `files/`: git tracks
    files, not directories, so a reverted rename would otherwise leave its shell."""
    base = files_root(root)
    while directory != base and base in directory.parents:
        if not safe_io.remove_empty_dir(root, directory):
            return
        directory = directory.parent


def _under(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


def reaches_beyond(paths: DocumentPaths, commit: str) -> bool:
    """Whether the commit touched a path outside this document's directory — where it is
    now, or where it was at that commit, since a rename may have moved the whole directory
    since. Such a revision is a shell's, and reverting it is refused while any turn of the
    user is unfinished (docs/workspace.md § Revisions). An unknown commit reaches nothing:
    the revert answers 404 for it."""
    root = paths.root
    if not git.is_commit(root, commit):
        return False
    try:
        # Each path where it lives now: a document moved since still owns its older
        # revisions. Both reads are bounded; past the bound the answer is the safe one.
        moved = git.renames_since(root, commit)
        changes = git.changed_paths(root, commit)
    except git.GitError:
        return True
    for _status, then, renamed_to in changes:
        for path in (then, renamed_to):
            if path is not None and not _under(moved.get(path, path), paths.relative):
                return True
    return False


def _revert(
    paths: DocumentPaths, commit: str, max_bytes: int, total_max_bytes: int, max_paths: int
) -> str:
    """Undo every path the commit touched — a shell revision may have changed files
    beyond this document — writing each one as it was in the commit's parent, where it
    lives now if it was moved since; a rename is moved back (docs/workspace.md
    § Revisions). Every restoration is resolved and read *before* the first write, so
    a path that cannot be restored leaves the tree untouched. Returns the commit the
    revert made."""
    paths.check()
    root = paths.root
    if not git.is_commit(root, commit):
        raise ApiError(404, "invalid_request", "No such revision")
    parent = git.parent_of(root, commit)
    try:
        moved = git.renames_since(root, commit)
        changes = git.changed_paths(root, commit)
    except git.GitError as exc:
        # The metadata alone is past the log bound: a shell revision over a workspace's
        # worth of files is not restored by the host (docs/workspace.md § Revisions).
        raise ApiError(409, "invalid_request", "That revision is too large to restore") from exc
    if len(changes) > max_paths:
        raise ApiError(409, "invalid_request", "That revision is too large to restore")

    # Phase one: what to do, with every byte to write already in hand.
    moves: list[tuple[Path, Path]] = []
    writes: list[tuple[Path, bytes]] = []
    removals: list[Path] = []
    held = 0

    def hold(data: bytes) -> bytes:
        # Every byte to restore is in hand before the first write; the sum is bounded,
        # or a shell revision over a workspace's worth of files would be held in memory.
        nonlocal held
        held += len(data)
        if held > total_max_bytes:
            raise ApiError(409, "invalid_request", "That revision is too large to restore")
        return data

    for status, then, renamed_to in changes:
        now = moved.get(then, then)
        if status == "R" and renamed_to is not None:
            current = moved.get(renamed_to, renamed_to)
            if safe_io.entry_kind(root, root / current) != "none":
                if safe_io.entry_kind(root, root / then) != "none":
                    # Something sits at the old place now; a revert never writes over
                    # what is there (docs/workspace.md § Revisions).
                    raise ApiError(409, "invalid_request", "That revision's old path is in use now")
                moves.append((current, then))
                # A rename that also edited the file: the bytes go back too, after the
                # move, as they were in the commit's parent.
                if parent is not None:
                    try:
                        before = git.show_bytes(root, parent, then, max_bytes)
                    except git.GitError as exc:
                        raise ApiError(
                            409, "invalid_request", "That revision is too large to restore"
                        ) from exc
                    if before is not None:
                        writes.append((then, hold(before)))
            continue
        if status == "A":
            # The commit created the file; a document's own fixed files are emptied
            # rather than removed, so the directory keeps its shape.
            if now.name in (DOC_FILE, NOTES_FILE):
                writes.append((now, b""))
            elif safe_io.entry_kind(root, root / now) == "file":
                removals.append(now)
            continue
        if parent is None:
            continue
        try:
            before = git.show_bytes(root, parent, then, max_bytes)
        except git.GitError as exc:
            raise ApiError(409, "invalid_request", "That revision is too large to restore") from exc
        if before is None:
            continue
        if safe_io.entry_kind(root, root / now) not in ("file", "none"):
            raise ApiError(409, "invalid_request", NOT_PLAIN)
        writes.append((now, hold(before)))
    if not moves and not writes and not removals:
        raise ApiError(400, "invalid_request", "That revision changed nothing")

    # Phase two: apply, every step through descriptors that follow nothing. What was
    # done is kept so that a failure anywhere — the tree, or the revision that records
    # it — rolls the tree back to as it was (docs/workspace.md § Revisions).
    restored: list[Path] = []
    done_moves: list[tuple[Path, Path]] = []
    moved_aside: list[tuple[Path, Path]] = []

    def rollback() -> None:
        git.reset_index(root)
        for source, target in reversed(moved_aside):
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.rename(root, root / target, root / source)
        for current, then in reversed(done_moves):
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.rename(root, root / then, root / current)
        git.reset_hard(root)
        for now, _data in writes:
            if not git.tracked_at_head(root, now):
                with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                    safe_io.remove_file(root, root / now)
                    _prune_empty(root, (root / now).parent)
        for _current, then in done_moves:
            _prune_empty(root, (root / then).parent)

    try:
        # (directory vacated, directory restored): once the tracked files are back, what
        # the vacated directory still holds — `source.*`, `figures/`, which history does
        # not record — goes the same way (docs/workspace.md § Revisions).
        vacated: dict[Path, Path] = {}
        for current, then in moves:
            git.move(root, root / current, root / then)
            done_moves.append((current, then))
            if current.parent != then.parent:
                vacated.setdefault(current.parent, then.parent)
            restored += [current, then]
        for source_dir, target_dir in vacated.items():
            if safe_io.entry_kind(root, root / source_dir) != "dir":
                continue
            for name, _is_dir in safe_io.list_dir(root, root / source_dir):
                if safe_io.entry_kind(root, root / target_dir / name) == "none":
                    safe_io.rename(root, root / source_dir / name, root / target_dir / name)
                    moved_aside.append((source_dir / name, target_dir / name))
            _prune_empty(root, root / source_dir)
        for now in removals:
            if safe_io.remove_file(root, root / now):
                restored.append(now)
                _prune_empty(root, (root / now).parent)
        for now, data in writes:
            safe_io.write_bytes(root, root / now, data)
            restored.append(now)
        made = git.commit_paths(root, git.USER_AUTHOR, f"revert {commit[:12]}", restored)
    except (safe_io.UnsafeEntry, OSError) as exc:
        _rollback_quietly(rollback, root)
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    except git.GitError as exc:
        _rollback_quietly(rollback, root)
        raise ApiError(
            409, "invalid_request", "That revision could not be recorded; nothing was changed"
        ) from exc
    if made is None:
        raise ApiError(400, "invalid_request", "That revision changed nothing")
    return made


def _rollback_quietly(rollback, root: Path) -> None:
    try:
        rollback()
    except Exception:  # noqa: BLE001 - the refusal stands; the state is logged
        logger.exception("could not roll back a failed revert in %s", root)


@dataclass(frozen=True)
class Reverted:
    commit: str
    revision: str


async def revert(
    db: AsyncSession,
    settings: Settings,
    document: Document,
    commit: str,
    *,
    locked: bool = False,
) -> Reverted:
    """Undo a commit, then bring the library in line with the tree — a reverted rename
    moves the directory, and the row follows — and report the document's revision now.
    All under the workspace lock, which the caller may already hold (`locked`)."""
    from .reconcile import reconcile_locked

    root = user_root(settings.workspaces_dir, document.user_id)
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        paths = await materialize_document(db, settings, document, locked=True)
        made = await asyncio.to_thread(
            _revert,
            paths,
            commit,
            settings.content_max_bytes,
            settings.revert_max_bytes,
            settings.revert_max_paths,
        )
        await reconcile_locked(db, settings, document.user_id)
        content = await read_content(db, settings, document, locked=True)
    return Reverted(commit=made, revision=content.revision)


# --- the tree -----------------------------------------------------------------------------


def _move_tree(root: Path, source: Path, target: Path, message: str) -> str | None:
    git.ensure_repo(root)
    if safe_io.entry_kind(root, root / source) == "none" or source == target:
        return None
    touched = [source, target]
    try:
        if safe_io.entry_kind(root, root / target) != "none":
            # Something else sits where the row now points — a directory the shell made
            # under that name. It moves aside, so the document lands where the database
            # says and the squatter keeps what it holds (docs/workspace.md § Layout).
            aside = target.with_name(f"{target.name} ({uuid.uuid4().hex[:6]})")
            git.move(root, root / target, root / aside)
            touched.append(aside)
        git.move(root, root / source, root / target)
    except safe_io.UnsafeEntry as exc:
        # An ancestor the shell swapped for a link: refused, and the caller's row rolls
        # back to where the files still are.
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    try:
        return git.commit_paths(root, git.USER_AUTHOR, message, touched)
    except git.GitError as exc:
        # Back where the row will point once the caller rolls back: the directory to its
        # old place, the squatter to the place it had. Plain renames, no git — the index
        # was reset by the failed commit and the next commit sees the tree as it is.
        safe_io.rename(root, root / target, root / source)
        if len(touched) == 3:
            safe_io.rename(root, root / touched[2], root / target)
        raise ApiError(
            409, "invalid_request", "The move could not be recorded; nothing was changed"
        ) from exc


async def move_path(settings: Settings, user_id: int, source: Path, target: Path) -> str | None:
    """Rename or move a document or folder directory after the database did. The caller
    holds the user's workspace lock across its database write and this move, so nothing
    can materialize at the new path in between (docs/workspace.md § Revisions). Returns
    the revision the move made, None when there was nothing to move."""
    root = user_root(settings.workspaces_dir, user_id)
    return await asyncio.to_thread(_move_tree, root, source, target, f"move {source} -> {target}")


@dataclass(frozen=True)
class Removal:
    """A directory moved aside for a delete: the revision that records the removal and
    where the directory waits, whole, until the rows are gone (docs/workspace.md)."""

    commit: str | None
    aside: Path


def _remove_tree(root: Path, relative: Path, message: str) -> Removal | None:
    git.ensure_repo(root)
    if safe_io.entry_kind(root, root / relative) == "none":
        return None
    aside = Path(f"{safe_io.TEMP_PREFIX}{uuid.uuid4().hex}")
    try:
        # Aside rather than gone: outside `files/` and outside history, the directory
        # waits whole — originals and crops included — until the rows are gone, and comes
        # back whole if they are not. Through descriptors that follow nothing: an
        # ancestor the shell swapped for a link to another tree is a refusal.
        safe_io.rename(root, root / relative, root / aside)
    except safe_io.UnsafeEntry as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    try:
        commit = git.commit_paths(root, git.USER_AUTHOR, message, [relative])
    except git.GitError as exc:
        # Back where it was before the refusal: nothing waits aside for a sweep to take.
        safe_io.rename(root, root / aside, root / relative)
        raise ApiError(
            409, "invalid_request", "The removal could not be recorded; nothing was changed"
        ) from exc
    return Removal(commit, aside)


async def remove_path(settings: Settings, user_id: int, relative: Path) -> Removal | None:
    """Move a directory aside for a delete after the database did; the caller holds the
    workspace lock. `discard_removed` deletes it once the rows are gone, `restore_removed`
    brings it back should they stay."""
    root = user_root(settings.workspaces_dir, user_id)
    return await asyncio.to_thread(_remove_tree, root, relative, f"delete {relative}")


async def discard_removed(settings: Settings, user_id: int, removal: Removal) -> None:
    """The rows are gone: so is the directory set aside, with a bounded set of
    descriptors however deep it is."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(safe_io.remove_tree, root, root / removal.aside)


def _restore_removed(root: Path, relative: Path, removal: Removal) -> None:
    safe_io.rename(root, root / removal.aside, root / relative)
    git.commit_paths(root, git.USER_AUTHOR, f"restore {relative}", [relative])


async def restore_removed(
    settings: Settings, user_id: int, relative: Path, removal: Removal
) -> None:
    """The rows a removal was made for did not go (their commit failed): the directory
    comes back whole from where it was set aside, as a revision of its own, so the row
    that stayed keeps its edits, notes, original and crops (docs/workspace.md
    § Revisions). The caller holds the workspace lock."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_restore_removed, root, relative, removal)


def sweep_aside(root: Path) -> int:
    """Remove what a delete left aside and never discarded (a crash between its commit
    and the discard); run under the workspace lock, where no delete is in flight."""
    swept = 0
    for name, is_dir in safe_io.list_dir(root, root):
        if is_dir and name.startswith(safe_io.TEMP_PREFIX):
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.remove_tree(root, root / name)
                swept += 1
    return swept


def directory_size(root: Path, path: Path | None = None) -> int:
    """Bytes under `path` (the whole user directory when None), walked through
    descriptors that follow nothing, and no deeper than the walk's depth bound
    (docs/workspace.md § Quota): a tree nested past it cannot be measured."""
    try:
        return safe_io.tree_size(root, root if path is None else path)
    except safe_io.TreeTooDeep as exc:
        raise ApiError(
            409,
            "invalid_request",
            "The workspace has a directory nested too deep to measure; delete it first",
        ) from exc


def workspace_size(root: Path) -> int:
    """What the user holds: the tree, the history and the originals beside it (the git
    and sources directories are the service's own, outside the mount, walked the same
    way from their own roots)."""
    return directory_size(root) + directory_size(git_dir(root)) + directory_size(sources_dir(root))
