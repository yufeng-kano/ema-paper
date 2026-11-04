"""Filing into the library: names, the suffixed insert that re-reads a taken name, the
commit that verifies itself, a folder with its directory, and a file kept as it is — what
the upload routes and the chat agent's download share (docs/workspace.md § Files) — and
the Files page's operations, which the library routes and the chat agent's library tools
both run: a new folder or markdown document, a move or a rename, a copy, Move to Trash and
Put Back (docs/workspace.md § The page's operations are the agent's). Nothing here is a
route: the worker runs the same steps, so this lives beside the tree it writes
(docs/project-structure.md). Each operation runs under the workspace lock its caller
holds, commits on the session it is given, and names its tree commits' `author`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import ColumnElement, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import ledger
from ..config import Settings
from ..db.models import FILE_KIND, MARKDOWN_KIND, Document, Folder, Job, utcnow
from ..errors import ApiError
from . import documents as workspace_documents
from . import safe_io
from .documents import (
    live_document,
    live_folder,
    markdown_name,
    nearest_live,
    own_subtree,
    owned_document,
    owned_folder,
    subtree_ids,
)
from .layout import (
    FILES_DIR,
    document_relative_dir,
    path_segment,
    segment_name,
    trash_entry,
    user_root,
)
from .locks import workspace_lock
from .trash import lock_user, read_row, refuse_while_answering

logger = logging.getLogger(__name__)

FOLDER_NAME_MAX = 255
# A path of folders made in one call: far past any reader's nesting, and far short of the
# depth a storage walk stops measuring at (`safe_io.DEPTH_MAX`).
FOLDER_PATH_DEPTH_MAX = 64
DOCUMENT_NAME_MAX = 512
# How many " (2)" attempts a colliding name gets before the folder is simply too crowded.
NAME_ATTEMPTS = 500
# An insert can lose a name race with another tab; the retry re-reads the folder's names.
INSERT_ATTEMPTS = 3
STORAGE_FULL = "The workspace is at its quota; free some space first"
# A copy of an item whose files the Trash's repair rule could not bring back is refused,
# not made from the baseline as if the item had never been read (docs/workspace.md § Copies).
STRANDED = "The item's files could not be brought back from the Trash; nothing was copied"
FILE_CLASH = "A file with this name already exists here"
FOLDER_CLASH = "A folder with this name already exists here"

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


def live_child(folders: dict[int, Folder], parent_id: int | None, name: str) -> Folder | None:
    """The live folder of that cleaned name directly under `parent_id` (None: the top
    level), among the user's folders as `user_folders` reads them."""
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    return next(
        (
            folder
            for folder in folders.values()
            if folder.parent_id == parent_id and folder.name == name and folder.id not in trashed
        ),
        None,
    )


async def ensure_folders(
    db: AsyncSession, settings: Settings, user_id: int, names: list[str]
) -> int | None:
    """The folder at the end of a path of names — `Papers`, then `2024` inside it — each
    found among the user's live folders by its cleaned name or made where it is missing
    (docs/chat.md § Tools, `download_file`; docs/mcp.md § Filing into Files). None for an
    empty path: the top level. A name a live document holds where a folder would be made
    is refused (409): made under a suffixed name, the folder would be made again at every
    call that names it. Every name is checked before the first is made, and a path past
    `FOLDER_PATH_DEPTH_MAX` names is refused (400). The caller holds the workspace lock."""
    if len(names) > FOLDER_PATH_DEPTH_MAX:
        raise ApiError(
            400, "invalid_request", f"A folder path is at most {FOLDER_PATH_DEPTH_MAX} folders deep"
        )
    cleaned = [clean_name(raw, FOLDER_NAME_MAX) for raw in names]
    parent_id: int | None = None
    for wanted in cleaned:
        folders = await workspace_documents.user_folders(db, user_id)
        found = live_child(folders, parent_id, wanted)
        if found is None:
            # No live folder holds the name here, so a name still taken is a document's.
            if wanted in await sibling_names(db, user_id, parent_id):
                raise ApiError(
                    409,
                    "invalid_request",
                    f"'{wanted}' is a document here, not a folder; name another folder",
                )
            found = await make_folder(db, settings, user_id, parent_id, wanted)
        parent_id = found.id
    return parent_id


