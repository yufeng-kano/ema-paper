"""Filing into the library: names, the suffixed insert that re-reads a taken name, the
commit that verifies itself, a folder with its directory, and a file kept as it is — what
the upload routes and the chat agent's download share (docs/workspace.md § Files). Nothing
here is a route: the worker files a download through the same steps, so this lives beside
the tree it writes (docs/project-structure.md)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import ColumnElement, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import ledger
from ..config import Settings
from ..db.models import FILE_KIND, Document, Folder
from ..errors import ApiError
from . import documents as workspace_documents
from . import safe_io
from .layout import FILES_DIR, document_relative_dir, path_segment, segment_name, user_root
from .trash import lock_user, read_row

logger = logging.getLogger(__name__)

FOLDER_NAME_MAX = 255
DOCUMENT_NAME_MAX = 512
# How many " (2)" attempts a colliding name gets before the folder is simply too crowded.
NAME_ATTEMPTS = 500
# An insert can lose a name race with another tab; the retry re-reads the folder's names.
INSERT_ATTEMPTS = 3
STORAGE_FULL = "The workspace is at its quota; free some space first"

# A name is a label in a tree, never a path. Separators would make a breadcrumb lie about
# where a file is, and control characters would make two different names look identical.
UNSAFE_IN_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")


# --- names and places -------------------------------------------------------------------


def clean_name(raw: str, limit: int) -> str:
    """A display name: NFC, no separators, no control characters, trimmed and capped.
    Normalized here, before uniqueness is decided, so two spellings of one name are one
    name in the library as they are one directory in the workspace (docs/workspace.md)."""
    name = UNSAFE_IN_NAME.sub(" ", unicodedata.normalize("NFC", raw)).strip()
    if not name:
        raise ApiError(400, "invalid_request", "A name is required")
    return name[:limit].strip()


def free_name(taken: set[str], name: str, limit: int) -> str:
    """`report.pdf` → `report (2).pdf` when the folder already holds that name.

    A file system resolves a collision rather than refusing the copy, and the suffix goes
    before the extension so the file still opens as what it is (docs/web.md § Files).

    The stem gives up whatever room the suffix needs. A name already at the column's limit
    would otherwise grow past it, and PostgreSQL answers that with a truncation error — on
    an upload, after `submit_parse` has committed, so the parse would run and bill the
    user's key while their request failed.
    """
    if name not in taken:
        return name
    stem, dot, extension = name.rpartition(".")
    if not stem:
        # No extension at all, or a dotfile — the whole thing is the stem.
        stem, dot, extension = name, "", ""
    for index in range(2, NAME_ATTEMPTS):
        suffix = f" ({index}){dot}{extension}"
        room = limit - len(suffix)
        # `room <= 0` needs an extension longer than the whole column, which `clean_name`
        # already makes impossible; should it happen, the extension gives way and the
        # counter stays, so every try is a new candidate (as the name migration does).
        candidate = f"{stem[:room]}{suffix}" if room > 0 else suffix[:limit]
        if candidate not in taken:
            return candidate
    raise ApiError(400, "invalid_request", "Too many items with this name in one folder")


def placed_in(column: ColumnElement, place: int | None) -> ColumnElement[bool]:
    """`folder_id = 3`, or `folder_id IS NULL` for the root."""
    return column.is_(None) if place is None else column == place


async def sibling_names(
    db: AsyncSession,
    user_id: int,
    place: int | None,
    *,
    exclude_folder: int | None = None,
    exclude_document: int | None = None,
) -> set[str]:
    """Every name taken in one folder — the folders and the documents in it alike. The
    workspace gives each name one directory, so a folder and a file cannot share one
    (docs/workspace.md § Layout); the caller holds the user's row so the set is current.
    What is in the Trash takes no name (docs/database.md § Rules)."""
    folders = select(Folder.name).where(
        Folder.user_id == user_id,
        placed_in(Folder.parent_id, place),
        Folder.trashed_at.is_(None),
    )
    if exclude_folder is not None:
        folders = folders.where(Folder.id != exclude_folder)
    documents = select(Document.name).where(
        Document.user_id == user_id,
        placed_in(Document.folder_id, place),
        Document.trashed_at.is_(None),
    )
    if exclude_document is not None:
        documents = documents.where(Document.id != exclude_document)
    taken = set((await db.execute(folders)).scalars().all())
    taken.update((await db.execute(documents)).scalars().all())
    return taken


async def folder_exists(db: AsyncSession, user_id: int, folder_id: int) -> bool:
    """Whether the folder is still there to file into: the caller's, and not in the Trash."""
    folders = await workspace_documents.user_folders(db, user_id)
    return folder_id in folders and folder_id not in workspace_documents.trashed_folder_ids(
        folders.values()
    )


