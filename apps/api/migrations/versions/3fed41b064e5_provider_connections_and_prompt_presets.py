"""provider connections and prompt presets

Revision ID: 3fed41b064e5
Revises: aab99edc7034
Create Date: 2026-08-22 17:59:46.433012

Adds user-defined OpenAI-compatible upstreams (`provider_connections`) and named
transcription prompts (`prompt_presets`), per docs/database.md. Each stored custom
`user_settings.system_prompt` becomes a preset named "Custom prompt" pointed at by
`prompt_preset_id`, so behavior does not change for anyone; the column then goes away.
Jobs record the upstream they ran on (`connection_id`, NULL = OpenRouter), which also
joins the dedup key — the same model id on two endpoints is not the same model.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3fed41b064e5"
down_revision: str | None = "aab99edc7034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEDUP_WHERE = sa.text("status = 'succeeded'")
MIGRATED_PRESET_NAME = "Custom prompt"


def upgrade() -> None:
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
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_provider_connections_user_name"),
    )
    op.create_index("ix_provider_connections_user_id", "provider_connections", ["user_id"])

    op.create_table(
        "prompt_presets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_prompt_presets_user_name"),
    )
    op.create_index("ix_prompt_presets_user_id", "prompt_presets", ["user_id"])

    op.add_column(
        "user_settings",
        sa.Column(
            "default_connection_id",
            sa.Integer(),
            sa.ForeignKey("provider_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "prompt_preset_id",
            sa.Integer(),
            sa.ForeignKey("prompt_presets.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Every stored custom prompt becomes a preset the settings row points at, so the
    # selected prompt survives the column's removal unchanged (docs/database.md § Rules).
    op.execute(
        sa.text(
            """
            INSERT INTO prompt_presets (user_id, name, text, created_at, updated_at)
            SELECT user_id, :name, system_prompt, now(), now()
            FROM user_settings WHERE system_prompt IS NOT NULL
            """
        ).bindparams(name=MIGRATED_PRESET_NAME)
    )
    op.execute(
        sa.text(
            """
            UPDATE user_settings SET prompt_preset_id = (
              SELECT id FROM prompt_presets p
              WHERE p.user_id = user_settings.user_id AND p.name = :name
            )
            WHERE system_prompt IS NOT NULL
            """
        ).bindparams(name=MIGRATED_PRESET_NAME)
    )
    op.drop_column("user_settings", "system_prompt")

    # No FK on purpose: provider identity is immutable job history. SET NULL would
    # relabel a deleted connection's jobs as OpenRouter jobs (wrong key for queued ones,
    # cross-upstream dedup hits for succeeded ones); RESTRICT would make connections
    # undeletable once any job exists. The worker fails a job whose connection is gone.
    op.add_column("jobs", sa.Column("connection_id", sa.Integer(), nullable=True))
    # Endpoint snapshot at enqueue: editing a connection's URL must not let results from
    # the old endpoint answer uploads aimed at the new one (docs/jobs.md § Dedup).
    op.add_column("jobs", sa.Column("connection_base_url", sa.String(length=1024), nullable=True))
    op.drop_index("ix_jobs_dedup", table_name="jobs")
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


def downgrade() -> None:
    op.drop_index("ix_jobs_dedup", table_name="jobs")
    op.create_index(
        "ix_jobs_dedup",
        "jobs",
        [
            "user_id",
            "sha256",
            "model",
            "profile",
            "profile_version",
            "pages_spec",
            "prompt_sha256",
            "pipeline_version",
        ],
        postgresql_where=DEDUP_WHERE,
        sqlite_where=DEDUP_WHERE,
    )
    op.drop_column("jobs", "connection_base_url")
    op.drop_column("jobs", "connection_id")

    op.add_column("user_settings", sa.Column("system_prompt", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE user_settings SET system_prompt = (
          SELECT text FROM prompt_presets WHERE prompt_presets.id = prompt_preset_id
        )
        WHERE prompt_preset_id IS NOT NULL
        """
    )
    op.drop_column("user_settings", "prompt_preset_id")
    op.drop_column("user_settings", "default_connection_id")

    op.drop_index("ix_prompt_presets_user_id", table_name="prompt_presets")
    op.drop_table("prompt_presets")
    op.drop_index("ix_provider_connections_user_id", table_name="provider_connections")
    op.drop_table("provider_connections")
