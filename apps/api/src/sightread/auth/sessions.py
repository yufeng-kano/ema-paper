"""Server-side web sessions (docs/auth.md § 1).

The cookie carries a random opaque token; only its SHA-256 hash is stored, so a database
leak cannot be replayed. Sessions expire after 30 days and are revocable server-side.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import User, UserSession, utcnow
from .crypto import hash_token

SESSION_COOKIE = "sr_session"
SESSION_TTL = timedelta(days=30)


async def create_session(db: AsyncSession, user: User) -> str:
    """Create a session row and return the plaintext cookie token (shown to the client once)."""
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=utcnow() + SESSION_TTL,
        )
    )
    await db.flush()
    return token


async def resolve_session_row(db: AsyncSession, token: str) -> tuple[UserSession, User] | None:
    """The session row and its user. The operator console reads the row's `created_at`,
    the sign-in its re-login window runs from (docs/admin.md § Signing in)."""
    token_hash = hash_token(token)
    row = (
        await db.execute(select(UserSession).where(UserSession.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.token_hash, token_hash):
        return None
    if row.expires_at <= utcnow():
        await db.delete(row)
        await db.commit()
        return None
    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    return None if user is None else (row, user)


async def resolve_session(db: AsyncSession, token: str) -> User | None:
    resolved = await resolve_session_row(db, token)
    return None if resolved is None else resolved[1]


async def delete_session(db: AsyncSession, token: str) -> None:
    row = (
        await db.execute(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
