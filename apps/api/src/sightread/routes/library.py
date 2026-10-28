"""Control plane `/api/library/*` — the web file library (docs/web.md § Files).

Thin like every route module. Folders and documents are names and places; the parse behind
a document is a `jobs` row this module never interprets, and the upload is handed straight
to `jobs.intake.submit_parse`, the same sequence `/v1/parse` runs
(docs/project-structure.md § Boundaries).

A document is an entry, not a second copy of a result: deleting one deletes the entry, and
the job and its output then fall under the parse retention (docs/jobs.md § Retention),
their usage and ledger rows outliving them (docs/database.md § Rules).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import urllib.parse
import uuid
import warnings
from collections.abc import Collection, Mapping
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import iterate_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from ..agents import PARSE
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..billing import ledger
from ..db.models import (
    DOCUMENT_VIEWS,
    FILE_KIND,
    MARKDOWN_KIND,
    ORIGINAL_VIEW,
    PARSE_KIND,
    AgentSetting,
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
    checked_parse_model,
    convert_kept_file,
    file_chunks,
    insufficient_credits,
    resolve_check,
    resolve_kind,
    store_upload,
    submit_parse,
    upload_chunks,
)
from ..jobs.queue import parse_pages_spec, retry_job
from ..jobs.runner import partial_result_payload, result_payload
from ..parsing.figures import crop_chunks, open_crop, parse_bbox_path
from ..parsing.images import ImageError, normalize_image
from ..workspace import documents as workspace_documents
from ..workspace import git, safe_io, trash
from ..workspace.filing import (
    DOCUMENT_NAME_MAX,
    FOLDER_NAME_MAX,
    INSERT_ATTEMPTS,
    STORAGE_FULL,
    clean_name,
    commit_filing,
    document_relative,
    folder_exists,
    folder_relative_dir,
    free_name,
    insert_named,
    keep_file,
    make_folder,
    sibling_names,
)
from ..workspace.layout import (
    document_relative_dir,
    path_segment,
    trash_entry,
    user_root,
)
from ..workspace.locks import backup_shared, workspace_lock
from ..workspace.trash import commit_then_discard, lock_user, read_row, refuse_while_answering
from .multipart import Sink, read_upload_form

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])

# A copy of an item whose files the Trash's repair rule could not bring back is refused,
# not made from the baseline as if the item had never been read (docs/workspace.md § Copies).
STRANDED = "The item's files could not be brought back from the Trash; nothing was copied"


# --- names and places -------------------------------------------------------------------


async def lock_job(db: DbSession, job_id: uuid.UUID) -> None:
    """Order filing against the worker's finalization of the parse: the worker reads the
    rows that keep the original under this lock (docs/jobs.md § Retention)."""
    await db.execute(select(Job.id).where(Job.id == job_id).with_for_update())


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


async def live_text_document(db: DbSession, user_id: int, document_id: int) -> Document:
    """A live document that is a parse or a markdown document: what the routes of a
    document's own take — its content, result, revisions, pages, figures, images, retry
    and re-convert — since a file of the library has none of those (docs/workspace.md
    § Files); 404 for one, as for nothing."""
    row = await live_document(db, user_id, document_id)
    if row.kind == FILE_KIND:
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
                        settings, user_id, before, after, moved.displaced
                    )
                except Exception:  # noqa: BLE001 - the move revision stays revertible
                    logger.exception("could not move %s back after a failed commit", after)
        raise


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
    credits: dict[uuid.UUID, int | None] | None = None,
    storage: dict[int, int] | None = None,
    originals: Collection[int] | None = None,
) -> dict:
    """The row the file list draws: the entry, plus the live state of its parse and of
    its re-convert, the view it was last read in and whether its original is kept.

    Joined rather than fetched per row, so one `GET /api/library` renders every name,
    status and progress bar the page needs (docs/web.md § Files). A markdown document
    (`job` None) has no parse to report on: every parse field is null. `failed` holds
    what a retry of each failed parse would send (`failed_page_counts`); `checking` says
    a running parse is past its last page and being checked; `reconvert` is the parse
    the row points at as its re-convert (docs/api.md); `credits` what each parse cost
    (`ledger.credits_by_job`, docs/billing.md § Routes); `storage` each document's share
    of the storage (`StorageUsage.documents`), None where the workspace was not measured;
    `originals` the ids whose original upload is kept (`kept_originals`), None where it
    was not looked up — a parse's `original` is then null (docs/api.md).
    """
    failed = failed or {}
    credits = credits or {}
    storage_bytes = None if storage is None else storage.get(row.id, 0)
    if row.kind == FILE_KIND:
        # A file kept as it is (docs/workspace.md § Files): no parse to report on; its
        # size is its share of the storage, the one copy counted once.
        return {
            "id": row.id,
            "folder_id": row.folder_id,
            "name": row.name,
            "job_id": None,
            "status": None,
            "kind": FILE_KIND,
            "media_type": row.media_type,
            "model": None,
            "page_count": None,
            "pages_done": None,
            "size_bytes": row.size_bytes,
            "storage_bytes": row.size_bytes,
            "error": None,
            "created_at": row.created_at,
            "finished_at": None,
            "failed_pages": None,
            "checking": False,
            "reconvert": None,
            "credits": None,
            "view": row.view,
            "original": None,
        }
    if job is None:
        return {
            "id": row.id,
            "folder_id": row.folder_id,
            "name": row.name,
            "job_id": None,
            "status": None,
            "kind": MARKDOWN_KIND,
            "media_type": None,
            "model": None,
            "page_count": None,
            "pages_done": None,
            "size_bytes": None,
            "storage_bytes": storage_bytes,
            "error": None,
            "created_at": row.created_at,
            "finished_at": None,
            "failed_pages": None,
            "checking": False,
            "reconvert": None,
            "credits": None,
            "view": row.view,
            "original": None,
        }
    return {
        "id": row.id,
        "folder_id": row.folder_id,
        "name": row.name,
        "job_id": str(row.job_id),
        "status": job.status,
        "kind": job.kind,
        "media_type": None,
        "model": job.model,
        "page_count": job.page_count,
        "pages_done": job.pages_done,
        "size_bytes": job.size_bytes,
        "storage_bytes": storage_bytes,
        "error": job.error,
        "created_at": row.created_at,
        "finished_at": job.finished_at,
        "failed_pages": failed.get(job.id),
        "checking": is_checking(job),
        "credits": credits.get(job.id),
        "view": row.view,
        # Whether `…/original` answers: known from one listing on the library read, not
        # looked up by the one-row answers (docs/api.md).
        "original": (row.id in originals) if originals is not None else None,
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
                "credits": credits.get(reconvert.id),
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


async def read_document_payload(db: DbSession, settings: AppSettings, row: Document) -> dict:
    """One row's payload, its parse and its re-convert read for it, and whether its
    original is kept."""
    job = await job_of(db, row.job_id) if row.job_id is not None else None
    reconvert = await job_of(db, row.reconvert_job_id) if row.reconvert_job_id is not None else None
    jobs = [job, reconvert]
    originals: set[int] = set()
    if job is not None:
        root = user_root(settings.workspaces_dir, row.user_id)
        if await asyncio.to_thread(workspace_documents.original_of, root, row.id) is not None:
            originals.add(row.id)
    return document_payload(
        row,
        job,
        await failed_page_counts(db, jobs),
        reconvert,
        await ledger.credits_by_job(db, [one.id for one in jobs if one is not None]),
        originals=originals,
    )


# --- read ---------------------------------------------------------------------------------


async def read_folders(db: DbSession, user_id: int):
    query = select(Folder).where(Folder.user_id == user_id).order_by(Folder.name)
    return (await db.execute(query)).scalars().all()


@router.get("")
async def read_library(user: SessionUser, db: DbSession, settings: AppSettings):
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
    every = [job for _, job in documents] + list(reconverts.values())
    failed = await failed_page_counts(db, every)
    credits = await ledger.credits_by_job(db, [job.id for job in every if job is not None])

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

    # Each document's share of the storage, measured on the read (docs/web.md § Files).
    try:
        storage = (
            await asyncio.to_thread(
                workspace_documents.storage_usage,
                user_root(settings.workspaces_dir, user_id),
                Path(settings.figures_dir),
                {row.id: row.job_id for row, _job in documents},
                {
                    row.id
                    for row, _job in documents
                    if row.trashed_at is not None or row.folder_id in trashed
                },
                whole=False,
            )
        ).documents
    except OSError:
        # A directory nested too deep to measure: the sizes are unknown, not the library.
        storage = None
    # Which parses can be read on their original (docs/api.md).
    originals = await asyncio.to_thread(
        workspace_documents.kept_originals, user_root(settings.workspaces_dir, user_id)
    )

    def origin(parent_id: int | None) -> list[str]:
        place = nearest_live(by_id, parent_id)
        return [row.name for row in workspace_documents.folder_chain(by_id, place)]

    def recent(row) -> float:
        return -row.trashed_at.timestamp()

    return {
        "folders": [folder_payload(row) for row in folders if row.id not in trashed],
        "documents": [
            document_payload(
                row,
                job,
                failed,
                reconverts.get(row.reconvert_job_id),
                credits,
                storage,
                originals,
            )
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
                    **document_payload(
                        row,
                        job,
                        failed,
                        reconverts.get(row.reconvert_job_id),
                        credits,
                        storage,
                        originals,
                    ),
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
        row = await make_folder(db, settings, user_id, parent_id, body.name)
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
        readable, row = await read_row(db, *gone)
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
            settings, user_id, entry, relative, f"put back {relative}"
        )
    except Exception:  # noqa: BLE001 - the rows stand; the repair rule brings the files
        logger.exception("the files of %s stay in the Trash for now", relative)
    return answer


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
    return {**(await read_document_payload(db, settings, row)), "trashed_at": row.trashed_at}


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
    return await read_document_payload(db, settings, row)


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
    """Empty Trash: every item in it, for good — rows first, then their files. The same
    function the worker's sweep runs, with a cutoff, for the Trash's expiry
    (docs/workspace.md § The Trash)."""
    await trash.empty_trash(db, settings, user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- documents ----------------------------------------------------------------------------


class DocumentUpdate(BaseModel):
    """Partial: only the fields present in the body change (docs/api.md)."""

    name: str | None = Field(default=None, min_length=1, max_length=DOCUMENT_NAME_MAX)
    folder_id: int | None = None


class DocumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=DOCUMENT_NAME_MAX)
    folder_id: int | None = None


def form_folder_id(fields: Mapping[str, str]) -> int | None:
    """`folder_id` as a multipart field: absent or empty means the root."""
    raw = fields.get("folder_id")
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", "'folder_id' must be a folder id") from exc


async def parse_auto_convert(db: DbSession, user_id: int) -> bool:
    """The parse agent's switch (docs/agents.md § Per-agent settings): a PDF or an image
    uploaded on the Files page is kept as it is unless the reader turned converting on
    arrival on."""
    value = await db.scalar(
        select(AgentSetting.auto_convert).where(
            AgentSetting.user_id == user_id, AgentSetting.agent == PARSE.name
        )
    )
    return bool(value)


def parse_accepts(filename: str, media_type: str) -> bool:
    """Whether intake would parse this upload: a PDF or an image (`resolve_kind`)."""
    try:
        resolve_kind(filename, media_type)
    except ApiError:
        return False
    return True


# What an upload is filed as, decided from its name and type before its bytes land
# (docs/api.md § `POST /api/library/documents`).
UPLOAD_MARKDOWN, UPLOAD_PARSE, UPLOAD_FILE = "markdown", "parse", "file"


@router.post("/documents", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request, user: SessionUser, db: DbSession, settings: AppSettings
):
    """The web upload: multipart `file` (+ optional `folder_id`) → a parse and a document,
    a markdown document, or a file kept as it is (docs/api.md § `POST /api/library/documents`).

    The body is read as it arrives (`routes.multipart`): what the file part is decides
    where its bytes stream to before the first of them lands — a PDF or an image the parse
    agent converts, and a markdown file, to the upload directory under their own caps, as
    for `POST /v1/parse`; anything else straight into the user's tree under
    `FILE_MAX_BYTES`, the one copy there will be (docs/workspace.md § Files).
    """
    # A primitive copy: a retry inside `insert_named` follows a rollback, which expires
    # the row behind `user`, and an expired attribute cannot be loaded here.
    user_id = user.id
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise ApiError(400, "invalid_request", "The upload must be multipart/form-data")
    # Cheap rejection before a byte is read: the largest body any kind may send — the
    # largest of the three caps, which a deployment may order as it likes; each kind's own
    # cap applies as its bytes arrive, and the capped stream below is what stops a request
    # that declares no length, or an untrue one.
    outer_cap, outer_name = max(
        (settings.file_max_bytes, "FILE_MAX_BYTES"),
        (settings.upload_max_bytes, "UPLOAD_MAX_BYTES"),
        (settings.content_max_bytes, "CONTENT_MAX_BYTES"),
    )
    declared = request.headers.get("content-length", "")
    declared_length = int(declared) if declared.isdigit() else None
    if declared_length is not None and declared_length > outer_cap + MULTIPART_ENVELOPE_BYTES:
        raise ApiError(413, "invalid_request", f"The upload exceeds {outer_name}")

    # Decided before the body: the parse agent's switch, and what the plan leaves for a
    # file — checked against the declared length before a byte of one is written
    # (docs/workspace.md § Files, § Quota).
    auto_convert = await parse_auto_convert(db, user_id)
    root = user_root(settings.workspaces_dir, user_id)
    # What an upload the process died in left in the tree goes before the quota is
    # measured, so it never counts against this one (docs/workspace.md § Files).
    await asyncio.to_thread(workspace_documents.sweep_staged_uploads, root)
    limit = ledger.storage_limit(settings, await ledger.plan_of(db, settings, user_id))
    used = (await workspace_documents.measure_storage(db, settings, user_id, root)).total
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(git.ensure_repo, root)

    def staged(suffix: str, max_bytes: int, cap_name: str, kind: str) -> Sink:
        path = upload_dir / f"{uuid.uuid4().hex}{suffix}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        return Sink(fd, path, max_bytes, cap_name, kind, lambda: path.unlink(missing_ok=True))

    def open_sink(filename: str, content_type: str) -> Sink:
        suffix = Path(filename).suffix.lower()[:16]
        if (
            content_type.split(";", 1)[0].strip() == workspace_documents.MARKDOWN_MEDIA_TYPE
            or suffix in workspace_documents.MARKDOWN_EXTENSIONS
        ):
            return staged(".md", settings.content_max_bytes, "CONTENT_MAX_BYTES", UPLOAD_MARKDOWN)
        if auto_convert and parse_accepts(filename, content_type):
            return staged(suffix, settings.upload_max_bytes, "UPLOAD_MAX_BYTES", UPLOAD_PARSE)
        if declared_length is not None and used + declared_length > limit:
            raise ApiError(409, "storage_full", STORAGE_FULL)
        # Into the tree under the staging name, which history excludes and reconciliation
        # leaves alone, its lock held through this descriptor until the file is in place
        # or removed (docs/workspace.md § Files); moved into place once the row exists.
        fd, temp = workspace_documents.stage_upload(root)

        def discard() -> None:
            with contextlib.suppress(OSError, safe_io.UnsafeEntry):
                safe_io.remove_file(root, temp)

        return Sink(fd, temp, settings.file_max_bytes, "FILE_MAX_BYTES", UPLOAD_FILE, discard)

    fields, part = await read_upload_form(
        request.headers,
        capped_stream(request.stream(), outer_cap + MULTIPART_ENVELOPE_BYTES, cap_name=outer_name),
        open_sink,
    )
    if part is None:
        raise ApiError(400, "invalid_request", "Multipart requests need a 'file' part")
    try:
        folder_id = form_folder_id(fields)
        if folder_id is not None:
            await live_folder(db, user_id, folder_id)
        filename = part.filename or "upload"
        if part.sink.kind == UPLOAD_MARKDOWN:
            return await file_markdown(
                part.sink.path,
                filename,
                user_id=user_id,
                folder_id=folder_id,
                db=db,
                settings=settings,
            )
        if part.sink.kind == UPLOAD_PARSE:
            return await file_parse(
                part.sink.path,
                filename,
                part.content_type,
                user=user,
                folder_id=folder_id,
                db=db,
                settings=settings,
            )
        return await file_upload(
            part.sink.path,
            filename,
            part.content_type,
            part.size,
            user_id=user_id,
            folder_id=folder_id,
            db=db,
            settings=settings,
        )
    finally:
        # Whatever was not moved into place goes: a staged upload the parse copied, the
        # temporary file of a filing that was refused. The descriptor last: the lock a
        # file's staging holds through it outlives the move into place (docs/workspace.md
        # § Files).
        await asyncio.to_thread(part.sink.discard)
        with contextlib.suppress(OSError):
            os.close(part.sink.fd)


async def file_parse(
    path: Path,
    filename: str,
    media_type: str,
    *,
    user: User,
    folder_id: int | None,
    db: DbSession,
    settings: AppSettings,
) -> dict:
    """A PDF or an image the parse agent converts on arrival: the parse queued from the
    staged upload by the same `jobs.intake` sequence `POST /v1/parse` runs, then the row
    naming it (docs/api.md § Why the web upload is a control-plane route)."""
    user_id = user.id
    # The library name is decided before the parse is queued: a name that cleans to
    # nothing is filed as `upload`, never a 400 after a billed parse is already on its
    # way (docs/api.md).
    try:
        wanted = clean_name(filename, DOCUMENT_NAME_MAX)
    except ApiError:
        wanted = "upload"
    submission = await submit_parse(
        db,
        settings,
        user=user,
        chunks=file_chunks(path),
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
            build=lambda name: Document(
                user_id=user_id, folder_id=place, job_id=job_id, kind=PARSE_KIND, name=name
            ),
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
        # A cache hit that only a file kept can have been purged since the lookup, that
        # file deleted for good meanwhile (docs/jobs.md § Dedup margin); under this lock
        # the purge waits. Said plainly, not as the name conflict the insert would make of
        # a missing job.
        if (
            submission.cached is not None
            and await db.scalar(select(Job.id).where(Job.id == job_id)) is None
        ):
            if submission.upload is not None:
                submission.upload.unlink(missing_ok=True)
            raise ApiError(
                409,
                "invalid_request",
                "The earlier parse of this file was just deleted; upload it again",
            )
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
    return await read_document_payload(db, settings, row)


async def file_markdown(
    staged: Path,
    filename: str,
    *,
    user_id: int,
    folder_id: int | None,
    db: DbSession,
    settings: AppSettings,
) -> dict:
    """A `.md` upload: no parse, a markdown document whose original is the file
    (docs/workspace.md § Markdown documents). Staged under the cap a `doc.md` is read
    under and checked before any row exists; the caller removes what is left of the
    staged file."""
    try:
        wanted = clean_name(filename, DOCUMENT_NAME_MAX)
    except ApiError:
        wanted = "upload"
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
                    user_id=user_id, folder_id=folder_id, job_id=None, kind=MARKDOWN_KIND, name=name
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
            await workspace_documents.adopt_markdown(db, settings, row, staged)
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
            undo=lambda: workspace_documents.discard_adopted(settings, user_id, row_id, relative),
        )
    return document_payload(row, None)


async def file_upload(
    temp: Path,
    filename: str,
    declared_type: str,
    size: int,
    *,
    user_id: int,
    folder_id: int | None,
    db: DbSession,
    settings: AppSettings,
) -> dict:
    """A file of any other kind, kept as it is (docs/workspace.md § Files): its bytes are in
    the tree already, under a write-in-progress name; the row and the move into place
    under one hold of the workspace lock, the row committed last, as a markdown filing
    is — no entry without its file, no file without its entry — through the filing the
    agent's download shares (`workspace/filing.py`)."""
    root = user_root(settings.workspaces_dir, user_id)
    async with backup_shared(root), workspace_lock(root):
        row = await keep_file(
            db,
            settings,
            user_id=user_id,
            folder_id=folder_id,
            temp=temp,
            filename=filename,
            declared_type=declared_type,
            size=size,
        )
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
                user_id=user_id, folder_id=folder_id, job_id=None, kind=MARKDOWN_KIND, name=name
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
    return await read_document_payload(db, settings, row)


