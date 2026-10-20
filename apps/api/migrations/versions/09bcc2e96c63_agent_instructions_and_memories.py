"""agent instructions and memories

The chat agent's instructions, its answer and translation languages and its two memory
switches on `agent_settings`, the memory snapshot a conversation's first turn takes
(`conversations.memory_ids`), and the `memories` table with its indexes (docs/chat.md
§ Instructions, § Memory). Every existing row reads as before: no instructions, the
reader's locale, memory on and not paused, and a conversation that takes its snapshot at
its next turn. No row is rewritten. The downgrade drops them all, the memories with them.

Revision ID: 09bcc2e96c63
Revises: ecd8f04df825
Create Date: 2026-10-20 13:09:46.975597
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "09bcc2e96c63"
down_revision: str | None = "ecd8f04df825"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("agent_settings", sa.Column("instructions", sa.Text(), nullable=True))
    op.add_column(
        "agent_settings", sa.Column("answer_language", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "agent_settings", sa.Column("translate_language", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "agent_settings",
        sa.Column("memory_auto", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "agent_settings",
        sa.Column("memory_paused", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("conversations", sa.Column("memory_ids", JSON, nullable=True))

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("source_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint(
            "kind IN ('term', 'preference', 'about', 'document')", name="ck_memories_kind"
        ),
        # A note about one document goes with it; the conversation that saved one does not
        # take it along (docs/database.md).
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memories_user_updated", "memories", ["user_id", "updated_at"])
    # What a document's delete (CASCADE) and a conversation's (SET NULL) look rows up by, so
    # neither scans every reader's memories (docs/database.md).
    op.create_index(
        "ix_memories_document_id",
        "memories",
        ["document_id"],
        postgresql_where=sa.text("document_id IS NOT NULL"),
        sqlite_where=sa.text("document_id IS NOT NULL"),
    )
    op.create_index(
        "ix_memories_conversation_id",
        "memories",
        ["conversation_id"],
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
        sqlite_where=sa.text("conversation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_memories_conversation_id", table_name="memories")
    op.drop_index("ix_memories_document_id", table_name="memories")
    op.drop_index("ix_memories_user_updated", table_name="memories")
    op.drop_table("memories")
    op.drop_column("conversations", "memory_ids")
    op.drop_column("agent_settings", "memory_paused")
    op.drop_column("agent_settings", "memory_auto")
    op.drop_column("agent_settings", "translate_language")
    op.drop_column("agent_settings", "answer_language")
    op.drop_column("agent_settings", "instructions")
