"""A document's files in the user's workspace (docs/workspace.md).

Materialization writes what is missing and never what exists; every write commits a
revision of the paths it wrote; every write happens under the user's workspace lock.
Paths come from the library's names through `layout.py`, never from request text, and
every file is opened through `safe_io`, which follows nothing the shell could have planted.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import Row, select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import ledger
from ..config import Settings
from ..db.models import Document, Folder, Job, Result, utcnow
from ..errors import ApiError
from . import git, safe_io
from .layout import (
    DOC_FILE,
    FIGURES_DIR,
    FILES_DIR,
    META_FILE,
    NOTES_FILE,
    SIGHTREAD_DIR,
    SOURCE_STEM,
    TRASH_DIR,
    document_relative_dir,
    files_root,
    git_dir,
    images_dir,
    path_segment,
    results_root,
    sources_dir,
    trash_entry,
    trash_root,
    user_root,
)
from .locks import workspace_lock

logger = logging.getLogger(__name__)

NOT_PLAIN = "The document's files are not regular files"
TOO_LARGE = "The document file is too large to open"
NOT_UTF8 = "The document file is not UTF-8 text"

# A document with no parse behind it (docs/workspace.md § Markdown documents): what the
# library calls its kind, and what an upload is recognised as one by.
MARKDOWN_KIND = "markdown"
MARKDOWN_MEDIA_TYPE = "text/markdown"
MARKDOWN_EXTENSIONS = (".md", ".markdown")
# An inserted image's file name: the first 16 hex digits of its bytes' SHA-256, as it is
# kept (docs/workspace.md § Inserted images). Never a name from the request.
IMAGE_NAME = re.compile(r"img-[0-9a-f]{16}\.(?:png|jpg)")
# What an inserted image may be uploaded as, and the only decoders it is offered, by
# Pillow's names (HEIF is HEIC's container); it is kept as PNG or JPEG.
IMAGE_MEDIA_TYPES = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
)
IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "HEIF")
IMAGE_SUFFIX_MEDIA = {".png": "image/png", ".jpg": "image/jpeg"}


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
        raise ApiError(409, "invalid_request", NOT_UTF8) from exc
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc


def _write_text(root: Path, path: Path, text: str) -> None:
    try:
        safe_io.write_text(root, path, text)
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc


# --- where a document lives ---------------------------------------------------------------


async def user_folders(db: AsyncSession, user_id: int) -> dict[int, Folder]:
    """Every folder of the user, by id: a library is a handful of rows."""
    rows = (await db.execute(select(Folder).where(Folder.user_id == user_id))).scalars().all()
    return {row.id: row for row in rows}


def folder_chain(by_id: dict[int, Folder], folder_id: int | None) -> list[Folder]:
    """The folders from the root down to `folder_id`."""
    chain: list[Folder] = []
    cursor = by_id.get(folder_id) if folder_id is not None else None
    seen: set[int] = set()
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        chain.append(cursor)
        cursor = by_id.get(cursor.parent_id) if cursor.parent_id is not None else None
    chain.reverse()
    return chain


async def folder_names(db: AsyncSession, user_id: int, folder_id: int | None) -> list[str]:
    """The folder chain from the root down to `folder_id`, as names."""
    if folder_id is None:
        return []
    return [row.name for row in folder_chain(await user_folders(db, user_id), folder_id)]


def trashed_folder_ids(folders: Iterable[Folder] | Iterable[Row]) -> set[int]:
    """Every folder in the Trash: put there itself, or inside one that was. Nothing marks
    the second kind, so it is computed (docs/database.md § Rules)."""
    by_id = {row.id: row for row in folders}
    trashed: dict[int, bool] = {}
    for start in by_id:
        trail: list[int] = []
        cursor: int | None = start
        verdict = False
        while cursor is not None:
            if cursor in trashed:
                verdict = trashed[cursor]
                break
            row = by_id.get(cursor)
            if row is None or cursor in trail:
                break
            trail.append(cursor)
            if row.trashed_at is not None:
                verdict = True
                break
            cursor = row.parent_id
        for folder_id in trail:
            trashed[folder_id] = verdict
    return {folder_id for folder_id, verdict in trashed.items() if verdict}


def is_live(document: Document, by_id: dict[int, Folder]) -> bool:
    """Neither the document nor any folder above it is in the Trash."""
    return document.trashed_at is None and all(
        row.trashed_at is None for row in folder_chain(by_id, document.folder_id)
    )


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


def markdown_baseline(root: Path, document_id: int, max_bytes: int) -> str:
    """A markdown document's baseline: its original as text, a leading byte-order mark
    dropped and CRLF line ends made LF, or "" for one written new, which keeps no original
    (docs/workspace.md § Markdown documents). 409 past `max_bytes` or when not UTF-8."""
    original = original_of(root, document_id)
    if original is None:
        return ""
    if original.stat().st_size > max_bytes:
        raise ApiError(409, "invalid_request", TOO_LARGE)
    try:
        text = original.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(409, "invalid_request", NOT_UTF8) from exc
    return text.removeprefix("\ufeff").replace("\r\n", "\n")


def is_utf8_file(path: Path) -> bool:
    """Whether a staged upload decodes as UTF-8, read in slices, never whole."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def meta_text(document_id: int, job: Job | None) -> str:
    """A document directory's `.meta.json`. `document_id` first, whatever follows: the
    lookup for a directory the tree moved matches `"document_id": N,`
    (`materialize_document`)."""
    if job is None:
        meta = {
            "document_id": document_id,
            "job_id": None,
            "model": None,
            "bbox_format": None,
            "page_count": None,
            "kind": MARKDOWN_KIND,
        }
    else:
        meta = {
            "document_id": document_id,
            "job_id": str(job.id),
            "model": job.model,
            "bbox_format": job.bbox_format,
            "page_count": job.page_count,
            "kind": job.kind,
        }
    return json.dumps(meta, indent=2) + "\n"


def _materialize_files(
    paths: DocumentPaths,
    *,
    document_id: int,
    job: Job | None,
    result: Result | None,
    figures_dir: Path,
    max_bytes: int,
) -> list[Path]:
    """Write what is missing. Returns the tracked files written (they need a commit).
    `job` None is a markdown document: `doc.md` from its original, and nothing of a parse
    (docs/workspace.md § Markdown documents)."""
    git.ensure_repo(paths.root)
    paths.check()
    try:
        safe_io.ensure_dir(paths.root, paths.directory)
    except safe_io.UnsafeEntry as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    written: list[Path] = []
    if safe_io.entry_kind(paths.root, paths.doc) == "none":
        if result is not None:
            # The reading copy when the parse was checked, else the transcription
            # (docs/workspace.md § Layout); `result.md` below is always the transcription.
            baseline = (
                result.reading_markdown if result.reading_markdown is not None else result.markdown
            )
        else:
            baseline = markdown_baseline(paths.root, document_id, max_bytes)
        if _write_if_missing(paths.root, paths.doc, baseline):
            written.append(paths.doc)
    if _write_if_missing(paths.root, paths.notes, ""):
        written.append(paths.notes)
    if _write_if_missing(paths.root, paths.meta, meta_text(document_id, job)):
        written.append(paths.meta)
    _copy_images(paths, document_id)  # excluded from history
    if job is None or result is None:
        return written  # a markdown document: no parse output, no crops, no shell copy
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


