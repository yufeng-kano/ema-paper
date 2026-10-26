"""chat tool calls

The chat agent's web tool calls (docs/chat.md § Tools): `chat_tool_calls`, one row per
`web_search`, `search_papers` or `download_file` call that was answered or kept a file —
what the per-day caps count and what a turn's web searches are charged from (docs/billing.md
§ A chat turn). Empty, with its indexes (docs/database.md § Rules); no existing row is read
or rewritten. The downgrade drops the table and nothing else — the day's counts and the
record behind searches already charged are lost with it, the credit entries themselves not.

Revision ID: 8d1142bbd272
Revises: 148254b2f3d9
Create Date: 2026-10-26 17:13:13.230157
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d1142bbd272"
down_revision: str | None = "148254b2f3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_tool_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["chat_turns.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_tool_calls_tool_created", "chat_tool_calls", ["tool", "created_at"], unique=False
    )
    op.create_index(
        "ix_chat_tool_calls_user_tool_created",
        "chat_tool_calls",
        ["user_id", "tool", "created_at"],
        unique=False,
    )
    op.create_index("ix_chat_tool_calls_turn_id", "chat_tool_calls", ["turn_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_chat_tool_calls_turn_id", table_name="chat_tool_calls")
    op.drop_index("ix_chat_tool_calls_user_tool_created", table_name="chat_tool_calls")
    op.drop_index("ix_chat_tool_calls_tool_created", table_name="chat_tool_calls")
    op.drop_table("chat_tool_calls")
