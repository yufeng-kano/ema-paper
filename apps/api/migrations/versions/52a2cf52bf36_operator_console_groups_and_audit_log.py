"""operator console groups and audit log

The operator console (docs/admin.md): `groups`, which bind people to permission strings;
`group_members`, who is in which; `audit_log`, one row per change made through the
console — with their constraints and indexes, the partial unique index that keeps one
built-in group among them included. One row is written: the built-in group, `owner`,
`builtin` true, an empty permission list (it holds every permission by the flag) and no
members. The name is copied here, never imported. No existing row is read or rewritten:
nobody is an owner until an `OWNER_EMAILS` address signs in (docs/database.md § Rules).
The downgrade drops the three tables — the groups, their members and the audit record go
with them.

Revision ID: 52a2cf52bf36
Revises: fa9aedf01cfd
Create Date: 2026-10-27 13:11:38.506433
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "52a2cf52bf36"
down_revision: str | None = "fa9aedf01cfd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("permissions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("builtin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_groups_name"),
    )
    op.create_index(
        "uq_groups_builtin",
        "groups",
        ["builtin"],
        unique=True,
        postgresql_where=sa.text("builtin"),
        sqlite_where=sa.text("builtin"),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("added_by", sa.Integer(), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
    )
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"], unique=False)
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("permission", sa.String(length=32), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"], unique=False)
    op.create_index(
        "ix_audit_log_actor_created", "audit_log", ["actor_id", "created_at"], unique=False
    )
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"], unique=False)
    # The built-in group (docs/admin.md § Groups). The code creates the same row when it
    # finds none, so nothing depends on this seed.
    op.execute(
        "INSERT INTO groups (name, permissions, builtin, created_at, updated_at) "
        "VALUES ('owner', '[]'::jsonb, true, now(), now())"
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_created", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_group_members_user_id", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index("uq_groups_builtin", table_name="groups")
    op.drop_table("groups")
