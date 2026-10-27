"""redeem codes

Redeem codes (docs/billing.md § Redeem codes): `redeem_codes`, the codes the operator hands
out; `redemptions`, each user's use of one; `plan_grants`, the bank of granted days and
its open period — all three empty, with their constraints and indexes. `credit_packs`
grows `source` (`pay` for every existing pack) and `redemption_id`; `users` grows
`redeem_failures` (0 for everyone) and `redeem_failed_at`. Nothing else is read or
rewritten (docs/database.md § Rules). The downgrade drops the three tables and the four
columns — codes and their redemptions are lost with them, and a redeemed pack's row
stays as a bought one's.

Revision ID: fa9aedf01cfd
Revises: d78cf7a42eea
Create Date: 2026-10-27 04:42:55.245569
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa9aedf01cfd"
down_revision: str | None = "d78cf7a42eea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "redeem_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=True),
        sa.Column("valid_days", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("max_uses", sa.Integer(), server_default="1", nullable=False),
        sa.Column("uses", sa.Integer(), server_default="0", nullable=False),
        sa.Column("redeem_by", sa.DateTime(timezone=True), nullable=True),
        sa.Column("label", sa.String(length=255), server_default="", nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('credits', 'plan')", name="ck_redeem_codes_kind"),
        sa.CheckConstraint("max_uses >= 1", name="ck_redeem_codes_max_uses"),
        sa.CheckConstraint("uses >= 0", name="ck_redeem_codes_uses"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "redemptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=True),
        sa.Column("plan_id", sa.String(length=64), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["code_id"], ["redeem_codes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_id", "user_id", name="uq_redemptions_code_user"),
    )
    op.create_index("ix_redemptions_user_id", "redemptions", ["user_id"], unique=False)
    op.create_table(
        "plan_grants",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.String(length=64), nullable=False),
        sa.Column("days_banked", sa.Integer(), server_default="0", nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("days_banked >= 0", name="ck_plan_grants_days"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "credit_packs",
        sa.Column("source", sa.String(length=8), server_default="pay", nullable=False),
    )
    op.add_column("credit_packs", sa.Column("redemption_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_credit_packs_source", "credit_packs", "source IN ('pay', 'code')"
    )
    op.create_unique_constraint("uq_credit_packs_redemption_id", "credit_packs", ["redemption_id"])
    op.create_foreign_key(
        "fk_credit_packs_redemption_id",
        "credit_packs",
        "redemptions",
        ["redemption_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "users", sa.Column("redeem_failures", sa.Integer(), server_default="0", nullable=False)
    )
    op.add_column("users", sa.Column("redeem_failed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "redeem_failed_at")
    op.drop_column("users", "redeem_failures")
    op.drop_constraint("fk_credit_packs_redemption_id", "credit_packs", type_="foreignkey")
    op.drop_constraint("uq_credit_packs_redemption_id", "credit_packs", type_="unique")
    op.drop_constraint("ck_credit_packs_source", "credit_packs", type_="check")
    op.drop_column("credit_packs", "redemption_id")
    op.drop_column("credit_packs", "source")
    op.drop_table("plan_grants")
    op.drop_index("ix_redemptions_user_id", table_name="redemptions")
    op.drop_table("redemptions")
    op.drop_table("redeem_codes")