async def find_or_make_folder_path(
    db: AsyncSession, settings: Settings, user_id: int, path: str
) -> dict:
    """The folder a data-plane parse is filed into (docs/mcp.md § Filing into Files): a
    path as `list_library` shows it — `Papers/2024`, or `/` for the top level — each name
    found among the live folders under the one before it and the missing ones made by
    `ensure_folders`, under the workspace lock. The parse tool's `folder` and
    `POST /v1/parse`'s. Every name is checked before anything is made, and a path whose
    folders all exist is answered without the lock, which a shell command in a conversation
    holds for its whole run. Answers `{id, path, created}`: the folder (None at the top
    level), its path as the library shows it, and the paths this call made."""
    names = [clean_name(part, FOLDER_NAME_MAX) for part in path.split("/") if part.strip()]
    if any(name in (".", "..") for name in names):
        raise ApiError(400, "invalid_request", "A folder path has no '.' or '..' in it")
    folders = await workspace_documents.user_folders(db, user_id)
    found: Folder | None = None
    for name in names:
        found = live_child(folders, None if found is None else found.id, name)
        if found is None:
            break
    folder_id = None if found is None else found.id
    made: list[Folder] = []
    if found is None and names:
        async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
            before = set(await workspace_documents.user_folders(db, user_id))
            folder_id = await ensure_folders(db, settings, user_id, names)
            folders = await workspace_documents.user_folders(db, user_id)
        made = [folder for key, folder in folders.items() if key not in before]

    def shown(folder_id: int) -> str:
        return "/".join(row.name for row in workspace_documents.folder_chain(folders, folder_id))

    return {
        "id": folder_id,
        "path": "/" if folder_id is None else shown(folder_id),
        "created": sorted(shown(row.id) for row in made),
    }


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


# --- the Files page's operations (docs/workspace.md § The page's operations are the agent's)


async def lock_job(db: AsyncSession, job_id) -> None:
    """Order filing against the worker's finalization of the parse: the worker reads the
    rows that keep the original under this lock (docs/jobs.md § Retention)."""
    await db.execute(select(Job.id).where(Job.id == job_id).with_for_update())


async def job_of(db: AsyncSession, job_id) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return job


async def flush_or_retry(db: AsyncSession, retries_left: int, message: str) -> bool:
    """Flush; answer False when a lost name race means the caller should choose again.

    A rename passes `0`: the name is what the user just typed, so a collision is theirs to
    resolve. A move passes what is left of its attempts, because a collision there is
    resolved with a suffix rather than refused (docs/web.md § Files). The flush, not a
    commit: the directory moves before the transaction ends, so a move the tree refuses
    rolls the row back (docs/workspace.md § Revisions).
    """
    try:
        await db.flush()
        return True
    except IntegrityError as exc:
        await db.rollback()
        if retries_left <= 0:
            raise ApiError(409, "invalid_request", message) from exc
        return False


