"""credit packs from console grants

An operator's credit grant from the console is a pack like a redeem code's (docs/billing.md
§ Operator grants, docs/admin.md § Users and grants): the check on `credit_packs.source`
widens from `pay` and `code` to `pay`, `code` and `grant`. No row is read or rewritten
(docs/database.md § Rules). The downgrade narrows it back and refuses while any pack has
`source = grant`, naming the count, rather than delete granted packs from the billing
record or relabel them as something they are not.

Revision ID: d632652e713f
Revises: 52a2cf52bf36
Create Date: 2026-10-27 15:20:31.222991
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d632652e713f"
down_revision: str | None = "52a2cf52bf36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CHECK = "ck_credit_packs_source"


def upgrade() -> None:
    op.drop_constraint(CHECK, "credit_packs", type_="check")
    op.create_check_constraint(CHECK, "credit_packs", "source IN ('pay', 'code', 'grant')")


def downgrade() -> None:
    granted = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM credit_packs WHERE source = 'grant'"))
        .scalar()
    )
    if granted:
        raise RuntimeError(
            f"{granted} credit pack(s) were granted from the operator console; the check "
            "cannot be narrowed without deleting them from the billing record"
        )
    op.drop_constraint(CHECK, "credit_packs", type_="check")
    op.create_check_constraint(CHECK, "credit_packs", "source IN ('pay', 'code')")
