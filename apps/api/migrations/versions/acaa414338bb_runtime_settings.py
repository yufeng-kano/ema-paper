"""runtime settings

The operator console's settings over `.env` (docs/admin.md § Settings): `runtime_settings`,
one row per key the database overrides, its value the text `.env` would hold kept as a JSON
string, with who stored it and when; and `ix_conversations_model`, which the settings
page's confirmation counts the conversations on a model through. No existing row is read
or rewritten: every setting stays `.env`'s until an operator stores one
(docs/database.md § Rules). The downgrade drops the index and the table — the stored
settings go with it, and `.env`'s are in effect again.

Revision ID: acaa414338bb
Revises: d632652e713f
Create Date: 2026-10-27 17:51:41.538233
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "acaa414338bb"
down_revision: str | None = "d632652e713f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_conversations_model", "conversations", ["model"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_conversations_model", table_name="conversations")
    op.drop_table("runtime_settings")