async def folder_relative_dir(db: AsyncSession, user_id: int, folder_id: int) -> Path:
    names = await workspace_documents.folder_names(db, user_id, folder_id)
    path = Path(FILES_DIR)
    for name in names:
        path /= path_segment(name)
    return path


async def document_relative(db: AsyncSession, row: Document) -> Path:
    names = await workspace_documents.folder_names(db, row.user_id, row.folder_id)
    return document_relative_dir(names, row.name)


# --- inserting and committing -------------------------------------------------------------


async def insert_named[RowT: (Folder, Document)](
    db: AsyncSession,
    *,
    user_id: int,
    into: int | None,
    build: Callable[[str], RowT],
    taken: Callable[[], Awaitable[set[str]]],
    wanted: str,
    limit: int,
    message: str,
    commit: bool = True,
) -> RowT:
    """Insert a row whose name has to be free where it lands, re-reading if it was taken.

    Two tabs can both read the same free name before either commits. The loser is not a
    conflict to report: a collision on create is resolved rather than refused
    (docs/web.md § Files), so it re-reads the folder and takes the next name. Only a
    request that keeps losing gives up. Without `commit` the row is flushed and left in
    the open transaction, for a caller that has more to make true before it commits
    (a filing, whose original lands first — docs/workspace.md § Backups).

    `into` is the folder the row lands in, decided again under the user's row lock: a
    move to the Trash takes that lock too, so a folder trashed since the caller's check
    answers 404 here rather than taking a row that would be hidden in the Trash at once
    (docs/database.md § Rules).
    """
    attempts = INSERT_ATTEMPTS
    while True:
        attempts -= 1
        await lock_user(db, user_id)
        if into is not None:
            # Columns, not rows: the session may hold the folders the caller's check
            # loaded, and a row already in it is not read again.
            folders = (
                await db.execute(
                    select(Folder.id, Folder.parent_id, Folder.trashed_at).where(
                        Folder.user_id == user_id
                    )
                )
            ).all()
            if into not in {folder.id for folder in folders} or into in (
                workspace_documents.trashed_folder_ids(folders)
            ):
                raise ApiError(404, "invalid_request", "No such folder")
        row = build(free_name(await taken(), wanted, limit))
        db.add(row)
        try:
            if commit:
                await db.commit()
            else:
                await db.flush()
            return row
        except IntegrityError as exc:
            await db.rollback()
            if attempts <= 0:
                raise ApiError(409, "invalid_request", message) from exc


async def commit_filing(
    db: AsyncSession,
    row: Document,
    *,
    what: str,
    undo: Callable[[], Awaitable[None]] | None = None,
) -> Document:
    """Commit a filing — its row inserted and its original in place. A commit that raises
    is verified on a connection of its own, as a move's is: the row present means
    PostgreSQL made it durable and the connection dropped after, so the filing stands
    and is answered as made — reported as nothing filed, the browser's retry would file
    a suffixed twin of the same parse; the row absent means it rolled back, and nothing
    was filed — `undo` then takes back what the filing put on disk for the row, so no
    file stands for a row that is gone; a read that fails says nothing either way, and
    is answered as unconfirmed rather than as a filing that did not happen
    (docs/workspace.md § Revisions)."""
    try:
        await db.commit()
        return row
    except Exception as exc:  # noqa: BLE001 - verified below, whatever raised
        readable, landed = await read_row(db, Document, row.id)
        if readable and isinstance(landed, Document):
            logger.warning("the filing of document %d was committed despite the error", row.id)
            return landed
        with contextlib.suppress(Exception):
            await db.rollback()
        if readable:
            logger.exception("the filing of document %d did not commit", row.id)
            if undo is not None:
                try:
                    await undo()
                except Exception:  # noqa: BLE001 - the refusal stands; the leftovers are logged
                    logger.exception("could not take back the files of document %d", row.id)
            raise ApiError(
                409, "invalid_request", f"{what} could not be filed; nothing was filed"
            ) from exc
        raise ApiError(
            409,
            "invalid_request",
            f"{what} may have been filed, but the database could not confirm it; "
            "reload the library before trying again",
        ) from exc


# --- folders and files ------------------------------------------------------------------


