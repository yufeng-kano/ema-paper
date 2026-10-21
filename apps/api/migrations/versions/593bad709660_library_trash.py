"""library trash

A folder or a document can be in the Trash: `trashed_at` on `folders` and `documents`, NULL
on every existing row, and the name constraints narrowed to the rows not in the Trash, so a
trashed `report.pdf` does not keep its folder from taking a new one (docs/database.md
§ Rules). `uq_folders_parent_name` and `uq_documents_folder_name` were constraints and
become partial unique indexes under the same names. The downgrade refuses while anything
is in the Trash, rather than choose which of two same-named rows survives.

Revision ID: 593bad709660
Revises: a19738bb7f6e
Create Date: 2026-10-21 18:01:47.484291
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "593bad709660"
down_revision: str | None = "a19738bb7f6e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("folders", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("documents", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))

    op.drop_constraint("uq_folders_parent_name", "folders", type_="unique")
    op.create_index(
        "uq_folders_parent_name",
        "folders",
        ["user_id", "parent_id", "name"],
        unique=True,
        postgresql_where=sa.text("trashed_at IS NULL"),
    )
    op.drop_index("uq_folders_root_name", table_name="folders")
    op.create_index(
        "uq_folders_root_name",
        "folders",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL AND trashed_at IS NULL"),
    )

    op.drop_constraint("uq_documents_folder_name", "documents", type_="unique")
    op.create_index(
        "uq_documents_folder_name",
        "documents",
        ["user_id", "folder_id", "name"],
        unique=True,
        postgresql_where=sa.text("trashed_at IS NULL"),
    )
    op.drop_index("uq_documents_root_name", table_name="documents")
    op.create_index(
        "uq_documents_root_name",
        "documents",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("folder_id IS NULL AND trashed_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    trashed = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM folders WHERE trashed_at IS NOT NULL)"
            " + (SELECT count(*) FROM documents WHERE trashed_at IS NOT NULL)"
        )
    ).scalar_one()
    if trashed:
        raise RuntimeError(f"{trashed} item(s) are in the Trash; empty it before downgrading")

    op.drop_index("uq_documents_root_name", table_name="documents")
    op.create_index(
        "uq_documents_root_name",
        "documents",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("folder_id IS NULL"),
    )
    op.drop_index("uq_documents_folder_name", table_name="documents")
    op.create_unique_constraint(
        "uq_documents_folder_name", "documents", ["user_id", "folder_id", "name"]
    )

    op.drop_index("uq_folders_root_name", table_name="folders")
    op.create_index(
        "uq_folders_root_name",
        "folders",
        ["user_id", "name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.drop_index("uq_folders_parent_name", table_name="folders")
    op.create_unique_constraint(
        "uq_folders_parent_name", "folders", ["user_id", "parent_id", "name"]
    )

    op.drop_column("documents", "trashed_at")
    op.drop_column("folders", "trashed_at")