def _copy_images(paths: DocumentPaths, document_id: int) -> None:
    """Each stored image the document directory's `figures/` lacks, copied in through a
    descriptor like a crop: the shell's view of what the text names."""
    store = images_dir(paths.root) / str(document_id)
    if not store.is_dir():
        return
    for image in store.iterdir():
        target = paths.figures / image.name
        if (
            IMAGE_NAME.fullmatch(image.name)
            and image.is_file()
            and not image.is_symlink()
            and safe_io.entry_kind(paths.root, target) == "none"
        ):
            with image.open("rb") as reader:
                safe_io.write_from(paths.root, target, reader)


async def materialize_document(
    db: AsyncSession, settings: Settings, document: Document, *, locked: bool = False
) -> DocumentPaths:
    """Make sure the document's directory exists with its files; idempotent. The path is
    resolved under the workspace lock, after the row is re-read: a rename on the Files
    page holds the same lock across its row write and its move (docs/workspace.md).
    `locked`: the caller already holds that lock (it is not reentrant). A markdown
    document (no job) has no result to wait for: its baseline is its original."""
    job = result = None
    if document.job_id is not None:
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
        # Decided under the lock a move to the Trash holds: a copy written now for a
        # document in the Trash would stand under `files/` for a file that is not there
        # (docs/workspace.md § The Trash).
        folders = await user_folders(db, document.user_id)
        if not is_live(document, folders):
            raise ApiError(404, "invalid_request", "No such document")
        paths = await document_paths(db, settings, document)
        if safe_io.entry_kind(root, paths.directory) == "none":
            # An interrupted move to or from the Trash may have left the directory there:
            # it goes back where the row says before anything fresh is written, and a
            # directory the Trash could not return is not replaced by a fresh copy
            # (docs/workspace.md § The Trash).
            await recover_trash(db, settings, document.user_id)
            if safe_io.entry_kind(root, paths.directory) == "none" and await asyncio.to_thread(
                _trash_holds, root, document, folders
            ):
                raise ApiError(
                    409,
                    "invalid_request",
                    "The document's files could not be brought back from the Trash",
                )
        if safe_io.entry_kind(root, paths.directory) == "none":
            # Missing where the row points while HEAD holds this document's metadata
            # elsewhere: the tree moved it (a reverted rename whose reconciliation
            # failed after the revert's commit) and the row has not followed. Reconcile
            # first, or a fresh copy would be written beside the real one
            # (docs/workspace.md § Revisions).
            elsewhere = await asyncio.to_thread(
                git.path_at_head_containing,
                root,
                f'"document_id": {document.id},',
                f"*/{META_FILE}",
            )
            if elsewhere is not None and elsewhere != paths.relative / META_FILE:
                from .reconcile import reconcile_locked

                await reconcile_locked(db, settings, document.user_id)
                await db.refresh(document)
                paths = await document_paths(db, settings, document)
        if safe_io.entry_kind(root, paths.directory) == "none":
            await ensure_within_quota(db, settings, document.user_id, root)
        written = await asyncio.to_thread(
            _materialize_files,
            paths,
            document_id=document.id,
            job=job,
            result=result,
            figures_dir=Path(settings.figures_dir),
            max_bytes=settings.content_max_bytes,
        )
        # What was written needs a commit — and so does what an earlier materialization
        # wrote and never committed (the process died between the two): left in place
        # untracked, the first edit would commit `doc.md` as a new file rather than a
        # change against the parse baseline, and its revert would empty the document
        # (docs/workspace.md § Layout).
        uncommitted = written or await asyncio.to_thread(
            git.missing_at_head,
            paths.root,
            [paths.relative / name for name in (DOC_FILE, NOTES_FILE, META_FILE)],
        )
        if uncommitted:
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


async def make_folder_dir(settings: Settings, user_id: int, relative: Path) -> None:
    """The directory of a folder the library just created, so the shell sees it before
    any document lands in it. Nothing to commit: git records no empty directory. The
    caller holds the workspace lock (docs/workspace.md § Layout)."""
    root = user_root(settings.workspaces_dir, user_id)

    def _make() -> None:
        git.ensure_repo(root)
        safe_io.ensure_dir(root, root / relative)

    await asyncio.to_thread(_make)


