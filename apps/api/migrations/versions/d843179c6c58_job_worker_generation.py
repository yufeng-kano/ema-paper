"""job worker generation

Revision ID: d843179c6c58
Revises: c8f28f7cd45e
Create Date: 2026-10-16 08:13:02.976223
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d843179c6c58"
down_revision: str | None = "c8f28f7cd45e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The fence a worker's writes to a running job carry (docs/jobs.md § Queue).
    op.add_column("jobs", sa.Column("worker_generation", sa.BigInteger(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE SEQUENCE IF NOT EXISTS worker_generation_seq")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP SEQUENCE IF EXISTS worker_generation_seq")
    op.drop_column("jobs", "worker_generation")
