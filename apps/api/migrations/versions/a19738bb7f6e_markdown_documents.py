"""markdown documents

A document may have no parse behind it: `documents.job_id` drops NOT NULL, and a row
without a job is a markdown document — uploaded as `.md` or written new (docs/workspace.md
§ Markdown documents). Every existing row keeps its job; no row is rewritten. The
downgrade refuses while any document has no job, rather than delete the reader's own
documents and their conversations to restore the constraint (docs/database.md § Rules).

Revision ID: a19738bb7f6e
Revises: 09bcc2e96c63
Create Date: 2026-10-21 12:40:26.469105
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a19738bb7f6e"
down_revision: str | None = "09bcc2e96c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("documents", "job_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    jobless = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM documents WHERE job_id IS NULL"))
        .scalar_one()
    )
    if jobless:
        raise RuntimeError(
            f"{jobless} markdown document(s) have no job; delete them before downgrading"
        )
    op.alter_column("documents", "job_id", existing_type=sa.Uuid(), nullable=False)
