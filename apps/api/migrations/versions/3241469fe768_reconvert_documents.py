"""reconvert documents

A library document can be parsed again from its original while it keeps the version it has
(docs/jobs.md § Re-converting a document): `documents.reconvert_job_id` names the re-convert
queued, running or failed (NULL: none). No row is rewritten. The downgrade drops the column
and its index: a re-convert still running then finishes as a parse in History that no
document takes (docs/database.md § Rules).

Revision ID: 3241469fe768
Revises: fe01cb0fe911
Create Date: 2026-10-22 08:34:30.340905
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3241469fe768"
down_revision: str | None = "fe01cb0fe911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "reconvert_job_id",
            sa.Uuid(),
            sa.ForeignKey(
                "jobs.id", name="fk_documents_reconvert_job_id_jobs", ondelete="SET NULL"
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_documents_reconvert_job_id",
        "documents",
        ["reconvert_job_id"],
        postgresql_where=sa.text("reconvert_job_id IS NOT NULL"),
        sqlite_where=sa.text("reconvert_job_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_reconvert_job_id", table_name="documents")
    op.drop_column("documents", "reconvert_job_id")
