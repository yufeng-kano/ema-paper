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
import sys
import urllib.parse
import uuid
from collections.abc import AsyncIterator, Collection, Mapping
from pathlib import Path

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
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
    UploadTicket,
    User,
)
from ..errors import ApiError
from ..jobs.chat_tools import PageUnavailable, original_page_png
from ..jobs.intake import (
    MULTIPART_ENVELOPE_BYTES,
    Submission,
    capped_stream,
    checked_parse_model,
    convert_kept_file,
    file_chunks,
    resolve_kind,
    retry_library_parse,
    store_upload,
    submit_parse,
    upload_chunks,
)
from ..jobs.queue import parse_pages_spec
from ..jobs.runner import partial_result_payload, result_payload
from ..parsing.figures import crop_chunks, open_crop, parse_bbox_path
from ..workspace import documents as workspace_documents
from ..workspace import filing, git, safe_io, trash
from ..workspace.agents_file import AGENTS_FILE
from ..workspace.documents import (
    live_document,
    live_folder,
    markdown_name,
    nearest_live,
    own_subtree,
    owned_document,
)
from ..workspace.filing import (
    DOCUMENT_NAME_MAX,
    FOLDER_NAME_MAX,
    STORAGE_FULL,
    clean_name,
    commit_filing,
    document_relative,
    folder_exists,
    folder_relative_dir,
    insert_named,
    job_of,
    keep_file,
    lock_job,
    make_folder,
    placed_in,
    sibling_names,
)
from ..workspace.layout import (
    document_relative_dir,
    trash_entry,
    user_root,
)
from ..workspace.locks import backup_shared, workspace_lock
from ..workspace.trash import commit_then_discard, lock_user, read_row, refuse_while_answering
from .multipart import Sink, read_upload_form

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])

# --- names and places -------------------------------------------------------------------


async def live_text_document(db: DbSession, user_id: int, document_id: int) -> Document:
    """A live document that is a parse or a markdown document: what the routes of a
    document's own take — its content, result, revisions, pages, figures, images, retry
    and re-convert — since a file of the library has none of those (docs/workspace.md
    § Files); 404 for one, as for nothing."""
    row = await live_document(db, user_id, document_id)
    if row.kind == FILE_KIND:
        raise ApiError(404, "invalid_request", "No such document")
    return row


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
    `originals` the ids whose `…/original` answers (`answering_original`), None where it
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
        # Whether `…/original` answers: known from one listing on the library read
        # (docs/api.md).
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


async def read_document_payload(db: DbSession, settings: AppSettings, row: Document) -> dict:
    """One row's payload, its parse and its re-convert read for it, and whether its
    original is kept."""
    job = await job_of(db, row.job_id) if row.job_id is not None else None
    reconvert = await job_of(db, row.reconvert_job_id) if row.reconvert_job_id is not None else None
    jobs = [job, reconvert]
    originals: set[int] = set()
    if job is not None:
        root = user_root(settings.workspaces_dir, row.user_id)
        if await workspace_documents.answering_original(db, root, row.id, job) is not None:
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
    # Which parses can be read on their original (docs/api.md): the kept ones, and, until a
    # finished parse's original is filed, the row its upload is filed for, read from the
    # upload meanwhile (`answering_original`) — looked at before the listing, so an
    # original filed in between is in the listing.
    unfiled = await asyncio.to_thread(
        lambda: {
            job.id
            for _row, job in documents
            if job is not None and workspace_documents.unfiled_upload(job) is not None
        }
    )
    originals = await asyncio.to_thread(
        workspace_documents.kept_originals, user_root(settings.workspaces_dir, user_id)
    )
    for job_id in unfiled:
        heir = workspace_documents.upload_heir(
            (row.id for row, job in documents if job is not None and job.id == job_id),
            originals,
        )
        if heir is not None:
            originals.add(heir)

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
    # The row and its directory move together, under the user's workspace lock, so no
    # content read can materialize at the new path in between (docs/workspace.md).
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        row = await filing.move_folder(
            db,
            settings,
            user.id,
            folder_id,
            name=body.name if renaming else None,
            parent_id=body.parent_id,
            move="parent_id" in provided,
            author=git.USER_AUTHOR,
        )
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


