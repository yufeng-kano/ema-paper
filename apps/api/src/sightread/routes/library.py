"""Control plane `/api/library/*` — the web file library (docs/web.md § Files).

Thin like every route module. Folders and documents are names and places; the parse behind
a document is a `jobs` row this module never interprets, and the upload is handed straight
to `jobs.intake.submit_parse`, the same sequence `/v1/parse` runs
(docs/project-structure.md § Boundaries).

A document is an entry, not a second copy of a result: deleting one deletes the entry, and
the job and its output stay in the parse history that billed for them (docs/database.md
§ Rules).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import unicodedata
import uuid
import warnings
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import iterate_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..agents import PARSE
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..db.models import (
    ChatTurn,
    Conversation,
    Document,
    Folder,
    Job,
    JobPage,
    Result,
    User,
    utcnow,
)
from ..errors import ApiError
from ..jobs import events
from ..jobs.chat_tools import PageUnavailable, original_page_png
from ..jobs.intake import (
    MULTIPART_ENVELOPE_BYTES,
    capped_stream,
    file_chunks,
    resolve_check,
    store_upload,
    submit_parse,
    upload_chunks,
)
from ..jobs.queue import parse_pages_spec, retry_job
from ..jobs.runner import partial_result_payload, result_payload
from ..parsing.figures import crop_chunks, open_crop, parse_bbox_path
from ..parsing.images import ImageError, normalize_image
from ..workspace import documents as workspace_documents
from ..workspace import safe_io
from ..workspace.layout import document_relative_dir, path_segment, trash_entry, user_root
from ..workspace.locks import backup_shared, workspace_lock

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])

FOLDER_NAME_MAX = 255
DOCUMENT_NAME_MAX = 512
# How many " (2)" attempts a colliding name gets before the folder is simply too crowded.
NAME_ATTEMPTS = 500
# An insert can lose a name race with another tab; the retry re-reads the folder's names.
INSERT_ATTEMPTS = 3
# A copy of an item whose files the Trash's repair rule could not bring back is refused,
# not made from the baseline as if the item had never been read (docs/workspace.md § Copies).
STRANDED = "The item's files could not be brought back from the Trash; nothing was copied"

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
    db: DbSession,
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


async def lock_job(db: DbSession, job_id: uuid.UUID) -> None:
    """Order filing against the worker's finalization of the parse: the worker reads the
    rows that keep the original under this lock (docs/jobs.md § Retention)."""
    await db.execute(select(Job.id).where(Job.id == job_id).with_for_update())


async def lock_user(db: DbSession, user_id: int) -> None:
    """Serialize this user's name decisions: the unique indexes cannot see across the
    two tables, so two requests choosing a name for a folder and a file at once are
    ordered here (docs/database.md)."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


async def refuse_while_answering(db: DbSession, user_id: int, document_ids) -> None:
    """A document whose conversation has an unfinished turn cannot go: the worker would
    carry on against rows and a directory that no longer exist (docs/chat.md). Decided
    under the locks a send takes, in the same order — the user's row, then the
    conversations' — so no turn can be queued between this check and the delete."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    await db.execute(
        select(Conversation.id).where(Conversation.document_id.in_(document_ids)).with_for_update()
    )
    busy = (
        await db.execute(
            select(ChatTurn.id)
            .join(Conversation, Conversation.id == ChatTurn.conversation_id)
            .where(Conversation.document_id.in_(document_ids), ChatTurn.finished_at.is_(None))
            .limit(1)
        )
    ).first()
    if busy is not None:
        raise ApiError(409, "invalid_request", "A conversation about this file is still answering")


async def owned_folder(db: DbSession, user_id: int, folder_id: int) -> Folder:
    row = (
        await db.execute(select(Folder).where(Folder.id == folder_id, Folder.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such folder")
    return row


async def owned_document(db: DbSession, user_id: int, document_id: int) -> Document:
    row = (
        await db.execute(
            select(Document).where(Document.id == document_id, Document.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such document")
    return row


async def live_folder(db: DbSession, user_id: int, folder_id: int) -> Folder:
    """A folder of the caller's that is not in the Trash, nor inside one that is — the
    only kind anything can be filed into, moved into or opened (docs/database.md § Rules)."""
    folders = await workspace_documents.user_folders(db, user_id)
    row = folders.get(folder_id)
    if row is None or folder_id in workspace_documents.trashed_folder_ids(folders.values()):
        raise ApiError(404, "invalid_request", "No such folder")
    return row


async def live_document(db: DbSession, user_id: int, document_id: int) -> Document:
    """A document of the caller's that is not in the Trash: what every route that reads,
    writes or moves one takes (docs/api.md)."""
    row = await owned_document(db, user_id, document_id)
    if not workspace_documents.is_live(row, await workspace_documents.user_folders(db, user_id)):
        raise ApiError(404, "invalid_request", "No such document")
    return row


def nearest_live(folders: dict[int, Folder], folder_id: int | None) -> int | None:
    """Where Put Back returns an item: the folder it was trashed from when that is live,
    else the nearest live folder above it, else the top level (docs/api.md)."""
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    seen: set[int] = set()
    while folder_id is not None:
        row = folders.get(folder_id)
        if row is None or folder_id in seen:
            return None
        if folder_id not in trashed:
            return folder_id
        seen.add(folder_id)
        folder_id = row.parent_id
    return None


def own_subtree(folders: dict[int, Folder], root_id: int) -> set[int]:
    """`root_id` and the folders that go with it: everything under it but a folder
    trashed on its own, and what is under that — the Trash's own entry, which neither a
    move to the Trash nor a delete takes along (docs/database.md § Rules)."""
    children: dict[int, list[Folder]] = {}
    for row in folders.values():
        if row.parent_id is not None:
            children.setdefault(row.parent_id, []).append(row)
    found = {root_id}
    frontier = [root_id]
    while frontier:
        for child in children.get(frontier.pop(), []):
            if child.trashed_at is None and child.id not in found:
                found.add(child.id)
                frontier.append(child.id)
    return found


async def subtree_ids(
    db: DbSession, user_id: int, root_id: int, for_update: bool = False
) -> set[int]:
    """`root_id` and every folder under it.

    Walked in Python over the account's own (id, parent_id) pairs rather than with a
    recursive CTE: a library is a handful of rows, and this is the same walk on PostgreSQL
    and on the SQLite test fallback.

    `for_update` locks those rows for the rest of the transaction (a PostgreSQL row lock;
    compiled away on the SQLite fallback). A move needs it: two tabs moving A into B and B
    into A can each pass the "not inside itself" check against a tree the other has not
    committed yet, and the pair of updates then commits a cycle — a subtree with no root,
    so every folder and file in it vanishes from the library.
    """
    query = select(Folder.id, Folder.parent_id).where(Folder.user_id == user_id)
    if for_update:
        query = query.with_for_update()
    pairs = (await db.execute(query)).all()
    children: dict[int | None, list[int]] = {}
    for folder_id, parent_id in pairs:
        children.setdefault(parent_id, []).append(folder_id)

    found = {root_id}
    frontier = [root_id]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


async def insert_named[RowT: (Folder, Document)](
    db: DbSession,
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


async def flush_or_retry(db: DbSession, retries_left: int, message: str) -> bool:
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
    db: DbSession, settings, user_id: int, before, after, *, landed: tuple[type, int, dict]
) -> None:
    """The row is flushed; now the directory, then the commit. A refusal from the tree
    rolls the row back to where the files still are; a commit that fails after the move
    puts the directory back where the row stayed — unless the row shows the commit
    landed after all (the connection dropped after PostgreSQL made it durable), which
    `landed` (the model, the id, the values the committed row holds) is read for on a
    fresh connection: then the files stay where the row points (docs/workspace.md)."""
    try:
        moved = await workspace_documents.move_path(settings, user_id, before, after)
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        if moved is not None:
            model, row_id, expected = landed
            readable, row = await _read_row(db, model, row_id)
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
                        settings, user_id, before, after, moved.displaced
                    )
                except Exception:  # noqa: BLE001 - the move revision stays revertible
                    logger.exception("could not move %s back after a failed commit", after)
        raise


async def _read_row(db: DbSession, model: type, row_id: int) -> tuple[bool, object | None]:
    """The row as the database holds it after a commit that raised — read on a connection
    of its own, since the request's may be the one that dropped. `(readable, row)`: a
    read that fails says nothing about the commit, and the caller treats it so."""
    try:
        async with AsyncSession(bind=db.bind, expire_on_commit=False) as fresh:
            return True, await fresh.get(model, row_id)
    except Exception:  # noqa: BLE001 - the database is away
        logger.exception("could not read row %s of %s after a failed commit", row_id, model)
        return False, None


async def commit_filing(
    db: DbSession,
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
        readable, landed = await _read_row(db, Document, row.id)
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


async def folder_exists(db: DbSession, user_id: int, folder_id: int) -> bool:
    """Whether the folder is still there to file into: the caller's, and not in the Trash."""
    folders = await workspace_documents.user_folders(db, user_id)
    return folder_id in folders and folder_id not in workspace_documents.trashed_folder_ids(
        folders.values()
    )


# --- payloads ---------------------------------------------------------------------------


