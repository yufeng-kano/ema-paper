"""subscriptions and credits

The billing tables (docs/billing.md § The ledger): the user's Kano Pay subscription, the
one checkout they may have open, the notifications already processed, and the credit
ledger — one row per allowance period and one entry per parse or turn. All five are
created empty; no existing row is read or rewritten, since every user is on Free without
a row. The downgrade drops the five tables and nothing else (docs/database.md § Rules).

Revision ID: bd4210aa38cf
Revises: 3241469fe768
Create Date: 2026-10-23 13:58:12.671404
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bd4210aa38cf"
down_revision: str | None = "3241469fe768"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_subscriptions_user_id_users", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("subscription_id", sa.String(255), nullable=False, unique=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("period_end > period_start", name="ck_subscriptions_period"),
    )
    op.create_table(
        "checkouts",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_checkouts_user_id_users", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("plan_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("transaction_id", sa.String(255), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "pay_events",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_pay_events_user_id_users", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "credit_periods",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_credit_periods_user_id_users", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan_id", sa.String(64), nullable=True),
        sa.Column("granted", sa.Integer(), nullable=False),
        sa.Column("spent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("period_end > period_start", name="ck_credit_periods_period"),
        sa.CheckConstraint("granted >= 0", name="ck_credit_periods_granted"),
    )
    op.create_table(
        "credit_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_credit_entries_user_id_users", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("state", sa.String(8), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", name="fk_credit_entries_job_id_jobs", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "turn_id",
            sa.Uuid(),
            sa.ForeignKey(
                "chat_turns.id", name="fk_credit_entries_turn_id_chat_turns", ondelete="CASCADE"
            ),
            nullable=True,
            unique=True,
        ),
        sa.Column("model", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount >= 0", name="ck_credit_entries_amount"),
        sa.CheckConstraint("kind IN ('parse', 'chat')", name="ck_credit_entries_kind"),
        sa.CheckConstraint(
            "state IN ('reserved', 'consumed', 'released')", name="ck_credit_entries_state"
        ),
        sa.CheckConstraint(
            "(kind = 'parse') = (job_id IS NOT NULL)", name="ck_credit_entries_parse_job"
        ),
        sa.CheckConstraint(
            "(kind = 'chat') = (turn_id IS NOT NULL)", name="ck_credit_entries_chat_turn"
        ),
    )
    op.create_index("ix_credit_entries_user_created", "credit_entries", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_credit_entries_user_created", table_name="credit_entries")
    op.drop_table("credit_entries")
    op.drop_table("credit_periods")
    op.drop_table("pay_events")
    op.drop_table("checkouts")
    op.drop_table("subscriptions")
