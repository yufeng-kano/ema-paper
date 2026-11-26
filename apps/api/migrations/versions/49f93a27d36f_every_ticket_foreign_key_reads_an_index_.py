"""Every ticket foreign key reads an index, and a code's pack is linked by its id

Three indexes: `ix_upload_tickets_job_id`, which a job delete's SET NULL and the parse
retention's ticket rule (`EXISTS … WHERE upload_tickets.job_id = jobs.id`) read;
`ix_upload_tickets_folder_id`, which a folder delete's SET NULL reads; and
`ix_document_tickets_document_id`, which a document delete's cascade reads. The two on
`upload_tickets` are partial (`IS NOT NULL`): a foreign-key action's `col = $1` implies
the predicate. `credit_packs.redemption_id` goes with its unique and its foreign key: a
code's pack is `redeem-<redemption id>`, so its id carries the link. No row is rewritten.
The downgrade restores the structure and refills `redemption_id` from the id of every code
pack whose redemption still exists.

Revision ID: 49f93a27d36f
Revises: 8ca4f1915c14
Create Date: 2026-11-24 19:24:24.743117
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "49f93a27d36f"
down_revision: str | None = "8ca4f1915c14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The partial indexes on upload_tickets: (name, column).
PARTIAL_INDEXES = (
    ("ix_upload_tickets_job_id", "job_id"),
    ("ix_upload_tickets_folder_id", "folder_id"),
)


def upgrade() -> None:
    op.drop_constraint("uq_credit_packs_redemption_id", "credit_packs", type_="unique")
    op.drop_constraint("fk_credit_packs_redemption_id", "credit_packs", type_="foreignkey")
    op.drop_column("credit_packs", "redemption_id")
    op.create_index("ix_document_tickets_document_id", "document_tickets", ["document_id"])
    for name, column in PARTIAL_INDEXES:
        op.create_index(
            name,
            "upload_tickets",
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )


def downgrade() -> None:
    for name, _column in PARTIAL_INDEXES:
        op.drop_index(name, table_name="upload_tickets")
    op.drop_index("ix_document_tickets_document_id", table_name="document_tickets")
    op.add_column("credit_packs", sa.Column("redemption_id", sa.Integer(), nullable=True))
    # A code's pack is `redeem-<redemption id>`. A pack whose redemption is gone keeps
    # NULL, as the foreign key's SET NULL left it.
    op.execute(
        "UPDATE credit_packs SET redemption_id = redemptions.id FROM redemptions "
        "WHERE credit_packs.source = 'code' AND credit_packs.id = 'redeem-' || redemptions.id"
    )
    op.create_unique_constraint("uq_credit_packs_redemption_id", "credit_packs", ["redemption_id"])
    op.create_foreign_key(
        "fk_credit_packs_redemption_id",
        "credit_packs",
        "redemptions",
        ["redemption_id"],
        ["id"],
        ondelete="SET NULL",
    )