def folder_payload(row: Folder) -> dict:
    return {
        "id": row.id,
        "parent_id": row.parent_id,
        "name": row.name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def is_checking(job: Job) -> bool:
    """A running parse past its last page: its format check is what it waits for
    (docs/parsing.md § Format check)."""
    return (
        job.status == "running"
        and job.check_model is not None
        and job.pages_done >= len(parse_pages_spec(job.pages_spec, job.page_count or 1))
    )


def document_payload(
    row: Document,
    job: Job | None,
    failed: dict[uuid.UUID, int] | None = None,
    reconvert: Job | None = None,
) -> dict:
    """The row the file list draws: the entry, plus the live state of its parse and of
    its re-convert.

    Joined rather than fetched per row, so one `GET /api/library` renders every name,
    status and progress bar the page needs (docs/web.md § Files). A markdown document
    (`job` None) has no parse to report on: every parse field is null. `failed` holds
    what a retry of each failed parse would send (`failed_page_counts`); `checking` says
    a running parse is past its last page and being checked; `reconvert` is the parse
    the row points at as its re-convert (docs/api.md).
    """
    failed = failed or {}
    if job is None:
        return {
            "id": row.id,
            "folder_id": row.folder_id,
            "name": row.name,
            "job_id": None,
            "status": None,
            "kind": workspace_documents.MARKDOWN_KIND,
            "model": None,
            "page_count": None,
            "pages_done": None,
            "size_bytes": None,
            "error": None,
            "created_at": row.created_at,
            "finished_at": None,
            "failed_pages": None,
            "checking": False,
            "reconvert": None,
        }
    return {
        "id": row.id,
        "folder_id": row.folder_id,
        "name": row.name,
        "job_id": str(row.job_id),
        "status": job.status,
        "kind": job.kind,
        "model": job.model,
        "page_count": job.page_count,
        "pages_done": job.pages_done,
        "size_bytes": job.size_bytes,
        "error": job.error,
        "created_at": row.created_at,
        "finished_at": job.finished_at,
        "failed_pages": failed.get(job.id),
        "checking": is_checking(job),
        "reconvert": (
            None
            if reconvert is None
            else {
                "job_id": str(reconvert.id),
                "status": reconvert.status,
                "model": reconvert.model,
                "page_count": reconvert.page_count,
                "pages_done": reconvert.pages_done,
                "error": reconvert.error,
                "failed_pages": failed.get(reconvert.id),
                "checking": is_checking(reconvert),
                "created_at": reconvert.created_at,
                "finished_at": reconvert.finished_at,
            }
        ),
    }


async def failed_page_counts(db: DbSession, jobs: list[Job | None]) -> dict[uuid.UUID, int]:
    """For each failed parse, how many of its pages have no succeeded row — what a retry
    sends (docs/jobs.md § Retrying failed pages). A row with markdown is a page that
    succeeded; a failed one stores none."""
    failed = [job for job in jobs if job is not None and job.status == "failed"]
    if not failed:
        return {}
    kept = dict(
        (
            await db.execute(
                select(JobPage.job_id, func.count())
                .where(
                    JobPage.job_id.in_([job.id for job in failed]),
                    JobPage.markdown.is_not(None),
                )
                .group_by(JobPage.job_id)
            )
        ).all()
    )
    return {
        job.id: len(parse_pages_spec(job.pages_spec, job.page_count or 1)) - kept.get(job.id, 0)
        for job in failed
    }


async def job_of(db: DbSession, job_id: uuid.UUID) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return job


async def read_document_payload(db: DbSession, row: Document) -> dict:
    """One row's payload, its parse and its re-convert read for it."""
    job = await job_of(db, row.job_id) if row.job_id is not None else None
    reconvert = await job_of(db, row.reconvert_job_id) if row.reconvert_job_id is not None else None
    return document_payload(row, job, await failed_page_counts(db, [job, reconvert]), reconvert)


# --- read ---------------------------------------------------------------------------------


async def read_folders(db: DbSession, user_id: int):
    query = select(Folder).where(Folder.user_id == user_id).order_by(Folder.name)
    return (await db.execute(query)).scalars().all()


@router.get("")
async def read_library(user: SessionUser, db: DbSession):
    """The whole library in one read — navigation between folders is then local.

    Both collections come from one snapshot. Under READ COMMITTED the two statements would
    see two, and the client would hold either documents whose folder it has never heard of
    (drawn nowhere) or a folder that has already been deleted (drawn empty) until something
    refreshed. `REPEATABLE READ` is the cheap way to make one response internally
    consistent. An isolation level applies to a transaction's first statement, and the
    request's session has already spent one on authentication, so that transaction is
    ended first and the snapshot asked for on the next one — on the same session, so a
    request holds one connection, never two (docs/api.md). The SQLite test fallback has no
    such isolation level and one writer, so it is asked for only where it exists.
    """
    user_id = user.id  # read before the rollback expires the row
    await db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    folders = await read_folders(db, user_id)
    documents = (
        await db.execute(
            select(Document, Job)
            .outerjoin(Job, Job.id == Document.job_id)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc())
        )
    ).all()
    # The re-converts the rows point at: a second read only when there is one.
    pointed = [row.reconvert_job_id for row, _ in documents if row.reconvert_job_id is not None]
    reconverts = (
        {
            job.id: job
            for job in (await db.execute(select(Job).where(Job.id.in_(pointed)))).scalars()
        }
        if pointed
        else {}
    )
    failed = await failed_page_counts(db, [job for _, job in documents] + list(reconverts.values()))

    # The live rows are the library; the Trash lists what was trashed itself, never
    # what sits inside a trashed folder (docs/api.md).
    by_id = {row.id: row for row in folders}
    trashed = workspace_documents.trashed_folder_ids(folders)
    items: dict[int, int] = {}
    for row, _job in documents:
        if row.trashed_at is not None or row.folder_id not in trashed:
            continue
        # A document that comes back with the nearest trashed folder above it.
        cursor = by_id.get(row.folder_id)
        while cursor is not None and cursor.trashed_at is None:
            cursor = by_id.get(cursor.parent_id) if cursor.parent_id is not None else None
        if cursor is not None:
            items[cursor.id] = items.get(cursor.id, 0) + 1

    def origin(parent_id: int | None) -> list[str]:
        place = nearest_live(by_id, parent_id)
        return [row.name for row in workspace_documents.folder_chain(by_id, place)]

    def recent(row) -> float:
        return -row.trashed_at.timestamp()

    return {
        "folders": [folder_payload(row) for row in folders if row.id not in trashed],
        "documents": [
            document_payload(row, job, failed, reconverts.get(row.reconvert_job_id))
            for row, job in documents
            if row.trashed_at is None and row.folder_id not in trashed
        ],
        "trash": {
            "folders": [
                {
                    **folder_payload(row),
                    "trashed_at": row.trashed_at,
                    "origin": origin(row.parent_id),
                    "items": items.get(row.id, 0),
                }
                for row in sorted((row for row in folders if row.trashed_at), key=recent)
            ],
            "documents": [
                {
                    **document_payload(row, job, failed, reconverts.get(row.reconvert_job_id)),
                    "trashed_at": row.trashed_at,
                    "origin": origin(row.folder_id),
                }
                for row, job in sorted(
                    ((row, job) for row, job in documents if row.trashed_at),
                    key=lambda pair: recent(pair[0]),
                )
            ],
        },
    }


# --- folders ------------------------------------------------------------------------------


class FolderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=FOLDER_NAME_MAX)
    parent_id: int | None = None


class FolderUpdate(BaseModel):
    """Partial: only the fields present in the body change (docs/api.md)."""

    name: str | None = Field(default=None, min_length=1, max_length=FOLDER_NAME_MAX)
    parent_id: int | None = None