async def move_then_commit(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    before: Path,
    after: Path,
    *,
    landed: tuple[type, int, dict],
    author: str,
) -> None:
    """The row is flushed; now the directory, then the commit. A refusal from the tree
    rolls the row back to where the files still are; a commit that fails after the move
    puts the directory back where the row stayed — unless the row shows the commit
    landed after all (the connection dropped after PostgreSQL made it durable), which
    `landed` (the model, the id, the values the committed row holds) is read for on a
    fresh connection: then the files stay where the row points (docs/workspace.md)."""
    try:
        moved = await workspace_documents.move_path(settings, user_id, before, after, author=author)
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        if moved is not None:
            model, row_id, expected = landed
            readable, row = await read_row(db, model, row_id)
            if not readable:
                # Indeterminate is not a rollback: the files stay where they are rather
                # than being moved away from a row that may now name them; the next
                # reconciliation follows the directory (docs/workspace.md § Revisions).
                logger.error(
                    "the move of %s to %s raised and cannot be verified; the files stay",
                    before,
                    after,
                )
            elif row is not None and all(
                getattr(row, key) == value for key, value in expected.items()
            ):
                logger.warning(
                    "the move of %s to %s was committed despite the error; the files stay",
                    before,
                    after,
                )
            else:
                try:
                    # The whole move undone: the directory back, and the squatter the
                    # move displaced back under its name (docs/workspace.md § Layout).
                    await workspace_documents.undo_move(
                        settings, user_id, before, after, moved.displaced, author=author
                    )
                except Exception:  # noqa: BLE001 - the move revision stays revertible
                    logger.exception("could not move %s back after a failed commit", after)
        raise


async def new_document(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    folder_id: int | None,
    name: str,
    *,
    text: str | None = None,
) -> Document:
    """A new markdown document, its name given `.md` and suffixed where the folder holds it
    (docs/workspace.md § Markdown documents). Without `text`, the page's *New document*: a
    row with no original, its baseline empty and its directory made at its first read — it
    writes nothing to the tree, so it needs no lock. With `text`, the text is kept as its
    original as an uploaded `.md` is: the caller holds the backup lock shared and the
    workspace lock, as for a filing. Either way the committed row is all it makes: the
    agent's `new_document` writes the directory next, as its own step, so a directory that
    cannot be written is told apart from no document made."""
    if folder_id is not None:
        await live_folder(db, user_id, folder_id)
    wanted = markdown_name(clean_name(name, DOCUMENT_NAME_MAX), DOCUMENT_NAME_MAX)
    try:
        row = await insert_named(
            db,
            user_id=user_id,
            into=folder_id,
            build=lambda name: Document(
                user_id=user_id, folder_id=folder_id, job_id=None, kind=MARKDOWN_KIND, name=name
            ),
            taken=lambda: sibling_names(db, user_id, folder_id),
            wanted=wanted,
            limit=DOCUMENT_NAME_MAX,
            message=FILE_CLASH,
            commit=text is None,
        )
    except ApiError:
        if folder_id is None or await folder_exists(db, user_id, folder_id):
            raise
        raise ApiError(404, "invalid_request", "No such folder") from None
    if text is None:
        return row
    row_id = row.id
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = upload_dir / f"{uuid.uuid4().hex}.md"
    try:
        await asyncio.to_thread(staged.write_text, text, encoding="utf-8")
        # The quota, the copy and their undo, as for a `.md` upload; the staged file goes
        # either way.
        await workspace_documents.adopt_markdown(db, settings, row, staged)
    except ApiError:
        await db.rollback()
        raise
    except Exception as exc:
        logger.exception("could not keep the text of document %d", row_id)
        await db.rollback()
        raise ApiError(
            409, "invalid_request", "The document could not be kept; nothing was made"
        ) from exc
    finally:
        staged.unlink(missing_ok=True)
    relative = await document_relative(db, row)
    return await commit_filing(
        db,
        row,
        what="The document",
        undo=lambda: workspace_documents.discard_adopted(settings, user_id, row_id, relative),
    )


