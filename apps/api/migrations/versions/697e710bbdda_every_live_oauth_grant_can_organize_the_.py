"""Every live OAuth grant can organize the library

A grant whose scope lacks `organize` (one approved before the scope existed: `parse
library`, or older) is revoked, so its connector is answered 401 and is approved again
through the consent page, which now says it can make folders, move, rename and copy items,
and move them to the Trash (docs/auth.md § 4); no grant is widened without it. Data only:
no table changes.

Revision ID: 697e710bbdda
Revises: 49f93a27d36f
Create Date: 2026-11-26 05:31:40.020945
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "697e710bbdda"
down_revision: str | None = "49f93a27d36f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Codes, access and refresh rows alike: each carries its grant's scope, and '' counts as
    # lacking `organize` too.
    op.execute(
        "UPDATE oauth_grants SET revoked_at = now() "
        "WHERE revoked_at IS NULL AND ' ' || scope || ' ' NOT LIKE '% organize %'"
    )


def downgrade() -> None:
    # A revoked grant stays revoked: its connector was approved again meanwhile, or will be.
    pass
