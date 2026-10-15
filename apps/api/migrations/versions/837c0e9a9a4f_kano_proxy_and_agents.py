"""kano proxy and agents

Revision ID: 837c0e9a9a4f
Revises: 5f1845730aa0
Create Date: 2026-10-15 19:02:32.384034

Kano Proxy replaces OpenRouter as the built-in upstream, and settings move from one
per-user row to one row per user and agent (docs/agents.md, docs/database.md § Rules):

- `kano_proxy_keys` (one per user) and `kano_connect_states` (pending connects) appear;
  `openrouter_keys` goes — the stored OpenRouter keys are not migrated, nothing can use
  them.
- `prompt_presets.agent` and `jobs.agent` appear, every existing row `parse`;
  `jobs.source_job_id` appears, NULL everywhere.
- Each `user_settings` row that selected anything becomes `agent_settings('parse')`: a
  connection user keeps the connection with the model it carried, everyone keeps the
  selected prompt preset, and an OpenRouter user lands on Kano Proxy (connection NULL)
  with no model. Then `user_settings` and `provider_connections.model` go.
- `jobs.profile` / `jobs.profile_version` go, and the dedup index trades them for
  `agent`. A historical job's profile stays readable in its result's `meta.profile`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '837c0e9a9a4f'
down_revision: str | None = '5f1845730aa0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEDUP_WHERE = sa.text("status = 'succeeded'")


def upgrade() -> None:
    op.create_table(
        "kano_proxy_keys",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
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

    op.create_table(
        "agent_settings",
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column(
            "connection_id",
            sa.Integer(),
            sa.ForeignKey("provider_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column(
            "prompt_preset_id",
            sa.Integer(),
            sa.ForeignKey("prompt_presets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("user_id", "agent"),
    )

    # Every existing preset and job belongs to the one agent there is. The server default
    # only backfills; new rows always name their agent.
    for table in ("prompt_presets", "jobs"):
        op.add_column(
            table,
            sa.Column("agent", sa.String(length=32), nullable=False, server_default="parse"),
        )
        op.alter_column(table, "agent", server_default=None)
    op.add_column("jobs", sa.Column("source_job_id", sa.Uuid(), nullable=True))

    # A connection user keeps the connection and the model it carried; an OpenRouter user
    # gets Kano Proxy (NULL) and no model. The prompt preset carries over either way.
    op.execute(
        sa.text(
            "INSERT INTO agent_settings (user_id, agent, connection_id, model, prompt_preset_id) "
            "SELECT us.user_id, 'parse', us.default_connection_id, pc.model, "
            "us.prompt_preset_id "
            "FROM user_settings us "
            "LEFT JOIN provider_connections pc ON pc.id = us.default_connection_id "
            "WHERE us.default_connection_id IS NOT NULL OR us.prompt_preset_id IS NOT NULL"
        )
    )
    op.drop_table("user_settings")
    op.drop_column("provider_connections", "model")
    op.drop_table("openrouter_keys")

    op.drop_index("ix_jobs_dedup", table_name="jobs")
    op.drop_column("jobs", "profile")
    op.drop_column("jobs", "profile_version")
    op.create_index(
        "ix_jobs_dedup",
        "jobs",
        [
            "user_id",
            "agent",
            "sha256",
            "model",
            "connection_id",
            "pages_spec",
            "prompt_sha256",
            "pipeline_version",
        ],
        postgresql_where=DEDUP_WHERE,
        sqlite_where=DEDUP_WHERE,
    )


def downgrade() -> None:
    # The old schema again, with what it can hold: profiles come back empty, OpenRouter
    # keys come back empty, and the parse agent's settings become user_settings again —
    # the connection's model back on the connection.
    op.drop_index("ix_jobs_dedup", table_name="jobs")
    op.add_column(
        "jobs",
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("jobs", "profile_version", server_default=None)
    op.add_column("jobs", sa.Column("profile", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_jobs_dedup",
        "jobs",
        [
            "user_id",
            "sha256",
            "model",
            "connection_id",
            "profile",
            "profile_version",
            "pages_spec",
            "prompt_sha256",
            "pipeline_version",
        ],
        postgresql_where=DEDUP_WHERE,
        sqlite_where=DEDUP_WHERE,
    )

    op.create_table(
        "openrouter_keys",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("masked", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.add_column(
        "provider_connections", sa.Column("model", sa.String(length=255), nullable=True)
    )
    op.execute(
        sa.text(
            "UPDATE provider_connections SET model = agent_settings.model "
            "FROM agent_settings WHERE agent_settings.connection_id = provider_connections.id "
            "AND agent_settings.agent = 'parse' AND agent_settings.model IS NOT NULL"
        )
    )
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("default_model", sa.String(length=255), nullable=True),
        sa.Column("default_profile", sa.String(length=64), nullable=True),
        sa.Column(
            "default_connection_id",
            sa.Integer(),
            sa.ForeignKey("provider_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "prompt_preset_id",
            sa.Integer(),
            sa.ForeignKey("prompt_presets.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.execute(
        sa.text(
            "INSERT INTO user_settings (user_id, default_connection_id, prompt_preset_id) "
            "SELECT user_id, connection_id, prompt_preset_id FROM agent_settings "
            "WHERE agent = 'parse'"
        )
    )

    op.drop_column("jobs", "source_job_id")
    op.drop_column("jobs", "agent")
    op.drop_column("prompt_presets", "agent")
    op.drop_table("agent_settings")
    op.drop_index("ix_kano_connect_states_user_id", table_name="kano_connect_states")
    op.drop_table("kano_connect_states")
    op.drop_table("kano_proxy_keys")