async def make_folder(
    db: AsyncSession, settings: Settings, user_id: int, parent_id: int | None, name: str
) -> Folder:
    """A folder: its row and its directory, the row's name suffixed where the parent holds
    it. The caller holds the workspace lock, so the shell sees the folder as soon as the
    row exists and a workspace that takes no host write refused it up front
    (docs/workspace.md § Layout); `parent_id` is the caller's checked, live folder."""
    try:
        row = await insert_named(
            db,
            user_id=user_id,
            into=parent_id,
            build=lambda name: Folder(user_id=user_id, parent_id=parent_id, name=name),
            taken=lambda: sibling_names(db, user_id, parent_id),
            wanted=clean_name(name, FOLDER_NAME_MAX),
            limit=FOLDER_NAME_MAX,
            message="A folder with this name already exists here",
        )
    except ApiError:
        # A parent deleted between the caller's check and the insert fails the foreign
        # key, which arrives as the same IntegrityError a taken name does — and retrying
        # with a destination that no longer exists only ever produces a 409 about the
        # wrong thing.
        if parent_id is None or await folder_exists(db, user_id, parent_id):
            raise
        raise ApiError(404, "invalid_request", "No such folder") from None
    relative = await folder_relative_dir(db, user_id, row.id)
    try:
        await workspace_documents.make_folder_dir(settings, user_id, relative)
    except (safe_io.UnsafeEntry, OSError):
        # An entry the shell planted where the directory belongs: the row stands, the
        # tree is the shell's, and the next materialization under it says so.
        logger.warning("the directory of folder %d of user %d could not be made", row.id, user_id)
    return row


async def ensure_folders(
    db: AsyncSession, settings: Settings, user_id: int, names: list[str]
) -> int | None:
    """The folder at the end of a path of names — `Papers`, then `2024` inside it — each
    found among the user's live folders by its cleaned name or made where it is missing
    (docs/chat.md § Tools, `download_file`). None for an empty path: the top level. The
    caller holds the workspace lock."""
    parent_id: int | None = None
    for raw in names:
        wanted = clean_name(raw, FOLDER_NAME_MAX)
        folders = await workspace_documents.user_folders(db, user_id)
        trashed = workspace_documents.trashed_folder_ids(folders.values())
        found = next(
            (
                folder
                for folder in folders.values()
                if folder.parent_id == parent_id
                and folder.name == wanted
                and folder.id not in trashed
            ),
            None,
        )
        if found is None:
            found = await make_folder(db, settings, user_id, parent_id, wanted)
        parent_id = found.id
    return parent_id


async def keep_file(
    db: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    folder_id: int | None,
    temp: Path,
    filename: str,
    declared_type: str | None,
    size: int,
    what: str = "The upload",
    revision: str = "upload",
) -> Document:
    """A file kept as it is (docs/workspace.md § Files): its bytes are in the tree already,
    under a write-in-progress name at the top of `files/`; the row and the move into place
    under the workspace lock the caller holds (with the backup lock shared), the row
    committed last, as a markdown filing is — no entry without its file, no file without
    its entry. `revision` names the commit (`upload <path>`, `download <path>`)."""
    try:
        wanted = clean_name(filename, DOCUMENT_NAME_MAX)
    except ApiError:
        wanted = "upload"
    media_type = workspace_documents.file_media_type(filename, declared_type)
    root = user_root(settings.workspaces_dir, user_id)
    # The bytes once they are there, counted with the tree they now sit in (§ Quota).
    limit = ledger.storage_limit(settings, await ledger.plan_of(db, settings, user_id))
    used = (await workspace_documents.measure_storage(db, settings, user_id, root)).total
    if used > limit:
        raise ApiError(409, "storage_full", STORAGE_FULL)
    directory = (
        await folder_relative_dir(db, user_id, folder_id)
        if folder_id is not None
        else Path(FILES_DIR)
    )

    async def taken() -> set[str]:
        # The rows' names, and the names on disk: a file the shell made under the wanted
        # name, with no row yet, would otherwise be overwritten by the move.
        names = await sibling_names(db, user_id, folder_id)
        try:
            entries = await asyncio.to_thread(safe_io.list_dir, root, root / directory)
        except (safe_io.UnsafeEntry, OSError):
            return names
        return names | {segment_name(name) for name, _is_dir in entries}

    try:
        row = await insert_named(
            db,
            user_id=user_id,
            into=folder_id,
            build=lambda name: Document(
                user_id=user_id,
                folder_id=folder_id,
                job_id=None,
                kind=FILE_KIND,
                name=name,
                media_type=media_type,
                size_bytes=size,
            ),
            taken=taken,
            wanted=wanted,
            limit=DOCUMENT_NAME_MAX,
            message="A file with this name already exists here",
            commit=False,
        )
    except ApiError:
        if folder_id is None or await folder_exists(db, user_id, folder_id):
            raise
        raise ApiError(404, "invalid_request", "No such folder") from None
    row_id = row.id
    relative = await document_relative(db, row)
    try:
        await asyncio.to_thread(
            workspace_documents.place_file, root, temp, relative, message=revision
        )
    except ApiError:
        await db.rollback()
        raise
    except Exception as exc:
        logger.exception("could not keep the file for document %d", row_id)
        await db.rollback()
        raise ApiError(
            409, "invalid_request", f"{what} could not be kept; nothing was filed"
        ) from exc
    return await commit_filing(
        db,
        row,
        what=what,
        undo=lambda: workspace_documents.discard_file(settings, user_id, relative),
    )
