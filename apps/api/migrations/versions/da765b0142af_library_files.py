"""library files

A library entry may be a file of any kind, kept once in the tree (docs/workspace.md § Files).
`documents.kind` says which of the three a row is — `parse`, `markdown` or `file` — NOT NULL,
default `parse`, backfilled from `job_id` (a row with a job is a parse, one without a markdown
document); `documents.media_type` and `documents.size_bytes` carry what a file is served as and
how large it is, NULL on every existing row; `agent_settings.auto_convert` (true) is the parse
agent's switch between converting an uploaded PDF or image on arrival and keeping it as a file
(docs/agents.md). No row is rewritten otherwise. The downgrade refuses while any row is a file,
naming the count — without `kind` such a row would read as a markdown document whose text no
route could find — and otherwise drops the four columns (docs/database.md § Rules).

Revision ID: da765b0142af
Revises: e014be293754
Create Date: 2026-10-25 17:36:00.525558
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "da765b0142af"
down_revision: str | None = "e014be293754"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="parse"),
    )
    op.execute("UPDATE documents SET kind = 'markdown' WHERE job_id IS NULL")
    op.add_column("documents", sa.Column("media_type", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "agent_settings",
        sa.Column("auto_convert", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    files = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM documents WHERE kind = 'file'"))
        .scalar_one()
    )
    if files:
        raise RuntimeError(
            f"{files} file(s) are kept in the library; delete them before downgrading"
        )
    op.drop_column("agent_settings", "auto_convert")
    op.drop_column("documents", "size_bytes")
    op.drop_column("documents", "media_type")
    op.drop_column("documents", "kind")
