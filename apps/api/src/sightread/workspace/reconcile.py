"""The tree is the truth (docs/workspace.md § The tree is the truth).

After a chat turn the workspace may differ from the library: a shell command moved a
document directory, renamed a folder, or created one. Reconciliation reads `.meta.json`
under `files/` and updates the rows to match. It never deletes a row: a document whose
directory vanished is materialized again on its next read, and deleting is done from the
page, where it is deliberate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Document, Folder
from .layout import META_FILE, files_root, path_segment, user_root

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Found:
    document_id: int
    folder_segments: tuple[str, ...]
    name: str


def _scan(root: Path) -> list[Found]:
    """Every document directory under files/, by the id its `.meta.json` carries."""
    base = files_root(root)
    found: list[Found] = []
    if not base.is_dir():
        return found
    for meta in base.rglob(META_FILE):
        try:
            payload = json.loads(meta.read_text())
            document_id = int(payload["document_id"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        relative = meta.parent.relative_to(base)
        parts = relative.parts
        if not parts:
            continue
        found.append(Found(document_id, tuple(parts[:-1]), parts[-1]))
    return found


async def _folder_for(
    db: AsyncSession, user_id: int, segments: tuple[str, ...], cache: dict
) -> int | None:
    """The folder row at this path, created when the shell made the directory."""
    parent: int | None = None
    for segment in segments:
        key = (parent, segment)
        if key in cache:
            parent = cache[key]
            continue
        rows = (
            (
                await db.execute(
                    select(Folder).where(
                        Folder.user_id == user_id,
                        Folder.parent_id.is_(None)
                        if parent is None
                        else Folder.parent_id == parent,
                    )
                )
            )
            .scalars()
            .all()
        )
        match = next((row for row in rows if path_segment(row.name) == segment), None)
        if match is None:
            match = Folder(user_id=user_id, parent_id=parent, name=segment)
            db.add(match)
            await db.flush()
        cache[key] = match.id
        parent = match.id
    return parent


async def reconcile_user(db: AsyncSession, settings: Settings, user_id: int) -> int:
    """Bring the user's rows in line with their tree. Returns how many rows changed."""
    root = user_root(settings.workspaces_dir, user_id)
    found = await asyncio.to_thread(_scan, root)
    if not found:
        return 0
    documents = {
        row.id: row
        for row in (await db.execute(select(Document).where(Document.user_id == user_id))).scalars()
    }
    cache: dict = {}
    changed = 0
    for entry in found:
        row = documents.get(entry.document_id)
        if row is None:
            continue
        folder_id = await _folder_for(db, user_id, entry.folder_segments, cache)
        # The segment is the name unless the name needed cleaning; a directory the shell
        # renamed carries the new name verbatim.
        name = entry.name if path_segment(row.name) != entry.name else row.name
        if row.folder_id != folder_id or row.name != name:
            row.folder_id = folder_id
            row.name = name[:512]
            changed += 1
    if changed:
        try:
            await db.commit()
        except IntegrityError:
            # A unique clash from the shell is reported, never fatal.
            await db.rollback()
            logger.warning("reconcile for user %d hit a name conflict; rows unchanged", user_id)
            return 0
    return changed
