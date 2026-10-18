"""remove three built-in subagents

`verifier`, `reader` and `translation-checker` left the built-ins (docs/chat.md § Subagents):
the readers' edits of them are deleted with them, rather than left behind as subagents of
their own. The downgrade restores none: the built-ins come back with their code, unedited.

Revision ID: ecd8f04df825
Revises: d4b5d736a2ff
Create Date: 2026-10-18 21:01:13.101432
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ecd8f04df825"
down_revision: str | None = "d4b5d736a2ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The built-ins this migration removed (agents/subagents.py before it).
REMOVED_BUILTINS = ("verifier", "reader", "translation-checker")


def upgrade() -> None:
    subagents = sa.table("subagents", sa.column("name", sa.String))
    op.execute(subagents.delete().where(subagents.c.name.in_(REMOVED_BUILTINS)))


def downgrade() -> None:
    pass
