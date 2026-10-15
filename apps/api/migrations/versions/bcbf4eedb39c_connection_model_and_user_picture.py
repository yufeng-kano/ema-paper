"""connection model and user picture

Revision ID: bcbf4eedb39c
Revises: 3fed41b064e5
Create Date: 2026-08-23 17:00:05.105969
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bcbf4eedb39c"
down_revision: str | None = "3fed41b064e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("picture", sa.String(length=1024), nullable=True))
    op.add_column("provider_connections", sa.Column("model", sa.String(length=255), nullable=True))
    # A connection is now a profile carrying its own model. The active connection's model
    # used to live in user_settings.default_model — move it onto the connection, then
    # clear the setting: default_model is an OpenRouter-only default from here on
    # (docs/database.md).
    op.execute(
        sa.text(
            "UPDATE provider_connections SET model = user_settings.default_model "
            "FROM user_settings "
            "WHERE user_settings.default_connection_id = provider_connections.id "
            "AND user_settings.default_model IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE user_settings SET default_model = NULL WHERE default_connection_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    # Put the active connection's model back where the old code looked for it.
    op.execute(
        sa.text(
            "UPDATE user_settings SET default_model = provider_connections.model "
            "FROM provider_connections "
            "WHERE user_settings.default_connection_id = provider_connections.id "
            "AND provider_connections.model IS NOT NULL"
        )
    )
    op.drop_column("provider_connections", "model")
    op.drop_column("users", "picture")
