"""SQLAlchemy models — the schema described in docs/database.md.

All timestamps are `timestamptz` holding UTC. JSON payload columns are `JSONB` on
PostgreSQL (the only supported production database) with a plain `JSON` variant so the
test suite can also run against SQLite when no PostgreSQL is available.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

JSON_COLUMN = JSONB().with_variant(JSON(), "sqlite")


class UtcDateTime(TypeDecorator):
    """`timestamptz` that always hands Python an aware UTC datetime.

    PostgreSQL already does this; the decorator keeps the SQLite test fallback honest,
    since SQLite drops the offset and would otherwise return naive datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime written to a timestamptz column")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


TIMESTAMPTZ = UtcDateTime()


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


# The views a PDF or an image is read in (docs/web.md § Result viewer, *Original and
# Markdown*): the file itself, or the document made of it.
ORIGINAL_VIEW = "original"
MARKDOWN_VIEW = "markdown"
DOCUMENT_VIEWS = frozenset({ORIGINAL_VIEW, MARKDOWN_VIEW})


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Google avatar URL, refreshed on each sign-in (docs/auth.md § 1).
    picture: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Which view a converted PDF or image opens on the first time it is read — `original`
    # or `markdown` (docs/web.md § Result viewer, *Original and Markdown*).
    default_view: Mapped[str] = mapped_column(
        String(16), default=MARKDOWN_VIEW, server_default=text("'markdown'")
    )
    # Refused redeem-code attempts in the window that `redeem_failed_at` opened
    # (docs/billing.md § Redeem codes).
    redeem_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    redeem_failed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class UserSession(Base):
    """A web session row; `token_hash` is the SHA-256 of the cookie value."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class PromptPreset(Base):
    """A named prompt for one agent; selecting one replaces that agent's template entirely
    (docs/parsing.md § Prompts)."""

    __tablename__ = "prompt_presets"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_prompt_presets_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    agent: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
EFFORT_DEFAULT = "medium"


class AgentSetting(Base):
    """What one user picked for one agent (docs/agents.md § Per-agent settings).

    No row means every default: the first chat model offered, the agent's default prompt.
    """

    __tablename__ = "agent_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    # The chat agent's pick among `CHAT_MODELS`; NULL means the first offered. The parse
    # and check agents' models are the deployment's, never stored here (docs/billing.md
    # § Models).
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The user's own template for the agent; NULL means the agent's default. It replaces
    # the default whole and is never set together with a preset (docs/agents.md).
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The legacy selection of a prompt preset; NULL means none (docs/parsing.md § Prompts).
    prompt_preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_presets.id", ondelete="SET NULL"), nullable=True
    )
    # The chat agent's reasoning depth (docs/chat.md); parse ignores it.
    effort: Mapped[str] = mapped_column(
        String(8), default=EFFORT_DEFAULT, server_default=text("'medium'")
    )
    # The chat agent's instructions from the reader, appended after the prompt as a block
    # of their own; NULL for none (docs/chat.md § Instructions).
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Language tags the chat agent answers and translates into; NULL follows the reader's
    # locale of each message.
    answer_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    translate_language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The chat agent's memory switches: it saves on its own, and memory is paused — kept,
    # neither read nor written (docs/chat.md § Memory).
    memory_auto: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    memory_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # The chat agent's idle compaction: the worker compacts the user's long conversations
    # before their prompt cache expires (docs/chat.md § Compaction).
    idle_compact: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # The parse agent's switch: a blocked page goes to `PARSE_FALLBACK_MODEL` on its own
    # (docs/parsing.md § Fallback model).
    auto_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # The check agent's switch: check every Files upload before it opens (docs/parsing.md
    # § Format check).
    auto_check: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # The parse agent's switch: a PDF or an image uploaded on the Files page is converted on
    # arrival; off — the default since 2026-10-26 — it is kept as a file until the reader
    # asks for Markdown (docs/workspace.md § Files).
    auto_convert: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    prompt_preset: Mapped[PromptPreset | None] = relationship(
        foreign_keys=[prompt_preset_id], lazy="selectin"
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # Queue claim: FIFO over queued rows (docs/jobs.md).
        Index("ix_jobs_status_created_at", "status", "created_at"),
        # Dedup cache lookup, per user, succeeded jobs only (docs/jobs.md § Dedup).
        Index(
            "ix_jobs_dedup",
            "user_id",
            "agent",
            "sha256",
            "model",
            "connection_id",
            "pages_spec",
            "prompt_sha256",
            "pipeline_version",
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The agent that ran this job (docs/agents.md).
    agent: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # pdf | image
    filename: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    pages_spec: Mapped[str] = mapped_column(String(255), default="")  # "" means all pages
    model: Mapped[str] = mapped_column(String(255))
    pipeline_version: Mapped[int] = mapped_column(Integer, default=0)
    bbox_format: Mapped[str] = mapped_column(String(32))
    # The job whose output this job took as input; NULL when the input was an upload.
    # No foreign key, for the same reason as `connection_id` (docs/database.md).
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # The upstream this job ran on; NULL means the built-in upstream — Kano Proxy, or
    # OpenRouter on historical rows (docs/api.md § Upstreams). Deliberately NOT a foreign
    # key: provider identity is immutable job history. A FK with SET NULL would relabel a
    # deleted connection's jobs as Kano Proxy jobs — a queued one would then run on the
    # wrong key, and a succeeded one would satisfy Kano Proxy dedup lookups with another
    # upstream's output (docs/database.md).
    connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Endpoint snapshot at enqueue — the connection's base_url, or KANO_PROXY_BASE_URL for
    # the built-in upstream; part of the dedup key so editing a connection's URL
    # invalidates results parsed through the old endpoint, and historical OpenRouter rows
    # (NULL) never answer a new upload (docs/jobs.md § Dedup).
    connection_base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # The effective prompt template, verbatim; its hash is part of the dedup key.
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_sha256: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # The fallback a blocked page goes to, snapshotted at enqueue while the agent's
    # `auto_fallback` was on; NULL otherwise (docs/parsing.md § Fallback model).
    fallback_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The model a retry runs the remaining pages on; NULL is `model` (docs/jobs.md
    # § Retrying failed pages). Neither is part of the dedup key.
    retry_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The format check, snapshotted when the job is queued or retried: NULL `check_model`
    # is a job with no check (docs/parsing.md § Format check). Not part of the dedup key.
    check_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    check_effort: Mapped[str | None] = mapped_column(String(8), nullable=True)
    check_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16))  # queued | running | succeeded | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # A library upload: the original is kept for its document (docs/jobs.md § Retention),
    # even when the document row lands after the parse has finished.
    retain_source: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The generation of the worker that claimed it, NULL while queued: every write a
    # worker makes to a running job is fenced on it, so a worker that lost the worker
    # lock cannot write over the one that took the job (docs/jobs.md § Queue).
    worker_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


# One value per worker start, taken with the worker lock (docs/jobs.md § Queue). On the
# metadata so `create_all` makes it where the dialect has sequences; the SQLite fallback
# counts from the highest generation on record instead.
WORKER_GENERATION_SEQ = Sequence("worker_generation_seq", metadata=Base.metadata)


class JobPage(Base):
    __tablename__ = "job_pages"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    page_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)  # vision
    status: Mapped[str] = mapped_column(String(16))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The page's transcription, written as the page finishes — what a partial result reads
    # (docs/jobs.md § Progress). NULL on failed pages and rows predating the column.
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The model that wrote the page, or failed it last; NULL on rows predating the column,
    # which the job's own model wrote (docs/parsing.md § Fallback model).
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The figure check's status of each box the page's markdown holds, keyed
    # "ymin,xmin,ymax,xmax"; NULL on failed pages and pages parsed without the check
    # (docs/parsing.md § Figure check).
    figures: Mapped[dict | None] = mapped_column(JSON_COLUMN, nullable=True)


class Result(Base):
    """Parsed output, kept as long as its job — `PARSE_RETENTION_DAYS` after the parse
    finished, or while a document names a parse of that original (docs/jobs.md
    § Retention); document bytes are never stored (docs/database.md)."""

    __tablename__ = "results"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    markdown: Mapped[str] = mapped_column(Text)
    pages: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    figures: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    errors: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    meta: Mapped[dict] = mapped_column(JSON_COLUMN, default=dict)
    # The checked copy a library document's doc.md starts from; NULL when the parse was
    # not checked (docs/parsing.md § Format check).
    reading_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class Folder(Base):
    """A directory in the web file library (docs/web.md § Files).

    `parent_id` NULL is the root. The self-reference cascades, so deleting a folder takes
    its subtree — and, through `documents.folder_id`, the files in it — exactly the way
    removing a directory does. `trashed_at` is set on a folder put in the Trash, and its
    subtree is in the Trash with it; names are unique among the rows not in the Trash
    (docs/database.md § Rules).
    """

    __tablename__ = "folders"
    __table_args__ = (
        Index(
            "uq_folders_parent_name",
            "user_id",
            "parent_id",
            "name",
            unique=True,
            postgresql_where=text("trashed_at IS NULL"),
            sqlite_where=text("trashed_at IS NULL"),
        ),
        # The root level needs its own index: NULLs are distinct to a unique index, so
        # the one above would happily allow two root folders called "Invoices"
        # (docs/database.md § Rules).
        Index(
            "uq_folders_root_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL AND trashed_at IS NULL"),
            sqlite_where=text("parent_id IS NULL AND trashed_at IS NULL"),
        ),
        # What the Trash's expiry reads (docs/workspace.md § The Trash).
        Index(
            "ix_folders_trashed_at",
            "trashed_at",
            postgresql_where=text("trashed_at IS NOT NULL"),
            sqlite_where=text("trashed_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255))
    trashed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


# Which of the three a library entry is (docs/database.md): a parse result, a markdown
# document, or a file of any kind kept once in the tree (docs/workspace.md § Files).
PARSE_KIND = "parse"
MARKDOWN_KIND = "markdown"
FILE_KIND = "file"


class Document(Base):
    """One file in the library: a name, a place in the tree, and the parse behind it.

    A document is an entry, never a second copy of the output — the markdown lives in
    `results`, keyed by `job_id`. Deleting one deletes the entry only; the job then falls
    under the parse retention, and its usage rows outlive it (docs/database.md § Rules).
    `job_id` NULL is a markdown document: no parse, its baseline its original
    (docs/workspace.md § Markdown documents). `trashed_at` as for a folder.
    """

    __tablename__ = "documents"
    __table_args__ = (
        Index(
            "uq_documents_folder_name",
            "user_id",
            "folder_id",
            "name",
            unique=True,
            postgresql_where=text("trashed_at IS NULL"),
            sqlite_where=text("trashed_at IS NULL"),
        ),
        Index(
            "uq_documents_root_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("folder_id IS NULL AND trashed_at IS NULL"),
            sqlite_where=text("folder_id IS NULL AND trashed_at IS NULL"),
        ),
        Index("ix_documents_user_created", "user_id", "created_at"),
        # The parse retention's keep rule and a job delete's cascade read it
        # (docs/jobs.md § Retention).
        Index("ix_documents_job_id", "job_id"),
        Index(
            "ix_documents_reconvert_job_id",
            "reconvert_job_id",
            postgresql_where=text("reconvert_job_id IS NOT NULL"),
            sqlite_where=text("reconvert_job_id IS NOT NULL"),
        ),
        Index(
            "ix_documents_trashed_at",
            "trashed_at",
            postgresql_where=text("trashed_at IS NOT NULL"),
            sqlite_where=text("trashed_at IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(512))
    # `parse`, `markdown` or `file` (docs/database.md): which routes answer for the row and
    # what its directory — or its one file — holds.
    kind: Mapped[str] = mapped_column(String(16), default=PARSE_KIND, server_default="parse")
    # A file's own (docs/workspace.md § Files): what its bytes are served as, and its size
    # in the tree as last reconciled; NULL on the other kinds.
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The view the reader last switched a PDF or an image to — `original` or `markdown`;
    # NULL while never switched (docs/web.md § Result viewer, *Original and Markdown*).
    view: Mapped[str | None] = mapped_column(String(16), nullable=True)
    trashed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # The re-convert queued, running or failed: a new parse of the document's original that
    # becomes its parse once it succeeds (docs/jobs.md § Re-converting a document).
    reconvert_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", name="fk_documents_reconvert_job_id_jobs", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


# Where a usage row's cost came from (docs/parsing.md § Upstream usage): OpenRouter's own
# billing (historical rows only — never written any more), an estimate Kano Proxy or a
# provider connection reported, or nobody — cost NULL.
COST_SOURCE_OPENROUTER = "openrouter"
COST_SOURCE_ESTIMATED = "estimated"
COST_SOURCE_UNKNOWN = "unknown"


class UsageLog(Base):
    __tablename__ = "usage_log"
    __table_args__ = (
        Index("ix_usage_log_user_created", "user_id", "created_at"),
        # A job delete's SET NULL reads it (docs/jobs.md § Retention).
        Index("ix_usage_log_job_id", "job_id"),
        # An unknown cost is NULL and only an unknown cost is: a call nobody priced must
        # never be summed as a free one (docs/database.md).
        CheckConstraint(
            "cost_source IN ('openrouter', 'estimated', 'unknown') "
            "AND (cost_source = 'unknown') = (cost IS NULL)",
            name="ck_usage_log_cost_source",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    # The chat turn that made the call; NULL on parse rows (docs/chat.md).
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_turns.id", ondelete="SET NULL"), nullable=True
    )
    # Which agent billed the call, written with the row (docs/database.md § Rules).
    agent: Mapped[str] = mapped_column(String(32), default="parse", server_default="parse")
    model: Mapped[str] = mapped_column(String(255))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # Prompt-cache reads and writes; 0 on parse rows (docs/chat.md § Prompt caching).
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class Conversation(Base):
    """One thread of the chat agent about one document (docs/chat.md)."""

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_document_updated", "document_id", "updated_at"),
        # What the idle compaction pass reads (docs/chat.md § Compaction).
        Index(
            "ix_conversations_cache_warm_until",
            "cache_warm_until",
            postgresql_where=text("cache_warm_until IS NOT NULL"),
            sqlite_where=text("cache_warm_until IS NOT NULL"),
        ),
        # What the settings page counts when a price would be removed (docs/admin.md
        # § Settings).
        Index("ix_conversations_model", "model"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), default="")
    # The shell this thread holds in the user's sandbox (docs/sandbox.md).
    session: Mapped[str] = mapped_column(String(64))
    # Set by the first turn and kept: the stored thinking blocks belong to this model
    # (docs/chat.md § History is append-only).
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The ids of the memories its first turn loaded, in the order they are rendered; NULL
    # until a turn took the snapshot (docs/chat.md § Memory).
    memory_ids: Mapped[list | None] = mapped_column(JSON_COLUMN, nullable=True)
    # The snapshot of the agent's memory file its first turn took, taken again by a
    # compaction; NULL until a turn took one, '' for a file that was empty or missing
    # (docs/chat.md § Memory, *The memory file*).
    memory_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The summary row the replay starts from; NULL while the conversation was never
    # compacted. No foreign key: a message goes only with its conversation, and a key
    # back to `messages` would close a cycle every DDL tool would have to break
    # (docs/chat.md § Compaction).
    compacted_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # What the last model call replayed and produced, and when the prompt cache it touched
    # expires; NULL before the first call, after a compaction, and when the call reached
    # no cache (docs/chat.md § Compaction).
    context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_warm_until: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # The turn whose branch the conversation shows and continues: an edited message makes
    # a sibling turn and moves it, a version switch moves it back; NULL until the first
    # turn. No foreign key, for the same cycle as `compacted_message_id`: a turn goes only
    # with its conversation (docs/chat.md § History is append-only, *An edited message
    # starts a version*).
    head_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


# A turn's kinds: a user message answered, the reader's `/compact`, the worker's idle
# compaction (docs/chat.md § Compaction).
MESSAGE_TURN = "message"
COMPACT_TURN = "compact"
IDLE_COMPACT_TURN = "idle_compact"
COMPACTION_TURNS = frozenset({COMPACT_TURN, IDLE_COMPACT_TURN})


class ChatTurn(Base):
    """The chat agent's queue: one row per user message (docs/jobs.md § Chat turns)."""

    __tablename__ = "chat_turns"
    __table_args__ = (
        Index("ix_chat_turns_status_created_at", "status", "created_at"),
        # The stream's "is a turn unfinished" poll, sends, cancels and deletes all ask by
        # conversation (docs/database.md).
        Index("ix_chat_turns_conversation_finished", "conversation_id", "finished_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    # The user message this turn answers. No FK the other way: messages.turn_id names the
    # turn that produced an assistant message.
    message_id: Mapped[int] = mapped_column(Integer)
    # message | compact | idle_compact (docs/chat.md § Compaction).
    kind: Mapped[str] = mapped_column(
        String(16), default=MESSAGE_TURN, server_default=text("'message'")
    )
    status: Mapped[str] = mapped_column(String(16))  # queued|running|succeeded|failed|cancelled
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The web app's locale when the message was sent: the language the turn answers in.
    locale: Mapped[str] = mapped_column(String(16), default="en", server_default="en")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # The turn before this one on its branch, NULL for a conversation's first: turns with
    # one parent are the versions of one message, and `conversations.head_id` names the
    # branch shown. A self-reference without a foreign key — the rows go only with their
    # conversation, which takes them together (docs/chat.md § History is append-only, *An
    # edited message starts a version*).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class ChatToolCall(Base):
    """One web tool call of a turn that was answered or kept a file — `web_search`,
    `search_papers`, `download_file` — what the per-day caps count and what a turn's web
    searches are charged from (docs/chat.md § Tools, docs/billing.md § A chat turn). A
    deleted turn keeps its rows for the day's count, as a purged parse keeps its entry."""

    __tablename__ = "chat_tool_calls"
    __table_args__ = (
        # The deployment's per-day caps, a user's per-day cap, and the turn's settlement.
        Index("ix_chat_tool_calls_tool_created", "tool", "created_at"),
        Index("ix_chat_tool_calls_user_tool_created", "user_id", "tool", "created_at"),
        Index("ix_chat_tool_calls_turn_id", "turn_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_turns.id", ondelete="SET NULL"), nullable=True
    )
    tool: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class Message(Base):
    """A turn of a conversation: content blocks exactly as exchanged with the model, so
    every later request replays them verbatim (docs/chat.md § History is append-only)."""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_id", "conversation_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_turns.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class ChatEvent(Base):
    """What the worker streamed while a turn ran; the SSE route replays and tails it."""

    __tablename__ = "chat_events"
    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_chat_events_conversation_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_turns.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    event: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict] = mapped_column(JSON_COLUMN, default=dict)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class Skill(Base):
    """A skill of the user's own, or the user's edit of a built-in under the built-in's
    name; the built-ins themselves live in code (docs/chat.md § Skills)."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_skills_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    argument_hint: Mapped[str] = mapped_column(String(128), default="")
    body: Mapped[str] = mapped_column(Text)
    # The reader runs it as `/name`; the agent loads it on its own (docs/chat.md § Skills).
    user_invocable: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    model_invocable: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class Subagent(Base):
    """A subagent of the user's own, or the user's edit of a built-in under its name
    (docs/chat.md § Subagents)."""

    __tablename__ = "subagents"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_subagents_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    # NULL: the conversation's model.
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Whether it may change files: the editor's writes and the shell.
    writes: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # A subset of the subagent tools (agents/chat.py), in their canonical order; `bash`
    # only with `writes`.
    tools: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


MEMORY_KINDS = ("term", "preference", "about", "document")


class Memory(Base):
    """One of the chat agent's notes about the reader, kept across conversations: the reader
    lists and edits them on the Agent page, the agent reads them from its system prompt and
    writes them with its `memory` tool (docs/chat.md § Memory)."""

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_user_updated", "user_id", "updated_at"),
        # What a document's delete (CASCADE) and a conversation's (SET NULL) look rows up
        # by: without them every such delete scans every reader's memories
        # (docs/database.md).
        Index(
            "ix_memories_document_id",
            "document_id",
            postgresql_where=text("document_id IS NOT NULL"),
            sqlite_where=text("document_id IS NOT NULL"),
        ),
        Index(
            "ix_memories_conversation_id",
            "conversation_id",
            postgresql_where=text("conversation_id IS NOT NULL"),
            sqlite_where=text("conversation_id IS NOT NULL"),
        ),
        CheckConstraint(
            "kind IN ('term', 'preference', 'about', 'document')", name="ck_memories_kind"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    # The one document it applies to; NULL for every document. A note about one document
    # goes with it, never widened to every document.
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    # Always loaded by a new conversation. (A plain default: `text` is a column here.)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The conversation that saved it, and the `tool_use_id` of the call: deleting the
    # conversation keeps what it saved.
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class ChatAttachment(Base):
    """A prepared upload waiting for the message that will carry it: the file is
    `{UPLOAD_DIR}/chat-attachment-{id}.{png|jpg|txt}`, and the send embeds both into the
    message and deletes them (docs/chat.md § What a user message carries)."""

    __tablename__ = "chat_attachments"
    __table_args__ = (Index("ix_chat_attachments_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # image | text
    media_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # The prepared text's length, for a text attachment.
    chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class UploadTicket(Base):
    """A single-use upload credential minted by the MCP `parse` tool (docs/auth.md § 5).

    Unspent it authenticates one `POST /v1/parse`; spending it binds `job_id`, after which
    it only reads that one job. `token_hash` is the SHA-256 of the `srt_...` plaintext.
    """

    __tablename__ = "upload_tickets"
    # Mint rate limit: tickets this user created in the last hour.
    __table_args__ = (Index("ix_upload_tickets_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    spent_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class OAuthClient(Base):
    """Dynamically registered Claude Connector clients (docs/auth.md § OAuth AS)."""

    __tablename__ = "oauth_clients"

    client_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255))
    redirect_uris: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class OAuthGrant(Base):
    __tablename__ = "oauth_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("oauth_clients.client_id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # code | access | refresh
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    pkce_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Set on `code` rows only: the token endpoint must check the code came back from the
    # same redirect URI the authorization request used (RFC 6749 § 4.1.3).
    redirect_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    scope: Mapped[str] = mapped_column(String(255), default="")
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


# --- billing (docs/billing.md § The ledger) -----------------------------------------------------


class Subscription(Base):
    """The user's Kano Pay subscription, the canonical copy (docs/billing.md § Kano Pay).

    No row is Free: every user has Free without one.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (CheckConstraint("period_end > period_start", name="ck_subscriptions_period"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    subscription_id: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    plan_id: Mapped[str] = mapped_column(String(64))
    period_start: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    # Pay's version (milliseconds): an update carrying an older one is ignored.
    version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class Checkout(Base):
    """The one checkout a user may have open: its idempotency key is written before Pay
    is called, so a retry repeats the same request (docs/billing.md § Kano Pay)."""

    __tablename__ = "checkouts"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    transaction_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class PayEvent(Base):
    """A processed Kano Pay notification, by Pay's own event id: a duplicate is answered
    and not applied again (docs/billing.md § Kano Pay)."""

    __tablename__ = "pay_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    # The record of the event outlives the user it was about.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class CreditPeriod(Base):
    """One row per user per allowance period — Free's UTC month or a paid period — created
    on first use; `granted - spent` is the balance (docs/billing.md § Periods)."""

    __tablename__ = "credit_periods"
    __table_args__ = (
        CheckConstraint("period_end > period_start", name="ck_credit_periods_period"),
        CheckConstraint("granted >= 0", name="ck_credit_periods_granted"),
    )

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    period_start: Mapped[datetime] = mapped_column(TIMESTAMPTZ, primary_key=True)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    # NULL = Free.
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    granted: Mapped[int] = mapped_column(Integer)
    # Reserved and consumed entries of the period; a release subtracts. May exceed
    # `granted` only through a chat turn's overdraft.
    spent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class CreditEntry(Base):
    """What a parse or a turn cost: one entry per job, reserved at enqueue and settled at
    its terminal state; one per turn, its amount raised as the turn runs (docs/billing.md
    § The ledger)."""

    __tablename__ = "credit_entries"
    __table_args__ = (
        Index("ix_credit_entries_user_created", "user_id", "created_at"),
        CheckConstraint("amount >= 0", name="ck_credit_entries_amount"),
        CheckConstraint("kind IN ('parse', 'chat')", name="ck_credit_entries_kind"),
        CheckConstraint(
            "state IN ('reserved', 'consumed', 'released')", name="ck_credit_entries_state"
        ),
        # A parse entry outlives its job (the retention sets this NULL); a chat entry
        # never has one.
        CheckConstraint("kind = 'parse' OR job_id IS NULL", name="ck_credit_entries_parse_job"),
        # A chat entry outlives its turn (a deleted conversation sets this NULL); a parse
        # entry never has one.
        CheckConstraint("kind = 'chat' OR turn_id IS NULL", name="ck_credit_entries_turn_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The period the entry was made in, where it is settled whenever that happens.
    period_start: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    kind: Mapped[str] = mapped_column(String(8))  # parse | chat
    state: Mapped[str] = mapped_column(String(8))  # reserved | consumed | released
    amount: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_turns.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class CreditPack(Base):
    """A top-up pack bought on Pro, written from Pay's canonical purchase, granted by a
    redeem code, or granted by an operator in the console (docs/billing.md § Top-up packs,
    § Redeem codes, § Operator grants): credits valid until `expires_at`, spent once the
    period's grant is."""

    __tablename__ = "credit_packs"
    __table_args__ = (
        Index("ix_credit_packs_user_expires", "user_id", "expires_at"),
        CheckConstraint("credits >= 0", name="ck_credit_packs_credits"),
        CheckConstraint("spent >= 0", name="ck_credit_packs_spent"),
        CheckConstraint("expires_at > purchased_at", name="ck_credit_packs_validity"),
        CheckConstraint("status IN ('paid', 'refunded')", name="ck_credit_packs_status"),
        CheckConstraint("source IN ('pay', 'code', 'grant')", name="ck_credit_packs_source"),
    )

    # Pay's purchaseId, `redeem-<redemption id>` for a code's credits, or
    # `grant-<request id>` for an operator's grant.
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    pack_id: Mapped[str] = mapped_column(String(64))
    credits: Mapped[int] = mapped_column(Integer)
    spent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16))  # paid | refunded
    purchased_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    # Pay's version (milliseconds): an update carrying an older one is ignored.
    version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # `pay`: bought, Pay's to refund; `code`: a redeem code's grant, and `grant`: an
    # operator's, which nothing refunds and the webhook and the refresh never touch
    # (docs/billing.md § Redeem codes, § Operator grants).
    source: Mapped[str] = mapped_column(String(8), default="pay", server_default=text("'pay'"))
    redemption_id: Mapped[int | None] = mapped_column(
        ForeignKey("redemptions.id", ondelete="SET NULL"), unique=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class CreditPackEntry(Base):
    """How much of an entry's amount one pack covered (docs/billing.md § The ledger), so a
    release, a retry or a settlement moves exactly that back or forward."""

    __tablename__ = "credit_pack_entries"
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_credit_pack_entries_amount"),)

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("credit_entries.id", ondelete="CASCADE"), primary_key=True
    )
    pack_id: Mapped[str] = mapped_column(
        ForeignKey("credit_packs.id", ondelete="CASCADE"), primary_key=True
    )
    amount: Mapped[int] = mapped_column(Integer)


class PackCheckout(Base):
    """The one pack checkout a user may have open, keyed as `checkouts` is: the key is
    written before Pay is called (docs/billing.md § Kano Pay, *Pack checkout*)."""

    __tablename__ = "pack_checkouts"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    pack_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    # Pay's purchaseId, once Pay answered.
    purchase_id: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


class RedeemCode(Base):
    """A code the operator hands out (docs/billing.md § Redeem codes): credits kept as a
    pack once redeemed, or days of a paid plan banked on `plan_grants`. No user_id: a
    code outlives every account that used it."""

    __tablename__ = "redeem_codes"
    __table_args__ = (
        CheckConstraint("kind IN ('credits', 'plan')", name="ck_redeem_codes_kind"),
        CheckConstraint("max_uses >= 1", name="ck_redeem_codes_max_uses"),
        CheckConstraint("uses >= 0", name="ck_redeem_codes_uses"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The random characters as printed, without the prefix; never logged.
    code: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # credits | plan
    credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    uses: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    redeem_by: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    # The operator's note, never shown to a user.
    label: Mapped[str] = mapped_column(String(255), default="", server_default=text("''"))
    disabled_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class Redemption(Base):
    """One user's use of one code, with what it granted as the code stood then
    (docs/billing.md § Redeem codes)."""

    __tablename__ = "redemptions"
    __table_args__ = (UniqueConstraint("code_id", "user_id", name="uq_redemptions_code_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("redeem_codes.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    credits: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class PlanGrant(Base):
    """The user's bank of granted days and, while one runs, the open granted period
    (docs/billing.md § Periods, *Granted days*)."""

    __tablename__ = "plan_grants"
    __table_args__ = (CheckConstraint("days_banked >= 0", name="ck_plan_grants_days"),)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    plan_id: Mapped[str] = mapped_column(String(64))
    days_banked: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    period_start: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class Group(Base):
    """A group of the operator console: binds people to permission strings (docs/admin.md
    § Groups). Code reads `permissions` and `builtin`, never `name`. The one built-in
    group holds every permission whatever its stored list, which is kept empty."""

    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("name", name="uq_groups_name"),
        # One built-in group: the owner group (docs/database.md).
        Index(
            "uq_groups_builtin",
            "builtin",
            unique=True,
            postgresql_where=text("builtin"),
            sqlite_where=text("builtin"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32))
    # Normalized permission strings in `ALL_PERMISSIONS` order (docs/admin.md
    # § Permissions); a JSON column, so it is reassigned whole, never mutated in place.
    permissions: Mapped[list] = mapped_column(JSON_COLUMN, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class GroupMember(Base):
    """One person in one group. A deleted group or account takes its memberships; the
    one who added it may go and leave the row (docs/admin.md § Groups)."""

    __tablename__ = "group_members"

    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # The member themselves for a sign-in's owner join.
    added_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    added_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class AuditLog(Base):
    """One change made through the operator console, written in the change's own
    transaction and never updated or deleted (docs/admin.md § The audit log). `before` and
    `after` hold ids, amounts, flags, permission strings and group names — never an
    address, a person's name, content or a secret."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_actor_created", "actor_id", "created_at"),
        Index("ix_audit_log_target", "target_type", "target_id"),
    )

    # BIGINT on PostgreSQL; SQLite numbers a row only through an INTEGER primary key.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True
    )
    # The record outlives the account that made the change.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # What the route required; NULL for a sign-in's owner join.
    permission: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(64))  # <area>.<verb>
    target_type: Mapped[str] = mapped_column(String(32))  # group | user | setting
    # No foreign key: a record outlives what it names.
    target_id: Mapped[str] = mapped_column(String(64))
    # A side with nothing is SQL NULL (docs/admin.md § The audit log): `none_as_null`, or a
    # JSON column writes Python None as the JSON value `null`.
    before: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True
    )
    after: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True).with_variant(JSON(none_as_null=True), "sqlite"), nullable=True
    )
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class RuntimeSetting(Base):
    """One setting of `.env` the operator console overrides (docs/admin.md § Settings): a
    key without a row is `.env`'s. `value` is the text `.env` would hold, kept as a JSON
    string — never the JSON it may contain, whose key order `jsonb` would not keep."""

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(JSON_COLUMN)
    # Who stored it; NULL once that account is deleted.
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
