"""job page markdown

Each finished page's transcription is stored on its `job_pages` row, so a running job has
a readable partial result (docs/jobs.md § Progress). NULL on failed pages and on rows
written before this column existed.

Revision ID: cd77f4fe600f
Revises: 5192e8e8b401
Create Date: 2026-08-25 03:37:19.286570
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd77f4fe600f"
down_revision: str | None = "5192e8e8b401"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_pages", sa.Column("markdown", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("job_pages", "markdown")
