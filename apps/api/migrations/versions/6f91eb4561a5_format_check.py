"""format check

A Files upload is checked before it opens (docs/parsing.md § Format check):
`agent_settings.auto_check` (the check agent's switch, on), `jobs.check_model`,
`check_effort` and `check_prompt` (the check snapshotted on the job; NULL is no check) and
`results.reading_markdown` (the checked copy a library document starts from). No row is
rewritten: a job queued before it runs unchecked, and a result written before it has no
reading copy (docs/database.md § Rules).

Revision ID: 6f91eb4561a5
Revises: 556f2408fba9
Create Date: 2026-10-21 21:09:34.937410
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f91eb4561a5"
down_revision: str | None = "556f2408fba9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_settings",
        sa.Column("auto_check", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("jobs", sa.Column("check_model", sa.String(length=255), nullable=True))
    op.add_column("jobs", sa.Column("check_effort", sa.String(length=8), nullable=True))
    op.add_column("jobs", sa.Column("check_prompt", sa.Text(), nullable=True))
    op.add_column("results", sa.Column("reading_markdown", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("results", "reading_markdown")
    op.drop_column("jobs", "check_prompt")
    op.drop_column("jobs", "check_effort")
    op.drop_column("jobs", "check_model")
    op.drop_column("agent_settings", "auto_check")
