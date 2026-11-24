"""jobs carry no provider connection or source job and always a prompt

Drops `jobs.connection_id` and `jobs.source_job_id`, and makes `jobs.prompt` NOT NULL
(docs/database.md § Tables, § Rules):

- A job of a provider connection loses its endpoint snapshot first: `connection_base_url`
  becomes NULL wherever `connection_id` is set, as an OpenRouter job's is, so its result
  never answers a dedup lookup and the worker fails it when it takes it rather than run it
  on Kano Proxy. A queued or running one keeps its status: the worker fails it through its
  finish, which releases its credits — a status written here would leave them reserved.
- `source_job_id` is NULL on every row; nothing is lost with it.
- A job with no stored prompt runs the parse agent's default template, which every job is
  (only `parse` queues jobs): that template, as it reads at this revision, becomes the
  row's `prompt`. Its `prompt_sha256` stays '', which no new job has, so it still never
  answers a dedup lookup.

The dedup index is rebuilt without `connection_id`. The downgrade restores the columns,
empty, the old index and a nullable `prompt`; the snapshots and prompts written here stay,
since nothing tells them from the rows' own.

Revision ID: 185b2581586c
Revises: 774fee25f32b
Create Date: 2026-11-24 13:15:39.467308
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "185b2581586c"
down_revision: str | None = "774fee25f32b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The parse agent's DEFAULT_PROMPT_TEMPLATE (agents/parse.py) as it reads at this revision,
# copied: a migration imports nothing from the app.
PARSE_DEFAULT_PROMPT = """Transcribe this page image into GitHub-flavoured Markdown.
Reproduce the text verbatim and in reading order, keeping headings, lists, tables and
formulas. Do not summarise, do not comment, and do not wrap your answer in a code fence.

Leave out the page's furniture: the page or slide number, and any running header or footer
that repeats across pages. A number that belongs to the content is not furniture and must be
kept — a number inside a sentence, a figure or table label, a citation marker, a numbered
heading, or any number inside a table.

{bbox_instructions}
The box must enclose the whole figure: every panel, and everything drawn as part of it —
axes, tick labels, axis titles, legends, colour bars, panel titles and labels, and
annotations. A figure made of several panels is one figure with one placeholder, unless
each panel has a caption of its own. The box must exclude the figure's caption and any
surrounding body text: the caption is the text on the next line, never part of the box.
Emit no other Markdown images.
"""


def upgrade() -> None:
    op.execute("UPDATE jobs SET connection_base_url = NULL WHERE connection_id IS NOT NULL")
    op.execute(
        sa.text("UPDATE jobs SET prompt = :prompt WHERE prompt IS NULL").bindparams(
            prompt=PARSE_DEFAULT_PROMPT
        )
    )
    op.alter_column("jobs", "prompt", existing_type=sa.TEXT(), nullable=False)
    op.drop_index(
        op.f("ix_jobs_dedup"),
        table_name="jobs",
        postgresql_where="((status)::text = 'succeeded'::text)",
    )
    op.create_index(
        "ix_jobs_dedup",
        "jobs",
        ["user_id", "agent", "sha256", "model", "pages_spec", "prompt_sha256", "pipeline_version"],
        unique=False,
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )
    op.drop_column("jobs", "source_job_id")
    op.drop_column("jobs", "connection_id")


def downgrade() -> None:
    op.add_column(
        "jobs", sa.Column("connection_id", sa.INTEGER(), autoincrement=False, nullable=True)
    )
    op.add_column("jobs", sa.Column("source_job_id", sa.UUID(), autoincrement=False, nullable=True))
    op.drop_index(
        "ix_jobs_dedup",
        table_name="jobs",
        postgresql_where=sa.text("status = 'succeeded'"),
        sqlite_where=sa.text("status = 'succeeded'"),
    )
    op.create_index(
        op.f("ix_jobs_dedup"),
        "jobs",
        [
            "user_id",
            "agent",
            "sha256",
            "model",
            "connection_id",
            "pages_spec",
            "prompt_sha256",
            "pipeline_version",
        ],
        unique=False,
        postgresql_where="((status)::text = 'succeeded'::text)",
    )
    op.alter_column("jobs", "prompt", existing_type=sa.TEXT(), nullable=True)