@router.delete(
    "/documents/{document_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Delete Immediately: a live file, or one in the Trash, for good. The parse itself
    is not deleted with it: it falls under the parse retention (docs/jobs.md
    § Retention)."""
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
    row = await live_text_document(db, user.id, document_id)
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
    row = await live_text_document(db, user.id, document_id)
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
    row = await live_text_document(db, user.id, document_id)
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
    row = await live_text_document(db, user.id, document_id)
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
    if model is not None:
        # One of the parse agent's two, as at enqueue (docs/agents.md § Enqueue rules).
        model = checked_parse_model(settings, model)
    root = user_root(settings.workspaces_dir, user.id)
    async with workspace_lock(root):
        # The user's row before the job's, the one order (docs/workspace.md § Revisions):
        # the retry takes the job's credits back under it (docs/billing.md § A parse).
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        row = await live_text_document(db, user.id, document_id)
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
        try:
            # The credits the failure released, reserved again for this run: the
            # document is charged once, by the run that delivers it (docs/billing.md
            # § A parse). Taken before the copy, so a refusal costs no copy either.
            await ledger.reserve_again(db, settings, job.id)
        except ledger.InsufficientCredits as exc:
            raise insufficient_credits(exc) from exc
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
            readable, landed = await read_row(db, Job, job_id)
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
        row = await live_text_document(db, user.id, document_id)
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
    """Dismiss a failed re-convert: the document stops pointing at it, and the parse, of
    the document's own original, is kept with it by the parse retention (docs/jobs.md
    § Re-converting a document, § Retention). Under the workspace lock, which a retry of
    it holds too."""
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        row = await live_text_document(db, user.id, document_id)
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
        row = await live_text_document(db, user.id, int(document_id))
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
    row = await live_text_document(db, user.id, document_id)
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


# --- files kept as they are (docs/workspace.md § Files) -------------------------------------


def content_disposition(disposition: str, name: str) -> str:
    """`inline` or `attachment` with the file's name: in ASCII for every client, and whole
    in UTF-8 (RFC 6266) for the ones that read it."""
    fallback = re.sub(r"[^\x20-\x7e]|[\"\\\\]", "_", name).strip() or "file"
    encoded = urllib.parse.quote(name, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """One `bytes=` range of a file of `size` bytes as (first, last), or None for a header
    that is not one such range — answered whole, as the specification allows; one past
    the end is unsatisfiable (416)."""
    match = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header)
    if match is None or not (match[1] or match[2]):
        return None
    past = ApiError(
        416,
        "invalid_request",
        "The range is past the end of the file",
        headers={"Content-Range": f"bytes */{size}"},
    )
    if match[1]:
        first = int(match[1])
        last = min(int(match[2]), size - 1) if match[2] else size - 1
        if first >= size or first > last:
            raise past
        return first, last
    suffix = int(match[2])
    if suffix == 0 or size == 0:
        raise past
    return max(0, size - suffix), size - 1


def file_security_headers(media_type: str, inline: bool) -> dict[str, str]:
    """The file route's own policy (docs/api.md): sandboxed, so nothing served from the
    tree runs as this origin — except a PDF shown inline, which a browser's viewer will
    not open in a sandbox: that one may be framed by this origin alone."""
    if inline and media_type == "application/pdf":
        return {
            "Content-Security-Policy": "frame-ancestors 'self'",
            "X-Frame-Options": "SAMEORIGIN",
        }
    return {"Content-Security-Policy": "sandbox", "X-Frame-Options": "DENY"}


def open_file_chunks(entry: int, first: int, last: int):
    """The bytes `first`..`last` of the open file `entry`, in chunks; the descriptor is
    closed when the stream ends, however it ends."""
    try:
        os.lseek(entry, first, os.SEEK_SET)
        remaining = last - first + 1
        while remaining > 0:
            chunk = os.read(entry, min(safe_io.READ_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        os.close(entry)


def file_response(
    entry: int, request: Request, *, name: str, media_type: str, inline: bool
) -> StreamingResponse:
    """The open file `entry` as `…/file` and `…/original` answer it (docs/api.md): inline
    or as an attachment, one `Range` answered with 206, a security policy of its own,
    never cached. The descriptor is closed when the stream ends — or here, when nothing
    streams."""
    try:
        size = os.fstat(entry).st_size
        first, last = 0, size - 1
        status_code = status.HTTP_200_OK
        wanted = parse_range(request.headers.get("range", ""), size)
        if wanted is not None:
            first, last = wanted
            status_code = status.HTTP_206_PARTIAL_CONTENT
    except BaseException:
        os.close(entry)
        raise
    headers = {
        "Content-Disposition": content_disposition("inline" if inline else "attachment", name),
        "Content-Length": str(max(last - first + 1, 0)),
        "Accept-Ranges": "bytes",
        # Never cached, as a crop is not: a cached file would outlive a sign-out.
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        **file_security_headers(media_type, inline),
    }
    if status_code == status.HTTP_206_PARTIAL_CONTENT:
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    return StreamingResponse(
        iterate_in_threadpool(open_file_chunks(entry, first, last)),
        status_code=status_code,
        media_type=media_type,
        headers=headers,
    )


@router.get("/documents/{document_id}/file")
async def document_file(
    document_id: int,
    request: Request,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
    download: bool = Query(default=False),
):
    """A file's bytes, the one copy, read from the tree (docs/api.md): inline for what a
    browser shows safely on its own, an attachment otherwise or on `?download=1`; one
    `Range` answered with 206; always under a content security policy of its own, which
    the Caddyfile leaves in place (docs/deployment.md § Security headers). Read without
    the workspace lock — a download of a video must not hold the shell for its length —
    through a descriptor opened along the path's components without following anything;
    what it then streams is whatever the file holds while it is read."""
    row = await live_document(db, user.id, document_id)
    if row.kind != FILE_KIND:
        raise ApiError(404, "invalid_request", "No such file")
    root = user_root(settings.workspaces_dir, row.user_id)
    relative = await document_relative(db, row)
    try:
        entry = await asyncio.to_thread(safe_io.open_for_read, root, root / relative, sys.maxsize)
    except (safe_io.UnsafeEntry, OSError) as exc:
        raise ApiError(409, "invalid_request", workspace_documents.NOT_PLAIN) from exc
    if entry is None:
        raise ApiError(404, "invalid_request", "The file is missing from the workspace")
    media_type = row.media_type or workspace_documents.file_media_type(row.name)
    inline = workspace_documents.served_inline(media_type) and not download
    return file_response(entry, request, name=row.name, media_type=media_type, inline=inline)


@router.get("/documents/{document_id}/original")
async def document_original(
    document_id: int,
    request: Request,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """A parsed document's original upload, kept beside the git directory, served as a
    file is — inline, one `Range` answered, the same security headers, never cached — for
    the result viewer's Original view (docs/api.md). 404 for a markdown document, for a
    file, whose bytes are `…/file`, and for a parse whose original was not kept."""
    row = await live_document(db, user.id, document_id)
    if row.job_id is None:
        raise ApiError(404, "invalid_request", "No such original")
    job = await job_of(db, row.job_id)
    root = user_root(settings.workspaces_dir, row.user_id)
    source = await asyncio.to_thread(workspace_documents.original_of, root, row.id)
    if source is None:
        raise ApiError(404, "invalid_request", "This document kept no original")
    try:
        entry = await asyncio.to_thread(os.open, source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ApiError(404, "invalid_request", "This document kept no original") from exc
    return file_response(
        entry,
        request,
        name=row.name,
        media_type=job.media_type,
        inline=workspace_documents.served_inline(job.media_type),
    )


def checked_view(value: str | None) -> str | None:
    """A view's name — `original` or `markdown` — or None for none; 400 otherwise."""
    if value is None:
        return None
    if value not in DOCUMENT_VIEWS:
        raise ApiError(400, "invalid_request", "view is original or markdown")
    return value


