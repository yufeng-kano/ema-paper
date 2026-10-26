"""reader views

A PDF or an image is one item whether converted or not, read on its original or as Markdown
(docs/web.md § Result viewer): `documents.view` remembers the view the reader last switched it
to (NULL on every existing row), `users.default_view` the view a converted one opens on the
first time (`markdown` on every row), and the parse agent's `auto_convert` turns off — its
default becomes false and every existing row is set to false, since an uploaded PDF or image
is now kept until the reader asks for Markdown (docs/database.md § Rules). The downgrade drops
the two columns and restores the default of true; the rows stay off.

Revision ID: 283c259880d3
Revises: da765b0142af
Create Date: 2026-10-26 02:08:50.807410
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "283c259880d3"
down_revision: str | None = "da765b0142af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("default_view", sa.String(length=16), nullable=False, server_default="markdown"),
    )
    op.add_column("documents", sa.Column("view", sa.String(length=16), nullable=True))
    op.alter_column(
        "agent_settings",
        "auto_convert",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("false"),
    )
    op.execute("UPDATE agent_settings SET auto_convert = false")


def downgrade() -> None:
    op.alter_column(
        "agent_settings",
        "auto_convert",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("true"),
    )
    op.drop_column("documents", "view")
    op.drop_column("users", "default_view")