async def move_folder(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    folder_id: int,
    *,
    name: str | None,
    parent_id: int | None,
    move: bool,
    author: str,
) -> Folder:
    """Rename a folder (`name`), move it into `parent_id` (`move`), or both, its directory
    with it. A rename that collides is refused: the name is what the user just typed, and
    quietly turning it into "Invoices (2)" would be a lie. A move that collides is
    suffixed, the way dropping a file into a folder that already holds one behaves. Refused
    while a conversation about a document under it is answering: a turn holds its
    document's path for its whole run (docs/chat.md § Conversations, turns, events)."""
    doomed = await subtree_ids(db, user_id, folder_id)
    await refuse_while_answering(
        db,
        user_id,
        select(Document.id).where(Document.user_id == user_id, Document.folder_id.in_(doomed)),
    )
    renaming = name is not None
    # The whole decision sits inside the retry: a rollback releases the lock the move was
    # validated under, so a second attempt has to establish both facts again.
    for attempt in range(INSERT_ATTEMPTS):
        row = await live_folder(db, user_id, folder_id)
        place = parent_id if move else row.parent_id

        if place != row.parent_id and place is not None:
            await live_folder(db, user_id, place)
            if place in await subtree_ids(db, user_id, row.id, for_update=True):
                raise ApiError(400, "invalid_request", "A folder cannot be moved inside itself")

        wanted = clean_name(name, FOLDER_NAME_MAX) if renaming else row.name
        await lock_user(db, user_id)
        taken = await sibling_names(db, user_id, place, exclude_folder=row.id)
        if renaming and wanted in taken:
            raise ApiError(409, "invalid_request", FOLDER_CLASH)

        before = await folder_relative_dir(db, user_id, row.id)
        row.parent_id = place
        row.name = free_name(taken, wanted, FOLDER_NAME_MAX)
        retries = 0 if renaming else INSERT_ATTEMPTS - 1 - attempt
        if await flush_or_retry(db, retries, FOLDER_CLASH):
            break
    # The tree follows the rows (docs/workspace.md): the directory moves, then the commit.
    after = await folder_relative_dir(db, user_id, row.id)
    landed = (Folder, row.id, {"parent_id": row.parent_id, "name": row.name})
    await move_then_commit(db, settings, user_id, before, after, landed=landed, author=author)
    return row


async def move_document(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    document_id: int,
    *,
    name: str | None,
    folder_id: int | None,
    move: bool,
    author: str,
) -> Document:
    """Rename a document or a file (`name`), move it into `folder_id` (`move`), or both,
    its directory with it. Same rule as a folder: a rename refuses, a move suffixes; a
    markdown document keeps `.md`; refused while a conversation about it is answering."""
    # A turn holds the document's path for its whole run (docs/chat.md § Tools).
    await refuse_while_answering(db, user_id, select(Document.id).where(Document.id == document_id))
    renaming = name is not None
    for attempt in range(INSERT_ATTEMPTS):
        row = await live_document(db, user_id, document_id)
        place = folder_id if move else row.folder_id
        if place != row.folder_id and place is not None:
            await live_folder(db, user_id, place)

        wanted = clean_name(name, DOCUMENT_NAME_MAX) if renaming else row.name
        if renaming and row.kind == MARKDOWN_KIND:
            wanted = markdown_name(wanted, DOCUMENT_NAME_MAX)
        await lock_user(db, user_id)
        taken = await sibling_names(db, user_id, place, exclude_document=row.id)
        if renaming and wanted in taken:
            raise ApiError(409, "invalid_request", FILE_CLASH)

        before = await document_relative(db, row)
        row.folder_id = place
        row.name = free_name(taken, wanted, DOCUMENT_NAME_MAX)
        # Two tabs moving same-named files into one folder is the same race a create
        # loses, and it has the same answer: take the next name, not a conflict.
        retries = 0 if renaming else INSERT_ATTEMPTS - 1 - attempt
        if await flush_or_retry(db, retries, FILE_CLASH):
            break
    after = await document_relative(db, row)
    landed = (Document, row.id, {"folder_id": row.folder_id, "name": row.name})
    await move_then_commit(db, settings, user_id, before, after, landed=landed, author=author)
    return row


# --- the Trash ----------------------------------------------------------------------------


