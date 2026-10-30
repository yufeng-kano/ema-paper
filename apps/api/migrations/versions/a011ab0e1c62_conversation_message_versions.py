"""conversation message versions

Versions of a sent message (docs/chat.md § History is append-only, *An edited message
starts a version*): `chat_turns.parent_id`, the turn before each turn on its branch, and
`conversations.head_id`, the turn whose branch the conversation shows — both nullable UUIDs
without a foreign key: a turn goes only with its conversation, and a key from
`conversations` back to `chat_turns` would close a cycle. Both are backfilled from the rows
as they stood: each turn's parent is the turn of its conversation made before it, by
`created_at` then id, and each conversation's head its newest turn, so every existing
conversation is one branch holding every turn, exactly what it showed before. No other row
is read or rewritten. The downgrade drops the two columns: a conversation that edited a
message meanwhile then shows every version's turn in one thread, in the order they were
made, and replays them all.

Revision ID: a011ab0e1c62
Revises: acaa414338bb
Create Date: 2026-10-30 07:03:19.821841
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a011ab0e1c62"
down_revision: str | None = "acaa414338bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_turns", sa.Column("parent_id", sa.Uuid(), nullable=True))
    op.add_column("conversations", sa.Column("head_id", sa.Uuid(), nullable=True))
    # Every turn's parent is the one made before it in its conversation: one branch each.
    op.execute(
        """
        UPDATE chat_turns
        SET parent_id = ordered.previous_id
        FROM (
            SELECT id,
                   LAG(id) OVER (PARTITION BY conversation_id ORDER BY created_at, id)
                       AS previous_id
            FROM chat_turns
        ) AS ordered
        WHERE chat_turns.id = ordered.id
        """
    )
    # Every conversation shows the branch of its newest turn: the whole thread.
    op.execute(
        """
        UPDATE conversations
        SET head_id = newest.id
        FROM (
            SELECT DISTINCT ON (conversation_id) conversation_id, id
            FROM chat_turns
            ORDER BY conversation_id, created_at DESC, id DESC
        ) AS newest
        WHERE conversations.id = newest.conversation_id
        """
    )


def downgrade() -> None:
    op.drop_column("conversations", "head_id")
    op.drop_column("chat_turns", "parent_id")
