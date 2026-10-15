"""A document's files in the user's workspace (docs/workspace.md).

Materialization writes what is missing and never what exists; every write commits a
revision of the paths it wrote; every write happens under the user's workspace lock.
Paths come from the library's names through `layout.py`, never from request text, and
every file is opened through `safe_io`, which follows nothing the shell could have planted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
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
    user_root,
)
from .locks import workspace_lock

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
        if safe_io.entry_kind(self.root, self.directory) != "dir":
            return None
        try:
            names = sorted(os.listdir(self.directory))
        except OSError:
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
) -> bool:
    """Write what is missing. Returns whether anything was written (needs a commit)."""
    git.ensure_repo(paths.root)
    files_root(paths.root).mkdir(parents=True, exist_ok=True)
    paths.check()
    paths.directory.mkdir(parents=True, exist_ok=True)
    changed = False
    changed |= _write_if_missing(paths.root, paths.doc, result.markdown)
    changed |= _write_if_missing(paths.root, paths.notes, "")
    meta = {
        "document_id": document_id,
        "job_id": str(job.id),
        "model": job.model,
        "bbox_format": job.bbox_format,
        "page_count": job.page_count,
        "kind": job.kind,
    }
    changed |= _write_if_missing(paths.root, paths.meta, json.dumps(meta, indent=2) + "\n")
    original = results_root(paths.root) / str(job.id) / "result.md"
    _write_if_missing(paths.root, original, result.markdown)  # excluded from history
    crops = figures_dir / str(job.id)
    if crops.is_dir() and safe_io.entry_kind(paths.root, paths.figures) == "none":
        # The crops directory is the service's own; the document directory is shared with
        # the shell, so each crop lands through a descriptor like every workspace file.
        for crop in crops.iterdir():
            if crop.is_file():
                with crop.open("rb") as reader:
                    safe_io.write_from(paths.root, paths.figures / crop.name, reader)
    return changed


async def materialize_document(
    db: AsyncSession, settings: Settings, document: Document
) -> DocumentPaths:
    """Make sure the document's directory exists with its files; idempotent. The path is
    resolved under the workspace lock, after the row is re-read: a rename on the Files
    page holds the same lock across its row write and its move (docs/workspace.md)."""
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
    async with workspace_lock(root):
        try:
            await db.refresh(document)
        except InvalidRequestError as exc:
            raise ApiError(404, "invalid_request", "No such document") from exc
        paths = await document_paths(db, settings, document)
        if safe_io.entry_kind(root, paths.directory) == "none":
            await ensure_within_quota(settings, root)
        changed = await asyncio.to_thread(
            _materialize_files,
            paths,
            document_id=document.id,
            job=job,
            result=result,
            figures_dir=Path(settings.figures_dir),
        )
        if changed:
            await asyncio.to_thread(
                git.commit_paths,
                paths.root,
                git.USER_AUTHOR,
                f"materialize {paths.relative}",
                [paths.relative],
            )
    return paths


async def ensure_within_quota(settings: Settings, root: Path) -> None:
    """Refuse new material once the workspace is at its quota (docs/workspace.md § Quota).
    A walk of the tree: cheap at the sizes a quota allows, and only on growth."""
    used = await asyncio.to_thread(workspace_size, root)
    if used >= settings.workspace_quota_bytes:
        raise ApiError(
            409, "invalid_request", "The workspace is at its quota; free some space first"
        )


def keep_source(
    root: Path, relative: Path, source: Path, *, kind: str = "pdf", copy: bool = False
) -> Path:
    """Copy a finished parse's upload into its document directory as `source.pdf` (or
    `source.<ext>` for an image — the parse's kind decides, never the upload's name),
    and unless `copy`, remove the upload (docs/jobs.md § Retention). Written through a
    descriptor opened without following anything, like every workspace file."""
    git.ensure_repo(root)
    directory = root / relative
    if safe_io.entry_kind(root, directory) not in ("dir", "none"):
        raise ApiError(409, "invalid_request", NOT_PLAIN)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if kind == "pdf" else (source.suffix.lower()[:16] or ".img")
    target = directory / f"{SOURCE_STEM}{suffix}"
    if safe_io.entry_kind(root, target) == "dir":
        safe_io.remove_tree(root, target)
    with source.open("rb") as reader:
        safe_io.write_from(root, target, reader)
    if not copy:
        source.unlink(missing_ok=True)
    return target


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
            await asyncio.to_thread(keep_source, root, relative, source, kind=job.kind)
    if upload is None:
        job.source_deleted_at = utcnow()
        await db.commit()


# --- content ------------------------------------------------------------------------------


def _read(paths: DocumentPaths, max_bytes: int) -> Content:
    paths.check()
    markdown = _read_text(paths.root, paths.doc, max_bytes)
    notes = _read_text(paths.root, paths.notes, max_bytes)
    return Content(
        markdown=markdown,
        revision=revision_of(markdown),
        notes=notes,
        notes_revision=revision_of(notes),
        has_source=paths.source() is not None,
    )


async def read_content(db: AsyncSession, settings: Settings, document: Document) -> Content:
    paths = await materialize_document(db, settings, document)
    return await asyncio.to_thread(_read, paths, settings.content_max_bytes)


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
        git.commit_paths(paths.root, author, message, [path.relative_to(paths.root)])
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
    paths = await materialize_document(db, settings, document)
    async with workspace_lock(paths.root):
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
    paths = await materialize_document(db, settings, document)
    async with workspace_lock(paths.root):
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


async def revisions(db: AsyncSession, settings: Settings, document: Document) -> list[git.Revision]:
    paths = await materialize_document(db, settings, document)
    return await asyncio.to_thread(git.log, paths.root, paths.relative)


def _prune_empty(root: Path, directory: Path) -> None:
    """Remove the directories a moved-back file left empty, up to `files/`: git tracks
    files, not directories, so a reverted rename would otherwise leave its shell."""
    base = files_root(root)
    while directory != base and base in directory.parents:
        if not safe_io.remove_empty_dir(root, directory):
            return
        directory = directory.parent


def _revert(paths: DocumentPaths, commit: str, max_bytes: int) -> str:
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
    moved = git.renames_since(root, commit)

    # Phase one: what to do, with every byte to write already in hand.
    moves: list[tuple[Path, Path]] = []
    writes: list[tuple[Path, bytes]] = []
    removals: list[Path] = []
    for status, then, renamed_to in git.changed_paths(root, commit):
        now = moved.get(then, then)
        if status == "R" and renamed_to is not None:
            current = moved.get(renamed_to, renamed_to)
            if safe_io.entry_kind(root, root / current) != "none":
                moves.append((current, then))
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
        writes.append((now, before))
    if not moves and not writes and not removals:
        raise ApiError(400, "invalid_request", "That revision changed nothing")

    # Phase two: apply, every step through descriptors that follow nothing.
    restored: list[Path] = []
    try:
        for current, then in moves:
            git.move(root, root / current, root / then)
            _prune_empty(root, (root / current).parent)
            restored += [current, then]
        for now in removals:
            if safe_io.remove_file(root, root / now):
                restored.append(now)
        for now, data in writes:
            safe_io.write_bytes(root, root / now, data)
            restored.append(now)
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    made = git.commit_paths(root, git.USER_AUTHOR, f"revert {commit[:12]}", restored)
    if made is None:
        raise ApiError(400, "invalid_request", "That revision changed nothing")
    return made


@dataclass(frozen=True)
class Reverted:
    commit: str
    revision: str


async def revert(db: AsyncSession, settings: Settings, document: Document, commit: str) -> Reverted:
    """Undo a commit, then bring the library in line with the tree — a reverted rename
    moves the directory, and the row follows — and report the document's revision now."""
    from .reconcile import reconcile_user

    paths = await materialize_document(db, settings, document)
    async with workspace_lock(paths.root):
        made = await asyncio.to_thread(_revert, paths, commit, settings.content_max_bytes)
    await reconcile_user(db, settings, document.user_id)
    content = await read_content(db, settings, document)
    return Reverted(commit=made, revision=content.revision)


