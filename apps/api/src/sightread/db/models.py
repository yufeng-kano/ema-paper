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
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
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

    settings: Mapped[UserSettings | None] = relationship(back_populates="user", lazy="selectin")
    openrouter_key: Mapped[OpenRouterKey | None] = relationship(
        back_populates="user", lazy="selectin"
    )


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


class OpenRouterKey(Base):
    """One per user. AES-256-GCM ciphertext only — never a plaintext key (docs/auth.md)."""

    __tablename__ = "openrouter_keys"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    masked: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="openrouter_key")


class ProviderConnection(Base):
    """A user-defined OpenAI-compatible upstream (docs/api.md § Upstreams).

    The connection's API key follows the OpenRouter key's rules exactly: AES-256-GCM
    ciphertext only, masked form for display, plaintext never stored (docs/auth.md § 3).

    A connection is a complete profile: endpoint, key and model travel together. `model`
    is required on creation and NULL only on rows that predate the column
    (docs/database.md).
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
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Record the `usage.cost` this endpoint reports, as an estimate; opt-in, never inferred
    # from the URL (docs/parsing.md § Upstream usage).
    report_cost: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class PromptPreset(Base):
    """A named transcription prompt; selecting one replaces the template entirely
    (docs/parsing.md § Prompts)."""

    __tablename__ = "prompt_presets"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_prompt_presets_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    default_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The active upstream; NULL means the built-in OpenRouter (docs/api.md § Upstreams).
    default_connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="SET NULL"), nullable=True
    )
    # The selected prompt preset; NULL means the shipped default (docs/parsing.md § Prompts).
    prompt_preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("prompt_presets.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="settings")
    default_connection: Mapped[ProviderConnection | None] = relationship(
        foreign_keys=[default_connection_id], lazy="selectin"
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
            "sha256",
            "model",
            "connection_id",
            "profile",
            "profile_version",
            "pages_spec",
            "prompt_sha256",
            "pipeline_version",
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # pdf | image
    filename: Mapped[str] = mapped_column(String(512))
    media_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    pages_spec: Mapped[str] = mapped_column(String(255), default="")  # "" means all pages
    model: Mapped[str] = mapped_column(String(255))
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    pipeline_version: Mapped[int] = mapped_column(Integer, default=0)
    bbox_format: Mapped[str] = mapped_column(String(32))
    # The upstream this job ran on; NULL means OpenRouter (docs/api.md § Upstreams).
    # Deliberately NOT a foreign key: provider identity is immutable job history. A FK
    # with SET NULL would relabel a deleted connection's jobs as OpenRouter jobs — a
    # queued one would then bill the wrong key, and a succeeded one would satisfy
    # OpenRouter dedup lookups with another upstream's output (docs/database.md).
    connection_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Endpoint snapshot at enqueue; part of the dedup key so editing a connection's URL
    # invalidates results parsed through the old endpoint (docs/jobs.md § Dedup).
    connection_base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # The effective prompt template, verbatim; its hash is part of the dedup key.
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_sha256: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16))  # queued | running | succeeded | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ, nullable=True)


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
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)


class Folder(Base):
    """A directory in the web file library (docs/web.md § Files).

    `parent_id` NULL is the root. The self-reference cascades, so deleting a folder takes
    its subtree — and, through `documents.folder_id`, the files in it — exactly the way
    removing a directory does.
    """

    __tablename__ = "folders"
    __table_args__ = (
        UniqueConstraint("user_id", "parent_id", "name", name="uq_folders_parent_name"),
        # The root level needs its own index: NULLs are distinct to a unique constraint, so
        # the one above would happily allow two root folders called "Invoices"
        # (docs/database.md § Rules).
        Index(
            "uq_folders_root_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
            sqlite_where=text("parent_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


class Document(Base):
    """One file in the library: a name, a place in the tree, and the parse behind it.

    A document is an entry, never a second copy of the output — the markdown lives in
    `results`, keyed by `job_id`. Deleting one deletes the entry only; the job stays in the
    parse history and its usage rows stay with it (docs/database.md § Rules).
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("user_id", "folder_id", "name", name="uq_documents_folder_name"),
        Index(
            "uq_documents_root_name",
            "user_id",
            "name",
            unique=True,
            postgresql_where=text("folder_id IS NULL"),
            sqlite_where=text("folder_id IS NULL"),
        ),
        Index("ix_documents_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, default=utcnow, onupdate=utcnow)


# Where a usage row's cost came from (docs/parsing.md § Upstream usage): OpenRouter's own
# billing, an estimate a provider connection reported, or nobody — cost NULL.
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
    model: Mapped[str] = mapped_column(String(255))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    cost_source: Mapped[str] = mapped_column(String(16))
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
