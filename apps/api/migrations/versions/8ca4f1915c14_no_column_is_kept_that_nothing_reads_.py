"""No column is kept that nothing reads, and every cascade reads an index

The columns nothing reads go: `credit_entries.model`, `credit_periods.plan_id`,
`chat_turns.started_at`, `upload_tickets.prefix`, `document_tickets.prefix`, and
`pay_events.type` and `received_at` (a `pay_events` row is its event id and the user it was
about). The foreign-key actions of a turn delete, and of a folder delete, read an index on
the referring column, and so does a turn's settlement on `usage_log`; the single-column
`user_id` indexes that a composite leading with `user_id` already covers go. No row is
rewritten. The downgrade restores the structure: the columns come back, the NOT NULL ones
filled with '' or the moment of the downgrade, and the indexes as they were.

Revision ID: 8ca4f1915c14
Revises: d10760d56782
Create Date: 2026-11-24 13:27:39.856429
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8ca4f1915c14"
down_revision: str | None = "d10760d56782"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The partial indexes: a foreign-key action's `col = $1` implies the predicate.
PARTIAL_INDEXES = (
    ("ix_documents_folder_id", "documents", "folder_id"),
    ("ix_folders_parent_id", "folders", "parent_id"),
    ("ix_messages_turn_id", "messages", "turn_id"),
    ("ix_usage_log_turn_id", "usage_log", "turn_id"),
)
COVERED_USER_INDEXES = (
    ("ix_documents_user_id", "documents"),
    ("ix_skills_user_id", "skills"),
    ("ix_subagents_user_id", "subagents"),
)


def upgrade() -> None:
    op.drop_column("chat_turns", "started_at")
    op.drop_column("credit_entries", "model")
    op.drop_column("credit_periods", "plan_id")
    op.drop_column("document_tickets", "prefix")
    op.drop_column("upload_tickets", "prefix")
    op.drop_column("pay_events", "type")
    op.drop_column("pay_events", "received_at")
    for name, table in COVERED_USER_INDEXES:
        op.drop_index(name, table_name=table)
    for name, table, column in PARTIAL_INDEXES:
        op.create_index(name, table, [column], postgresql_where=sa.text(f"{column} IS NOT NULL"))
    op.create_index("ix_chat_events_turn_id", "chat_events", ["turn_id"])


def _restore_not_null(table: str, column: sa.Column, fill: sa.TextClause) -> None:
    """Add a NOT NULL column back, existing rows filled with `fill`, without the default
    the schema never had."""
    column.server_default = sa.DefaultClause(fill)
    op.add_column(table, column)
    op.alter_column(table, column.name, server_default=None)


def downgrade() -> None:
    op.drop_index("ix_chat_events_turn_id", table_name="chat_events")
    for name, table, _ in PARTIAL_INDEXES:
        op.drop_index(name, table_name=table)
    for name, table in COVERED_USER_INDEXES:
        op.create_index(name, table, ["user_id"])
    _restore_not_null(
        "pay_events", sa.Column("type", sa.String(length=64), nullable=False), sa.text("''")
    )
    _restore_not_null(
        "pay_events",
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.text("now()"),
    )
    _restore_not_null(
        "upload_tickets", sa.Column("prefix", sa.String(length=32), nullable=False), sa.text("''")
    )
    _restore_not_null(
        "document_tickets",
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.text("''"),
    )
    op.add_column("credit_periods", sa.Column("plan_id", sa.String(length=64), nullable=True))
    op.add_column("credit_entries", sa.Column("model", sa.String(length=255), nullable=True))
    op.add_column("chat_turns", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
