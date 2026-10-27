"""The console's dependencies (docs/admin.md § Signing in).

`require_console_session` guards the whole `/api/admin` router: the browser session and
nothing else. `require_permission` guards each route after it — and after the CSRF
header a mutation declares — so the refusals come in the documented order: a foreign
credential or no session (401), no `X-Requested-With` (403 `auth`), the permission
missing (403 `permission_denied`), the session past the re-login window (403
`reauth_required`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Request

from ..auth.deps import AppSettings, DbSession
from ..auth.sessions import SESSION_COOKIE, resolve_session_row
from ..config import Settings
from ..db.models import User, UserSession, utcnow
from ..errors import ApiError
from .groups import permissions_of
from .permissions import ALL_PERMISSIONS


@dataclass
class Operator:
    """Who is using the console: the account, the session it signed in with, what its
    groups give it (in `ALL_PERMISSIONS` order, read on every request) and the request's
    client address, which the audit log records."""

    user: User
    session: UserSession
    permissions: tuple[str, ...]
    ip: str | None


async def require_console_session(request: Request, db: DbSession) -> Operator:
    # An API key, an OAuth access token or an upload ticket arrives as `Authorization`;
    # refusing on the header's presence keeps a script holding a browser's cookie too
    # out of the console (docs/auth.md § 1).
    if "authorization" in request.headers:
        raise ApiError(401, "auth", "The console takes a browser session only")
    token = request.cookies.get(SESSION_COOKIE)
    resolved = await resolve_session_row(db, token) if token else None
    if resolved is None:
        raise ApiError(401, "auth", "Not signed in")
    session, user = resolved
    return Operator(
        user=user,
        session=session,
        permissions=await permissions_of(db, user.id),
        ip=request.client.host if request.client else None,
    )


ConsoleSession = Annotated[Operator, Depends(require_console_session)]


def reauth_due(operator: Operator, settings: Settings) -> bool:
    """The session is older than `ADMIN_REAUTH_HOURS`, counted from its sign-in, which
    nothing renews."""
    age = utcnow() - operator.session.created_at
    return age > timedelta(hours=settings.admin_reauth_hours)


def require_permission(permission: str):
    """A route's one permission check, then the re-login window: someone who lacks the
    permission is never sent to sign in again for nothing."""
    if permission not in ALL_PERMISSIONS:
        raise ValueError(f"Unknown permission: {permission}")

    async def dependency(operator: ConsoleSession, settings: AppSettings) -> Operator:
        if permission not in operator.permissions:
            raise ApiError(403, "permission_denied", f"This needs the {permission} permission")
        if reauth_due(operator, settings):
            raise ApiError(403, "reauth_required", "Sign in again to use the console")
        return operator

    return dependency