@router.post("/folders", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_folder(
    body: FolderCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    # A primitive copy: a retry inside `insert_named` follows a rollback, which expires
    # the row behind `user`, and an expired attribute cannot be loaded here.
    user_id = user.id
    if body.parent_id is not None:
        await live_folder(db, user_id, body.parent_id)
    parent_id = body.parent_id
    # The row and its directory under one hold of the workspace lock, as a move or a
    # delete: the shell sees the folder as soon as the row exists, and a workspace that
    # takes no host write (recovering) refuses it up front (docs/workspace.md § Layout).
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        try:
            row = await insert_named(
                db,
                user_id=user_id,
                into=parent_id,
                build=lambda name: Folder(user_id=user_id, parent_id=parent_id, name=name),
                taken=lambda: sibling_names(db, user_id, parent_id),
                wanted=clean_name(body.name, FOLDER_NAME_MAX),
                limit=FOLDER_NAME_MAX,
                message="A folder with this name already exists here",
            )
        except ApiError:
            # A parent deleted between the check above and the insert fails the foreign
            # key, which arrives as the same IntegrityError a taken name does — and
            # retrying with a destination that no longer exists only ever produces a 409
            # about the wrong thing.
            if parent_id is None or await folder_exists(db, user_id, parent_id):
                raise
            raise ApiError(404, "invalid_request", "No such folder") from None
        relative = await folder_relative_dir(db, user_id, row.id)
        try:
            await workspace_documents.make_folder_dir(settings, user_id, relative)
        except (safe_io.UnsafeEntry, OSError):
            # An entry the shell planted where the directory belongs: the row stands,
            # the tree is the shell's, and the next materialization under it says so.
            logger.warning(
                "the directory of folder %d of user %d could not be made", row.id, user_id
            )
    return folder_payload(row)


@router.put("/folders/{folder_id}", dependencies=[CsrfGuard])
async def update_folder(
    folder_id: int, body: FolderUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    """Rename, move, or both.

    A rename that collides is refused: the name is what the user just typed, and quietly
    turning it into "Invoices (2)" would be a lie. A move that collides is suffixed, the
    way dropping a file into a folder that already holds one behaves.
    """
    provided = body.model_fields_set
    renaming = "name" in provided and body.name is not None
    clash = "A folder with this name already exists here"

    # The row and its directory move together, under the user's workspace lock, so no
    # content read can materialize at the new path in between (docs/workspace.md).
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        doomed = await subtree_ids(db, user.id, folder_id)
        await refuse_while_answering(
            db,
            user.id,
            select(Document.id).where(Document.user_id == user.id, Document.folder_id.in_(doomed)),
        )
        return await _update_folder_locked(
            folder_id, body, user, db, settings, provided, renaming, clash
        )


async def _update_folder_locked(
    folder_id, body, user, db, settings, provided, renaming: bool, clash: str
):
    # The whole decision sits inside the retry: a rollback releases the lock the move was
    # validated under, so a second attempt has to establish both facts again.
    for attempt in range(INSERT_ATTEMPTS):
        row = await live_folder(db, user.id, folder_id)
        parent_id = body.parent_id if "parent_id" in provided else row.parent_id

        if parent_id != row.parent_id and parent_id is not None:
            await live_folder(db, user.id, parent_id)
            if parent_id in await subtree_ids(db, user.id, row.id, for_update=True):
                raise ApiError(400, "invalid_request", "A folder cannot be moved inside itself")

        wanted = clean_name(body.name, FOLDER_NAME_MAX) if renaming else row.name
        await lock_user(db, user.id)
        taken = await sibling_names(db, user.id, parent_id, exclude_folder=row.id)
        if renaming and wanted in taken:
            raise ApiError(409, "invalid_request", clash)

        before = await folder_relative_dir(db, user.id, row.id)
        row.parent_id = parent_id
        row.name = free_name(taken, wanted, FOLDER_NAME_MAX)
        retries = 0 if renaming else INSERT_ATTEMPTS - 1 - attempt
        if await flush_or_retry(db, retries, clash):
            break
    # The tree follows the rows (docs/workspace.md): the directory moves, then the commit.
    after = await folder_relative_dir(db, user.id, row.id)
    landed = (Folder, row.id, {"parent_id": row.parent_id, "name": row.name})
    await move_then_commit(db, settings, user.id, before, after, landed=landed)
    return folder_payload(row)


@router.delete(
    "/folders/{folder_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_folder(
    folder_id: int, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Delete Immediately: a live folder, or one in the Trash, for good, with its own
    subtree — the documents in it are entries, so they go too. What in it was trashed on
    its own is moved up to the folder's parent first and stays in the Trash
    (docs/database.md § Rules).

    The subtree is deleted explicitly rather than left to `ON DELETE CASCADE`: the same
    rows disappear on PostgreSQL and on the SQLite test fallback, which does not enforce
    foreign keys unless asked to.
    """
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        await workspace_documents.recover_trash(db, settings, user_id)
        # The user's row first — the lock every folder creation takes before it inserts
        # — so the subtree read here is the subtree deleted below: a folder created
        # under it meanwhile would otherwise go by cascade, its documents' originals
        # kept and its conversations unchecked (docs/workspace.md § Layout).
        await lock_user(db, user_id)
        folders = await workspace_documents.user_folders(db, user_id)
        row = folders.get(folder_id)
        trashed = workspace_documents.trashed_folder_ids(folders.values())
        if row is None or (row.trashed_at is None and row.id in trashed):
            # Inside a trashed folder: the folder is what the Trash holds.
            raise ApiError(404, "invalid_request", "No such folder")
        relative = await folder_relative_dir(db, user_id, row.id)
        doomed = own_subtree(folders, row.id)
        documents = (
            (
                await db.execute(
                    select(Document).where(
                        Document.user_id == user_id, Document.folder_id.in_(doomed)
                    )
                )
            )
            .scalars()
            .all()
        )
        document_ids = [document.id for document in documents if document.trashed_at is None]
        await refuse_while_answering(db, user_id, document_ids)
        # Trashed on their own: out of the cascade's way, to the parent, still trashed.
        for document in documents:
            if document.trashed_at is not None:
                document.folder_id = row.parent_id
        for folder in folders.values():
            if folder.parent_id in doomed and folder.id not in doomed:
                folder.parent_id = row.parent_id
        await db.flush()
        await db.execute(
            delete(Document).where(Document.user_id == user_id, Document.id.in_(document_ids))
        )
        await db.execute(delete(Folder).where(Folder.user_id == user_id, Folder.id.in_(doomed)))
        if row.trashed_at is None:
            await remove_then_commit(
                db, settings, user_id, relative, document_ids, gone=(Folder, row.id)
            )
        else:
            await commit_then_discard(
                db,
                settings,
                user_id,
                [trash_entry("folder", row.id)],
                document_ids,
                gone=(Folder, row.id),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def remove_then_commit(
    db: DbSession,
    settings,
    user_id: int,
    relative,
    document_ids: list[int],
    *,
    gone: tuple[type, int],
) -> None:
    """The rows are deleted in the transaction; the directory goes before it commits. A
    removal the tree refuses keeps the rows, and the next read restores what is missing;
    a commit that fails after the removal keeps the rows too, and the files come back
    from the removal's own revision, so the edits they held are not lost to a fresh copy
    (docs/workspace.md § Revisions) — unless the row `gone` names is found deleted on a
    fresh connection, the commit having landed after all: then the delete finishes as
    if it had been acknowledged. The originals kept outside the mount go last, once the
    rows are gone for good."""
    try:
        removal = await workspace_documents.remove_path(settings, user_id, relative)
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        readable, row = await _read_row(db, *gone)
        if readable and row is None:
            logger.warning("the delete of %s was committed despite the error", relative)
            await _finish_delete(settings, user_id, relative, removal, document_ids)
        elif removal is not None:
            # Rolled back, or unknown: the files come back — an orphaned directory
            # beside no row is a nuisance, files gone with a row that stayed is a loss.
            try:
                await workspace_documents.restore_removed(settings, user_id, relative, removal)
            except Exception:  # noqa: BLE001 - the directory waits aside, whole
                logger.exception("could not restore %s after a failed delete", relative)
        raise
    await _finish_delete(settings, user_id, relative, removal, document_ids)


async def _finish_delete(
    settings, user_id: int, relative, removal, document_ids: list[int]
) -> None:
    if removal is not None:
        try:
            await workspace_documents.discard_removed(settings, user_id, removal)
        except Exception:  # noqa: BLE001 - the next reconciliation sweeps it
            logger.exception("could not discard %s set aside by a delete", relative)
    try:
        await workspace_documents.discard_originals(settings, user_id, document_ids)
    except Exception:  # noqa: BLE001 - an original with no row is dead weight, not a leak
        logger.exception("could not discard the originals of %s", relative)


# --- the Trash ----------------------------------------------------------------------------


async def stash_then_commit(
    db: DbSession, settings, user_id: int, relative, entry: str, *, landed: tuple[type, int]
) -> None:
    """The row is flushed with `trashed_at`; now the directory into the Trash, then the
    commit. A refusal from the tree rolls the row back to where the files still are; a
    commit that fails after the move brings the directory back — unless the row `landed`
    names is found trashed on a fresh connection, the commit having landed after all. A
    row that cannot be read leaves the entry where it is: the repair rule returns it
    should the row turn out live (docs/workspace.md § The Trash)."""
    try:
        moved = await workspace_documents.stash_path(settings, user_id, relative, entry)
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        if moved:
            readable, row = await _read_row(db, *landed)
            if not readable:
                logger.error("the move of %s to the Trash raised and cannot be verified", relative)
            elif row is not None and row.trashed_at is not None:
                logger.warning(
                    "the move of %s to the Trash was committed despite the error", relative
                )
            else:
                try:
                    await workspace_documents.unstash_path(
                        settings, user_id, entry, relative, f"restore {relative}"
                    )
                except Exception:  # noqa: BLE001 - the repair rule returns it
                    logger.exception("could not bring %s back from the Trash", relative)
        raise


async def put_back_then_move(
    db: DbSession,
    settings,
    user_id: int,
    entry: str,
    relative,
    *,
    landed: tuple[type, int],
    what: str,
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
        readable, row = await _read_row(db, *landed)
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
            settings, user_id, entry, relative, f"put back {relative}"
        )
    except Exception:  # noqa: BLE001 - the rows stand; the repair rule brings the files
        logger.exception("the files of %s stay in the Trash for now", relative)
    return answer


async def commit_then_discard(
    db: DbSession,
    settings,
    user_id: int,
    entries: list[str],
    document_ids: list[int],
    *,
    gone: tuple[type, int],
) -> None:
    """Rows deleted for good from the Trash commit first; their entries, originals and
    images go after. A commit that raised is checked on a fresh connection: the row `gone`
    names found deleted means it landed, and the cleanup runs as if acknowledged —
    otherwise nothing on disk has changed. A process that dies in between leaves entries
    with no rows, which the repair rule removes (docs/workspace.md § The Trash)."""
    try:
        await db.commit()
    except BaseException:
        readable, row = await _read_row(db, *gone)
        if readable and row is None:
            logger.warning("the delete of %s was committed despite the error", entries)
            await _discard_for_good(settings, user_id, entries, document_ids)
        raise
    await _discard_for_good(settings, user_id, entries, document_ids)


async def _discard_for_good(
    settings, user_id: int, entries: list[str], document_ids: list[int]
) -> None:
    try:
        await workspace_documents.discard_trash(settings, user_id, entries)
    except Exception:  # noqa: BLE001 - the repair rule removes an entry with no row
        logger.exception("could not remove %s from the Trash", entries)
    try:
        await workspace_documents.discard_originals(settings, user_id, document_ids)
    except Exception:  # noqa: BLE001 - reconciliation sweeps an original with no row
        logger.exception("could not discard the originals of %s", entries)


@router.post("/documents/{document_id}/trash", dependencies=[CsrfGuard])
async def trash_document(document_id: int, user: SessionUser, db: DbSession, settings: AppSettings):
    """Move to Trash: the row marked, its directory out of `files/` into the Trash, as one
    revision (docs/workspace.md § The Trash)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        # What an interrupted move left goes back first: an entry under this item's
        # name would refuse the move below.
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
        )
    return {**(await read_document_payload(db, row)), "trashed_at": row.trashed_at}


@router.post("/folders/{folder_id}/trash", dependencies=[CsrfGuard])
async def trash_folder(folder_id: int, user: SessionUser, db: DbSession, settings: AppSettings):
    """Move to Trash, a folder with everything in it (docs/workspace.md § The Trash)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
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
            db, settings, user_id, relative, trash_entry("folder", row.id), landed=(Folder, row.id)
        )
    return {**folder_payload(row), "trashed_at": row.trashed_at}


@router.post("/documents/{document_id}/restore", dependencies=[CsrfGuard])
async def restore_document(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    """Put Back: to the folder it was trashed from, or the nearest live one above it, a
    taken name suffixed as a move's is (docs/api.md)."""
    user_id = user.id
    clash = "A file with this name already exists here"
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
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
            if await flush_or_retry(db, INSERT_ATTEMPTS - 1 - attempt, clash):
                break
        relative = await document_relative(db, row)
        row = (
            await put_back_then_move(
                db,
                settings,
                user_id,
                trash_entry("document", row.id),
                relative,
                landed=(Document, row.id),
                what="The file",
            )
            or row
        )
    return await read_document_payload(db, row)


@router.post("/folders/{folder_id}/restore", dependencies=[CsrfGuard])
async def restore_folder(folder_id: int, user: SessionUser, db: DbSession, settings: AppSettings):
    """Put Back a folder, with everything that went to the Trash with it (docs/api.md)."""
    user_id = user.id
    clash = "A folder with this name already exists here"
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
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
            if await flush_or_retry(db, INSERT_ATTEMPTS - 1 - attempt, clash):
                break
        relative = await folder_relative_dir(db, user_id, row.id)
        row = (
            await put_back_then_move(
                db,
                settings,
                user_id,
                trash_entry("folder", row.id),
                relative,
                landed=(Folder, row.id),
                what="The folder",
            )
            or row
        )
    return folder_payload(row)


@router.delete("/trash", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def empty_trash(user: SessionUser, db: DbSession, settings: AppSettings) -> Response:
    """Empty Trash: every item in it, for good — rows first, then their files
    (docs/workspace.md § The Trash)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        await workspace_documents.recover_trash(db, settings, user_id)
        await lock_user(db, user_id)
        folders = await workspace_documents.user_folders(db, user_id)
        trashed = workspace_documents.trashed_folder_ids(folders.values())
        documents = (
            (await db.execute(select(Document).where(Document.user_id == user_id))).scalars().all()
        )
        doomed = [
            document.id
            for document in documents
            if document.trashed_at is not None or document.folder_id in trashed
        ]
        roots: list[tuple[type, int]] = [
            (Folder, row.id) for row in folders.values() if row.trashed_at is not None
        ] + [(Document, row.id) for row in documents if row.trashed_at is not None]
        if not roots:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await refuse_while_answering(db, user_id, doomed)
        await db.execute(
            delete(Document).where(Document.user_id == user_id, Document.id.in_(doomed))
        )
        await db.execute(delete(Folder).where(Folder.user_id == user_id, Folder.id.in_(trashed)))
        entries = [
            trash_entry("folder" if model is Folder else "document", row_id)
            for model, row_id in roots
        ]
        await commit_then_discard(db, settings, user_id, entries, doomed, gone=roots[0])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- documents ----------------------------------------------------------------------------


class DocumentUpdate(BaseModel):
    """Partial: only the fields present in the body change (docs/api.md)."""

    name: str | None = Field(default=None, min_length=1, max_length=DOCUMENT_NAME_MAX)
    folder_id: int | None = None


class DocumentFromJob(BaseModel):
    job_id: uuid.UUID
    folder_id: int | None = None


class DocumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=DOCUMENT_NAME_MAX)
    folder_id: int | None = None


def form_folder_id(form: FormData) -> int | None:
    """`folder_id` as a multipart field: absent or empty means the root."""
    raw = form.get("folder_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", "'folder_id' must be a folder id") from exc


@router.post("/documents", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request, user: SessionUser, db: DbSession, settings: AppSettings
):
    """The web upload: multipart `file` (+ optional `folder_id`) → a parse and a file.

    Transport only. The bytes stream to disk through `jobs.intake`, exactly as they do for
    `POST /v1/parse`; what this route adds is the name and the place (docs/api.md § Why the
    web upload is a control-plane route).
    """
    # A primitive copy: a retry inside `insert_named` follows a rollback, which expires
    # the row behind `user`, and an expired attribute cannot be loaded here.
    user_id = user.id
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ApiError(400, "invalid_request", "The upload must be multipart/form-data")
    # Cheap rejection before a byte is read; the capped stream below is what stops a
    # request that declares no length, or an untrue one.
    declared_length = request.headers.get("content-length", "")
    if declared_length.isdigit() and int(declared_length) > settings.upload_max_bytes * 2:
        raise ApiError(413, "invalid_request", "The upload exceeds UPLOAD_MAX_BYTES")

    # The parser reads a bounded stream rather than the unbounded one `request.form()`
    # hands it: it spools every file part to disk as that part arrives, so without this a
    # chunked request could fill the upload directory long before `store_upload` sees a
    # byte of it.
    try:
        form = await MultiPartParser(
            request.headers,
            capped_stream(request.stream(), settings.upload_max_bytes + MULTIPART_ENVELOPE_BYTES),
        ).parse()
    except MultiPartException as exc:
        raise ApiError(400, "invalid_request", "The multipart body could not be read") from exc

    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise ApiError(400, "invalid_request", "Multipart requests need a 'file' part")
    folder_id = form_folder_id(form)
    if folder_id is not None:
        await live_folder(db, user_id, folder_id)

    filename = upload.filename or "upload"
    media_type = upload.content_type or ""
    if (
        media_type == workspace_documents.MARKDOWN_MEDIA_TYPE
        or Path(filename).suffix.lower() in workspace_documents.MARKDOWN_EXTENSIONS
    ):
        return await file_markdown(
            upload, filename, user_id=user_id, folder_id=folder_id, db=db, settings=settings
        )
    # The library name is decided before the parse is queued: a name that cleans to
    # nothing is filed as `upload`, like a parse filed from the history, never a 400
    # after a billed parse is already on its way (docs/api.md).
    try:
        wanted = clean_name(filename, DOCUMENT_NAME_MAX)
    except ApiError:
        wanted = "upload"
    submission = await submit_parse(
        db,
        settings,
        user=user,
        chunks=upload_chunks(upload),
        media_type=media_type,
        filename=filename,
        # The browser cannot tell a lost response from a lost request, so its retry of an
        # upload that in fact landed must not buy a second parse (docs/jobs.md § Dedup).
        reuse_in_flight=True,
        # A library upload keeps its original even if the worker finishes before the
        # document row below exists (docs/jobs.md § Retention).
        retain_source=True,
        # Checked before it opens (docs/parsing.md § Format check).
        check=True,
    )
    # A dedup hit is a finished parse rather than a new one, so the file points at the job
    # that already produced it (docs/jobs.md § Dedup).
    job_id = submission.job.id if submission.job is not None else submission.cached.job_id

    # The folder can be deleted while the document is being stored and probed. The parse is
    # committed and will bill by now, so the file lands at the root rather than being lost
    # behind a foreign-key failure dressed up as a name conflict.
    if folder_id is not None and not await folder_exists(db, user_id, folder_id):
        folder_id = None

    # The job is committed and running by now, so the file has to land: `insert_named`
    # re-reads and retries rather than answering a conflict.
    async def land(place: int | None) -> Document:
        async def taken() -> set[str]:
            await lock_job(db, job_id)
            return await sibling_names(db, user_id, place)

        return await insert_named(
            db,
            user_id=user_id,
            into=place,
            build=lambda name: Document(user_id=user_id, folder_id=place, job_id=job_id, name=name),
            taken=taken,
            wanted=wanted,
            limit=DOCUMENT_NAME_MAX,
            message="A file with this name already exists here",
            commit=False,
        )

    # The row, its original and their commit under one hold of the workspace lock (then
    # the user's row and the job's: the one order every holder takes): a process that
    # dies between the insert and the copy leaves no row without the original it was
    # promised, and the backup lock held shared around it keeps a snapshot from seeing
    # one without the other (docs/workspace.md § Revisions, § Backups).
    root = user_root(settings.workspaces_dir, user_id)
    async with backup_shared(root), workspace_lock(root):
        try:
            row = await land(folder_id)
        except ApiError as exc:
            # The remaining sliver of that same race: deleted or moved to the Trash between
            # the check above and this insert — the 404 of the check under the user's row
            # lock, or a foreign-key failure rather than a name one.
            if folder_id is None or (
                exc.status_code != 404 and await folder_exists(db, user_id, folder_id)
            ):
                raise
            row = await land(None)
        row_id = row.id

        job = await job_of(db, job_id)
        # The parse may already be done (the worker then left the upload for this row),
        # or answered from the cache or joined in flight (the fresh upload is this row's
        # to keep). A copy that fails, or that the quota refuses, takes the row with it:
        # the transaction rolls back, so no entry stands without the original it was
        # promised, and no suffixed twin comes of the browser's retry.
        try:
            await workspace_documents.adopt_source(
                db, settings, row, job, upload=submission.upload, locked=True
            )
        except ApiError:
            await db.rollback()
            raise
        except Exception as exc:
            logger.exception("could not keep the upload for document %d", row_id)
            await db.rollback()
            raise ApiError(
                409, "invalid_request", "The upload could not be kept; nothing was filed"
            ) from exc
        # A commit that raises is verified before it is reported, and a rollback takes
        # the original and the shell's copy back with it (docs/workspace.md).
        relative = document_relative_dir(
            await workspace_documents.folder_names(db, user_id, row.folder_id), row.name
        )
        row = await commit_filing(
            db,
            row,
            what="The upload",
            undo=lambda: workspace_documents.discard_adopted(settings, user_id, row_id, relative),
        )
    return await read_document_payload(db, row)


async def file_markdown(
    upload: UploadFile,
    filename: str,
    *,
    user_id: int,
    folder_id: int | None,
    db: DbSession,
    settings: AppSettings,
) -> dict:
    """A `.md` upload: no parse, a markdown document whose original is the file
    (docs/workspace.md § Markdown documents). Staged and checked before any row exists,
    under the cap a `doc.md` is read under anyway."""
    try:
        wanted = clean_name(filename, DOCUMENT_NAME_MAX)
    except ApiError:
        wanted = "upload"
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = upload_dir / f"{uuid.uuid4().hex}.md"
    await store_upload(
        upload_chunks(upload), staged, settings.content_max_bytes, cap_name="CONTENT_MAX_BYTES"
    )
    try:
        if not await asyncio.to_thread(workspace_documents.is_utf8_file, staged):
            raise ApiError(400, "invalid_request", "A markdown file must be UTF-8 text")
        root = user_root(settings.workspaces_dir, user_id)
        # The row, its original and their commit under one hold of the workspace lock, the
        # backup lock shared around it, as for a parse's upload (docs/workspace.md § Backups).
        async with backup_shared(root), workspace_lock(root):
            try:
                row = await insert_named(
                    db,
                    user_id=user_id,
                    into=folder_id,
                    build=lambda name: Document(
                        user_id=user_id, folder_id=folder_id, job_id=None, name=name
                    ),
                    taken=lambda: sibling_names(db, user_id, folder_id),
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
            try:
                await workspace_documents.adopt_markdown(settings, row, staged)
            except ApiError:
                await db.rollback()
                raise
            except Exception as exc:
                logger.exception("could not keep the upload for document %d", row_id)
                await db.rollback()
                raise ApiError(
                    409, "invalid_request", "The upload could not be kept; nothing was filed"
                ) from exc
            relative = document_relative_dir(
                await workspace_documents.folder_names(db, user_id, row.folder_id), row.name
            )
            row = await commit_filing(
                db,
                row,
                what="The upload",
                undo=lambda: workspace_documents.discard_adopted(
                    settings, user_id, row_id, relative
                ),
            )
    finally:
        staged.unlink(missing_ok=True)
    return document_payload(row, None)


@router.post("/documents/new", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_document(body: DocumentCreate, user: SessionUser, db: DbSession):
    """A new, empty markdown document: a row with no job and no original — its baseline is
    empty (docs/workspace.md § Markdown documents). Its directory is made the first time it
    is read, like any document's. A taken name is suffixed, as a new folder's is."""
    user_id, folder_id = user.id, body.folder_id
    if folder_id is not None:
        await live_folder(db, user_id, folder_id)
    try:
        row = await insert_named(
            db,
            user_id=user_id,
            into=folder_id,
            build=lambda name: Document(
                user_id=user_id, folder_id=folder_id, job_id=None, name=name
            ),
            taken=lambda: sibling_names(db, user_id, folder_id),
            wanted=clean_name(body.name, DOCUMENT_NAME_MAX),
            limit=DOCUMENT_NAME_MAX,
            message="A file with this name already exists here",
        )
    except ApiError:
        if folder_id is None or await folder_exists(db, user_id, folder_id):
            raise
        raise ApiError(404, "invalid_request", "No such folder") from None
    return document_payload(row, None)


@router.post("/documents/from-job", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def file_job(body: DocumentFromJob, user: SessionUser, db: DbSession, settings: AppSettings):
    """File a parse that is already in the history — no upload, no second parse.

    How a result that came in over the API or the connector becomes a file
    (docs/web.md § History). A failed job is refused: it has no result to open.
    """
    job = (
        await db.execute(select(Job).where(Job.id == body.job_id, Job.user_id == user.id))
    ).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    if job.status == "failed":
        raise ApiError(400, "invalid_request", "A failed parse has no result to file")
    if body.folder_id is not None:
        await live_folder(db, user.id, body.folder_id)

    # Plain values, not attributes: a lost name race rolls back, which expires `job` and
    # `user`, and an expired attribute cannot be lazily reloaded inside an async session.
    user_id, folder_id, job_id = user.id, body.folder_id, job.id
    # A filename made only of separators cleans to nothing; it is still a parse worth
    # filing, under the same fallback an upload without a name gets.
    try:
        wanted = clean_name(job.filename, DOCUMENT_NAME_MAX)
    except ApiError:
        wanted = "upload"

    async def taken() -> set[str]:
        await lock_job(db, job_id)
        # Decided again under the lock: a parse that failed since the check above has
        # no result to file, and a row for it would stand against the 400 the route
        # promises (docs/api.md).
        current = (await db.execute(select(Job.status).where(Job.id == job_id))).scalar_one()
        if current == "failed":
            raise ApiError(400, "invalid_request", "A failed parse has no result to file")
        # A parse still running keeps its upload for this row: the worker, which reads
        # its recipients under this same row lock, either sees the row or finds the
        # job marked for retention and leaves the upload for `inherit_original` to
        # take — never a row filed between its look and its commit with nothing kept.
        await db.execute(
            update(Job)
            .where(Job.id == job_id, Job.status.in_(("queued", "running")))
            .values(retain_source=True)
        )
        return await sibling_names(db, user_id, folder_id)

    # The row, its copy of the original and their commit under one hold of the workspace
    # lock, the backup lock shared around it, as for an upload: no row without its
    # original, in the database or in a snapshot (docs/workspace.md § Backups).
    root = user_root(settings.workspaces_dir, user_id)
    async with backup_shared(root), workspace_lock(root):
        try:
            row = await insert_named(
                db,
                user_id=user_id,
                into=folder_id,
                build=lambda name: Document(
                    user_id=user_id, folder_id=folder_id, job_id=job_id, name=name
                ),
                taken=taken,
                wanted=wanted,
                limit=DOCUMENT_NAME_MAX,
                message="A file with this name already exists here",
                commit=False,
            )
        except ApiError:
            # Same race as creating a folder: a destination deleted after the check above
            # fails the foreign key, and that is a missing folder, not a name conflict.
            if folder_id is None or await folder_exists(db, user_id, folder_id):
                raise
            raise ApiError(404, "invalid_request", "No such folder") from None
        row_id = row.id
        job = await job_of(db, job_id)
        # The original went with the first document filed from this parse: this one
        # takes a copy, so its pages render too (docs/workspace.md § Layout). A copy that
        # fails takes the row with it: the transaction rolls back.
        try:
            filed = await workspace_documents.inherit_original(db, settings, row, job, locked=True)
        except Exception as exc:
            logger.exception("could not copy the original for document %d", row_id)
            await db.rollback()
            raise ApiError(
                409, "invalid_request", "The original could not be copied; nothing was filed"
            ) from exc
        # A commit that raises is verified before it is reported, and a rollback takes
        # the copy of the original back with it (docs/workspace.md).
        relative = document_relative_dir(
            await workspace_documents.folder_names(db, user_id, row.folder_id), row.name
        )
        row = await commit_filing(
            db,
            row,
            what="The parse",
            undo=lambda: workspace_documents.discard_adopted(settings, user_id, row_id, relative),
        )
        if not filed:
            raise ApiError(404, "invalid_request", "The file was deleted while it was being filed")
    return await read_document_payload(db, row)


@router.put("/documents/{document_id}", dependencies=[CsrfGuard])
async def update_document(
    document_id: int,
    body: DocumentUpdate,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """Rename and/or move a file. Same rule as a folder: rename refuses, move suffixes."""
    provided = body.model_fields_set
    renaming = "name" in provided and body.name is not None
    clash = "A file with this name already exists here"

    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        # A turn holds the document's path for its whole run (docs/chat.md § Tools).
        await refuse_while_answering(
            db, user.id, select(Document.id).where(Document.id == document_id)
        )
        return await _update_document_locked(
            document_id, body, user, db, settings, provided, renaming, clash
        )


async def _update_document_locked(
    document_id, body, user, db, settings, provided, renaming: bool, clash: str
):
    for attempt in range(INSERT_ATTEMPTS):
        row = await live_document(db, user.id, document_id)
        folder_id = body.folder_id if "folder_id" in provided else row.folder_id
        if folder_id != row.folder_id and folder_id is not None:
            await live_folder(db, user.id, folder_id)

        wanted = clean_name(body.name, DOCUMENT_NAME_MAX) if renaming else row.name
        await lock_user(db, user.id)
        taken = await sibling_names(db, user.id, folder_id, exclude_document=row.id)
        if renaming and wanted in taken:
            raise ApiError(409, "invalid_request", clash)

        before = await document_relative(db, row)
        row.folder_id = folder_id
        row.name = free_name(taken, wanted, DOCUMENT_NAME_MAX)
        # Two tabs moving same-named files into one folder is the same race a create
        # loses, and it has the same answer: take the next name, not a conflict.
        retries = 0 if renaming else INSERT_ATTEMPTS - 1 - attempt
        if await flush_or_retry(db, retries, clash):
            break
    after = await document_relative(db, row)
    landed = (Document, row.id, {"folder_id": row.folder_id, "name": row.name})
    await move_then_commit(db, settings, user.id, before, after, landed=landed)
    return await read_document_payload(db, row)


@router.delete(
    "/documents/{document_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Delete Immediately: a live file, or one in the Trash, for good. The parse itself
    stays in the history."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        await workspace_documents.recover_trash(db, settings, user_id)
        row = await owned_document(db, user_id, document_id)
        live = workspace_documents.is_live(row, await workspace_documents.user_folders(db, user_id))
        if not live and row.trashed_at is None:
            # Inside a trashed folder: the folder is what the Trash holds.
            raise ApiError(404, "invalid_request", "No such document")
        relative = await document_relative(db, row)
        await refuse_while_answering(db, user_id, [row.id])
        await db.delete(row)
        await db.flush()
        if live:
            await remove_then_commit(
                db, settings, user_id, relative, [row.id], gone=(Document, row.id)
            )
        else:
            await commit_then_discard(
                db,
                settings,
                user_id,
                [trash_entry("document", row.id)],
                [row.id],
                gone=(Document, row.id),
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def take_pending_reconvert(db: DbSession, settings: AppSettings, row: Document) -> Document:
    """A re-convert that succeeded while its document still points at it — the worker
    stopped between the parse's terminal commit and the swap — taken before the document
    is read; one that cannot be taken now leaves the document as it is (docs/jobs.md
    § Re-converting a document). The document comes back read again: a take that failed
    rolled the session back, which expires every row the session holds."""
    if row.reconvert_job_id is None:
        return row
    document_id, user_id = row.id, row.user_id
    try:
        await workspace_documents.take_reconvert(db, settings, row)
    except Exception:  # noqa: BLE001 - the document opens as it is; the next read tries again
        logger.exception("document %d could not take its re-convert", document_id)
    return await live_document(db, user_id, document_id)


@router.get("/documents/{document_id}/result")
async def document_result(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_document(db, user.id, document_id)
    row = await take_pending_reconvert(db, settings, row)
    if row.job_id is None:
        raise ApiError(404, "invalid_request", "A markdown document has no parse result")
    # The job before the result, deliberately: under READ COMMITTED each statement gets its
    # own snapshot, so read the other way a job finishing between the two reads answers
    # "no result" + "succeeded" — a 404 for a result that now exists. This way the same
    # race reads "running" and answers a partial instead, which the next poll replaces.
    job = await job_of(db, row.job_id)
    result = (
        await db.execute(select(Result).where(Result.job_id == row.job_id))
    ).scalar_one_or_none()
    if result is not None:
        return result_payload(result)
    return await partial_result(db, job)


async def partial_result(db: DbSession, job: Job) -> dict:
    """A still-running job's finished pages, or the 404 a job without any result earns.

    Control plane only — the data plane stays 404 until the job finishes
    (docs/api.md § Partial results).
    """
    if job.status not in ("queued", "running"):
        raise ApiError(404, "invalid_request", "This job has no result yet")
    rows = (
        (
            await db.execute(
                select(JobPage).where(JobPage.job_id == job.id).order_by(JobPage.page_no)
            )
        )
        .scalars()
        .all()
    )
    return partial_result_payload(job, list(rows))


async def figure_crop_response(settings: AppSettings, job: Job, page: int, bbox: str) -> Response:
    """One stored crop, addressed the way its placeholder addresses it, or 404.

    The bbox is parsed and re-cleaned into the canonical filename, so the path on disk is
    built from four bounded integers — never from request text.
    """
    cleaned = parse_bbox_path(bbox)
    if cleaned is None or page < 1:
        raise ApiError(404, "invalid_request", "No such figure")
    # Where it is now: published under the job's name, or still in a running attempt's
    # directory — opened there and then, since the publication can move it before a
    # response would open the path, and streamed from that descriptor in a thread, so a
    # figure-heavy result holds no crop in memory and blocks no other request
    # (docs/parsing.md § Figure crops).
    handle = await asyncio.to_thread(open_crop, Path(settings.figures_dir), job.id, page, cleaned)
    if handle is None:
        raise ApiError(404, "invalid_request", "No crop is stored for this figure")
    size = os.fstat(handle.fileno()).st_size
    # Never cached: browser caches key on the URL, not the session, so a cached crop
    # would outlive a sign-out and answer the next account in the same browser profile.
    return StreamingResponse(
        iterate_in_threadpool(crop_chunks(handle)),
        media_type="image/png",
        headers={"Cache-Control": "private, no-store", "Content-Length": str(size)},
    )


@router.get("/documents/{document_id}/figures/{page}/{bbox}")
async def document_figure(
    document_id: int,
    page: int,
    bbox: str,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    row = await live_document(db, user.id, document_id)
    if row.job_id is None:
        raise ApiError(404, "invalid_request", "No such figure")
    job = await job_of(db, row.job_id)
    try:
        return await figure_crop_response(settings, job, page, bbox)
    except ApiError:
        pass  # not this parse's crop
    # One an earlier parse of the same original made: a placeholder from before a
    # re-convert, in a reverted text or quoted in the notes (docs/parsing.md § Figure
    # crops). Newest first.
    others = (
        await db.execute(
            select(Job)
            .where(
                Job.user_id == user.id,
                Job.sha256 == job.sha256,
                Job.status == "succeeded",
                Job.id != job.id,
            )
            .order_by(Job.finished_at.desc())
        )
    ).scalars()
    for other in others:
        try:
            return await figure_crop_response(settings, other, page, bbox)
        except ApiError:
            continue
    raise ApiError(404, "invalid_request", "No crop is stored for this figure")


# --- a parse page by page, and the retry of its failed pages --------------------------------


@router.get("/documents/{document_id}/pages")
async def document_pages(document_id: int, user: SessionUser, db: DbSession):
    """Every page of the parse's selection with its state — what the retry dialog lists
    (docs/api.md): the document's parse, or its failed re-convert, since that is what a
    retry would send. A page with no row is `pending`: queued, running, or never reached
    before a failure that ended the job."""
    row = await live_document(db, user.id, document_id)
    if row.job_id is None:
        raise ApiError(404, "invalid_request", "A markdown document has no pages")
    job = await job_of(db, row.job_id)
    if job.status != "failed" and row.reconvert_job_id is not None:
        reconvert = await job_of(db, row.reconvert_job_id)
        if reconvert.status == "failed":
            job = reconvert
    rows = {
        page.page_no: page
        for page in (await db.execute(select(JobPage).where(JobPage.job_id == job.id))).scalars()
    }
    return {
        "pages": [
            {
                "page": page_no,
                "status": rows[page_no].status if page_no in rows else "pending",
                "reason": rows[page_no].error if page_no in rows else None,
                "model": rows[page_no].model if page_no in rows else None,
            }
            for page_no in parse_pages_spec(job.pages_spec, job.page_count or 1)
        ]
    }


@router.get("/documents/{document_id}/pages/{page}/image")
async def document_page_image(
    document_id: int,
    page: int,
    request: Request,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """One page of the document's original as a PNG, for the retry dialog: drawn the way
    the chat agent's `view_page` draws it, smaller, a few at a time per process
    (docs/api.md § Limits). Never cached, as a crop is not."""
    row = await live_document(db, user.id, document_id)
    if row.job_id is None:
        raise ApiError(404, "invalid_request", "A markdown document has no pages")
    job = await job_of(db, row.job_id)
    last = (job.page_count or 1) if job.kind == "pdf" else 1
    if not 1 <= page <= last:
        raise ApiError(404, "invalid_request", "No such page")
    root = user_root(settings.workspaces_dir, row.user_id)
    source = workspace_documents.original_of(root, row.id)
    if source is None:
        raise ApiError(404, "invalid_request", "This document kept no original")
    slots = request.app.state.preview_slots
    try:
        data = await original_page_png(
            root,
            source,
            kind=job.kind,
            page=page,
            render_slots=slots,
            decode_slot=slots,
            max_bytes=settings.upload_max_bytes,
            max_pixels=settings.chat_page_image_max_pixels,
            long_edge_px=settings.page_preview_long_edge,
        )
    except PageUnavailable as exc:
        raise ApiError(404, "invalid_request", str(exc)) from exc
    return Response(
        content=data, media_type="image/png", headers={"Cache-Control": "private, no-store"}
    )


class DocumentRetry(BaseModel):
    """The model the failed pages are sent to; none is the job's own."""

    model: str | None = Field(default=None, max_length=255)


@router.post(
    "/documents/{document_id}/retry",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_document(
    document_id: int, body: DocumentRetry, user: SessionUser, db: DbSession, settings: AppSettings
):
    """Put the failed parse behind the document back on the queue, its succeeded pages
    kept (docs/jobs.md § Retrying failed pages) — or its failed re-convert, when the
    document's own parse stands (§ Re-converting a document). Its upload went with the
    failure, so it runs on a copy of the document's original. Decided under the workspace
    lock — the original cannot go while it is copied, nor the file to the Trash — and the
    job's row lock, from `failed` only: two retries cannot both put it back. A commit that
    raises is read back on a connection of its own, as a filing's is: the copy goes only
    with a retry that did not land."""
    model = (body.model or "").strip() or None
    root = user_root(settings.workspaces_dir, user.id)
    async with workspace_lock(root):
        row = await live_document(db, user.id, document_id)
        if row.job_id is None:
            raise ApiError(404, "invalid_request", "A markdown document has no parse to retry")
        await lock_job(db, row.job_id)
        job = await job_of(db, row.job_id)
        if job.status != "failed" and row.reconvert_job_id is not None:
            await lock_job(db, row.reconvert_job_id)
            job = await job_of(db, row.reconvert_job_id)
        if job.agent != PARSE.name or job.status != "failed":
            raise ApiError(409, "invalid_request", "Only a failed parse can be retried")
        original = workspace_documents.original_of(root, row.id)
        if original is None:
            raise ApiError(
                409,
                "invalid_request",
                "This document kept no original to parse again; upload the file again",
            )
        upload_dir = Path(settings.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        copy = upload_dir / f"{job.id}.retry-{uuid.uuid4().hex[:12]}{original.suffix}"
        await asyncio.to_thread(shutil.copyfile, original, copy)
        try:
            # The check as the settings say now: a check that failed may have failed on
            # its model. One with no model of its own follows this run's parse model, the
            # one the retry named (docs/jobs.md § Retrying failed pages).
            check = await resolve_check(db, settings, user.id, model or job.model)
            await retry_job(db, job.id, source_path=str(copy), model=model, check=check)
            await events.notify(db, job.id)
        except BaseException:
            copy.unlink(missing_ok=True)
            raise
        # Read before the commit: a rollback expires the row.
        job_id = job.id
        try:
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - verified below, whatever raised
            readable, landed = await _read_row(db, Job, job_id)
            with contextlib.suppress(Exception):
                await db.rollback()
            if readable and landed is not None and landed.source_path == str(copy):
                logger.warning("the retry of job %s was committed despite the error", job_id)
                return {"job_id": str(job_id), "status": "queued"}
            if readable:
                copy.unlink(missing_ok=True)
                raise ApiError(
                    409, "invalid_request", "The retry could not be queued; nothing changed"
                ) from exc
            # The copy stays: a retry that landed runs on it, and the upload sweeper
            # takes it otherwise (docs/jobs.md § Retrying failed pages).
            raise ApiError(
                409,
                "invalid_request",
                "The retry may have been queued, but the database could not confirm it; "
                "reload the library before trying again",
            ) from exc
    return {"job_id": str(job.id), "status": "queued"}


class DocumentReconvert(BaseModel):
    """The model the new parse runs on; none is the parse agent's own."""

    model: str | None = Field(default=None, max_length=255)


@router.post(
    "/documents/{document_id}/reconvert",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_202_ACCEPTED,
)
async def reconvert_document(
    document_id: int,
    body: DocumentReconvert,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """Parse the document's original again, whole, as a Files upload of it would be,
    never answered from the cache: the cached answer is the one being replaced. The
    document keeps its version and points at the new parse, which it takes once that
    succeeds (docs/jobs.md § Re-converting a document). Decided under the workspace
    lock, which every writer of the pointer holds, so two clicks cannot start two
    parses; the job and the pointer land in one commit."""
    model = (body.model or "").strip() or None
    root = user_root(settings.workspaces_dir, user.id)
    async with workspace_lock(root):
        row = await live_document(db, user.id, document_id)
        if row.job_id is None:
            raise ApiError(404, "invalid_request", "A markdown document has no parse to re-convert")
        job = await job_of(db, row.job_id)
        if job.status != "succeeded":
            raise ApiError(
                409,
                "invalid_request",
                "Only a parsed document can be re-converted; a failed parse is retried",
            )
        # A failed re-convert gives way to the new one; any other is on its way (one that
        # succeeded is being taken).
        if (
            row.reconvert_job_id is not None
            and (await job_of(db, row.reconvert_job_id)).status != "failed"
        ):
            raise ApiError(409, "invalid_request", "The document is already being re-converted")
        original = workspace_documents.original_of(root, row.id)
        if original is None:
            raise ApiError(
                409,
                "invalid_request",
                "This document kept no original to parse again; upload the file again",
            )
        submission = await submit_parse(
            db,
            settings,
            user=user,
            chunks=file_chunks(original),
            media_type=job.media_type,
            filename=job.filename,
            model=model,
            force=True,
            check=True,
            reconvert=row,
        )
    return {"job_id": str(submission.job.id), "status": "queued"}


@router.delete(
    "/documents/{document_id}/reconvert",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def dismiss_reconvert(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Dismiss a failed re-convert: the document stops pointing at it, and the parse
    stays in History (docs/jobs.md § Re-converting a document). Under the workspace lock,
    which a retry of it holds too."""
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        row = await live_document(db, user.id, document_id)
        if row.reconvert_job_id is None:
            raise ApiError(404, "invalid_request", "The document has no re-convert")
        if (await job_of(db, row.reconvert_job_id)).status != "failed":
            raise ApiError(409, "invalid_request", "Only a failed re-convert can be dismissed")
        row.reconvert_job_id = None
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- images inserted into a document --------------------------------------------------------

IMAGE_CAP_NAME = "DOCUMENT_IMAGE_MAX_BYTES"


def prepare_image(
    source: Path, out_dir: Path, stem: str, *, max_pixels: int, long_edge: int
) -> Path:
    """The upload as it is kept: decoded only as one of the accepted formats, refused past
    the pixel cap read from its header, then upright, flattened and downscaled as an
    attachment is (docs/workspace.md § Inserted images)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=workspace_documents.IMAGE_FORMATS) as opened:
                too_large = opened.width * opened.height > max_pixels
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ) as exc:
        raise ApiError(
            400, "invalid_request", "The file is not a PNG, JPEG, WebP, GIF or HEIC image"
        ) from exc
    if too_large:
        raise ApiError(400, "invalid_request", "The image has too many pixels")
    try:
        normalized = normalize_image(
            source,
            out_dir,
            max_long_edge=long_edge,
            stem=stem,
            formats=workspace_documents.IMAGE_FORMATS,
        )
    except ImageError as exc:
        raise ApiError(400, "invalid_request", "The image could not be read") from exc
    return normalized.path


@router.post("/images", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def upload_image(request: Request, user: SessionUser, db: DbSession, settings: AppSettings):
    """An image for a document's text: kept as the document's, its path answered for the
    markdown line the caller writes (docs/api.md)."""
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ApiError(400, "invalid_request", "The upload must be multipart/form-data")
    cap = settings.document_image_max_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap + MULTIPART_ENVELOPE_BYTES:
        raise ApiError(413, "invalid_request", f"The upload exceeds {IMAGE_CAP_NAME}")
    # Bounded before the parser spools a byte, as an attachment's body is.
    try:
        form = await MultiPartParser(
            request.headers,
            capped_stream(
                request.stream(), cap + MULTIPART_ENVELOPE_BYTES, cap_name=IMAGE_CAP_NAME
            ),
        ).parse()
    except MultiPartException as exc:
        raise ApiError(400, "invalid_request", "The multipart body could not be read") from exc
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise ApiError(400, "invalid_request", "Multipart requests need a 'file' part")
        document_id = form.get("document_id")
        if not isinstance(document_id, str) or not document_id.isdigit():
            raise ApiError(400, "invalid_request", "Multipart requests need a 'document_id' part")
        row = await live_document(db, user.id, int(document_id))
        upload_dir = Path(settings.upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
        stem = uuid.uuid4().hex
        staged = upload_dir / f"{stem}.upload"
        try:
            await store_upload(upload_chunks(upload), staged, cap, cap_name=IMAGE_CAP_NAME)
            prepared = await asyncio.to_thread(
                prepare_image,
                staged,
                upload_dir,
                stem,
                max_pixels=settings.chat_page_image_max_pixels,
                long_edge=settings.document_image_long_edge,
            )
            try:
                name = await workspace_documents.keep_image(db, settings, row, prepared)
            finally:
                prepared.unlink(missing_ok=True)
        finally:
            staged.unlink(missing_ok=True)
    finally:
        await form.close()
    return {"path": f"figures/{name}"}


@router.get("/documents/{document_id}/images/{name}")
async def document_image(
    document_id: int, name: str, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_document(db, user.id, document_id)
    root = user_root(settings.workspaces_dir, row.user_id)
    image = workspace_documents.image_of(root, row.id, name)
    if image is None:
        raise ApiError(404, "invalid_request", "No such image")
    try:
        handle = image.open("rb")
    except OSError as exc:
        raise ApiError(404, "invalid_request", "No such image") from exc
    size = os.fstat(handle.fileno()).st_size
    # Never cached, as a crop is not: a cached image would outlive a sign-out.
    return StreamingResponse(
        iterate_in_threadpool(crop_chunks(handle)),
        media_type=workspace_documents.IMAGE_SUFFIX_MEDIA[image.suffix],
        headers={"Cache-Control": "private, no-store", "Content-Length": str(size)},
    )


# --- copies -------------------------------------------------------------------------------


class DocumentCopy(BaseModel):
    folder_id: int | None = None


class FolderCopy(BaseModel):
    parent_id: int | None = None


async def keep_parses(db: DbSession, jobs: list[Job]) -> None:
    """The job rows behind the documents a copy files, locked in one order, read again
    under the lock and marked for retention while they run, as a filing from History
    marks one: the worker finishing one meanwhile sees the new rows among its recipients
    or leaves the upload for them (docs/jobs.md § Retention)."""
    for job in sorted(jobs, key=lambda job: str(job.id)):
        await lock_job(db, job.id)
        await db.refresh(job)
        if job.status in ("queued", "running"):
            job.retain_source = True
    await db.flush()


async def donors_of(db: DbSession, documents: list[Document]) -> dict[uuid.UUID, list[int]]:
    """Every document of each parse behind `documents`: whose original a copy may take
    when its source holds none (docs/workspace.md § Copies)."""
    job_ids = {document.job_id for document in documents if document.job_id is not None}
    donors: dict[uuid.UUID, list[int]] = {}
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
    source: Document,
    target: Document,
    job: Job | None,
    inner: Path,
    donors: dict[uuid.UUID, list[int]],
) -> workspace_documents.CopiedDocument:
    others = donors.get(source.job_id, []) if source.job_id is not None else []
    return workspace_documents.CopiedDocument(
        source_id=source.id,
        target_id=target.id,
        job=job,
        inner=inner,
        donors=(source.id, *(other for other in others if other not in (source.id, target.id))),
        parsing=job is not None and job.status in ("queued", "running"),
    )


async def copy_then_commit(
    db: DbSession,
    settings,
    user_id: int,
    source: Path,
    target: Path,
    documents: list[workspace_documents.CopiedDocument],
    *,
    folder: bool,
    landed: tuple[type, int],
):
    """The rows are flushed; now the files, then the commit. A copy the tree refuses rolls
    the rows back; a commit that raises is checked on a fresh connection — landed, the
    copy stands and its row is answered; rolled back, the copy is removed as a revision of
    its own; unknown, answered as unconfirmed (docs/workspace.md § Copies)."""
    try:
        made = await workspace_documents.copy_path(
            settings, user_id, source, target, documents, folder=folder
        )
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - verified below, whatever raised
        readable, row = await _read_row(db, *landed)
        with contextlib.suppress(Exception):
            await db.rollback()
        if readable and row is not None:
            logger.warning("the copy to %s was committed despite the error", target)
            return row
        if readable:
            try:
                await workspace_documents.remove_copy(
                    settings, user_id, target, made, [document.target_id for document in documents]
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


@router.post(
    "/documents/{document_id}/copy", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED
)
async def copy_document(
    document_id: int, body: DocumentCopy, user: SessionUser, db: DbSession, settings: AppSettings
):
    """A copy of a document — its text, notes, original and images as they are now — into
    a folder, or beside it as a duplicate (docs/workspace.md § Copies)."""
    user_id, folder_id = user.id, body.folder_id
    root = user_root(settings.workspaces_dir, user_id)
    # The rows, the files and the commit under one hold of the workspace lock, the backup
    # lock shared around it, as a filing: no row without its original, in the database
    # or in a snapshot (docs/workspace.md § Backups).
    async with backup_shared(root), workspace_lock(root):
        # What an interrupted move to or from the Trash left goes back first: the copy
        # reads the source's directory, and the edits may be waiting there.
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
        await workspace_documents.ensure_within_quota(settings, root)
        job = await job_of(db, source.job_id) if source.job_id is not None else None
        if job is not None:
            await keep_parses(db, [job])
        taken = await sibling_names(db, user_id, folder_id)
        row = Document(
            user_id=user_id,
            folder_id=folder_id,
            job_id=source.job_id,
            name=free_name(taken, source.name, DOCUMENT_NAME_MAX),
        )
        db.add(row)
        await flush_or_retry(db, 0, "A file with this name already exists here")
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
        )
    # A commit that raised and landed answers with the row as committed: the request's
    # session rolled back.
    return await read_document_payload(db, landed or row)


@router.post(
    "/folders/{folder_id}/copy", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED
)
async def copy_folder(
    folder_id: int, body: FolderCopy, user: SessionUser, db: DbSession, settings: AppSettings
):
    """A copy of a folder with every live folder and document under it, in the same shape
    (docs/workspace.md § Copies)."""
    user_id, parent_id = user.id, body.parent_id
    root = user_root(settings.workspaces_dir, user_id)
    async with backup_shared(root), workspace_lock(root):
        await workspace_documents.recover_trash(db, settings, user_id)
        await lock_user(db, user_id)
        source = await live_folder(db, user_id, folder_id)
        folders = await workspace_documents.user_folders(db, user_id)
        inside = own_subtree(folders, source.id)
        if parent_id is not None:
            await live_folder(db, user_id, parent_id)
            if parent_id in inside:
                raise ApiError(400, "invalid_request", "A folder cannot be copied inside itself")
        await workspace_documents.ensure_within_quota(settings, root)
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
            job.id: job
            for job in (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()
        }
        await keep_parses(db, list(jobs.values()))
        taken = await sibling_names(db, user_id, parent_id)
        top = Folder(
            user_id=user_id,
            parent_id=parent_id,
            name=free_name(taken, source.name, FOLDER_NAME_MAX),
        )
        db.add(top)
        await flush_or_retry(db, 0, "A folder with this name already exists here")
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
        )
    return folder_payload(landed or top)


# --- the workspace: where a row's directory is ----------------------------------------------


async def folder_relative_dir(db: DbSession, user_id: int, folder_id: int) -> Path:
    names = await workspace_documents.folder_names(db, user_id, folder_id)
    path = Path("files")
    for name in names:
        path /= path_segment(name)
    return path


async def document_relative(db: DbSession, row: Document) -> Path:
    names = await workspace_documents.folder_names(db, row.user_id, row.folder_id)
    return document_relative_dir(names, row.name)


# --- content, notes, revisions (docs/workspace.md) -----------------------------------------


class ContentPut(BaseModel):
    markdown: str
    base_revision: str = Field(min_length=1, max_length=64)


class NotesPut(BaseModel):
    text: str
    base_revision: str = Field(min_length=1, max_length=64)


@router.get("/documents/{document_id}/content")
async def document_content(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_document(db, user.id, document_id)
    row = await take_pending_reconvert(db, settings, row)
    content = await workspace_documents.read_content(db, settings, row)
    return {
        "markdown": content.markdown,
        "revision": content.revision,
        "notes": content.notes,
        "notes_revision": content.notes_revision,
        "has_source": content.has_source,
    }


@router.put("/documents/{document_id}/content", dependencies=[CsrfGuard])
async def put_document_content(
    document_id: int, body: ContentPut, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_document(db, user.id, document_id)
    revision = await workspace_documents.write_content(
        db, settings, row, markdown=body.markdown, base_revision=body.base_revision
    )
    return {"revision": revision}


@router.put("/documents/{document_id}/notes", dependencies=[CsrfGuard])
async def put_document_notes(
    document_id: int, body: NotesPut, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_document(db, user.id, document_id)
    revision = await workspace_documents.write_notes(
        db, settings, row, text=body.text, base_revision=body.base_revision
    )
    return {"revision": revision}


@router.get("/documents/{document_id}/revisions")
async def document_revisions(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_document(db, user.id, document_id)
    history = await workspace_documents.revisions(db, settings, row)
    return {
        "revisions": [
            {
                "commit": entry.commit,
                "author": entry.author,
                "message": entry.message,
                "created_at": entry.created_at,
                "files": list(entry.files),
            }
            for entry in history
        ]
    }


@router.post("/documents/{document_id}/revisions/{commit}/revert", dependencies=[CsrfGuard])
async def revert_document_revision(
    document_id: int,
    commit: str,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    row = await live_document(db, user.id, document_id)
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ApiError(404, "invalid_request", "No such revision")
    # The workspace lock first, then the rows — the order every mutation takes. A shell
    # revision may have moved the document; undoing that under a running turn would pull
    # the path out from under it (docs/chat.md § Conversations).
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        # A revision that reached beyond this document (a shell's) may be what another
        # conversation's turn is working on right now: refused while any turn of the
        # user is unfinished (docs/workspace.md § Revisions).
        # Decided before anything is written: materializing first would hand a turn
        # that is between tool calls a fresh copy of the document it just removed.
        paths = await workspace_documents.document_paths(db, settings, row)
        beyond = await asyncio.to_thread(workspace_documents.reaches_beyond, paths, commit)
        guarded = (
            select(Document.id).where(Document.user_id == user.id)
            if beyond
            else select(Document.id).where(Document.id == row.id)
        )
        await refuse_while_answering(db, user.id, guarded)
        await workspace_documents.materialize_document(db, settings, row, locked=True)
        reverted = await workspace_documents.revert(db, settings, row, commit, locked=True)
    return {"revision": reverted.revision, "commit": reverted.commit}
