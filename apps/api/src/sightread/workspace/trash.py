"""Deleting for good from the Trash (docs/workspace.md § The Trash).

*Empty Trash* and the Trash's expiry are one function, `empty_trash`, which the route
calls without a cutoff and the worker's sweep with one. It lives here, with the locks and
the commit-then-discard it shares with the library's routes, because the worker never
imports a route (docs/project-structure.md § Boundaries).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import ChatTurn, Conversation, Document, Folder, User
from ..errors import ApiError
from . import documents as workspace_documents
from .layout import trash_entry, user_root
from .locks import workspace_lock

logger = logging.getLogger(__name__)


async def lock_user(db: AsyncSession, user_id: int) -> None:
    """Serialize this user's name decisions: the unique indexes cannot see across the
    two tables, so two requests choosing a name for a folder and a file at once are
    ordered here (docs/database.md)."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


async def refuse_while_answering(db: AsyncSession, user_id: int, document_ids) -> None:
    """A document whose conversation has an unfinished turn cannot go: the worker would
    carry on against rows and a directory that no longer exist (docs/chat.md). Decided
    under the locks a send takes, in the same order — the user's row, then the
    conversations' — so no turn can be queued between this check and the delete."""
    await lock_user(db, user_id)
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


async def read_row(db: AsyncSession, model: type, row_id: int) -> tuple[bool, object | None]:
    """The row as the database holds it after a commit that raised — read on a connection
    of its own, since the request's may be the one that dropped. `(readable, row)`: a
    read that fails says nothing about the commit, and the caller treats it so."""
    try:
        async with AsyncSession(bind=db.bind, expire_on_commit=False) as fresh:
            return True, await fresh.get(model, row_id)
    except Exception:  # noqa: BLE001 - the database is away
        logger.exception("could not read row %s of %s after a failed commit", row_id, model)
        return False, None


async def commit_then_discard(
    db: AsyncSession,
    settings: Settings,
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
        readable, row = await read_row(db, *gone)
        if readable and row is None:
            logger.warning("the delete of %s was committed despite the error", entries)
            await _discard_for_good(settings, user_id, entries, document_ids)
        raise
    await _discard_for_good(settings, user_id, entries, document_ids)


async def _discard_for_good(
    settings: Settings, user_id: int, entries: list[str], document_ids: list[int]
) -> None:
    try:
        await workspace_documents.discard_trash(settings, user_id, entries)
    except Exception:  # noqa: BLE001 - the repair rule removes an entry with no row
        logger.exception("could not remove %s from the Trash", entries)
    try:
        await workspace_documents.discard_originals(settings, user_id, document_ids)
    except Exception:  # noqa: BLE001 - reconciliation sweeps an original with no row
        logger.exception("could not discard the originals of %s", entries)


async def empty_trash(
    db: AsyncSession, settings: Settings, user_id: int, *, trashed_before: datetime | None = None
) -> int:
    """Delete for good what is in the user's Trash — everything, for *Empty Trash*, or
    only the items whose own `trashed_at` is older than `trashed_before`, for the Trash's
    expiry — rows first, then their files, under the workspace lock. Returns how many
    items it deleted.

    An expired folder takes everything under it: an item trashed on its own inside it was
    trashed earlier, nothing inside a trashed folder being trashable afterwards, so it has
    expired too, and its entry goes with the rest. 409 while a conversation about a file
    it would delete is still answering, as for a delete from the Files page."""
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        await workspace_documents.recover_trash(db, settings, user_id)
        await lock_user(db, user_id)

        def expired(row: Folder | Document) -> bool:
            return row.trashed_at is not None and (
                trashed_before is None or row.trashed_at < trashed_before
            )

        folders = await workspace_documents.user_folders(db, user_id)
        doomed_folders = {
            folder_id
            for folder_id in folders
            if any(expired(row) for row in workspace_documents.folder_chain(folders, folder_id))
        }
        documents = (
            (await db.execute(select(Document).where(Document.user_id == user_id))).scalars().all()
        )
        doomed = [
            document.id
            for document in documents
            if expired(document) or document.folder_id in doomed_folders
        ]
        # Every deleted row put in the Trash itself has an entry there.
        roots: list[tuple[type, int]] = [
            (Folder, folder_id)
            for folder_id in doomed_folders
            if folders[folder_id].trashed_at is not None
        ] + [
            (Document, row.id)
            for row in documents
            if row.id in doomed and row.trashed_at is not None
        ]
        if not roots:
            return 0
        await refuse_while_answering(db, user_id, doomed)
        await db.execute(
            delete(Document).where(Document.user_id == user_id, Document.id.in_(doomed))
        )
        await db.execute(
            delete(Folder).where(Folder.user_id == user_id, Folder.id.in_(doomed_folders))
        )
        entries = [
            trash_entry("folder" if model is Folder else "document", row_id)
            for model, row_id in roots
        ]
        await commit_then_discard(db, settings, user_id, entries, doomed, gone=roots[0])
    return len(roots)
