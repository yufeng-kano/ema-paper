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
from . import upload_tickets
from .api_keys import KEY_PREFIX, resolve_api_key
from .oauth_as import resolve_access_token
from .sessions import SESSION_COOKIE, resolve_session
from .upload_tickets import TICKET_PREFIX

DbSession = Annotated[AsyncSession, Depends(db_session)]


async def get_settings_from_app(request: Request) -> Settings:
    """The settings in effect: `.env`'s with the database's overrides on top, read once
    per request — FastAPI caches a dependency's answer within one — so a request runs on
    one snapshot from start to end (docs/admin.md § Settings)."""
    return await request.app.state.runtime_settings.current()


AppSettings = Annotated[Settings, Depends(get_settings_from_app)]


async def require_session_user(request: Request, db: DbSession) -> User:
    """Control plane auth: the `sr_session` cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    user = await resolve_session(db, token) if token else None
    if user is None:
        raise ApiError(401, "auth", "Not signed in")
    return user


SessionUser = Annotated[User, Depends(require_session_user)]


async def resolve_bearer(db: AsyncSession, credential: str) -> User | None:
    """A bearer credential is either a project API key or an OAuth access token.

    Both resolve to the same `User` and carry the same rights: the connector path and the
    scripting path differ only in how the credential was obtained (docs/auth.md). An upload
    ticket is neither, so it resolves to nobody here.
    """
    if credential.startswith(TICKET_PREFIX):
        # `srt_` also starts with `sr_`, so tickets are ruled out before the API key path
        # can look at one. A ticket authenticates only the four routes whose dependencies
        # are below — `/mcp` and `/v1/models` included, everything else refuses it.
        return None
    if credential.startswith(KEY_PREFIX):
        return await resolve_api_key(db, credential)
    return await resolve_access_token(db, credential)


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
    if ticket.spent_at is None or ticket.job_id is None:
        raise ApiError(401, "auth", upload_tickets.REJECTION_MESSAGE)
    return ticket.job_id


TicketJobId = Annotated[uuid.UUID, Depends(require_ticket_job)]


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
