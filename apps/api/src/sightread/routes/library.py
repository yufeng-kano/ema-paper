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
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, delete, select
from sqlalchemy.exc import IntegrityError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

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
)
from ..errors import ApiError
from ..jobs.intake import (
    MULTIPART_ENVELOPE_BYTES,
    capped_stream,
    submit_parse,
    upload_chunks,
)
from ..jobs.runner import partial_result_payload, result_payload
from ..parsing.figures import crop_path, parse_bbox_path
from ..workspace import documents as workspace_documents
from ..workspace.layout import document_relative_dir, path_segment, user_root
from ..workspace.locks import backup_shared, workspace_lock

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["library"])

FOLDER_NAME_MAX = 255
DOCUMENT_NAME_MAX = 512
# How many " (2)" attempts a colliding name gets before the folder is simply too crowded.
NAME_ATTEMPTS = 500
# An insert can lose a name race with another tab; the retry re-reads the folder's names.
INSERT_ATTEMPTS = 3

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
        # already makes impossible; keeping the tail is still the safe answer.
        candidate = f"{stem[:room]}{suffix}" if room > 0 else suffix[-limit:]
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
    (docs/workspace.md § Layout); the caller holds the user's row so the set is current."""
    folders = select(Folder.name).where(
        Folder.user_id == user_id, placed_in(Folder.parent_id, place)
    )
    if exclude_folder is not None:
        folders = folders.where(Folder.id != exclude_folder)
    documents = select(Document.name).where(
        Document.user_id == user_id, placed_in(Document.folder_id, place)
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
    build: Callable[[str], RowT],
    taken: Callable[[], Awaitable[set[str]]],
    wanted: str,
    limit: int,
    message: str,
) -> RowT:
    """Insert a row whose name has to be free where it lands, re-reading if it was taken.

    Two tabs can both read the same free name before either commits. The loser is not a
    conflict to report: a collision on create is resolved rather than refused
    (docs/web.md § Files), so it re-reads the folder and takes the next name. Only a
    request that keeps losing gives up.
    """
    attempts = INSERT_ATTEMPTS
    while True:
        attempts -= 1
        await lock_user(db, user_id)
        row = build(free_name(await taken(), wanted, limit))
        db.add(row)
        try:
            await db.commit()
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


async def move_then_commit(db: DbSession, settings, user_id: int, before, after) -> None:
    """The row is flushed; now the directory, then the commit. A refusal from the tree
    rolls the row back to where the files still are; a commit that fails after the move
    puts the directory back where the row stayed (docs/workspace.md § Revisions)."""
    try:
        moved = await workspace_documents.move_path(settings, user_id, before, after)
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        if moved is not None:
            try:
                await workspace_documents.move_path(settings, user_id, after, before)
            except Exception:  # noqa: BLE001 - the move revision stays revertible
                logger.exception("could not move %s back after a failed commit", after)
        raise


async def folder_exists(db: DbSession, user_id: int, folder_id: int) -> bool:
    query = select(Folder.id).where(Folder.id == folder_id, Folder.user_id == user_id)
    return (await db.execute(query)).first() is not None


# --- payloads ---------------------------------------------------------------------------


def folder_payload(row: Folder) -> dict:
    return {
        "id": row.id,
        "parent_id": row.parent_id,
        "name": row.name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def document_payload(row: Document, job: Job) -> dict:
    """The row the file list draws: the entry, plus the live state of its parse.

    Joined rather than fetched per row, so one `GET /api/library` renders every name,
    status and progress bar the page needs (docs/web.md § Files).
    """
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
    }


async def job_of(db: DbSession, job_id: uuid.UUID) -> Job:
    job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return job


# --- read ---------------------------------------------------------------------------------


async def read_folders(db: DbSession, user_id: int):
    query = select(Folder).where(Folder.user_id == user_id).order_by(Folder.name)
    return (await db.execute(query)).scalars().all()


