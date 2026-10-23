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
    LargeBinary,
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


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    google_sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Google avatar URL, refreshed on each sign-in (docs/auth.md § 1).
    picture: Mapped[str | None] = mapped_column(String(1024), nullable=True)
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


class KanoProxyKey(Base):
    """One per user: the key the Kano Proxy connect flow obtained (docs/auth.md § 6).

    AES-256-GCM ciphertext only — never a plaintext key (docs/auth.md § 3).
    """

    __tablename__ = "kano_proxy_keys"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    masked: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class KanoConnectState(Base):
    """A pending Kano Proxy connect, bound to the user who started it (docs/auth.md § 6).

    `state_hash` is the SHA-256 of the `state` sent to Kano Proxy; the PKCE verifier is
    replayed at the token step, so it is encrypted rather than hashed. Single use:
    consuming a state deletes its row.
    """

    __tablename__ = "kano_connect_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    verifier_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)


class ProviderConnection(Base):
    """A user-defined OpenAI-compatible upstream (docs/api.md § Upstreams).

    The connection's API key follows the Kano Proxy key's rules exactly: AES-256-GCM
    ciphertext only, masked form for display, plaintext never stored (docs/auth.md § 3).

    A connection is an endpoint, a key and the cost flag; which model runs on it is each
    agent's own setting (`AgentSetting.model`).
    """

    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_provider_connections_user_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str] = mapped_column(String(1024))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    masked: Mapped[str] = mapped_column(String(64))
    # Record the `usage.cost` this endpoint reports, as an estimate; opt-in, never inferred
    # from the URL (docs/parsing.md § Upstream usage).
    report_cost: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


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

    No row means every default: Kano Proxy, no model chosen, the agent's default prompt.
    """

    __tablename__ = "agent_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    agent: Mapped[str] = mapped_column(String(32), primary_key=True)
    # The upstream; NULL means the built-in Kano Proxy (docs/api.md § Upstreams).
    connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="SET NULL"), nullable=True
    )
    # A model id on that upstream; NULL means not chosen yet.
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
    # The parse agent's fallback: a model of the same upstream a blocked page is sent to,
    # on its own only while `auto_fallback` is on (docs/parsing.md § Fallback model).
    fallback_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auto_fallback: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    # The check agent's switch: check every Files upload before it opens (docs/parsing.md
    # § Format check).
    auto_check: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    connection: Mapped[ProviderConnection | None] = relationship(
        foreign_keys=[connection_id], lazy="selectin"
    )
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
    """Parsed output, kept indefinitely; document bytes are never stored (docs/database.md)."""

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


class Document(Base):
    """One file in the library: a name, a place in the tree, and the parse behind it.

    A document is an entry, never a second copy of the output — the markdown lives in
    `results`, keyed by `job_id`. Deleting one deletes the entry only; the job stays in the
    parse history and its usage rows stay with it (docs/database.md § Rules). `job_id` NULL
    is a markdown document: no parse, its baseline its original (docs/workspace.md
    § Markdown documents). `trashed_at` as for a folder.
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
        Index(
            "ix_documents_reconvert_job_id",
            "reconvert_job_id",
            postgresql_where=text("reconvert_job_id IS NOT NULL"),
            sqlite_where=text("reconvert_job_id IS NOT NULL"),
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
    __table_args__ = (Index("ix_conversations_document_updated", "document_id", "updated_at"),)

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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


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
    status: Mapped[str] = mapped_column(String(16))  # queued|running|succeeded|failed|cancelled
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The web app's locale when the message was sent: the language the turn answers in.
    locale: Mapped[str] = mapped_column(String(16), default="en", server_default="en")
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


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
        CheckConstraint(
            "(kind = 'parse') = (job_id IS NOT NULL)", name="ck_credit_entries_parse_job"
        ),
        CheckConstraint(
            "(kind = 'chat') = (turn_id IS NOT NULL)", name="ck_credit_entries_chat_turn"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The period the entry was made in, where it is settled whenever that happens.
    period_start: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    kind: Mapped[str] = mapped_column(String(8))  # parse | chat
    state: Mapped[str] = mapped_column(String(8))  # reserved | consumed | released
    amount: Mapped[int] = mapped_column(Integer)
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_turns.id", ondelete="CASCADE"), unique=True, nullable=True
    )
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
