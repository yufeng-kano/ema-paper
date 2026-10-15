"""usage cost source and connection report cost

Every usage row now says where its cost came from (`cost_source`: openrouter, estimated,
unknown), an unknown cost is NULL instead of 0, and a provider connection can opt in to
recording the cost its endpoint reports, as an estimate (`report_cost`, off). See
docs/parsing.md § Upstream usage and docs/database.md § Rules.

Backfill: rows of a job that ran on a connection recorded 0 in place of a discarded cost,
so they become unknown with cost NULL; every other row is OpenRouter billing, unchanged.

Revision ID: 5f1845730aa0
Revises: cd77f4fe600f
Create Date: 2026-10-15 18:22:44.615001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f1845730aa0"
down_revision: str | None = "cd77f4fe600f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_connections",
        sa.Column("report_cost", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.add_column("usage_log", sa.Column("cost_source", sa.String(length=16), nullable=True))
    op.alter_column("usage_log", "cost", existing_type=sa.Numeric(12, 6), nullable=True)
    op.execute(
        sa.text(
            "UPDATE usage_log SET cost_source = 'unknown', cost = NULL "
            "FROM jobs WHERE usage_log.job_id = jobs.id AND jobs.connection_id IS NOT NULL"
        )
    )
    op.execute(sa.text("UPDATE usage_log SET cost_source = 'openrouter' WHERE cost_source IS NULL"))
    op.alter_column("usage_log", "cost_source", existing_type=sa.String(16), nullable=False)
    op.create_check_constraint(
        "ck_usage_log_cost_source",
        "usage_log",
        "cost_source IN ('openrouter', 'estimated', 'unknown') "
        "AND (cost_source = 'unknown') = (cost IS NULL)",
    )


def downgrade() -> None:
    # The old schema has no unknown and no estimate: both go back to what it recorded for
    # a connection call, 0.
    op.drop_constraint("ck_usage_log_cost_source", "usage_log", type_="check")
    op.execute(sa.text("UPDATE usage_log SET cost = 0 WHERE cost_source <> 'openrouter'"))
    op.alter_column("usage_log", "cost", existing_type=sa.Numeric(12, 6), nullable=False)
    op.drop_column("usage_log", "cost_source")
    op.drop_column("provider_connections", "report_cost")
