"""figure check

Every page's figure boxes are checked before the page counts as succeeded (docs/parsing.md
§ Figure check): `job_pages.figures` holds the status of each box (NULL: no check). No row
is rewritten: a page kept from before it keeps no statuses, and its figures carry no
`check` (docs/database.md § Rules).

Revision ID: fe01cb0fe911
Revises: 6f91eb4561a5
Create Date: 2026-10-22 07:50:34.312571
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fe01cb0fe911"
down_revision: str | None = "6f91eb4561a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


JSON = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column("job_pages", sa.Column("figures", JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("job_pages", "figures")
