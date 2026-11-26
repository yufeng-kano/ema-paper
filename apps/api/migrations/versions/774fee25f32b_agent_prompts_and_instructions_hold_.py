"""agent prompts and instructions hold what presets held

Drops `prompt_presets` and `agent_settings.prompt_preset_id` (docs/database.md § Tables),
first moving every preset still in force to where the agent keeps reading it, so no
agent's prompt changes what it says:

- `parse` and `check`: a preset replaced the template whole wherever the row has no
  prompt of its own, so its text becomes the row's `prompt`, verbatim — the same text,
  the same `prompt_sha256` in the dedup key.
- `chat`: a non-blank preset was a system block of its own, sent verbatim between the
  prompt and the instructions wherever the row has no prompt of its own. Its text leads
  the row's `instructions` — a blank line, then the instructions the reader had — which
  are also sent verbatim, with no token substitution. The value is not capped:
  `CHAT_INSTRUCTIONS_MAX_CHARS` applies to writes (docs/chat.md § Instructions).

A row with a prompt of its own never ran its preset, and an unselected preset ran
nowhere; neither is moved. The downgrade restores the table and the column, empty: the
moved texts stay where the upgrade put them, which the code before this revision reads
the same way.

Revision ID: 774fee25f32b
Revises: 4449f68533c0
Create Date: 2026-11-24 12:39:30.124423
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "774fee25f32b"
down_revision: str | None = "4449f68533c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_settings AS s SET prompt = p.text FROM prompt_presets AS p "
        "WHERE s.prompt_preset_id = p.id AND s.agent IN ('parse', 'check') "
        "AND COALESCE(s.prompt, '') = ''"
    )
    op.execute(
        "UPDATE agent_settings AS s SET instructions = p.text || CASE "
        "WHEN s.instructions ~ '\\S' THEN E'\\n\\n' || s.instructions ELSE '' END "
        "FROM prompt_presets AS p "
        "WHERE s.prompt_preset_id = p.id AND s.agent = 'chat' "
        "AND COALESCE(s.prompt, '') = '' AND p.text ~ '\\S'"
    )
    op.drop_constraint(
        op.f("agent_settings_prompt_preset_id_fkey"), "agent_settings", type_="foreignkey"
    )
    op.drop_column("agent_settings", "prompt_preset_id")
    op.drop_index(op.f("ix_prompt_presets_user_id"), table_name="prompt_presets")
    op.drop_table("prompt_presets")


def downgrade() -> None:
    op.create_table(
        "prompt_presets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_prompt_presets_user_name"),
    )
    op.create_index(op.f("ix_prompt_presets_user_id"), "prompt_presets", ["user_id"], unique=False)
    op.add_column("agent_settings", sa.Column("prompt_preset_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("agent_settings_prompt_preset_id_fkey"),
        "agent_settings",
        "prompt_presets",
        ["prompt_preset_id"],
        ["id"],
        ondelete="SET NULL",
    )
