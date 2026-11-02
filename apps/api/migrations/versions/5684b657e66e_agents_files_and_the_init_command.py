"""agents files and the init command

`conversations.agents_file` is the snapshot of the AGENTS.md files on the document's path
a conversation's first turn takes, taken again by a compaction (docs/chat.md § AGENTS.md):
nullable text, NULL on every existing row — a conversation already open takes its snapshot
at its next compaction, a new one at its first turn. And `/init` becomes the composer's own
command, as `/rename` did (docs/chat.md § Skills): the only rows rewritten are skills named
`init`, each renamed to its reader's next free `init-2`, `init-3`…, so `/init` never runs a
skill in the instruction's place and the skill stays the reader's. The downgrade drops the
column; the renamed skills keep their new names.

Revision ID: 5684b657e66e
Revises: 62b2a3fa78bd
Create Date: 2026-11-02 05:21:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '5684b657e66e'
down_revision: str | None = '62b2a3fa78bd'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("agents_file", sa.Text(), nullable=True))
    # The name, not the app's constant, so the migration reads the same whatever the code
    # says later.
    skills = sa.table(
        "skills",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("name", sa.String),
    )
    bind = op.get_bind()
    clashing = bind.execute(
        sa.select(skills.c.id, skills.c.user_id, skills.c.name)
        .where(skills.c.name == "init")
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
    # The renamed skills keep their new names: nothing tells them from a skill the reader
    # named `init-2` themselves.
    op.drop_column("conversations", "agents_file")
