"""FastAPI dependencies shared by both planes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import UploadTicket, User
from ..db.session import db_session
from ..errors import ApiError
from . import document_tickets, upload_tickets
from .api_keys import KEY_PREFIX, resolve_api_key
from .oauth_as import LIBRARY_SCOPE, resolve_access_token
from .sessions import SESSION_COOKIE, resolve_session
from .upload_tickets import TICKET_PREFIX

DbSession = Annotated[AsyncSession, Depends(db_session)]


async def get_settings_from_app(request: Request, db: DbSession) -> Settings:
    """The settings in effect: `.env`'s with the database's overrides on top, read once
    per request — FastAPI caches a dependency's answer within one — so a request runs on
    one snapshot from start to end (docs/admin.md § Settings). The rows are read on the
    request's own session, the same one its routes get, never a second connection."""
    return await request.app.state.runtime_settings.current(db)


AppSettings = Annotated[Settings, Depends(get_settings_from_app)]


async def require_session_user(request: Request, db: DbSession) -> User:
    """Control plane auth: the `sr_session` cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    user = await resolve_session(db, token) if token else None
    if user is None:
        raise ApiError(401, "auth", "Not signed in")
    return user


SessionUser = Annotated[User, Depends(require_session_user)]


# What a connector approved before the `library` scope existed is told (docs/auth.md § 4).
LIBRARY_SCOPE_MESSAGE = (
    "This connector was approved before it could read your library: remove it in Claude's "
    "settings and add it again."
)


@dataclass
class Bearer:
    """A durable data-plane credential resolved: its user, and the scopes of an OAuth
    grant — None for an API key, which carries the account's own rights."""

    user: User
    scopes: set[str] | None


async def resolve_bearer_identity(db: AsyncSession, credential: str) -> Bearer | None:
    """A bearer credential is either a project API key or an OAuth access token.

    Both resolve to the same `User`: the connector path and the scripting path differ only
    in how the credential was obtained and, for a token, in the scopes its grant holds
    (docs/auth.md § 4). An upload or a document ticket is neither, so it resolves to nobody
    here.
    """
    if credential.startswith((TICKET_PREFIX, document_tickets.TICKET_PREFIX)):
        # Tickets are ruled out before the API key path can look at one. A ticket
        # authenticates only the routes whose dependencies are below — the upload and its
        # job's reads, or one document's routes; `/mcp` and `/v1/models` refuse it.
        return None
    if credential.startswith(KEY_PREFIX):
        user = await resolve_api_key(db, credential)
        return None if user is None else Bearer(user=user, scopes=None)
    resolved = await resolve_access_token(db, credential)
    return None if resolved is None else Bearer(user=resolved[0], scopes=resolved[1])


async def resolve_bearer(db: AsyncSession, credential: str) -> User | None:
    """The user a durable bearer credential speaks for, whatever its scopes."""
    identity = await resolve_bearer_identity(db, credential)
    return None if identity is None else identity.user