# --- the tree -----------------------------------------------------------------------------


def _move_tree(root: Path, source: Path, target: Path, message: str) -> None:
    git.ensure_repo(root)
    if safe_io.entry_kind(root, root / source) == "none" or source == target:
        return
    touched = [source, target]
    if safe_io.entry_kind(root, root / target) != "none":
        # Something else sits where the row now points — a directory the shell made under
        # that name. It moves aside, so the document lands where the database says and
        # the squatter keeps what it holds (docs/workspace.md § Layout).
        aside = target.with_name(f"{target.name} ({uuid.uuid4().hex[:6]})")
        git.move(root, root / target, root / aside)
        touched.append(aside)
    git.move(root, root / source, root / target)
    git.commit_paths(root, git.USER_AUTHOR, message, touched)


async def move_path(settings: Settings, user_id: int, source: Path, target: Path) -> None:
    """Rename or move a document or folder directory after the database did. The caller
    holds the user's workspace lock across its database write and this move, so nothing
    can materialize at the new path in between (docs/workspace.md § Revisions)."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_move_tree, root, source, target, f"move {source} -> {target}")


def _remove_tree(root: Path, relative: Path, message: str) -> None:
    git.ensure_repo(root)
    try:
        # Through descriptors that follow nothing: an ancestor the shell swapped for a
        # link to another tree is a refusal, never a deletion there.
        removed = safe_io.remove_tree(root, root / relative)
    except safe_io.UnsafeEntry as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    if not removed:
        return
    git.commit_paths(root, git.USER_AUTHOR, message, [relative])


async def remove_path(settings: Settings, user_id: int, relative: Path) -> None:
    """Delete a directory after the database did; the caller holds the workspace lock."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_remove_tree, root, relative, f"delete {relative}")


def directory_size(path: Path) -> int:
    """Bytes under `path`, following nothing: a symlink counts as itself, never its target."""
    total = 0
    if not path.is_dir() or path.is_symlink():
        return 0
    for current, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def workspace_size(root: Path) -> int:
    """What the user holds: the tree and the history beside it."""
    return directory_size(root) + directory_size(git_dir(root))