async def stash_then_commit(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    relative: Path,
    entry: str,
    *,
    landed: tuple[type, int],
    author: str,
) -> None:
    """The row is flushed with `trashed_at`; now the directory into the Trash, then the
    commit. A refusal from the tree rolls the row back to where the files still are; a
    commit that fails after the move brings the directory back — unless the row `landed`
    names is found trashed on a fresh connection, the commit having landed after all. A
    row that cannot be read leaves the entry where it is: the repair rule returns it
    should the row turn out live (docs/workspace.md § The Trash)."""
    try:
        moved = await workspace_documents.stash_path(
            settings, user_id, relative, entry, author=author
        )
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        if moved:
            readable, row = await read_row(db, *landed)
            if not readable:
                logger.error("the move of %s to the Trash raised and cannot be verified", relative)
            elif row is not None and row.trashed_at is not None:
                logger.warning(
                    "the move of %s to the Trash was committed despite the error", relative
                )
            else:
                try:
                    await workspace_documents.unstash_path(
                        settings, user_id, entry, relative, f"restore {relative}", author=author
                    )
                except Exception:  # noqa: BLE001 - the repair rule returns it
                    logger.exception("could not bring %s back from the Trash", relative)
        raise


async def put_back_then_move(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    entry: str,
    relative: Path,
    *,
    landed: tuple[type, int],
    what: str,
    author: str,
):
    """Put Back: the rows are flushed live at their new place; they commit first — a
    commit that raises is checked on a fresh connection, as a filing's is — and then the
    directory comes back from the Trash. A move that fails leaves the rows back and the
    files in the Trash for the repair rule, which returns them at the next read of the
    document or the next reconciliation (docs/workspace.md § The Trash). Returns the row
    as committed when the commit raised and landed (the request's session rolled back),
    else None."""
    answer = None
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - verified below, whatever raised
        readable, row = await read_row(db, *landed)
        with contextlib.suppress(Exception):
            await db.rollback()
        if readable and row is not None and row.trashed_at is None:
            logger.warning("the put back of %s was committed despite the error", relative)
            answer = row
        elif readable:
            raise ApiError(
                409, "invalid_request", f"{what} could not be put back; nothing was changed"
            ) from exc
        else:
            raise ApiError(
                409,
                "invalid_request",
                f"{what} may have been put back, but the database could not confirm it; "
                "reload the library before trying again",
            ) from exc
    try:
        await workspace_documents.unstash_path(
            settings, user_id, entry, relative, f"put back {relative}", author=author
        )
    except Exception:  # noqa: BLE001 - the rows stand; the repair rule brings the files
        logger.exception("the files of %s stay in the Trash for now", relative)
    return answer


async def trash_document(
    db: AsyncSession, settings: Settings, user_id: int, document_id: int, *, author: str
) -> Document:
    """Move to Trash: the row marked, its directory out of `files/` into the Trash, as one
    revision (docs/workspace.md § The Trash)."""
    # What an interrupted move left goes back first: an entry under this item's name
    # would refuse the move below.
    await workspace_documents.recover_trash(db, settings, user_id)
    await lock_user(db, user_id)
    row = await live_document(db, user_id, document_id)
    # A turn holds the document's path for its whole run (docs/chat.md § Tools).
    await refuse_while_answering(db, user_id, [row.id])
    relative = await document_relative(db, row)
    row.trashed_at = utcnow()
    await db.flush()
    await stash_then_commit(
        db,
        settings,
        user_id,
        relative,
        trash_entry("document", row.id),
        landed=(Document, row.id),
        author=author,
    )
    return row


async def trash_folder(
    db: AsyncSession, settings: Settings, user_id: int, folder_id: int, *, author: str
) -> Folder:
    """Move to Trash, a folder with everything in it (docs/workspace.md § The Trash)."""
    await workspace_documents.recover_trash(db, settings, user_id)
    await lock_user(db, user_id)
    row = await live_folder(db, user_id, folder_id)
    inside = own_subtree(await workspace_documents.user_folders(db, user_id), row.id)
    await refuse_while_answering(
        db,
        user_id,
        select(Document.id).where(Document.user_id == user_id, Document.folder_id.in_(inside)),
    )
    relative = await folder_relative_dir(db, user_id, row.id)
    row.trashed_at = utcnow()
    await db.flush()
    await stash_then_commit(
        db,
        settings,
        user_id,
        relative,
        trash_entry("folder", row.id),
        landed=(Folder, row.id),
        author=author,
    )
    return row