@router.post("/documents/{document_id}/trash", dependencies=[CsrfGuard])
async def trash_document(document_id: int, user: SessionUser, db: DbSession, settings: AppSettings):
    """Move to Trash: the row marked, its directory out of `files/` into the Trash, as one
    revision (docs/workspace.md § The Trash)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        row = await filing.trash_document(
            db, settings, user_id, document_id, author=git.USER_AUTHOR
        )
    return {**(await read_document_payload(db, settings, row)), "trashed_at": row.trashed_at}


@router.post("/folders/{folder_id}/trash", dependencies=[CsrfGuard])
async def trash_folder(folder_id: int, user: SessionUser, db: DbSession, settings: AppSettings):
    """Move to Trash, a folder with everything in it (docs/workspace.md § The Trash)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        row = await filing.trash_folder(db, settings, user_id, folder_id, author=git.USER_AUTHOR)
    return {**folder_payload(row), "trashed_at": row.trashed_at}


@router.post("/documents/{document_id}/restore", dependencies=[CsrfGuard])
async def restore_document(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    """Put Back: to the folder it was trashed from, or the nearest live one above it, a
    taken name suffixed as a move's is (docs/api.md)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        row = await filing.put_back_document(
            db, settings, user_id, document_id, author=git.USER_AUTHOR
        )
    return await read_document_payload(db, settings, row)


@router.post("/folders/{folder_id}/restore", dependencies=[CsrfGuard])
async def restore_folder(folder_id: int, user: SessionUser, db: DbSession, settings: AppSettings):
    """Put Back a folder, with everything that went to the Trash with it (docs/api.md)."""
    user_id = user.id
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        row = await filing.put_back_folder(db, settings, user_id, folder_id, author=git.USER_AUTHOR)
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
        # An AGENTS.md is the reader's instructions to the chat agent for the folder, kept
        # as a file where the agent reads it, never a document of its own (docs/chat.md
        # § AGENTS.md, docs/workspace.md § Markdown documents).
        if Path(filename).name != AGENTS_FILE and (
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
            _, row = await file_parse(
                file_chunks(part.sink.path),
                filename,
                part.content_type,
                user=user,
                folder_id=folder_id,
                db=db,
                settings=settings,
            )
            return await read_document_payload(db, settings, row)
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
    chunks: AsyncIterator[bytes],
    filename: str,
    media_type: str,
    *,
    user: User,
    folder_id: int | None,
    db: DbSession,
    settings: AppSettings,
    model: str | None = None,
    force: bool = False,
    ticket: UploadTicket | None = None,
    reuse_filed: bool = False,
) -> tuple[Submission, Document]:
    """A PDF or an image the parse agent converts on arrival: the parse queued from the
    upload by the same `jobs.intake` sequence `POST /v1/parse` runs, then the row naming
    it (docs/api.md § Why the web upload is a control-plane route) — the Files page's
    upload, and a data-plane parse filed into Files with its `model`, `force` and upload
    ticket (docs/mcp.md § Filing into Files).

    With `reuse_filed`, a live document of the same parse already in the folder answers
    instead of a new row, its fresh upload discarded: a command the connector runs again
    files no twin. An upload ticket the intake spent goes back when the filing is refused
    after it, so the connector's read after the upload fails rather than land a parse
    nothing filed (docs/mcp.md § Filing into Files). Answers the submission and the
    document."""
    user_id = user.id
    ticket_id = None if ticket is None else ticket.id
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
        chunks=chunks,
        media_type=media_type,
        filename=filename,
        model=model,
        force=force,
        ticket=ticket,
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

    try:
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
            if reuse_filed:
                filed = await db.scalar(
                    select(Document)
                    .where(
                        Document.user_id == user_id,
                        placed_in(Document.folder_id, folder_id),
                        Document.job_id == job_id,
                        Document.trashed_at.is_(None),
                    )
                    .order_by(Document.id)
                    .limit(1)
                )
                if filed is not None:
                    # The same parse is here already: that document is the answer, and the
                    # fresh copy kept for a new row has no row to be the original of.
                    if submission.upload is not None:
                        submission.upload.unlink(missing_ok=True)
                    return submission, filed
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
                undo=lambda: workspace_documents.discard_adopted(
                    settings, user_id, row_id, relative
                ),
            )
        return submission, row
    except Exception:
        # Nothing was filed: the ticket the intake spent on the job goes back, so the
        # connector's read after the upload fails rather than land a parse nothing
        # filed (docs/mcp.md § Filing into Files). One spent on another job since is
        # another upload's.
        if ticket_id is not None:
            try:
                await db.rollback()
                await db.execute(
                    update(UploadTicket)
                    .where(UploadTicket.id == ticket_id, UploadTicket.job_id == job_id)
                    .values(spent_at=None, job_id=None)
                )
                await db.commit()
            except Exception:  # noqa: BLE001 - the filing's own error is the answer
                logger.warning("upload ticket %d could not be given back", ticket_id)
        raise


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
    wanted = markdown_name(wanted, DOCUMENT_NAME_MAX)
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
async def create_document(
    body: DocumentCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    """A new, empty markdown document: a row with no job and no original — its baseline is
    empty (docs/workspace.md § Markdown documents). Its directory is made the first time it
    is read, like any document's. A taken name is suffixed, as a new folder's is."""
    row = await filing.new_document(db, settings, user.id, body.folder_id, body.name)
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
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        row = await filing.move_document(
            db,
            settings,
            user.id,
            document_id,
            name=body.name if renaming else None,
            folder_id=body.folder_id,
            move="folder_id" in provided,
            author=git.USER_AUTHOR,
        )
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
    document's own parse stands (§ Re-converting a document) — under the workspace lock,
    through the function the chat agent's `convert_file` retries with
    (`retry_library_parse`)."""
    model = (body.model or "").strip() or None
    if model is not None:
        # One of the parse agent's two, as at enqueue (docs/agents.md § Enqueue rules).
        model = checked_parse_model(settings, model)
    root = user_root(settings.workspaces_dir, user.id)
    async with workspace_lock(root):
        row = await live_text_document(db, user.id, document_id)
        job_id = await retry_library_parse(db, settings, user_id=user.id, row=row, model=model)
    return {"job_id": str(job_id), "status": "queued"}


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
            # Filed by the worker just after the parse ended, under the lock held here, or
            # by the sweep when that failed (docs/jobs.md § Retention); or never kept.
            raise ApiError(
                409,
                "invalid_request",
                "This document's original is still being filed; re-convert it in a moment"
                if workspace_documents.unfiled_upload(job) is not None
                else "This document kept no original to parse again; upload the file again",
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
                workspace_documents.prepare_inserted_image,
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
    download: bool = Query(default=False),
):
    """A parsed document's original upload, kept beside the git directory, served as a
    file is — inline, one `Range` answered, the same security headers, never cached — for
    the result viewer's Original view, and as an attachment on `?download=1`, for a failed
    parse's *Download original* (docs/api.md). Until a finished parse's original is filed,
    its upload answers in its place. 404 for a markdown document, for a file, whose bytes
    are `…/file`, and for a parse whose original was not kept."""
    row = await live_document(db, user.id, document_id)
    if row.job_id is None:
        raise ApiError(404, "invalid_request", "No such original")
    job = await job_of(db, row.job_id)
    root = user_root(settings.workspaces_dir, row.user_id)
    source = await workspace_documents.answering_original(db, root, row.id, job)
    entry = (
        None
        if source is None
        else await asyncio.to_thread(workspace_documents.open_original, root, row.id, source)
    )
    if entry is None:
        raise ApiError(404, "invalid_request", "This document kept no original")
    return file_response(
        entry,
        request,
        name=row.name,
        media_type=job.media_type,
        inline=workspace_documents.served_inline(job.media_type) and not download,
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


@router.post(
    "/documents/{document_id}/copy", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED
)
async def copy_document(
    document_id: int, body: DocumentCopy, user: SessionUser, db: DbSession, settings: AppSettings
):
    """A copy of a document — its text, notes, original and images as they are now — into
    a folder, or beside it as a duplicate (docs/workspace.md § Copies)."""
    user_id = user.id
    root = user_root(settings.workspaces_dir, user_id)
    # The rows, the files and the commit under one hold of the workspace lock, the backup
    # lock shared around it, as a filing: no row without its original, in the database
    # or in a snapshot (docs/workspace.md § Backups).
    async with backup_shared(root), workspace_lock(root):
        row = await filing.copy_document(
            db, settings, user_id, document_id, body.folder_id, author=git.USER_AUTHOR
        )
    return await read_document_payload(db, settings, row)


@router.post(
    "/folders/{folder_id}/copy", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED
)
async def copy_folder(
    folder_id: int, body: FolderCopy, user: SessionUser, db: DbSession, settings: AppSettings
):
    """A copy of a folder with every live folder and document under it, in the same shape
    (docs/workspace.md § Copies)."""
    user_id = user.id
    root = user_root(settings.workspaces_dir, user_id)
    async with backup_shared(root), workspace_lock(root):
        row = await filing.copy_folder(
            db, settings, user_id, folder_id, body.parent_id, author=git.USER_AUTHOR
        )
    return folder_payload(row)


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