class DocumentView(BaseModel):
    view: str = Field(max_length=16)


@router.put(
    "/documents/{document_id}/view",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def put_document_view(
    document_id: int, body: DocumentView, user: SessionUser, db: DbSession
) -> Response:
    """The view the reader just switched a PDF or an image to, remembered on the row so
    the document opens on it next time (docs/web.md § Result viewer, *Original and
    Markdown*). Without the workspace lock: the row changes, nothing in the tree does. A
    document with one view takes none (docs/api.md)."""
    view = checked_view(body.view)
    row = await live_document(db, user.id, document_id)
    if row.kind == MARKDOWN_KIND:
        raise ApiError(400, "invalid_request", "A markdown document has one view")
    if row.kind == FILE_KIND:
        try:
            resolve_kind(row.name, row.media_type or "")
        except ApiError as exc:
            raise ApiError(400, "invalid_request", "This file has one view") from exc
        if view != ORIGINAL_VIEW:
            raise ApiError(400, "invalid_request", "Convert the file to read it as Markdown")
    row.view = view
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class DocumentConvert(BaseModel):
    model: str | None = Field(default=None, max_length=255)
    # The view the document opens on once converted: the result viewer names `markdown`
    # when the reader asked for it there (docs/api.md).
    view: str | None = Field(default=None, max_length=16)


@router.post("/documents/{document_id}/convert", dependencies=[CsrfGuard])
async def convert_document(
    document_id: int,
    body: DocumentConvert,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """A PDF or an image kept as a file becomes a document (docs/jobs.md § Converting a
    file). The parse is queued from the file as a Files upload's is — the credits
    reserved, the per-user cap and the dedup cache applied, the file read in place and
    copied into the upload directory as the job's source — then, under the same hold of
    the workspace lock, the file moves aside out of the tree and the row becomes a parse
    naming the job, its id, name and place kept. A row that does not commit brings the
    file back; the worker files the source as the document's original when the parse
    ends (§ Retention)."""
    model = (body.model or "").strip() or None
    view = checked_view(body.view)
    user_id = user.id
    root = user_root(settings.workspaces_dir, user_id)
    async with backup_shared(root), workspace_lock(root):
        await workspace_documents.recover_trash(db, settings, user_id)
        row = await live_document(db, user_id, document_id)
        if row.kind != FILE_KIND:
            raise ApiError(404, "invalid_request", "No such file")
        row = await convert_kept_file(db, settings, user=user, row=row, model=model, view=view)
    return await read_document_payload(db, settings, row)


# --- copies -------------------------------------------------------------------------------


class DocumentCopy(BaseModel):
    folder_id: int | None = None


class FolderCopy(BaseModel):
    parent_id: int | None = None


async def keep_parses(db: DbSession, jobs: list[Job]) -> None:
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
        file=source.kind == FILE_KIND,
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
        readable, row = await read_row(db, *landed)
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
    return await read_document_payload(db, settings, landed or row)


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
        )
    return folder_payload(landed or top)


# --- the workspace: where a row's directory is ----------------------------------------------


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
    row = await live_text_document(db, user.id, document_id)
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
    row = await live_text_document(db, user.id, document_id)
    revision = await workspace_documents.write_content(
        db, settings, row, markdown=body.markdown, base_revision=body.base_revision
    )
    return {"revision": revision}


@router.put("/documents/{document_id}/notes", dependencies=[CsrfGuard])
async def put_document_notes(
    document_id: int, body: NotesPut, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_text_document(db, user.id, document_id)
    revision = await workspace_documents.write_notes(
        db, settings, row, text=body.text, base_revision=body.base_revision
    )
    return {"revision": revision}


@router.get("/documents/{document_id}/revisions")
async def document_revisions(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await live_text_document(db, user.id, document_id)
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
    row = await live_text_document(db, user.id, document_id)
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
