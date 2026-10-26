"""conversation compaction and memory file

Compaction and the memory file (docs/chat.md § Compaction, § Memory): `chat_turns.kind`
(`message` on every existing row), `agent_settings.idle_compact` (`true` on every row —
idle compaction is on unless the reader turns it off), and on `conversations` the nullable
`memory_file` (the snapshot of memory.md a turn takes), `compacted_message_id` (the summary
row the replay starts from — no foreign key, since a message goes only with its
conversation), `context_tokens` and `cache_warm_until`
(what the last model call replayed and when the cache it touched expires), with the partial
index the idle compaction pass reads. The only rows rewritten are skills named `new` or
`compact`, the composer's own commands from this version on (docs/chat.md § Skills): each is
renamed to the next free `name-2`, `name-3`…, so `/compact` never runs a skill in the
runner's place and the skill stays the reader's. Otherwise no row is read or rewritten: an
open conversation goes on as it was, takes its memory file snapshot at its next turn, and
is first measured by its next model call. The downgrade drops the columns and the index
and leaves the new names; a conversation compacted meanwhile then replays its whole history
again, the summary row among it.

Revision ID: d78cf7a42eea
Revises: 8d1142bbd272
Create Date: 2026-10-26 19:12:16.340991
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d78cf7a42eea"
down_revision: str | None = "8d1142bbd272"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_turns",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="message"),
    )
    op.add_column(
        "agent_settings",
        sa.Column("idle_compact", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("conversations", sa.Column("memory_file", sa.Text(), nullable=True))
    op.add_column("conversations", sa.Column("compacted_message_id", sa.Integer(), nullable=True))
    op.add_column("conversations", sa.Column("context_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "conversations", sa.Column("cache_warm_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_conversations_cache_warm_until",
        "conversations",
        ["cache_warm_until"],
        unique=False,
        postgresql_where=sa.text("cache_warm_until IS NOT NULL"),
        sqlite_where=sa.text("cache_warm_until IS NOT NULL"),
    )
    # Skills named after the composer's own commands, reserved from this version on: the
    # next free `name-2`, `name-3`… of that reader (the names, not the app's constant, so
    # the migration reads the same whatever the code says later).
    skills = sa.table(
        "skills",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("name", sa.String),
    )
    bind = op.get_bind()
    clashing = bind.execute(
        sa.select(skills.c.id, skills.c.user_id, skills.c.name)
        .where(skills.c.name.in_(["compact", "new"]))
        .order_by(skills.c.id)
    ).all()
    for skill_id, user_id, name in clashing:
        taken = set(
            bind.execute(sa.select(skills.c.name).where(skills.c.user_id == user_id)).scalars()
        )
        suffix = 2
        while f"{name}-{suffix}" in taken:
            suffix += 1
        bind.execute(
            sa.update(skills).where(skills.c.id == skill_id).values(name=f"{name}-{suffix}")
        )


def downgrade() -> None:
    op.drop_index("ix_conversations_cache_warm_until", table_name="conversations")
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("cache_warm_until")
        batch.drop_column("context_tokens")
        batch.drop_column("compacted_message_id")
        batch.drop_column("memory_file")
    with op.batch_alter_table("agent_settings") as batch:
        batch.drop_column("idle_compact")
    with op.batch_alter_table("chat_turns") as batch:
        batch.drop_column("kind")
