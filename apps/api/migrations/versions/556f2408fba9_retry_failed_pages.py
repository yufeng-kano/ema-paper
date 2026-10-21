"""retry failed pages

A parse with a failed page fails whole, and its failed pages are retried alone (docs/jobs.md
§ Retrying failed pages): `agent_settings.fallback_model` and `auto_fallback` (the parse
agent's fallback for a blocked page, off), `jobs.fallback_model` (its snapshot at enqueue),
`jobs.retry_model` (the model a retry runs the remaining pages on) and `job_pages.model`
(the model that wrote a page). All NULL or false on existing rows: no row is rewritten,
and a job already `succeeded` with page errors keeps its result (docs/database.md § Rules).

Revision ID: 556f2408fba9
Revises: 593bad709660
Create Date: 2026-10-21 19:31:24.534828
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "556f2408fba9"
down_revision: str | None = "593bad709660"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_settings", sa.Column("fallback_model", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "agent_settings",
        sa.Column("auto_fallback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("jobs", sa.Column("fallback_model", sa.String(length=255), nullable=True))
    op.add_column("jobs", sa.Column("retry_model", sa.String(length=255), nullable=True))
    op.add_column("job_pages", sa.Column("model", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("job_pages", "model")
    op.drop_column("jobs", "retry_model")
    op.drop_column("jobs", "fallback_model")
    op.drop_column("agent_settings", "auto_fallback")
    op.drop_column("agent_settings", "fallback_model")