async def put_back_document(
    db: AsyncSession, settings: Settings, user_id: int, document_id: int, *, author: str
) -> Document:
    """Put Back: to the folder it was trashed from, or the nearest live one above it, a
    taken name suffixed as a move's is (docs/api.md)."""
    await workspace_documents.recover_trash(db, settings, user_id)
    for attempt in range(INSERT_ATTEMPTS):
        await lock_user(db, user_id)
        row = await owned_document(db, user_id, document_id)
        if row.trashed_at is None:
            raise ApiError(404, "invalid_request", "No such document in the Trash")
        place = nearest_live(await workspace_documents.user_folders(db, user_id), row.folder_id)
        taken = await sibling_names(db, user_id, place)
        row.folder_id = place
        row.name = free_name(taken, row.name, DOCUMENT_NAME_MAX)
        row.trashed_at = None
        if await flush_or_retry(db, INSERT_ATTEMPTS - 1 - attempt, FILE_CLASH):
            break
    relative = await document_relative(db, row)
    landed = await put_back_then_move(
        db,
        settings,
        user_id,
        trash_entry("document", row.id),
        relative,
        landed=(Document, row.id),
        what="The file",
        author=author,
    )
    return landed or row


async def put_back_folder(
    db: AsyncSession, settings: Settings, user_id: int, folder_id: int, *, author: str
) -> Folder:
    """Put Back a folder, with everything that went to the Trash with it (docs/api.md)."""
    await workspace_documents.recover_trash(db, settings, user_id)
    for attempt in range(INSERT_ATTEMPTS):
        await lock_user(db, user_id)
        row = await owned_folder(db, user_id, folder_id)
        if row.trashed_at is None:
            raise ApiError(404, "invalid_request", "No such folder in the Trash")
        place = nearest_live(await workspace_documents.user_folders(db, user_id), row.parent_id)
        taken = await sibling_names(db, user_id, place)
        row.parent_id = place
        row.name = free_name(taken, row.name, FOLDER_NAME_MAX)
        row.trashed_at = None
        if await flush_or_retry(db, INSERT_ATTEMPTS - 1 - attempt, FOLDER_CLASH):
            break
    relative = await folder_relative_dir(db, user_id, row.id)
    landed = await put_back_then_move(
        db,
        settings,
        user_id,
        trash_entry("folder", row.id),
        relative,
        landed=(Folder, row.id),
        what="The folder",
        author=author,
    )
    return landed or row


# --- copies -------------------------------------------------------------------------------


async def keep_parses(db: AsyncSession, jobs: list[Job]) -> None:
    """The job rows behind the documents a copy files, locked in one order, read again
    under the lock and marked for retention while they run, as the Files page's upload
    marks its own: the worker finishing one meanwhile sees the new rows among its
    recipients or leaves the upload for them (docs/jobs.md § Retention)."""
    for job in sorted(jobs, key=lambda job: str(job.id)):
        await lock_job(db, job.id)
        await db.refresh(job)
        if job.status in ("queued", "running"):
            job.retain_source = True
    await db.flush()


async def donors_of(db: AsyncSession, documents: list[Document]) -> dict:
    """Every document of each parse behind `documents`: whose original a copy may take
    when its source holds none (docs/workspace.md § Copies)."""
    job_ids = {document.job_id for document in documents if document.job_id is not None}
    donors: dict = {}
    if job_ids:
        rows = await db.execute(
            select(Document.id, Document.job_id)
            .where(Document.job_id.in_(job_ids))
            .order_by(Document.id)
        )
        for document_id, job_id in rows.all():
            donors.setdefault(job_id, []).append(document_id)
    return donors


