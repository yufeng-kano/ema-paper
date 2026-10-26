"""credit packs

Top-up packs (docs/billing.md § Top-up packs): `credit_packs`, a pack bought on Pro and
written from Pay's canonical purchase; `credit_pack_entries`, how much of an entry's amount
each pack covered; `pack_checkouts`, the one pack checkout a user may have open. All three
empty, with their constraints and indexes (docs/database.md § Rules). No existing row is
read or rewritten. The downgrade drops the three tables and nothing else — packs already
bought are lost with them.

Revision ID: 148254b2f3d9
Revises: 283c259880d3
Create Date: 2026-10-26 10:21:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "148254b2f3d9"
down_revision: str | None = "283c259880d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "credit_packs",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("pack_id", sa.String(length=64), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("spent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("credits >= 0", name="ck_credit_packs_credits"),
        sa.CheckConstraint("spent >= 0", name="ck_credit_packs_spent"),
        sa.CheckConstraint("expires_at > purchased_at", name="ck_credit_packs_validity"),
        sa.CheckConstraint("status IN ('paid', 'refunded')", name="ck_credit_packs_status"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_credit_packs_user_expires", "credit_packs", ["user_id", "expires_at"], unique=False
    )
    op.create_table(
        "credit_pack_entries",
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("pack_id", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.CheckConstraint("amount >= 0", name="ck_credit_pack_entries_amount"),
        sa.ForeignKeyConstraint(["entry_id"], ["credit_entries.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pack_id"], ["credit_packs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("entry_id", "pack_id"),
    )
    op.create_table(
        "pack_checkouts",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("pack_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("purchase_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("purchase_id"),
    )


def downgrade() -> None:
    op.drop_table("pack_checkouts")
    op.drop_table("credit_pack_entries")
    op.drop_index("ix_credit_packs_user_expires", table_name="credit_packs")
    op.drop_table("credit_packs")
