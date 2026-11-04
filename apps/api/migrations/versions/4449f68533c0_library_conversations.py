"""library conversations

A library conversation is the Files page's chat thread, about the reader's whole library
rather than one document: a `conversations` row whose `document_id` is NULL (docs/chat.md
§ Library conversations). The column loses NOT NULL — its foreign key stays ON DELETE
CASCADE, so deleting a document takes its own threads and never a library one — and the
Files page's list gets its partial index on `(user_id, updated_at)`. No row is read or
rewritten: every existing conversation keeps its document. The downgrade refuses while any
conversation has no document, rather than delete the reader's library conversations with
their messages to restore NOT NULL.

Revision ID: 4449f68533c0
Revises: cfeffbf0a598
Create Date: 2026-11-04 14:59:23.714496
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '4449f68533c0'
down_revision: str | None = 'cfeffbf0a598'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("conversations", "document_id", existing_type=sa.Integer(), nullable=True)
    op.create_index(
        "ix_conversations_library",
        "conversations",
        ["user_id", "updated_at"],
        postgresql_where=sa.text("document_id IS NULL"),
        sqlite_where=sa.text("document_id IS NULL"),
    )


def downgrade() -> None:
    library = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM conversations WHERE document_id IS NULL"))
        .scalar_one()
    )
    if library:
        raise RuntimeError(
            f"{library} library conversation(s) have no document; delete them before downgrading"
        )
    op.drop_index("ix_conversations_library", table_name="conversations")
    op.alter_column("conversations", "document_id", existing_type=sa.Integer(), nullable=False)
