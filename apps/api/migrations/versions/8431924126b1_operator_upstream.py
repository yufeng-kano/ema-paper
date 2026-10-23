"""operator upstream

Step 4 of docs/billing.md § Transition: every model call runs on the operator's own Kano
Proxy key, so the stored user keys go. `kano_proxy_keys`, `kano_connect_states` and
`provider_connections` are dropped — nothing can use their ciphertext any more — with
`agent_settings.connection_id` and `fallback_model`; a `parse` or `check` row's `model`
is set to NULL (their models are the deployment's), a `chat` row keeps its pick.
`jobs.connection_id` stays as history. The downgrade recreates the tables and the
columns empty: there is no key to put back (docs/database.md § Migrations).

Revision ID: 8431924126b1
Revises: 1906f6693711
Create Date: 2026-10-23 16:59:12.974459
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8431924126b1"
down_revision: str | None = "1906f6693711"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE agent_settings SET model = NULL WHERE agent IN ('parse', 'check')")
    op.drop_column("agent_settings", "connection_id")
    op.drop_column("agent_settings", "fallback_model")
    op.drop_table("kano_connect_states")
    op.drop_table("kano_proxy_keys")
    op.drop_table("provider_connections")


def downgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("base_url", sa.String(length=1024), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("masked", sa.String(length=64), nullable=False),
        sa.Column("report_cost", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_provider_connections_user_name"),
    )
    op.create_index("ix_provider_connections_user_id", "provider_connections", ["user_id"])
    op.create_table(
        "kano_proxy_keys",
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("masked", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "kano_connect_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("state_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("verifier_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kano_connect_states_user_id", "kano_connect_states", ["user_id"])
    op.add_column(
        "agent_settings",
        sa.Column(
            "connection_id",
            sa.Integer(),
            sa.ForeignKey("provider_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_settings", sa.Column("fallback_model", sa.String(length=255), nullable=True)
    )
