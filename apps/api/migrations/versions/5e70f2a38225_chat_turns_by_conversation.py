"""chat turns by conversation

Revision ID: 5e70f2a38225
Revises: f74c75c0416e
Create Date: 2026-10-15 23:37:46.549964
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "5e70f2a38225"
down_revision: str | None = "f74c75c0416e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The stream's "is a turn unfinished" poll, sends, cancels and deletes all ask for
    # a conversation's turns (docs/database.md).
    op.create_index(
        "ix_chat_turns_conversation_finished", "chat_turns", ["conversation_id", "finished_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_chat_turns_conversation_finished", table_name="chat_turns")
