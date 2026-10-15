"""chat agent, workspace revisions, sandbox

The chat agent's tables (docs/chat.md): conversations about a document, their messages,
the turn queue the worker drains and the event stream it writes. `agent_settings.effort`
is the chat agent's reasoning depth. `usage_log` learns which agent billed a row (backfilled
from the job), the turn that made the call, and the prompt-cache token counts.

Revision ID: f74c75c0416e
Revises: 837c0e9a9a4f
Create Date: 2026-10-15 16:35:26.885067
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f74c75c0416e"
down_revision: str | None = "837c0e9a9a4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("session", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversations_document_updated", "conversations", ["document_id", "updated_at"]
    )
    op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"])

    op.create_table(
        "chat_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("locale", sa.String(length=16), server_default="en", nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("started_at", TIMESTAMPTZ, nullable=True),
        sa.Column("finished_at", TIMESTAMPTZ, nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_turns_status_created_at", "chat_turns", ["status", "created_at"])
    op.create_index(op.f("ix_chat_turns_user_id"), "chat_turns", ["user_id"])

    op.create_table(
        "chat_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("data", JSON, nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["chat_turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_chat_events_conversation_seq"),
    )

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", JSON, nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["chat_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id", "id"])

    op.add_column(
        "agent_settings",
        sa.Column(
            "effort", sa.String(length=8), server_default=sa.text("'medium'"), nullable=False
        ),
    )

    op.add_column("usage_log", sa.Column("turn_id", sa.Uuid(), nullable=True))
    op.add_column(
        "usage_log",
        sa.Column("agent", sa.String(length=32), server_default="parse", nullable=False),
    )
    op.add_column(
        "usage_log",
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "usage_log",
        sa.Column("cache_write_tokens", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_foreign_key(
        "fk_usage_log_turn_id", "usage_log", "chat_turns", ["turn_id"], ["id"], ondelete="SET NULL"
    )
    # Every existing row was billed by the job it belongs to (docs/database.md § Rules);
    # a row whose job is gone is historical and keeps the default, `parse`.
    op.execute(
        sa.text(
            "UPDATE usage_log SET agent = jobs.agent "
            "FROM jobs WHERE usage_log.job_id = jobs.id"
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_usage_log_turn_id", "usage_log", type_="foreignkey")
    op.drop_column("usage_log", "cache_write_tokens")
    op.drop_column("usage_log", "cache_read_tokens")
    op.drop_column("usage_log", "agent")
    op.drop_column("usage_log", "turn_id")
    op.drop_column("agent_settings", "effort")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("chat_events")
    op.drop_index(op.f("ix_chat_turns_user_id"), table_name="chat_turns")
    op.drop_index("ix_chat_turns_status_created_at", table_name="chat_turns")
    op.drop_table("chat_turns")
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_index("ix_conversations_document_updated", table_name="conversations")
    op.drop_table("conversations")