async def current_revision(db: AsyncSession, settings: Settings, document: Document) -> str | None:
    """The revision the viewer would save against right now: the hash of `doc.md` as it
    is on disk, or of its baseline — the parse result (its reading copy when it was
    checked), a markdown document's original — when the document is not materialized yet;
    None when neither can be read. The per-turn context of a chat message (docs/chat.md
    § Prompt caching), read without the workspace lock: a hint, not a write."""
    paths = await document_paths(db, settings, document)
    try:
        text = await asyncio.to_thread(
            safe_io.read_text, paths.root, paths.doc, settings.content_max_bytes
        )
    except FileNotFoundError:
        text = None
    except (safe_io.UnsafeEntry, safe_io.EntryTooLarge, OSError, UnicodeDecodeError):
        return None
    if text is not None:
        return revision_of(text)
    if document.job_id is None:
        try:
            baseline = await asyncio.to_thread(
                markdown_baseline, paths.root, document.id, settings.content_max_bytes
            )
        except (ApiError, OSError):
            return None
        return revision_of(baseline)
    row = (
        await db.execute(
            select(Result.reading_markdown, Result.markdown).where(Result.job_id == document.job_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return revision_of(row.reading_markdown if row.reading_markdown is not None else row.markdown)


async def ensure_within_quota(
    db: AsyncSession, settings: Settings, user_id: int, root: Path
) -> None:
    """Refuse new material once the workspace is at the plan's storage (docs/workspace.md
    § Quota, docs/billing.md § Plans). A walk of the tree: cheap at the sizes a quota
    allows, and only on growth."""
    limit = ledger.storage_limit(settings, await ledger.plan_of(db, settings, user_id))
    used = await asyncio.to_thread(workspace_size, root)
    if used >= limit:
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
    relative: Path | None,
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
    next materialization. `relative` None makes no shell copy: the document is in the
    Trash, and its directory is not under `files/` (docs/workspace.md § The Trash).
    Returns the original's path."""
    git.ensure_repo(root)
    canonical = sources_dir(root) / f"{document_id}{_source_suffix(source, kind)}"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    # A name `original_of` never picks: its stem is not the id.
    staging = canonical.with_name(f".tmp-{canonical.name}")
    shutil.copyfile(source, staging)
    os.replace(staging, canonical)
    if relative is not None:
        _copy_source(root, root / relative, canonical)
    if not copy:
        source.unlink(missing_ok=True)
    return canonical


def copy_original(root: Path, source_id: int, target_id: int) -> bool:
    """A copy of one document's original as another's, whole or not at all; False when
    the first holds none. The shell's copy is not made here: the next materialization
    makes it from the original (docs/workspace.md § Layout)."""
    original = original_of(root, source_id)
    if original is None:
        return False
    canonical = original.with_name(f"{target_id}{original.suffix}")
    staging = canonical.with_name(f".tmp-{canonical.name}")
    shutil.copyfile(original, staging)
    os.replace(staging, canonical)
    return True


async def inherit_original(
    db: AsyncSession, settings: Settings, document: Document, job: Job, *, locked: bool = False
) -> bool:
    """A parse filed again from the History page: its upload went to the first document
    filed from it, so the new one takes a copy of that original — from any document of
    the parse that holds one — and, when none does but the upload is still there (the
    worker left it for a row that had not landed), takes the upload as `adopt_source`
    does (docs/workspace.md § Layout). False when the row is gone by the time the lock
    is held: another tab deleted it, and no original is kept under an id with no row."""
    root = user_root(settings.workspaces_dir, document.user_id)
    # The siblings, the quota and the copy under the workspace lock, as retention takes
    # them: a delete of the last sibling holds the same lock, so it cannot take the
    # original away between the look and the copy, and two filings at once cannot both
    # measure the room and both take it (§ Quota). `locked`: the caller holds it.
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        try:
            await db.refresh(document)
        except InvalidRequestError:
            return False  # deleted since it was filed; a delete holds this lock
        if original_of(root, document.id) is not None:
            # Retention got here first: the row was among the recipients it read.
            return True
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
            try:
                await ensure_within_quota(db, settings, document.user_id, root)
            except ApiError:
                logger.warning(
                    "workspace of user %d is at its quota; document %d keeps no original",
                    document.user_id,
                    document.id,
                )
                return True
            for sibling_id in siblings:
                if await asyncio.to_thread(copy_original, root, sibling_id, document.id):
                    return True
    try:
        # No commit of its own: the filing commits the row and the job together, through
        # the verified commit every filing takes (docs/workspace.md § Revisions).
        await adopt_source(db, settings, document, job, locked=locked, commit=False)
    except InvalidRequestError:
        return False
    return True


def _discard_originals(root: Path, document_ids: list[int]) -> None:
    for document_id in document_ids:
        while (original := original_of(root, document_id)) is not None:
            original.unlink(missing_ok=True)
        shutil.rmtree(images_dir(root) / str(document_id), ignore_errors=True)


async def discard_originals(settings: Settings, user_id: int, document_ids: list[int]) -> None:
    """The originals and inserted images of documents whose rows are gone
    (docs/workspace.md § Layout, § Inserted images)."""
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
    locked: bool = False,
    commit: bool = True,
) -> None:
    """The library row arrived after its parse finished, so the worker found no document
    to keep the upload for and left it in place — or the parse was answered from the
    cache, or joined one already on its way, and `upload` is the fresh file this row keeps
    for itself (docs/jobs.md § Retention): take it now. A copy that fails, or that the
    quota refuses (`ApiError`, 409), leaves nothing behind — the original and the shell's
    copy go — since the caller rolls the row back and no file may stay for a row that is
    gone (docs/workspace.md § Layout). `locked`: the caller holds the workspace lock.
    `commit`: whether the job's `source_deleted_at` is committed here; a filing passes
    False and commits the row and the job together, through its own verified commit."""
    if upload is not None:
        source = upload
    else:
        # Any terminal state: a failed parse's upload is kept for its row too (docs/jobs.md).
        if (
            job.status not in ("succeeded", "failed")
            or not job.source_path
            or job.source_deleted_at is not None
        ):
            return
        source = Path(job.source_path)
    root = user_root(settings.workspaces_dir, document.user_id)
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        if not source.is_file():
            # Taken already — by retention, which read this row as a recipient under the
            # job's row lock — or never there; looked at under the lock retention holds,
            # so a copy is never started from a file it is moving away.
            return
        await db.refresh(document)
        relative = document_relative_dir(
            await folder_names(db, document.user_id, document.folder_id), document.name
        )
        try:
            await ensure_within_quota(db, settings, document.user_id, root)
        except ApiError:
            # No room for the one file nothing can recreate: the filing is refused whole
            # (the caller rolls the row back), never a row that could not render a page.
            # The upload goes either way — a parse outside the library would have lost
            # it too (docs/workspace.md § Layout).
            source.unlink(missing_ok=True)
            raise
        try:
            await asyncio.to_thread(
                keep_source, root, relative, source, document_id=document.id, kind=job.kind
            )
        except Exception:
            await asyncio.to_thread(
                _undo_adoption,
                root,
                document.id,
                root / relative,
                _source_suffix(source, job.kind),
            )
            raise
    if upload is None:
        job.source_deleted_at = utcnow()
        if commit:
            await db.commit()


def _keep_markdown(root: Path, upload: Path, document_id: int) -> None:
    canonical = sources_dir(root) / f"{document_id}.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    staging = canonical.with_name(f".tmp-{canonical.name}")
    shutil.copyfile(upload, staging)
    os.replace(staging, canonical)


async def adopt_markdown(
    db: AsyncSession, settings: Settings, document: Document, upload: Path
) -> None:
    """Keep an uploaded `.md` as the markdown document's original, `{document_id}.md` beside
    the other originals; the upload goes either way (docs/workspace.md § Markdown
    documents). The quota refusing it (409), or a copy that fails, leaves nothing behind:
    the caller rolls the row back. No shell copy: `doc.md` is made from it. The caller
    holds the workspace lock."""
    root = user_root(settings.workspaces_dir, document.user_id)
    try:
        await ensure_within_quota(db, settings, document.user_id, root)
        await asyncio.to_thread(_keep_markdown, root, upload, document.id)
    except BaseException:
        await asyncio.to_thread(_discard_originals, root, [document.id])
        raise
    finally:
        upload.unlink(missing_ok=True)


def _keep_image(paths: DocumentPaths, document_id: int, prepared: Path) -> str:
    """Store a prepared image under its hash, then copy it into `figures/`; its name."""
    digest = hashlib.sha256()
    with prepared.open("rb") as reader:
        while chunk := reader.read(safe_io.READ_CHUNK):
            digest.update(chunk)
    name = f"img-{digest.hexdigest()[:16]}{prepared.suffix}"
    store = images_dir(paths.root) / str(document_id)
    store.mkdir(parents=True, exist_ok=True)
    kept = store / name
    if not kept.is_file():
        staging = store / f".tmp-{uuid.uuid4().hex}"
        try:
            shutil.copyfile(prepared, staging)
            os.replace(staging, kept)
        finally:
            staging.unlink(missing_ok=True)
    target = paths.figures / name
    if safe_io.entry_kind(paths.root, target) == "none":
        with kept.open("rb") as reader:
            safe_io.write_from(paths.root, target, reader)
    return name


async def keep_image(
    db: AsyncSession, settings: Settings, document: Document, prepared: Path
) -> str:
    """An image inserted into the document, prepared at `prepared`: kept beside the
    originals and copied into the document directory, under the workspace lock, with the
    quota checked first. Returns the file's name; the text that names it is the caller's
    (docs/workspace.md § Inserted images)."""
    root = user_root(settings.workspaces_dir, document.user_id)
    async with workspace_lock(root):
        paths = await materialize_document(db, settings, document, locked=True)
        await ensure_within_quota(db, settings, document.user_id, root)
        try:
            return await asyncio.to_thread(_keep_image, paths, document.id, prepared)
        except safe_io.UnsafeEntry as exc:
            raise ApiError(409, "invalid_request", NOT_PLAIN) from exc


def image_of(root: Path, document_id: int, name: str) -> Path | None:
    """The stored image `name` of a document, or None: only a name the store makes, only a
    plain file."""
    if not IMAGE_NAME.fullmatch(name):
        return None
    image = images_dir(root) / str(document_id) / name
    return image if image.is_file() and not image.is_symlink() else None


def _undo_adoption(root: Path, document_id: int, directory: Path, suffix: str) -> None:
    _discard_originals(root, [document_id])
    with contextlib.suppress(OSError, safe_io.UnsafeEntry):
        safe_io.remove_file(root, directory / f"{SOURCE_STEM}{suffix}")
    with contextlib.suppress(OSError):
        directory.rmdir()  # only when the copy was all it held


def _discard_adopted(root: Path, document_id: int, directory: Path) -> None:
    _discard_originals(root, [document_id])
    try:
        entries = safe_io.list_dir(root, directory)
    except (OSError, safe_io.UnsafeEntry):
        return
    for name, is_dir in entries:
        if not is_dir and name.startswith(f"{SOURCE_STEM}."):
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.remove_file(root, directory / name)
    with contextlib.suppress(OSError):
        directory.rmdir()  # only when the copy was all it held


async def discard_adopted(
    settings: Settings, user_id: int, document_id: int, relative: Path
) -> None:
    """What a filing took for a row that did not commit — the original under the row's
    id and the shell's `source.*` copy, with the directory if the copy was all it held —
    goes with the rollback: no file stands for a row that is gone, and the retry lands
    under the same name rather than a suffixed one (docs/workspace.md § Revisions). The
    caller holds the workspace lock."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_discard_adopted, root, document_id, root / relative)


# --- content ------------------------------------------------------------------------------


def _read(paths: DocumentPaths, max_bytes: int, *, document_id: int, parsed: bool) -> Content:
    paths.check()
    markdown = _read_text(paths.root, paths.doc, max_bytes)
    notes = _read_text(paths.root, paths.notes, max_bytes)
    return Content(
        markdown=markdown,
        revision=revision_of(markdown),
        notes=notes,
        notes_revision=revision_of(notes),
        # A markdown document's original is its first text, not pages to render.
        has_source=parsed
        and (original_of(paths.root, document_id) is not None or paths.source() is not None),
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
            _read,
            paths,
            settings.content_max_bytes,
            document_id=document.id,
            parsed=document.job_id is not None,
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
        raise ApiError(409, "revision_conflict", "The file changed since it was loaded")
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


# --- re-convert ---------------------------------------------------------------------------


def _take_parse(
    paths: DocumentPaths, *, document_id: int, job: Job, result: Result, max_bytes: int
) -> None:
    """`doc.md` and `.meta.json` from a re-convert's parse, as one revision; both as they
    were when it cannot be recorded (docs/workspace.md § Revisions)."""
    paths.check()
    try:
        before = {
            path: safe_io.read_bytes(paths.root, path, max_bytes)
            for path in (paths.doc, paths.meta)
        }
    except safe_io.EntryTooLarge as exc:
        raise ApiError(409, "invalid_request", TOO_LARGE) from exc
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    try:
        # The baseline materialization writes: the reading copy when the parse was checked.
        _write_text(
            paths.root,
            paths.doc,
            result.reading_markdown if result.reading_markdown is not None else result.markdown,
        )
        _write_text(paths.root, paths.meta, meta_text(document_id, job))
        git.commit_paths(
            paths.root,
            git.USER_AUTHOR,
            f"reconvert {paths.relative}",
            [paths.doc_relative, paths.relative / META_FILE],
        )
    except (ApiError, git.GitError) as exc:
        # Both files as they were, whichever step failed: a parse half taken, with no
        # revision, is neither version.
        for path, data in before.items():
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                if data is None:
                    safe_io.remove_file(paths.root, path)
                else:
                    safe_io.write_bytes(paths.root, path, data)
        raise ApiError(
            409, "invalid_request", "The re-convert could not be recorded; the file is as it was"
        ) from exc


async def take_reconvert(
    db: AsyncSession, settings: Settings, document: Document, *, locked: bool = False
) -> bool:
    """The document takes its re-convert once that parse has succeeded (docs/jobs.md
    § Re-converting a document): its directory as it stands is recorded first, then
    `doc.md` and `.meta.json` take the new parse as one revision, then the row names the
    new job and the pointer clears — files before the row, so a commit that fails leaves
    the pointer standing over files already in place, for the next reader to finish. A
    document in the Trash keeps its version: only the pointer clears. True when the
    document's parse changed. `locked`: the caller holds the workspace lock."""
    document_id, user_id = document.id, document.user_id
    root = user_root(settings.workspaces_dir, user_id)
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        try:
            # Read again under the lock: a delete, a dismissal and another taker hold it.
            if await db.get(Document, document_id, populate_existing=True) is None:
                return False
            if document.reconvert_job_id is None:
                return False
            job = await db.get(Job, document.reconvert_job_id, populate_existing=True)
            if job is None or job.status != "succeeded":
                return False
            if not is_live(document, await user_folders(db, user_id)):
                document.reconvert_job_id = None
                await db.commit()
                return False
            result = (await db.execute(select(Result).where(Result.job_id == job.id))).scalar_one()
            # The version it has, recorded, so the revision below is the change from it.
            paths = await materialize_document(db, settings, document, locked=True)
            await asyncio.to_thread(
                _take_parse,
                paths,
                document_id=document_id,
                job=job,
                result=result,
                max_bytes=settings.content_max_bytes,
            )
            document.job_id = job.id
            document.reconvert_job_id = None
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        try:
            # The new parse's crops and `result.md`, as any materialization writes them;
            # a copy that fails is finished by the next one.
            await materialize_document(db, settings, document, locked=True)
        except Exception:  # noqa: BLE001 - the document took its parse; the copies can wait
            logger.exception("could not materialize document %d after its re-convert", document_id)
    return True


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
        if renamed_to is not None and _is_own_rename(
            root, paths.relative, then, moved.get(renamed_to, renamed_to)
        ):
            # The revision that renamed the document itself: where its files came from
            # is this document's too. A file moved in from elsewhere is not that.
            continue
        for path in (then, renamed_to):
            if path is not None and not _under(moved.get(path, path), paths.relative):
                return True
    return False


def _is_own_rename(root: Path, document: Path, then: Path, now: Path) -> bool:
    """Whether a rename pair (`then` → `now`, the new side where it lives today) is the
    document's own directory being renamed: the new side is under the document, the old
    side ends with the same path inside it, and the directory it came from is gone —
    renamed away. A file a command moved in from another document fails the last two:
    its old side is that document's, and that document still stands
    (docs/workspace.md § Revisions)."""
    if not _under(now, document) or now == document:
        return False
    inner = now.relative_to(document)
    if len(then.parts) <= len(inner.parts) or then.parts[-len(inner.parts) :] != inner.parts:
        return False
    came_from = Path(*then.parts[: len(then.parts) - len(inner.parts)])
    if came_from == document or came_from.parts[:1] != (FILES_DIR,):
        return False
    return safe_io.entry_kind(root, root / came_from) == "none"


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

    # What the files the revert will write or remove hold *now* — which may be an edit a
    # failed shell commit left unrecorded, not what the head revision has — so a rollback
    # puts back exactly that. A file too large to hold is restored from the head instead.
    before_revert: dict[Path, bytes | None] = {}
    unheld: set[Path] = set()
    for now in [now for now, _data in writes] + removals:
        try:
            before_revert[now] = safe_io.read_bytes(root, root / now, max_bytes)
        except (safe_io.EntryTooLarge, safe_io.UnsafeEntry, OSError):
            # Present but not readable whole: the head revision is the best it can get.
            unheld.add(now)

    # Phase two: apply, every step through descriptors that follow nothing. What was
    # done is kept so that a failure anywhere — the tree, or the revision that records
    # it — rolls the tree back to as it was (docs/workspace.md § Revisions).
    restored: list[Path] = []
    done_moves: list[tuple[Path, Path]] = []
    moved_aside: list[tuple[Path, Path]] = []

    def rollback() -> None:
        # Only what this revert touched goes back, and to what it held before the revert
        # began: the tree may hold edits a failed shell commit left unrecorded, in these
        # files or elsewhere, and those are not this revert's to discard
        # (docs/workspace.md § Revisions).
        git.reset_index(root)
        for source, target in reversed(moved_aside):
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.rename(root, root / target, root / source)
        for current, then in reversed(done_moves):
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.rename(root, root / then, root / current)
        for now, data in before_revert.items():
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                if data is None:
                    if safe_io.remove_file(root, root / now):
                        _prune_empty(root, (root / now).parent)
                else:
                    safe_io.write_bytes(root, root / now, data)
        tracked = [now for now in unheld if git.tracked_at_head(root, now)]
        if tracked:
            git.restore_paths(root, tracked)
        for now in unheld - set(tracked):
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
        try:
            await reconcile_locked(db, settings, document.user_id)
            content = await read_content(db, settings, document, locked=True)
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - the revert stands; what failed is reported
            # The revert is a revision now; what failed is bringing the rows in line with
            # it (the database away at that moment). Said so, rather than reported as a
            # revert that did not happen: the rows follow at the next read of the
            # document, which reconciles first when HEAD holds the document elsewhere
            # than the row says, or at the next turn's reconciliation
            # (docs/workspace.md § Revisions).
            logger.exception(
                "revision %s of user %d was reverted as %s, but the library could not follow",
                commit,
                document.user_id,
                made,
            )
            raise ApiError(
                409,
                "invalid_request",
                "The revision was reverted, but the library could not be brought in line "
                "with it; reload the document",
            ) from exc
    return Reverted(commit=made, revision=content.revision)


# --- the tree -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """A directory move: the revision it made, and where a squatter that sat at the
    destination was moved to, so a rollback can put it back (docs/workspace.md § Layout)."""

    commit: str | None
    displaced: Path | None


def _move_tree(root: Path, source: Path, target: Path, message: str) -> Move | None:
    git.ensure_repo(root)
    if safe_io.entry_kind(root, root / source) == "none" or source == target:
        return None
    touched = [source, target]
    aside: Path | None = None
    try:
        if safe_io.entry_kind(root, root / target) != "none":
            # Something else sits where the row now points — a directory the shell made
            # under that name. It moves aside, so the document lands where the database
            # says and the squatter keeps what it holds (docs/workspace.md § Layout).
            aside = target.with_name(f"{target.name} ({uuid.uuid4().hex[:6]})")
            git.move(root, root / target, root / aside)
            touched.append(aside)
        git.move(root, root / source, root / target)
    except (safe_io.UnsafeEntry, OSError, git.GitError) as exc:
        # An ancestor the shell swapped for a link, or the disk refusing: refused, the
        # squatter back where it sat, and the caller's row rolls back to where the
        # files still are.
        if aside is not None:
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.rename(root, root / aside, root / target)
        plain = isinstance(exc, safe_io.UnsafeEntry)
        message = NOT_PLAIN if plain else "The move failed; nothing was changed"
        raise ApiError(409, "invalid_request", message) from exc
    try:
        return Move(git.commit_paths(root, git.USER_AUTHOR, message, touched), aside)
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


async def move_path(settings: Settings, user_id: int, source: Path, target: Path) -> Move | None:
    """Rename or move a document or folder directory after the database did. The caller
    holds the user's workspace lock across its database write and this move, so nothing
    can materialize at the new path in between (docs/workspace.md § Revisions). Returns
    the move — its revision, and the squatter it displaced — or None when there was
    nothing to move."""
    root = user_root(settings.workspaces_dir, user_id)
    return await asyncio.to_thread(_move_tree, root, source, target, f"move {source} -> {target}")


def _undo_move(root: Path, source: Path, target: Path, displaced: Path | None) -> None:
    touched = [target, source]
    git.move(root, root / target, root / source)
    if displaced is not None and safe_io.entry_kind(root, root / displaced) != "none":
        # The squatter back under the name it had, not left at its suffixed one.
        git.move(root, root / displaced, root / target)
        touched.append(displaced)
    git.commit_paths(root, git.USER_AUTHOR, f"restore {source}", touched)


async def undo_move(
    settings: Settings, user_id: int, source: Path, target: Path, displaced: Path | None
) -> None:
    """A move whose row did not commit: the whole of it undone — the directory back to
    `source`, and the squatter the move displaced back to `target` — as one revision, so
    a failed request renames nothing (docs/workspace.md § Layout)."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_undo_move, root, source, target, displaced)


@dataclass(frozen=True)
class Removal:
    """A directory moved aside for a delete: the revision that records the removal and
    where the directory waits, whole, until the rows are gone (docs/workspace.md)."""

    commit: str | None
    aside: Path


# Beside each aside directory, the path it was removed from: what lets reconciliation
# bring back an aside that holds no document metadata (docs/workspace.md § Revisions).
ORIGIN_SUFFIX = ".origin"


def origin_marker(root: Path, aside: str | Path) -> Path:
    return root / f"{Path(aside).name}{ORIGIN_SUFFIX}"


def aside_origin(root: Path, aside: str | Path) -> Path | None:
    """The path an aside directory was removed from, or None when nothing says."""
    try:
        text = safe_io.read_text(root, origin_marker(root, aside), 4096)
    except (safe_io.UnsafeEntry, safe_io.EntryTooLarge, OSError, UnicodeDecodeError):
        return None
    if text is None:
        return None
    origin = Path(text.strip())
    if not origin.parts or origin.parts[0] != FILES_DIR or ".." in origin.parts:
        return None
    return origin


def _remove_tree(root: Path, relative: Path, message: str) -> Removal | None:
    git.ensure_repo(root)
    if safe_io.entry_kind(root, root / relative) == "none":
        return None
    aside = Path(f"{safe_io.TEMP_PREFIX}{uuid.uuid4().hex}")
    try:
        # Where it stood, written first: what brings the directory back should the rows
        # survive and nothing inside it name a document. First, so a process that dies
        # between the two steps leaves a marker beside no aside — swept by the next
        # reconciliation, the directory standing where it was — and never an aside that
        # no marker names, which that sweep would take whole (docs/workspace.md
        # § Revisions).
        safe_io.write_text(root, origin_marker(root, aside), relative.as_posix() + "\n")
    except (OSError, safe_io.UnsafeEntry) as exc:
        raise ApiError(
            409, "invalid_request", "The removal could not be recorded; nothing was changed"
        ) from exc
    try:
        # Aside rather than gone: outside `files/` and outside history, the directory
        # waits whole — originals and crops included — until the rows are gone, and comes
        # back whole if they are not. Through descriptors that follow nothing: an
        # ancestor the shell swapped for a link to another tree is a refusal.
        safe_io.rename(root, root / relative, root / aside)
    except safe_io.UnsafeEntry as exc:
        with contextlib.suppress(OSError, safe_io.UnsafeEntry):
            safe_io.remove_file(root, origin_marker(root, aside))
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    try:
        commit = git.commit_paths(root, git.USER_AUTHOR, message, [relative])
    except git.GitError as exc:
        # Back where it was before the refusal: nothing waits aside for a sweep to take.
        safe_io.rename(root, root / aside, root / relative)
        safe_io.remove_file(root, origin_marker(root, aside))
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

    def _discard() -> None:
        safe_io.remove_tree(root, root / removal.aside)
        safe_io.remove_file(root, origin_marker(root, removal.aside))

    await asyncio.to_thread(_discard)


def _restore_removed(root: Path, relative: Path, removal: Removal) -> None:
    safe_io.rename(root, root / removal.aside, root / relative)
    safe_io.remove_file(root, origin_marker(root, removal.aside))
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


def sweep_orphaned_originals(root: Path, live_ids: Collection[int]) -> int:
    """Remove the originals beside the git directory whose document row is gone: what a
    delete the process died in, after its rows' commit and before its cleanup, leaves
    behind (docs/workspace.md § Revisions). Under the workspace lock, which every writer
    of an original holds, so none on its way to a row about to commit is taken."""
    directory = sources_dir(root)
    if not directory.is_dir():
        return 0
    swept = 0
    for original in directory.iterdir():
        try:
            document_id = int(original.stem)
        except ValueError:
            continue  # a staging file, named so `original_of` never picks it
        if document_id in live_ids:
            continue
        original.unlink(missing_ok=True)
        swept += 1
    return swept


def sweep_orphaned_images(root: Path, live_ids: Collection[int]) -> int:
    """The images of documents whose rows are gone, as `sweep_orphaned_originals` takes
    their originals, under the same lock."""
    directory = images_dir(root)
    if not directory.is_dir():
        return 0
    swept = 0
    for entry in directory.iterdir():
        if not entry.name.isdigit() or int(entry.name) in live_ids:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        swept += 1
    return swept


def sweep_aside(root: Path, keep: Collection[str] = ()) -> int:
    """Remove what a delete left aside and never discarded (a crash between its commit
    and the discard); run under the workspace lock, where no delete is in flight. `keep`
    names the aside directories that still hold a document whose row survived, which
    reconciliation restores instead (docs/workspace.md § Revisions)."""
    swept = 0
    entries = safe_io.list_dir(root, root)
    present = {name for name, is_dir in entries if is_dir}
    for name, is_dir in entries:
        if not name.startswith(safe_io.TEMP_PREFIX):
            continue
        if is_dir and name not in keep:
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.remove_tree(root, root / name)
                safe_io.remove_file(root, origin_marker(root, name))
                swept += 1
        elif not is_dir and name.endswith(ORIGIN_SUFFIX):
            # An origin whose aside is gone, or goes with this sweep.
            aside = name.removesuffix(ORIGIN_SUFFIX)
            if aside not in present or aside not in keep:
                with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                    safe_io.remove_file(root, root / name)
    return swept


# --- the Trash ----------------------------------------------------------------------------

# What the Trash names its entries: `trash_entry` (layout.py) in reverse.
TRASH_ENTRY = re.compile(r"(document|folder)-([0-9]+)")


def _stash_tree(root: Path, relative: Path, entry: str) -> bool:
    git.ensure_repo(root)
    if safe_io.entry_kind(root, root / relative) == "none":
        return False  # never materialized (a parse still running): nothing to move
    target = trash_root(root) / entry
    if safe_io.entry_kind(root, target) != "none":
        # An entry the repair rule could not return, its place taken: kept, never
        # overwritten (docs/workspace.md § The Trash).
        raise ApiError(409, "invalid_request", "The Trash still holds an earlier copy of this item")
    try:
        # Through descriptors that follow nothing, into `.sightread/trash` (made on the
        # way when missing): an ancestor the shell swapped for a link is a refusal.
        safe_io.rename(root, root / relative, target)
    except safe_io.UnsafeEntry as exc:
        raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
    except OSError as exc:
        raise ApiError(
            409, "invalid_request", "The move to the Trash failed; nothing was changed"
        ) from exc
    try:
        git.commit_paths(root, git.USER_AUTHOR, f"trash {relative}", [relative])
    except git.GitError as exc:
        # Back where it was before the refusal: nothing waits in the Trash for a row
        # that will not say so.
        safe_io.rename(root, target, root / relative)
        raise ApiError(
            409,
            "invalid_request",
            "The move to the Trash could not be recorded; nothing was changed",
        ) from exc
    return True


async def stash_path(settings: Settings, user_id: int, relative: Path, entry: str) -> bool:
    """Move a folder's or a document's directory into the Trash after the database
    marked it, as the revision `trash <path>`; False when there was no directory. The
    caller holds the workspace lock (docs/workspace.md § The Trash)."""
    root = user_root(settings.workspaces_dir, user_id)
    return await asyncio.to_thread(_stash_tree, root, relative, entry)


def _unstash_tree(root: Path, entry: str, relative: Path, message: str) -> bool:
    git.ensure_repo(root)
    source = trash_root(root) / entry
    if safe_io.entry_kind(root, source) == "none":
        return False
    touched = [relative]
    aside: Path | None = None
    if safe_io.entry_kind(root, root / relative) != "none":
        # Something the shell made under that name meanwhile moves aside, as a move moves
        # a squatter, so the item lands where its row says (docs/workspace.md § Layout).
        aside = relative.with_name(f"{relative.name} ({uuid.uuid4().hex[:6]})")
        git.move(root, root / relative, root / aside)
        touched.append(aside)
    try:
        safe_io.rename(root, source, root / relative)
    except BaseException:
        if aside is not None:
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.rename(root, root / aside, root / relative)
        raise
    try:
        git.commit_paths(root, git.USER_AUTHOR, message, touched)
    except git.GitError:
        safe_io.rename(root, root / relative, source)
        if aside is not None:
            safe_io.rename(root, root / aside, root / relative)
        raise
    return True


async def unstash_path(
    settings: Settings, user_id: int, entry: str, relative: Path, message: str
) -> bool:
    """Bring an entry of the Trash back to `relative` under `files/` as one revision:
    Put Back once its rows are committed, or a move to the Trash whose rows did not
    commit. False when the Trash held nothing for it; raises when the tree refuses, the
    entry then still in the Trash for the repair rule. The caller holds the workspace
    lock."""
    root = user_root(settings.workspaces_dir, user_id)
    return await asyncio.to_thread(_unstash_tree, root, entry, relative, message)


def trash_home(row: Folder | Document, by_id: dict[int, Folder]) -> Path:
    """Where the directory of a row that is not trashed itself belongs, relative to the
    user root: under `files/` when the row is live, else inside the entry of the nearest
    trashed folder above it, which holds everything under that folder."""
    parent = row.parent_id if isinstance(row, Folder) else row.folder_id
    chain = folder_chain(by_id, parent)
    home = Path(FILES_DIR)
    below = chain
    for index in range(len(chain) - 1, -1, -1):
        if chain[index].trashed_at is not None:
            home = Path(SIGHTREAD_DIR, TRASH_DIR, trash_entry("folder", chain[index].id))
            below = chain[index + 1 :]
            break
    for folder in below:
        home /= path_segment(folder.name)
    return home / path_segment(row.name)


def _trash_entries(root: Path) -> list[tuple[str, int, str]]:
    """`(kind, row id, entry name)` for each entry of the Trash."""
    if safe_io.entry_kind(root, trash_root(root)) != "dir":
        return []
    found = []
    for name, _is_dir in safe_io.list_dir(root, trash_root(root)):
        match = TRASH_ENTRY.fullmatch(name)
        if match is not None:
            found.append((match[1], int(match[2]), name))
    return found


def _return_entry(root: Path, name: str, home: Path) -> bool:
    source = trash_root(root) / name
    if safe_io.entry_kind(root, root / home) != "none":
        logger.warning("the Trash keeps %s: %s is taken", name, home)
        return False
    try:
        safe_io.rename(root, source, root / home)
    except (safe_io.UnsafeEntry, OSError):
        logger.exception("could not return %s from the Trash to %s", name, home)
        return False
    if home.parts[0] != FILES_DIR:
        return True  # from one entry of the Trash into another: outside history
    try:
        git.ensure_repo(root)
        git.commit_paths(root, git.USER_AUTHOR, f"restore {home}", [home])
    except git.GitError:
        logger.exception("the return of %s to %s could not be recorded; undone", name, home)
        with contextlib.suppress(OSError, safe_io.UnsafeEntry):
            safe_io.rename(root, root / home, source)
        return False
    return True


def _discard_entries(root: Path, names: Iterable[str]) -> None:
    for name in names:
        safe_io.remove_tree(root, trash_root(root) / name)


async def recover_trash(db: AsyncSession, settings: Settings, user_id: int) -> int:
    """The repair rule of docs/workspace.md § The Trash: an entry whose row is not trashed
    itself goes back where the row places it, and an entry whose row is gone — a delete
    for good that died before its cleanup — is removed. Returns how many entries moved.
    Reads the rows only; the caller holds the workspace lock, which every move into or
    out of the Trash holds."""
    root = user_root(settings.workspaces_dir, user_id)
    entries = await asyncio.to_thread(_trash_entries, root)
    if not entries:
        return 0
    folders = await user_folders(db, user_id)
    wanted = [row_id for kind, row_id, _name in entries if kind == "document"]
    documents = (
        {
            row.id: row
            for row in (
                await db.execute(
                    select(Document).where(Document.user_id == user_id, Document.id.in_(wanted))
                )
            ).scalars()
        }
        if wanted
        else {}
    )
    moved = 0
    for kind, row_id, name in entries:
        row = folders.get(row_id) if kind == "folder" else documents.get(row_id)
        if row is None:
            logger.warning("the Trash of user %d held %s with no row; removed", user_id, name)
            await asyncio.to_thread(_discard_entries, root, [name])
            continue
        if row.trashed_at is not None:
            continue
        if await asyncio.to_thread(_return_entry, root, name, trash_home(row, folders)):
            moved += 1
    return moved


def _trash_holds(root: Path, document: Document, by_id: dict[int, Folder]) -> bool:
    """Whether the Trash holds an entry the document's directory may be in: its own, or a
    folder's above it — what the repair rule could not return."""
    names = {name for _kind, _row_id, name in _trash_entries(root)}
    return trash_entry("document", document.id) in names or any(
        trash_entry("folder", row.id) in names for row in folder_chain(by_id, document.folder_id)
    )


async def held_in_trash(
    settings: Settings, user_id: int, *, folders: Iterable[int], documents: Iterable[int]
) -> bool:
    """Whether the Trash holds an entry of any of these rows. Asked of live rows after the
    repair rule ran, it is what the rule could not bring back: their files are not under
    `files/`, and nothing may take them for never written (docs/workspace.md § Copies)."""
    root = user_root(settings.workspaces_dir, user_id)
    held = {(kind, row_id) for kind, row_id, _name in await asyncio.to_thread(_trash_entries, root)}
    return any(("folder", row_id) in held for row_id in folders) or any(
        ("document", row_id) in held for row_id in documents
    )


async def discard_trash(settings: Settings, user_id: int, names: Iterable[str]) -> None:
    """The entries of items deleted for good, once their rows are gone."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_discard_entries, root, list(names))


# --- copies -------------------------------------------------------------------------------


@dataclass(frozen=True)
class CopiedDocument:
    """One document of a copy: its row and the new one, the parse behind both, where its
    directory sits below the copied item's (`.` for a document copied on its own), the
    documents whose original the new one may take, its source first, and whether that
    parse is still running — a directory at its path is then not its document's yet, and
    the copy gets its files when the parse ends, as the original does."""

    source_id: int
    target_id: int
    job: Job | None
    inner: Path
    donors: tuple[int, ...]
    parsing: bool


@dataclass(frozen=True)
class Copy:
    """A copy on disk: the revision it made, and where a squatter that sat at the
    destination was moved to (docs/workspace.md § Copies)."""

    commit: str | None
    displaced: Path | None


def _copy_stored_images(root: Path, source_id: int, target_id: int) -> None:
    store = images_dir(root) / str(source_id)
    if not store.is_dir() or store.is_symlink():
        return
    target = images_dir(root) / str(target_id)
    target.mkdir(parents=True, exist_ok=True)
    for image in store.iterdir():
        if IMAGE_NAME.fullmatch(image.name) and image.is_file() and not image.is_symlink():
            shutil.copyfile(image, target / image.name)


def _undo_copy(root: Path, target: Path, displaced: Path | None, target_ids: list[int]) -> None:
    safe_io.remove_tree(root, root / target)
    if displaced is not None and safe_io.entry_kind(root, root / displaced) != "none":
        safe_io.rename(root, root / displaced, root / target)
    _discard_originals(root, target_ids)


def _copy_files(
    root: Path, source: Path, target: Path, documents: list[CopiedDocument], *, folder: bool
) -> Copy:
    git.ensure_repo(root)
    aside: Path | None = None
    if safe_io.entry_kind(root, root / target) != "none":
        # A directory the shell made under the copy's name: aside, as a move's squatter.
        aside = target.with_name(f"{target.name} ({uuid.uuid4().hex[:6]})")
        try:
            git.move(root, root / target, root / aside)
        except (safe_io.UnsafeEntry, OSError) as exc:
            raise ApiError(
                409, "invalid_request", "The copy could not be made; nothing was copied"
            ) from exc
    directories = {document.inner for document in documents}
    parsing = {document.inner for document in documents if document.parsing}

    def left_behind(inner: Path) -> bool:
        # What materialization makes again for the new id — the shell's copy of the
        # original, the crops and images — the metadata, written fresh below, and the
        # directory of a parse still running, which is not its document's yet.
        return inner in parsing or (
            inner.parent in directories
            and (inner.name in (FIGURES_DIR, META_FILE) or inner.name.startswith(f"{SOURCE_STEM}."))
        )

    try:
        if safe_io.entry_kind(root, root / source) == "dir" and Path() not in parsing:
            safe_io.copy_tree(root, root / source, root / target, skip=left_behind)
        elif folder:
            safe_io.ensure_dir(root, root / target)
        for document in documents:
            directory = root / target / document.inner
            if not document.parsing and safe_io.entry_kind(root, directory) == "dir":
                safe_io.write_text(
                    root, directory / META_FILE, meta_text(document.target_id, document.job)
                )
            for donor in document.donors:
                if copy_original(root, donor, document.target_id):
                    break
            _copy_stored_images(root, document.source_id, document.target_id)
        touched = [target] if aside is None else [target, aside]
        commit = git.commit_paths(root, git.USER_AUTHOR, f"copy {source} -> {target}", touched)
    except BaseException as exc:
        # Whole or not at all: what the copy made goes, and the squatter comes back.
        try:
            _undo_copy(root, target, aside, [document.target_id for document in documents])
        except Exception:  # noqa: BLE001 - the refusal stands; the leftovers are logged
            logger.exception("could not take back the copy at %s", target)
        if isinstance(exc, safe_io.TreeTooDeep):
            raise ApiError(
                409, "invalid_request", "The item has a directory nested too deep to copy"
            ) from exc
        if isinstance(exc, safe_io.UnsafeEntry):
            raise ApiError(409, "invalid_request", NOT_PLAIN) from exc
        if isinstance(exc, (OSError, git.GitError)):
            raise ApiError(
                409, "invalid_request", "The copy could not be made; nothing was copied"
            ) from exc
        raise
    return Copy(commit, aside)


async def copy_path(
    settings: Settings,
    user_id: int,
    source: Path,
    target: Path,
    documents: list[CopiedDocument],
    *,
    folder: bool,
) -> Copy:
    """Copy a document's or a folder's directory to `target` after the database made
    the rows, with the originals and inserted images under the new ids, as the revision
    `copy <from> -> <to>`; whole or nothing (409). The caller holds the workspace lock
    and checked the quota (docs/workspace.md § Copies)."""
    root = user_root(settings.workspaces_dir, user_id)
    return await asyncio.to_thread(_copy_files, root, source, target, documents, folder=folder)


def _remove_copy(root: Path, target: Path, made: Copy, target_ids: list[int]) -> None:
    safe_io.remove_tree(root, root / target)
    touched = [target]
    if made.displaced is not None and safe_io.entry_kind(root, root / made.displaced) != "none":
        git.move(root, root / made.displaced, root / target)
        touched.append(made.displaced)
    _discard_originals(root, target_ids)
    git.commit_paths(root, git.USER_AUTHOR, f"remove {target}", touched)


async def remove_copy(
    settings: Settings, user_id: int, target: Path, made: Copy, target_ids: list[int]
) -> None:
    """A copy whose rows did not commit: its directory, originals and images go, the
    squatter it displaced comes back, as a revision of its own."""
    root = user_root(settings.workspaces_dir, user_id)
    await asyncio.to_thread(_remove_copy, root, target, made, target_ids)


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
    """What the user holds: the tree, the history, the originals and the inserted images
    beside it (the git, sources and images directories are the service's own, outside the
    mount, walked the same way from their own roots)."""
    return (
        directory_size(root)
        + directory_size(git_dir(root))
        + directory_size(sources_dir(root))
        + directory_size(images_dir(root))
    )
