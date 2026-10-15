"""A document's files in the user's workspace (docs/workspace.md).

Materialization writes what is missing and never what exists; every write commits a
revision; every write happens under the user's workspace lock. Paths come from the
library's names through `layout.py`, never from request text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Document, Folder, Job, Result
from ..errors import ApiError
from . import git
from .layout import (
    DOC_FILE,
    FIGURES_DIR,
    META_FILE,
    NOTES_FILE,
    SOURCE_STEM,
    document_relative_dir,
    files_root,
    results_root,
    user_root,
)
from .locks import workspace_lock


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
        """`source.<ext>` when the original upload was kept."""
        for candidate in sorted(self.directory.glob(f"{SOURCE_STEM}.*")):
            if candidate.is_file():
                return candidate
        return None

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


def _write_if_missing(path: Path, text: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
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
    paths.directory.mkdir(parents=True, exist_ok=True)
    changed = False
    changed |= _write_if_missing(paths.doc, result.markdown)
    changed |= _write_if_missing(paths.notes, "")
    meta = {
        "document_id": document_id,
        "job_id": str(job.id),
        "model": job.model,
        "bbox_format": job.bbox_format,
        "page_count": job.page_count,
        "kind": job.kind,
    }
    changed |= _write_if_missing(paths.meta, json.dumps(meta, indent=2) + "\n")
    original = results_root(paths.root) / str(job.id) / "result.md"
    _write_if_missing(original, result.markdown)  # ignored by git, no commit needed
    crops = figures_dir / str(job.id)
    if crops.is_dir() and not paths.figures.exists():
        shutil.copytree(crops, paths.figures)
    return changed


async def materialize_document(
    db: AsyncSession, settings: Settings, document: Document
) -> DocumentPaths:
    """Make sure the document's directory exists with its files; idempotent."""
    paths = await document_paths(db, settings, document)
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
    async with workspace_lock(paths.root):
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
                git.commit_all, paths.root, git.USER_AUTHOR, f"materialize {paths.relative}"
            )
    return paths


def keep_source(root: Path, relative: Path, source: Path) -> Path:
    """Move a finished parse's upload into its document directory (docs/jobs.md
    § Retention). Synchronous: called by the worker at terminal state."""
    git.ensure_repo(root)
    directory = root / relative
    directory.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()[:16] or ".bin"
    target = directory / f"{SOURCE_STEM}{suffix}"
    shutil.move(str(source), str(target))
    return target


# --- content ------------------------------------------------------------------------------


def _read(paths: DocumentPaths) -> Content:
    markdown = paths.doc.read_text() if paths.doc.exists() else ""
    notes = paths.notes.read_text() if paths.notes.exists() else ""
    return Content(
        markdown=markdown,
        revision=revision_of(markdown),
        notes=notes,
        notes_revision=revision_of(notes),
        has_source=paths.source() is not None,
    )


async def read_content(db: AsyncSession, settings: Settings, document: Document) -> Content:
    paths = await materialize_document(db, settings, document)
    return await asyncio.to_thread(_read, paths)


def _write_file(
    paths: DocumentPaths, path: Path, text: str, base_revision: str, author: str, message: str
) -> str:
    current = path.read_text() if path.exists() else ""
    if revision_of(current) != base_revision:
        raise ApiError(409, "invalid_request", "The file changed since it was loaded")
    if text != current:
        path.write_text(text)
        git.commit_all(paths.root, author, message)
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
        )


# --- revisions ----------------------------------------------------------------------------


async def revisions(db: AsyncSession, settings: Settings, document: Document) -> list[git.Revision]:
    paths = await materialize_document(db, settings, document)
    return await asyncio.to_thread(git.log, paths.root, paths.relative)


def _revert(paths: DocumentPaths, commit: str) -> str:
    if not git.is_commit(paths.root, commit):
        raise ApiError(404, "invalid_request", "No such revision")
    parent = git.parent_of(paths.root, commit)
    restored = 0
    for relative, path in ((paths.doc_relative, paths.doc), (paths.notes_relative, paths.notes)):
        before = git.show(paths.root, parent, relative) if parent else None
        after = git.show(paths.root, commit, relative)
        if before == after:
            continue
        if before is None:
            # The commit created the file; reverting it makes it empty, never deletes it —
            # the document directory keeps its shape.
            before = ""
        path.write_text(before)
        restored += 1
    if restored == 0:
        raise ApiError(400, "invalid_request", "That revision changed nothing in this document")
    git.commit_all(paths.root, git.USER_AUTHOR, f"revert {commit[:12]} in {paths.relative}")
    return revision_of(paths.doc.read_text())


async def revert(db: AsyncSession, settings: Settings, document: Document, commit: str) -> str:
    paths = await materialize_document(db, settings, document)
    async with workspace_lock(paths.root):
        return await asyncio.to_thread(_revert, paths, commit)


# --- the tree -----------------------------------------------------------------------------


def _move_tree(root: Path, source: Path, target: Path, message: str) -> None:
    git.ensure_repo(root)
    if not (root / source).exists() or source == target:
        return
    if (root / target).exists():
        # Two names that clean to the same segment: keep both, the second disambiguated.
        target = target.with_name(f"{target.name} ({uuid.uuid4().hex[:6]})")
    git.move(root, root / source, root / target)
    git.commit_all(root, git.USER_AUTHOR, message)


async def move_path(settings: Settings, user_id: int, source: Path, target: Path) -> None:
    """Rename or move a document or folder directory after the database did."""
    root = user_root(settings.workspaces_dir, user_id)
    async with workspace_lock(root):
        await asyncio.to_thread(_move_tree, root, source, target, f"move {source} -> {target}")


def _remove_tree(root: Path, relative: Path, message: str) -> None:
    git.ensure_repo(root)
    target = root / relative
    if not target.exists():
        return
    shutil.rmtree(target, ignore_errors=True)
    git.commit_all(root, git.USER_AUTHOR, message)


async def remove_path(settings: Settings, user_id: int, relative: Path) -> None:
    root = user_root(settings.workspaces_dir, user_id)
    async with workspace_lock(root):
        await asyncio.to_thread(_remove_tree, root, relative, f"delete {relative}")


def directory_size(path: Path) -> int:
    """Bytes under `path`, following nothing."""
    total = 0
    if not path.exists():
        return 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total
