"""conversation compaction and memory file

Compaction and the memory file (docs/chat.md § Compaction, § Memory): `chat_turns.kind`
(`message` on every existing row), `agent_settings.idle_compact` (`true` on every row —
idle compaction is on unless the reader turns it off), and on `conversations` the nullable
`memory_file` (the snapshot of memory.md a turn takes), `compacted_message_id` (the summary
row the replay starts from — no foreign key, since a message goes only with its
conversation), `context_tokens` and `cache_warm_until`
(what the last model call replayed and when the cache it touched expires), with the partial
index the idle compaction pass reads. No row is read or rewritten: an open conversation goes
on as it was, takes its memory file snapshot at its next turn, and is first measured by its
next model call. The downgrade drops the columns and the index; a conversation compacted
meanwhile then replays its whole history again, the summary row among it.

Revision ID: d78cf7a42eea
Revises: 8d1142bbd272
Create Date: 2026-10-26 19:12:16.340991
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d78cf7a42eea"
down_revision: str | None = "8d1142bbd272"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_turns",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="message"),
    )
    op.add_column(
        "agent_settings",
        sa.Column("idle_compact", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("conversations", sa.Column("memory_file", sa.Text(), nullable=True))
    op.add_column("conversations", sa.Column("compacted_message_id", sa.Integer(), nullable=True))
    op.add_column("conversations", sa.Column("context_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "conversations", sa.Column("cache_warm_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_conversations_cache_warm_until",
        "conversations",
        ["cache_warm_until"],
        unique=False,
        postgresql_where=sa.text("cache_warm_until IS NOT NULL"),
        sqlite_where=sa.text("cache_warm_until IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_cache_warm_until", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("cache_warm_until")
        batch.drop_column("context_tokens")
        batch.drop_column("compacted_message_id")
        batch.drop_column("memory_file")
    with op.batch_alter_table("agent_settings") as batch:
        batch.drop_column("idle_compact")
    with op.batch_alter_table("chat_turns") as batch:
        batch.drop_column("kind")
