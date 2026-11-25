"""Short-lived document tickets for the MCP library tools (docs/auth.md § 6).

Format ``srd_<32 url-safe chars>``. Returned by `read_document` or `write_document` exactly
once, stored as a SHA-256 hash. A ticket names one document of one account and a scope:
`read` serves that document's `GET` routes under `/v1/library/documents/{id}`, `write` its
two saves as well. Use never spends it; its lifetime does.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import DocumentTicket, User, utcnow
from .crypto import hash_token, new_token
from .tickets import check_rate, delete_expired, resolve_ticket

TICKET_PREFIX = "srd_"
READ = "read"
WRITE = "write"

# The agent's only recovery hint, so it travels in the 401 body itself (docs/auth.md § 6).
REJECTION_MESSAGE = (
    "Document ticket expired or for another document — call read_document (or "
    "write_document to save) again for a fresh one."
)


async def mint(
    db: AsyncSession, settings: Settings, user: User, document_id: int, scope: str
) -> tuple[DocumentTicket, str]:
    """Issue a ticket for one document of this user, tidying up their dead ones on the way.

    The rate limit counts tickets minted in the last hour, expired ones included, as upload
    tickets count theirs. Commits, and returns the row plus the plaintext shown once.
    """
    now = utcnow()
    await check_rate(
        db,
        DocumentTicket,
        user.id,
        settings.document_ticket_rate_per_hour,
        "Too many document tickets minted; retry later",
    )
    await delete_expired(db, DocumentTicket, user.id, now)
    token = new_token(TICKET_PREFIX)
    row = DocumentTicket(
        user_id=user.id,
        document_id=document_id,
        scope=scope,
        token_hash=hash_token(token),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.document_ticket_ttl_seconds),
    )
    db.add(row)
    await db.commit()
    return row, token


async def resolve(db: AsyncSession, presented: str) -> tuple[DocumentTicket, User] | None:
    """Look a ticket up by its hash; None means "not a live ticket"."""
    return await resolve_ticket(db, DocumentTicket, TICKET_PREFIX, presented)
