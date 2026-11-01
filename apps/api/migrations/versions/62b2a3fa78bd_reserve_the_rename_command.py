"""reserve the rename command

`/rename` becomes the composer's own command, as `/new` and `/compact` are (docs/chat.md
§ Skills, § Conversations, turns, events): no table changes, and the only rows rewritten
are skills named `rename`, each renamed to its reader's next free `rename-2`, `rename-3`…,
so `/rename` never runs a skill in the title's place and the skill stays the reader's. The
downgrade changes nothing: the renamed skills keep their new names.

Revision ID: 62b2a3fa78bd
Revises: 67f4dce0969f
Create Date: 2026-11-01 18:06:28.813307
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '62b2a3fa78bd'
down_revision: str | None = '67f4dce0969f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        .where(skills.c.name == "rename")
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
    # named `rename-2` themselves.
    pass
