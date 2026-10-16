"""normalize library names

Revision ID: c8f28f7cd45e
Revises: 5e70f2a38225
Create Date: 2026-10-16 00:09:47.132728
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8f28f7cd45e"
down_revision: str | None = "5e70f2a38225"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The same cleaning the library applies to a new name (routes.library.clean_name), so a
# legacy name lands exactly where a fresh one would.
UNSAFE_IN_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")
# The columns' own limits (db.models): folders 255, documents 512.
NAME_MAX = {"folders": 255, "documents": 512}


def _clean(raw: str, limit: int) -> str:
    name = UNSAFE_IN_NAME.sub(" ", unicodedata.normalize("NFC", raw)).strip()
    return name[:limit].strip() or "upload"


def _free(taken: set[str], name: str, limit: int) -> str:
    """`report.pdf` → `report (2).pdf` while the name is taken (routes.library.free_name)."""
    if name not in taken:
        return name
    stem, dot, extension = name.rpartition(".")
    if not stem:
        stem, dot, extension = name, "", ""
    index = 2
    while True:
        suffix = f" ({index}){dot}{extension}"
        room = limit - len(suffix)
        # No room for a stem (a legacy extension nearly as long as the column): the
        # extension gives way, never the counter, so each try is a new candidate and
        # the loop ends.
        candidate = f"{stem[:room]}{suffix}" if room > 0 else suffix[:limit]
        if candidate not in taken:
            return candidate
        index += 1


def upgrade() -> None:
    """Folders and documents share one namespace per level from now on, in NFC
    (docs/workspace.md § Layout): every legacy name is normalized, and where two entries
    of one parent then share a name — a folder and a document, two spellings — the later
    one by id (folders first) takes the suffix a collision gets today, so no two rows
    ever map to one workspace directory."""
    bind = op.get_bind()
    folders = sa.table(
        "folders",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("parent_id", sa.Integer),
        sa.column("name", sa.String),
    )
    documents = sa.table(
        "documents",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("folder_id", sa.Integer),
        sa.column("name", sa.String),
    )
    # (table, id, user, parent, name), folders before documents, each by id.
    rows: list[tuple[str, int, int, int | None, str]] = [
        ("folders", *row)
        for row in bind.execute(
            sa.select(
                folders.c.id, folders.c.user_id, folders.c.parent_id, folders.c.name
            ).order_by(folders.c.id)
        )
    ] + [
        ("documents", *row)
        for row in bind.execute(
            sa.select(
                documents.c.id, documents.c.user_id, documents.c.folder_id, documents.c.name
            ).order_by(documents.c.id)
        )
    ]
    taken: dict[tuple[int, int | None], set[str]] = {}
    changes: list[tuple[sa.TableClause, int, str]] = []
    for table, row_id, user_id, parent_id, name in rows:
        level = taken.setdefault((user_id, parent_id), set())
        wanted = _free(level, _clean(name, NAME_MAX[table]), NAME_MAX[table])
        level.add(wanted)
        if wanted != name:
            changes.append((folders if table == "folders" else documents, row_id, wanted))
    # Two passes: a row's new name may be another row's old one (the composed spelling
    # beside the decomposed), and the unique indexes hold throughout. Every changing row
    # first takes a name nothing else can hold, then its own.
    run = uuid.uuid4().hex
    for target, row_id, _wanted in changes:
        bind.execute(sa.update(target).where(target.c.id == row_id).values(name=f"~{run}-{row_id}"))
    for target, row_id, wanted in changes:
        bind.execute(sa.update(target).where(target.c.id == row_id).values(name=wanted))


def downgrade() -> None:
    # The old names are not kept: a name normalized or suffixed here is a valid name
    # under the old rules too, so there is nothing to undo.
    pass