@router.get("")
async def read_library(request: Request, user: SessionUser):
    """The whole library in one read — navigation between folders is then local.

    Both collections come from one snapshot. Under READ COMMITTED the two statements would
    see two, and the client would hold either documents whose folder it has never heard of
    (drawn nowhere) or a folder that has already been deleted (drawn empty) until something
    refreshed. `REPEATABLE READ` is the cheap way to make one response internally
    consistent — asked for on a session opened here for this read alone, since an
    isolation level applies to a connection's first transaction only and the request's
    own session has already authenticated on its own; the SQLite test fallback has no such
    isolation level and one writer, so it is asked for only where it exists (docs/api.md).
    """
    async with request.app.state.sessionmaker() as reader:
        if reader.get_bind().dialect.name == "postgresql":
            await reader.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        folders = await read_folders(reader, user.id)
        documents = (
            await reader.execute(
                select(Document, Job)
                .join(Job, Job.id == Document.job_id)
                .where(Document.user_id == user.id)
                .order_by(Document.created_at.desc())
            )
        ).all()

    return {
        "folders": [folder_payload(row) for row in folders],
        "documents": [document_payload(row, job) for row, job in documents],
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
async def create_folder(body: FolderCreate, user: SessionUser, db: DbSession):
    if body.parent_id is not None:
        await owned_folder(db, user.id, body.parent_id)
    parent_id = body.parent_id
    try:
        row = await insert_named(
            db,
            user_id=user.id,
            build=lambda name: Folder(user_id=user.id, parent_id=parent_id, name=name),
            taken=lambda: sibling_names(db, user.id, parent_id),
            wanted=clean_name(body.name, FOLDER_NAME_MAX),
            limit=FOLDER_NAME_MAX,
            message="A folder with this name already exists here",
        )
    except ApiError:
        # A parent deleted between the check above and the insert fails the foreign key,
        # which arrives as the same IntegrityError a taken name does — and retrying with a
        # destination that no longer exists only ever produces a 409 about the wrong thing.
        if parent_id is None or await folder_exists(db, user.id, parent_id):
            raise
        raise ApiError(404, "invalid_request", "No such folder") from None
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
        row = await owned_folder(db, user.id, folder_id)
        parent_id = body.parent_id if "parent_id" in provided else row.parent_id

        if parent_id != row.parent_id and parent_id is not None:
            await owned_folder(db, user.id, parent_id)
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
    await move_then_commit(db, settings, user.id, before, after)
    return folder_payload(row)


@router.delete(
    "/folders/{folder_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_folder(
    folder_id: int, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Delete a folder and its subtree — the documents in it are entries, so they go too.

    The subtree is deleted explicitly rather than left to `ON DELETE CASCADE`: the same
    rows disappear on PostgreSQL and on the SQLite test fallback, which does not enforce
    foreign keys unless asked to.
    """
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        row = await owned_folder(db, user.id, folder_id)
        relative = await folder_relative_dir(db, user.id, row.id)
        doomed = await subtree_ids(db, user.id, row.id)
        await refuse_while_answering(
            db,
            user.id,
            select(Document.id).where(Document.user_id == user.id, Document.folder_id.in_(doomed)),
        )
        await db.execute(
            delete(Document).where(Document.user_id == user.id, Document.folder_id.in_(doomed))
        )
        await db.execute(delete(Folder).where(Folder.user_id == user.id, Folder.id.in_(doomed)))
        await remove_then_commit(db, settings, user.id, relative)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def remove_then_commit(db: DbSession, settings, user_id: int, relative) -> None:
    """The rows are deleted in the transaction; the directory goes before it commits. A
    removal the tree refuses keeps the rows, and the next read restores what is missing;
    a commit that fails after the removal keeps the rows too, and the files come back
    from the removal's own revision, so the edits they held are not lost to a fresh copy
    (docs/workspace.md § Revisions)."""
    try:
        removal = await workspace_documents.remove_path(settings, user_id, relative)
    except BaseException:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        if removal is not None:
            try:
                await workspace_documents.restore_removed(settings, user_id, relative, removal)
            except Exception:  # noqa: BLE001 - the directory waits aside, whole
                logger.exception("could not restore %s after a failed delete", relative)
        raise
    if removal is not None:
        try:
            await workspace_documents.discard_removed(settings, user_id, removal)
        except Exception:  # noqa: BLE001 - the next reconciliation sweeps it
            logger.exception("could not discard %s set aside by a delete", relative)


# --- documents ----------------------------------------------------------------------------


class DocumentUpdate(BaseModel):
    """Partial: only the fields present in the body change (docs/api.md)."""

    name: str | None = Field(default=None, min_length=1, max_length=DOCUMENT_NAME_MAX)
    folder_id: int | None = None


class DocumentFromJob(BaseModel):
    job_id: uuid.UUID
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
        await owned_folder(db, user.id, folder_id)

    filename = upload.filename or "upload"
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
        media_type=upload.content_type or "",
        filename=filename,
        # The browser cannot tell a lost response from a lost request, so its retry of an
        # upload that in fact landed must not buy a second parse (docs/jobs.md § Dedup).
        reuse_in_flight=True,
        # A library upload keeps its original even if the worker finishes before the
        # document row below exists (docs/jobs.md § Retention).
        retain_source=True,
    )
    # A dedup hit is a finished parse rather than a new one, so the file points at the job
    # that already produced it (docs/jobs.md § Dedup).
    job_id = submission.job.id if submission.job is not None else submission.cached.job_id

    # The folder can be deleted while the document is being stored and probed. The parse is
    # committed and will bill by now, so the file lands at the root rather than being lost
    # behind a foreign-key failure dressed up as a name conflict.
    if folder_id is not None and not await folder_exists(db, user.id, folder_id):
        folder_id = None

    # The job is committed and running by now, so the file has to land: `insert_named`
    # re-reads and retries rather than answering a conflict.
    async def land(place: int | None) -> Document:
        async def taken() -> set[str]:
            await lock_job(db, job_id)
            return await sibling_names(db, user.id, place)

        return await insert_named(
            db,
            user_id=user.id,
            build=lambda name: Document(user_id=user.id, folder_id=place, job_id=job_id, name=name),
            taken=taken,
            wanted=wanted,
            limit=DOCUMENT_NAME_MAX,
            message="A file with this name already exists here",
        )

    # The row's commit and the upload it takes into the directory land in one backup
    # snapshot: the upload directory is not backed up (docs/workspace.md § Backups).
    async with backup_shared(user_root(settings.workspaces_dir, user.id)):
        try:
            row = await land(folder_id)
        except ApiError:
            # The remaining sliver of that same race: deleted between the check above and
            # this insert, which arrives as a foreign-key failure rather than a name one.
            if folder_id is None or await folder_exists(db, user.id, folder_id):
                raise
            row = await land(None)

        job = await job_of(db, job_id)
        # The parse may already be done (the worker then left the upload for this row),
        # or answered from the cache or joined in flight (the fresh upload is this row's
        # to keep).
        await workspace_documents.adopt_source(db, settings, row, job, upload=submission.upload)
    return document_payload(row, job)


@router.post("/documents/from-job", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def file_job(body: DocumentFromJob, user: SessionUser, db: DbSession):
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
        await owned_folder(db, user.id, body.folder_id)

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
        return await sibling_names(db, user_id, folder_id)

    try:
        row = await insert_named(
            db,
            user_id=user_id,
            build=lambda name: Document(
                user_id=user_id, folder_id=folder_id, job_id=job_id, name=name
            ),
            taken=taken,
            wanted=wanted,
            limit=DOCUMENT_NAME_MAX,
            message="A file with this name already exists here",
        )
    except ApiError:
        # Same race as creating a folder: a destination deleted after the check above fails
        # the foreign key, and that is a missing folder, not a name conflict.
        if folder_id is None or await folder_exists(db, user_id, folder_id):
            raise
        raise ApiError(404, "invalid_request", "No such folder") from None
    return document_payload(row, await job_of(db, job_id))


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
        row = await owned_document(db, user.id, document_id)
        folder_id = body.folder_id if "folder_id" in provided else row.folder_id
        if folder_id != row.folder_id and folder_id is not None:
            await owned_folder(db, user.id, folder_id)

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
    await move_then_commit(db, settings, user.id, before, after)
    return document_payload(row, await job_of(db, row.job_id))


@router.delete(
    "/documents/{document_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_document(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Remove the file from the library. The parse itself stays in the history."""
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        row = await owned_document(db, user.id, document_id)
        relative = await document_relative(db, row)
        await refuse_while_answering(db, user.id, select(Document.id).where(Document.id == row.id))
        await db.delete(row)
        await db.flush()
        await remove_then_commit(db, settings, user.id, relative)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/documents/{document_id}/result")
async def document_result(document_id: int, user: SessionUser, db: DbSession):
    row = await owned_document(db, user.id, document_id)
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


def figure_crop_response(settings: AppSettings, job: Job, page: int, bbox: str) -> FileResponse:
    """One stored crop, addressed the way its placeholder addresses it, or 404.

    The bbox is parsed and re-cleaned into the canonical filename, so the path on disk is
    built from four bounded integers — never from request text.
    """
    cleaned = parse_bbox_path(bbox)
    if cleaned is None or page < 1:
        raise ApiError(404, "invalid_request", "No such figure")
    path = crop_path(Path(settings.figures_dir), job.id, page, cleaned)
    if not path.is_file():
        raise ApiError(404, "invalid_request", "No crop is stored for this figure")
    # Never cached: browser caches key on the URL, not the session, so a cached crop
    # would outlive a sign-out and answer the next account in the same browser profile.
    return FileResponse(
        path, media_type="image/png", headers={"Cache-Control": "private, no-store"}
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
    row = await owned_document(db, user.id, document_id)
    return figure_crop_response(settings, await job_of(db, row.job_id), page, bbox)


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
    row = await owned_document(db, user.id, document_id)
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
    row = await owned_document(db, user.id, document_id)
    revision = await workspace_documents.write_content(
        db, settings, row, markdown=body.markdown, base_revision=body.base_revision
    )
    return {"revision": revision}


@router.put("/documents/{document_id}/notes", dependencies=[CsrfGuard])
async def put_document_notes(
    document_id: int, body: NotesPut, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await owned_document(db, user.id, document_id)
    revision = await workspace_documents.write_notes(
        db, settings, row, text=body.text, base_revision=body.base_revision
    )
    return {"revision": revision}


@router.get("/documents/{document_id}/revisions")
async def document_revisions(
    document_id: int, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await owned_document(db, user.id, document_id)
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
    row = await owned_document(db, user.id, document_id)
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ApiError(404, "invalid_request", "No such revision")
    # The workspace lock first, then the rows — the order every mutation takes. A shell
    # revision may have moved the document; undoing that under a running turn would pull
    # the path out from under it (docs/chat.md § Conversations).
    async with workspace_lock(user_root(settings.workspaces_dir, user.id)):
        # A revision that reached beyond this document (a shell's) may be what another
        # conversation's turn is working on right now: refused while any turn of the
        # user is unfinished (docs/workspace.md § Revisions).
        paths = await workspace_documents.materialize_document(db, settings, row, locked=True)
        beyond = await asyncio.to_thread(workspace_documents.reaches_beyond, paths, commit)
        guarded = (
            select(Document.id).where(Document.user_id == user.id)
            if beyond
            else select(Document.id).where(Document.id == row.id)
        )
        await refuse_while_answering(db, user.id, guarded)
        reverted = await workspace_documents.revert(db, settings, row, commit, locked=True)
    return {"revision": reverted.revision, "commit": reverted.commit}
