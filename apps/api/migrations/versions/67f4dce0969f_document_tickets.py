"""document tickets

Short-lived credentials for one document, minted by the MCP library tools (docs/auth.md
§ 7, docs/database.md): a new table and its mint-rate index, nothing else read or
rewritten. The OAuth grants' second scope needs no schema change: `oauth_grants.scope` is
text already. The downgrade drops the table; the tickets in it expire within the hour anyway.

Revision ID: 67f4dce0969f
Revises: a011ab0e1c62
Create Date: 2026-11-01 15:45:51.176306
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "67f4dce0969f"
down_revision: str | None = "a011ab0e1c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_tickets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope IN ('read', 'write')", name="ck_document_tickets_scope"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(
        "ix_document_tickets_user_created",
        "document_tickets",
        ["user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_tickets_user_created", table_name="document_tickets")
    op.drop_table("document_tickets")
