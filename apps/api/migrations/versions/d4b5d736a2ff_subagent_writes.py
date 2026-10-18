"""subagent writes

`subagents.writes`: whether a subagent may change files (docs/chat.md § Subagents). Off for
every row, except the reader's edits of the built-ins that write — `translator` and
`translation-checker` — which take the built-in's `writes` with them, as an edit takes
every field it does not change.

Revision ID: d4b5d736a2ff
Revises: 90dc3c3c5c36
Create Date: 2026-10-18 13:18:10.160759
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b5d736a2ff"
down_revision: str | None = "90dc3c3c5c36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The built-ins that write when this migration was written (agents/subagents.py).
WRITING_BUILTINS = ("translator", "translation-checker")


def upgrade() -> None:
    op.add_column(
        "subagents",
        sa.Column("writes", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    subagents = sa.table("subagents", sa.column("name", sa.String), sa.column("writes", sa.Boolean))
    op.execute(subagents.update().where(subagents.c.name.in_(WRITING_BUILTINS)).values(writes=True))


def downgrade() -> None:
    op.drop_column("subagents", "writes")
