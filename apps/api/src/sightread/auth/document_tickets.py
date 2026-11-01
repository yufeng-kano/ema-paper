"""Short-lived document tickets for the MCP library tools (docs/auth.md § 7).

Format ``srd_<32 url-safe chars>``. Returned by `read_document` or `write_document` exactly
once, stored as a SHA-256 hash plus a display string. A ticket names one document of one
account and a scope: `read` serves that document's `GET` routes under
`/v1/library/documents/{id}`, `write` its two saves as well. Use never spends it; its
lifetime does.
"""

from __future__ import annotations

import hmac
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import DocumentTicket, User, utcnow
from ..errors import ApiError
from .crypto import hash_token

TICKET_PREFIX = "srd_"
TICKET_RANDOM_CHARS = 32
MINT_WINDOW = timedelta(hours=1)
READ = "read"
WRITE = "write"

# The agent's only recovery hint, so it travels in the 401 body itself (docs/auth.md § 7).
REJECTION_MESSAGE = (
    "Document ticket expired or for another document — call read_document (or "
    "write_document to save) again for a fresh one."
)


async def mint(
    db: AsyncSession, settings: Settings, user: User, document_id: int, scope: str
) -> tuple[DocumentTicket, str]:
    """Issue a ticket for one document of this user, tidying up their dead ones on the way.

    The rate limit counts tickets minted in the last hour, before cleanup, as upload
    tickets count theirs. Commits, and returns the row plus the plaintext shown once.
    """
    now = utcnow()
    minted = await db.scalar(
        select(func.count())
        .select_from(DocumentTicket)
        .where(DocumentTicket.user_id == user.id, DocumentTicket.created_at > now - MINT_WINDOW)
    )
    if minted >= settings.document_ticket_rate_per_hour:
        raise ApiError(429, "rate_limit", "Too many document tickets minted; retry later")

    await db.execute(
        delete(DocumentTicket).where(
            DocumentTicket.user_id == user.id, DocumentTicket.expires_at <= now
        )
    )
    # token_urlsafe(24) is exactly 32 url-safe characters.
    token = TICKET_PREFIX + secrets.token_urlsafe(24)[:TICKET_RANDOM_CHARS]
    row = DocumentTicket(
        user_id=user.id,
        document_id=document_id,
        scope=scope,
        token_hash=hash_token(token),
        prefix=f"{TICKET_PREFIX}...{token[-4:]}",
        created_at=now,
        expires_at=now + timedelta(seconds=settings.document_ticket_ttl_seconds),
    )
    db.add(row)
    await db.commit()
    return row, token


async def resolve(db: AsyncSession, presented: str) -> tuple[DocumentTicket, User] | None:
    """Look a ticket up by hash and compare in constant time; None means "not a live ticket"."""
    if not presented.startswith(TICKET_PREFIX):
        return None
    token_hash = hash_token(presented)
    row = (
        await db.execute(select(DocumentTicket).where(DocumentTicket.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.token_hash, token_hash):
        return None
    if row.expires_at <= utcnow():
        return None
    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    return None if user is None else (row, user)