def bearer_credential(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()


async def require_bearer_user(request: Request, db: DbSession) -> User:
    """Data plane auth: `Authorization: Bearer <API key | OAuth access token>`."""
    credential = bearer_credential(request)
    if credential is None:
        raise ApiError(
            401,
            "auth",
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await resolve_bearer(db, credential)
    if user is None:
        raise ApiError(401, "auth", "Invalid, expired or revoked credential")
    return user


@dataclass
class Uploader:
    """Who is posting to `/v1/parse`: a durable credential, or one upload ticket."""

    user: User
    ticket: UploadTicket | None = None


async def _live_ticket(request: Request, db: AsyncSession) -> tuple[UploadTicket, User]:
    """Resolve the presented `srt_` token or answer the one documented 401."""
    resolved = await upload_tickets.resolve(db, bearer_credential(request) or "")
    if resolved is None:
        raise ApiError(401, "auth", upload_tickets.REJECTION_MESSAGE)
    return resolved


def _presents_ticket(request: Request) -> bool:
    credential = bearer_credential(request)
    return credential is not None and credential.startswith(TICKET_PREFIX)


async def require_uploader(request: Request, db: DbSession) -> Uploader:
    """`POST /v1/parse` auth: the data plane bearer, or an unspent upload ticket."""
    if not _presents_ticket(request):
        return Uploader(user=await require_bearer_user(request, db))
    ticket, user = await _live_ticket(request, db)
    if ticket.spent_at is not None:
        raise ApiError(401, "auth", upload_tickets.REJECTION_MESSAGE)
    return Uploader(user=user, ticket=ticket)


UploaderCaller = Annotated[Uploader, Depends(require_uploader)]


async def require_job_reader(request: Request, db: DbSession) -> User:
    """`GET /v1/jobs/{id}*` auth: the data plane bearer, or the ticket that created the job.

    A spent ticket reads exactly the job it is bound to; anything else is the same 401 as
    an expired one, so the agent gets the recovery hint either way (docs/auth.md § 5).
    """
    if not _presents_ticket(request):
        return await require_bearer_user(request, db)
    ticket, user = await _live_ticket(request, db)
    try:
        wanted = uuid.UUID(request.path_params.get("job_id", ""))
    except ValueError:
        wanted = None
    if ticket.spent_at is None or ticket.job_id is None or ticket.job_id != wanted:
        raise ApiError(401, "auth", upload_tickets.REJECTION_MESSAGE)
    return user


JobReader = Annotated[User, Depends(require_job_reader)]


async def require_ticket_job(request: Request, db: DbSession) -> uuid.UUID:
    """`GET /v1/jobs/last*` auth: an upload ticket, resolved to the job it created.

    `last` exists so the MCP curl commands run without a job id to fill in (docs/api.md,
    docs/mcp.md). A durable credential already knows its job id and uses the explicit
    routes, so anything but a ticket is refused outright.
    """
    if not _presents_ticket(request):
        raise ApiError(
            400, "invalid_request", "'last' needs an upload ticket; use /v1/jobs/{id} instead"
        )
    ticket, _ = await _live_ticket(request, db)
    if ticket.spent_at is None:
        # A live ticket whose upload is still arriving, was refused (credits, the running
        # cap, the file) or never sent: the expired copy would send the agent to mint
        # again for an upload refused the same way (docs/auth.md § 5).
        raise ApiError(
            409,
            "invalid_request",
            "This ticket has not started a parse yet: its upload is still arriving, was "
            "refused, or was not sent. A refused or unsent upload can be sent once more "
            "with it",
        )
    if ticket.job_id is None:
        raise ApiError(401, "auth", upload_tickets.REJECTION_MESSAGE)
    return ticket.job_id


TicketJobId = Annotated[uuid.UUID, Depends(require_ticket_job)]


async def _library_bearer(request: Request, db: AsyncSession) -> User:
    """The library on the data plane takes a durable credential whose rights reach it: an
    API key, or an OAuth token whose grant holds `library` (docs/api.md § The library on
    the data plane)."""
    credential = bearer_credential(request)
    if credential is None:
        raise ApiError(401, "auth", "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    if credential.startswith(document_tickets.TICKET_PREFIX):
        raise ApiError(
            401,
            "auth",
            "A document ticket reads only its own document; list the library with an API "
            "key, or with the connector's list_library tool",
        )
    identity = await resolve_bearer_identity(db, credential)
    if identity is None:
        raise ApiError(401, "auth", "Invalid, expired or revoked credential")
    if identity.scopes is not None and LIBRARY_SCOPE not in identity.scopes:
        raise ApiError(403, "auth", LIBRARY_SCOPE_MESSAGE)
    return identity.user


async def require_library_user(request: Request, db: DbSession) -> User:
    """`GET /v1/library`: a durable credential with the library in reach, never a ticket."""
    return await _library_bearer(request, db)


LibraryUser = Annotated[User, Depends(require_library_user)]


async def _document_caller(request: Request, db: AsyncSession, scope: str) -> User:
    """A `/v1/library/documents/{document_id}*` route: a durable credential, or a document
    ticket for that one document whose scope covers the route (docs/auth.md § 7). Either
    way the route still resolves the document as the caller's own and live."""
    credential = bearer_credential(request)
    if credential is None or not credential.startswith(document_tickets.TICKET_PREFIX):
        return await _library_bearer(request, db)
    resolved = await document_tickets.resolve(db, credential)
    if resolved is None:
        raise ApiError(401, "auth", document_tickets.REJECTION_MESSAGE)
    ticket, user = resolved
    wanted = request.path_params.get("document_id", "")
    if str(ticket.document_id) != wanted or (
        scope == document_tickets.WRITE and ticket.scope != document_tickets.WRITE
    ):
        raise ApiError(401, "auth", document_tickets.REJECTION_MESSAGE)
    return user


async def require_document_reader(request: Request, db: DbSession) -> User:
    return await _document_caller(request, db, document_tickets.READ)


async def require_document_writer(request: Request, db: DbSession) -> User:
    return await _document_caller(request, db, document_tickets.WRITE)


DocumentReader = Annotated[User, Depends(require_document_reader)]
DocumentWriter = Annotated[User, Depends(require_document_writer)]


async def require_reader_user(request: Request, db: DbSession) -> User:
    """`GET /v1/models` is a safe read, so it accepts either a session cookie or a bearer
    credential (docs/api.md)."""
    if request.headers.get("authorization"):
        return await require_bearer_user(request, db)
    return await require_session_user(request, db)


ReaderUser = Annotated[User, Depends(require_reader_user)]


async def require_csrf_header(request: Request) -> None:
    """CSRF pairing for the cookie-authenticated control plane: SameSite=Lax plus a header
    that a cross-site form post cannot set (docs/api.md)."""
    if not request.headers.get("x-requested-with"):
        raise ApiError(403, "auth", "Missing X-Requested-With header")


CsrfGuard = Depends(require_csrf_header)
