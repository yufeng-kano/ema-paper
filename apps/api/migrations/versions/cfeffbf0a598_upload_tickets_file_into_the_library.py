"""upload tickets file into the library

Where a connector parse goes (docs/mcp.md § Filing into Files, docs/database.md): three
columns on `upload_tickets` — whether the minting grant reached the library, whether the
upload is filed into Files, and the folder it is filed into (NULL the top level, set NULL
when the folder is deleted for good). Every existing ticket reads as one that files
nothing, which is what it was minted as; nothing else is read or rewritten. The downgrade
drops the three columns.

Revision ID: cfeffbf0a598
Revises: 5684b657e66e
Create Date: 2026-11-04 13:26:03.190296
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cfeffbf0a598"
down_revision: str | None = "5684b657e66e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_tickets",
        sa.Column("library_access", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "upload_tickets",
        sa.Column("into_library", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("upload_tickets", sa.Column("folder_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_upload_tickets_folder_id_folders",
        "upload_tickets",
        "folders",
        ["folder_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_upload_tickets_folder_id_folders", "upload_tickets", type_="foreignkey")
    op.drop_column("upload_tickets", "folder_id")
    op.drop_column("upload_tickets", "into_library")
    op.drop_column("upload_tickets", "library_access")
