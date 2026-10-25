"""parse retention and account deletion

A finished parse no file names is deleted `PARSE_RETENTION_DAYS` after it finished
(docs/jobs.md § Retention), and its ledger entry stays: `credit_entries.job_id` no longer
cascades from the job but is set NULL, so the period's `spent` and the usage breakdown keep
agreeing (docs/billing.md § The ledger). The check that an entry has a job exactly when it
is a parse gives way to one that a chat entry has none. The indexes the purge and a job's
foreign-key actions read are added, `documents.job_id` and `usage_log.job_id`, with the
partial `trashed_at` indexes the Trash's expiry reads (docs/workspace.md § The Trash). No
row is rewritten and none is deleted: the parses already past their time go at the first
sweep after the deploy. The downgrade drops the indexes, restores the cascade and the old
check, and refuses while any parse entry has no job, rather than delete it from the
billing record (docs/database.md § Rules).

Revision ID: e014be293754
Revises: 8431924126b1
Create Date: 2026-10-25 12:33:53.346714
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e014be293754"
down_revision: str | None = "8431924126b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = "fk_credit_entries_job_id_jobs"


def upgrade() -> None:
    op.drop_constraint(FK, "credit_entries", type_="foreignkey")
    op.create_foreign_key(FK, "credit_entries", "jobs", ["job_id"], ["id"], ondelete="SET NULL")
    op.drop_constraint("ck_credit_entries_parse_job", "credit_entries", type_="check")
    op.create_check_constraint(
        "ck_credit_entries_parse_job", "credit_entries", "kind = 'parse' OR job_id IS NULL"
    )
    op.create_index("ix_documents_job_id", "documents", ["job_id"])
    op.create_index("ix_usage_log_job_id", "usage_log", ["job_id"])
    for table in ("documents", "folders"):
        op.create_index(
            f"ix_{table}_trashed_at",
            table,
            ["trashed_at"],
            postgresql_where=sa.text("trashed_at IS NOT NULL"),
            sqlite_where=sa.text("trashed_at IS NOT NULL"),
        )


def downgrade() -> None:
    orphaned = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM credit_entries WHERE kind = 'parse' AND job_id IS NULL")
        )
        .scalar()
    )
    if orphaned:
        raise RuntimeError(
            f"{orphaned} parse credit entries have no job any more; the cascade cannot be "
            "restored without deleting them from the billing record"
        )
    for table in ("documents", "folders"):
        op.drop_index(f"ix_{table}_trashed_at", table_name=table)
    op.drop_index("ix_usage_log_job_id", table_name="usage_log")
    op.drop_index("ix_documents_job_id", table_name="documents")
    op.drop_constraint("ck_credit_entries_parse_job", "credit_entries", type_="check")
    op.create_check_constraint(
        "ck_credit_entries_parse_job", "credit_entries", "(kind = 'parse') = (job_id IS NOT NULL)"
    )
    op.drop_constraint(FK, "credit_entries", type_="foreignkey")
    op.create_foreign_key(FK, "credit_entries", "jobs", ["job_id"], ["id"], ondelete="CASCADE")
