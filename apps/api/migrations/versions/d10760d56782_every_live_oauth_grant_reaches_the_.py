"""Every live OAuth grant reaches the library

A grant whose scope lacks `library` (one approved before the scope existed, `parse` alone)
is revoked, so its connector is answered 401 and is approved again through the consent page
(docs/auth.md § 4); no grant is widened without it. An unspent upload ticket minted under
such a grant is deleted, since every remaining ticket now reaches the library; a spent one
only reads its own job and stays. Then `upload_tickets.library_access` goes.

Revision ID: d10760d56782
Revises: 185b2581586c
Create Date: 2026-11-24 12:59:13.885602
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d10760d56782"
down_revision: str | None = "185b2581586c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Codes, access and refresh rows alike: each carries its grant's scope, and '' counts as
    # lacking `library` too.
    op.execute(
        "UPDATE oauth_grants SET revoked_at = now() "
        "WHERE revoked_at IS NULL AND ' ' || scope || ' ' NOT LIKE '% library %'"
    )
    op.execute("DELETE FROM upload_tickets WHERE spent_at IS NULL AND library_access = false")
    op.drop_column("upload_tickets", "library_access")


def downgrade() -> None:
    op.add_column(
        "upload_tickets",
        sa.Column("library_access", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    # Every ticket left was minted under a grant or a key that reaches the library.
    op.execute("UPDATE upload_tickets SET library_access = true")