def copied(
    source: Document, target: Document, job: Job | None, inner: Path, donors: dict
) -> workspace_documents.CopiedDocument:
    others = donors.get(source.job_id, []) if source.job_id is not None else []
    return workspace_documents.CopiedDocument(
        source_id=source.id,
        target_id=target.id,
        job=job,
        inner=inner,
        donors=(source.id, *(other for other in others if other not in (source.id, target.id))),
        parsing=job is not None and job.status in ("queued", "running"),
        file=source.kind == FILE_KIND,
    )


async def copy_then_commit(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    source: Path,
    target: Path,
    documents: list[workspace_documents.CopiedDocument],
    *,
    folder: bool,
    landed: tuple[type, int],
    author: str,
):
    """The rows are flushed; now the files, then the commit. A copy the tree refuses rolls
    the rows back; a commit that raises is checked on a fresh connection — landed, the
    copy stands and its row is answered; rolled back, the copy is removed as a revision of
    its own; unknown, answered as unconfirmed (docs/workspace.md § Copies)."""
    try:
        made = await workspace_documents.copy_path(
            settings, user_id, source, target, documents, folder=folder, author=author
        )
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - verified below, whatever raised
        readable, row = await read_row(db, *landed)
        with contextlib.suppress(Exception):
            await db.rollback()
        if readable and row is not None:
            logger.warning("the copy to %s was committed despite the error", target)
            return row
        if readable:
            try:
                await workspace_documents.remove_copy(
                    settings,
                    user_id,
                    target,
                    made,
                    [document.target_id for document in documents],
                    author=author,
                )
            except Exception:  # noqa: BLE001 - the refusal stands; the leftovers are logged
                logger.exception("could not take back the copy at %s", target)
            raise ApiError(
                409, "invalid_request", "The copy could not be made; nothing was copied"
            ) from exc
        raise ApiError(
            409,
            "invalid_request",
            "The copy may have been made, but the database could not confirm it; "
            "reload the library before trying again",
        ) from exc
    return None


async def copy_document(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    document_id: int,
    folder_id: int | None,
    *,
    author: str,
) -> Document:
    """A copy of a document — its text, notes, original and images as they are now — into
    a folder, or beside it as a duplicate (docs/workspace.md § Copies). The caller holds
    the backup lock shared and the workspace lock, as for a filing: no row without its
    original, in the database or in a snapshot (docs/workspace.md § Backups)."""
    root = user_root(settings.workspaces_dir, user_id)
    # What an interrupted move to or from the Trash left goes back first: the copy reads
    # the source's directory, and the edits may be waiting there.
    await workspace_documents.recover_trash(db, settings, user_id)
    await lock_user(db, user_id)
    source = await live_document(db, user_id, document_id)
    if folder_id is not None:
        await live_folder(db, user_id, folder_id)
    above = workspace_documents.folder_chain(
        await workspace_documents.user_folders(db, user_id), source.folder_id
    )
    if await workspace_documents.held_in_trash(
        settings, user_id, folders=[row.id for row in above], documents=[source.id]
    ):
        raise ApiError(409, "invalid_request", STRANDED)
    await workspace_documents.ensure_within_quota(db, settings, user_id, root)
    job = await job_of(db, source.job_id) if source.job_id is not None else None
    if job is not None:
        await keep_parses(db, [job])
    taken = await sibling_names(db, user_id, folder_id)
    row = Document(
        user_id=user_id,
        folder_id=folder_id,
        job_id=source.job_id,
        kind=source.kind,
        media_type=source.media_type,
        size_bytes=source.size_bytes,
        name=free_name(taken, source.name, DOCUMENT_NAME_MAX),
    )
    db.add(row)
    await flush_or_retry(db, 0, FILE_CLASH)
    documents = [copied(source, row, job, Path(), await donors_of(db, [source]))]
    landed = await copy_then_commit(
        db,
        settings,
        user_id,
        await document_relative(db, source),
        await document_relative(db, row),
        documents,
        folder=False,
        landed=(Document, row.id),
        author=author,
    )
    # A commit that raised and landed answers with the row as committed: the session
    # rolled back.
    return landed or row


