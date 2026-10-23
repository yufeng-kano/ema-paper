"""credit entries outlive turns

`credit_entries.turn_id` no longer cascades from the turn: a conversation the reader
deletes takes its turns and leaves their entries behind, with `turn_id` NULL, so the
period's `spent` and the usage breakdown keep agreeing (docs/billing.md § The ledger).
The check that a chat entry has a turn gives way to one that a parse entry has none.
No row is rewritten. The downgrade restores the cascade and refuses while any chat
entry has no turn, rather than delete it from the billing record.

Revision ID: 1906f6693711
Revises: bd4210aa38cf
Create Date: 2026-10-23 15:24:04.161691
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1906f6693711"
down_revision: str | None = "bd4210aa38cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK = "fk_credit_entries_turn_id_chat_turns"


def upgrade() -> None:
    op.drop_constraint(FK, "credit_entries", type_="foreignkey")
    op.create_foreign_key(
        FK, "credit_entries", "chat_turns", ["turn_id"], ["id"], ondelete="SET NULL"
    )
    op.drop_constraint("ck_credit_entries_chat_turn", "credit_entries", type_="check")
    op.create_check_constraint(
        "ck_credit_entries_turn_kind", "credit_entries", "kind = 'chat' OR turn_id IS NULL"
    )


def downgrade() -> None:
    orphaned = (
        op.get_bind()
        .execute(
            sa.text("SELECT count(*) FROM credit_entries WHERE kind = 'chat' AND turn_id IS NULL")
        )
        .scalar()
    )
    if orphaned:
        raise RuntimeError(
            f"{orphaned} chat credit entries have no turn any more; the cascade cannot be "
            "restored without deleting them from the billing record"
        )
    op.drop_constraint("ck_credit_entries_turn_kind", "credit_entries", type_="check")
    op.create_check_constraint(
        "ck_credit_entries_chat_turn", "credit_entries", "(kind = 'chat') = (turn_id IS NOT NULL)"
    )
    op.drop_constraint(FK, "credit_entries", type_="foreignkey")
    op.create_foreign_key(
        FK, "credit_entries", "chat_turns", ["turn_id"], ["id"], ondelete="CASCADE"
    )