async def copy_folder(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    folder_id: int,
    parent_id: int | None,
    *,
    author: str,
) -> Folder:
    """A copy of a folder with every live folder and document under it, in the same shape
    (docs/workspace.md § Copies). The caller holds the backup lock shared and the
    workspace lock."""
    root = user_root(settings.workspaces_dir, user_id)
    await workspace_documents.recover_trash(db, settings, user_id)
    await lock_user(db, user_id)
    source = await live_folder(db, user_id, folder_id)
    folders = await workspace_documents.user_folders(db, user_id)
    inside = own_subtree(folders, source.id)
    if parent_id is not None:
        await live_folder(db, user_id, parent_id)
        if parent_id in inside:
            raise ApiError(400, "invalid_request", "A folder cannot be copied inside itself")
    await workspace_documents.ensure_within_quota(db, settings, user_id, root)
    sources = (
        (
            await db.execute(
                select(Document)
                .where(
                    Document.user_id == user_id,
                    Document.folder_id.in_(inside),
                    Document.trashed_at.is_(None),
                )
                .order_by(Document.id)
            )
        )
        .scalars()
        .all()
    )
    above = workspace_documents.folder_chain(folders, source.parent_id)
    if await workspace_documents.held_in_trash(
        settings,
        user_id,
        folders=[*inside, *(row.id for row in above)],
        documents=[document.id for document in sources],
    ):
        raise ApiError(409, "invalid_request", STRANDED)
    job_ids = {document.job_id for document in sources if document.job_id is not None}
    jobs = {
        job.id: job for job in (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()
    }
    await keep_parses(db, list(jobs.values()))
    taken = await sibling_names(db, user_id, parent_id)
    top = Folder(
        user_id=user_id,
        parent_id=parent_id,
        name=free_name(taken, source.name, FOLDER_NAME_MAX),
    )
    db.add(top)
    await flush_or_retry(db, 0, FOLDER_CLASH)
    # The folders under it parent by parent, so each new row has its new parent's id;
    # their names were unique where they were, and are where they land.
    mapping = {source.id: top.id}
    pending = [source.id]
    while pending:
        parent = pending.pop(0)
        for child in sorted(folders.values(), key=lambda row: row.id):
            if child.parent_id == parent and child.id in inside and child.id not in mapping:
                made = Folder(user_id=user_id, parent_id=mapping[parent], name=child.name)
                db.add(made)
                await db.flush()
                mapping[child.id] = made.id
                pending.append(child.id)
    targets = []
    for document in sources:
        made = Document(
            user_id=user_id,
            folder_id=mapping[document.folder_id],
            job_id=document.job_id,
            kind=document.kind,
            media_type=document.media_type,
            size_bytes=document.size_bytes,
            name=document.name,
        )
        db.add(made)
        targets.append(made)
    await db.flush()
    donors = await donors_of(db, list(sources))
    documents = []
    for document, made in zip(sources, targets, strict=True):
        chain = workspace_documents.folder_chain(folders, document.folder_id)
        below = chain[[row.id for row in chain].index(source.id) + 1 :]
        inner = Path(*(path_segment(row.name) for row in below), path_segment(document.name))
        documents.append(
            copied(
                document,
                made,
                jobs.get(document.job_id) if document.job_id is not None else None,
                inner,
                donors,
            )
        )
    landed = await copy_then_commit(
        db,
        settings,
        user_id,
        await folder_relative_dir(db, user_id, source.id),
        await folder_relative_dir(db, user_id, top.id),
        documents,
        folder=True,
        landed=(Folder, top.id),
        author=author,
    )
    return landed or top
