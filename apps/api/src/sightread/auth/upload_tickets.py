"""Single-use upload tickets for the MCP `parse` tool (docs/auth.md § 5).

Format ``srt_<32 url-safe chars>``. Returned by the tool exactly once, stored as a SHA-256
hash plus a display string. A ticket is worth one `POST /v1/parse` — a parse, or the retry
of a failed one — and then only reads of the job that upload created — never an API key's
rights, never `/mcp`.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import UploadTicket, User, utcnow
from ..errors import ApiError
from .crypto import hash_token

TICKET_PREFIX = "srt_"
TICKET_RANDOM_CHARS = 32
MINT_WINDOW = timedelta(hours=1)

# The agent's only recovery hint, so it travels in the 401 body itself (docs/auth.md § 5).
REJECTION_MESSAGE = (
    "Upload ticket expired or already spent — call the parse tool again for a fresh "
    "ticket. Re-uploading the same file joins its parse while it runs and returns it once "
    "it has succeeded; a failed parse is resumed with the retry command."
)


def generate_ticket() -> str:
    # token_urlsafe(24) is exactly 32 url-safe characters.
    return TICKET_PREFIX + secrets.token_urlsafe(24)[:TICKET_RANDOM_CHARS]


def display_prefix(token: str) -> str:
    """The `srt_...abc4` form stored alongside the hash."""
    return f"{TICKET_PREFIX}...{token[-4:]}"


async def mint(db: AsyncSession, settings: Settings, user: User) -> tuple[UploadTicket, str]:
    """Issue a ticket for this user, tidying up their dead ones on the way.

    The rate limit counts tickets *minted* in the last hour, before cleanup, so a short TTL
    cannot dilute it. Commits, and returns the row plus the plaintext shown exactly once.
    """
    now = utcnow()
    minted = await db.scalar(
        select(func.count())
        .select_from(UploadTicket)
        .where(UploadTicket.user_id == user.id, UploadTicket.created_at > now - MINT_WINDOW)
    )
    if minted >= settings.upload_ticket_rate_per_hour:
        raise ApiError(429, "rate_limit", "Too many upload tickets minted; retry later")

    # Opportunistic cleanup in the same transaction: every mint tidies up, so the table
    # stays small without a sweeper (docs/auth.md § 5).
    await db.execute(
        delete(UploadTicket).where(UploadTicket.user_id == user.id, UploadTicket.expires_at <= now)
    )
    token = generate_ticket()
    row = UploadTicket(
        user_id=user.id,
        token_hash=hash_token(token),
        prefix=display_prefix(token),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.upload_ticket_ttl_seconds),
    )
    db.add(row)
    await db.commit()
    return row, token


async def resolve(db: AsyncSession, presented: str) -> tuple[UploadTicket, User] | None:
    """Look a ticket up by hash and compare in constant time; None means "not a live ticket"."""
    if not presented.startswith(TICKET_PREFIX):
        return None
    token_hash = hash_token(presented)
    row = (
        await db.execute(select(UploadTicket).where(UploadTicket.token_hash == token_hash))
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.token_hash, token_hash):
        return None
    if row.expires_at <= utcnow():
        return None
    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    return None if user is None else (row, user)


def spend(ticket: UploadTicket, job_id: uuid.UUID) -> None:
    """Bind a ticket to the job its upload produced and burn it (docs/auth.md § 5)."""
    ticket.job_id = job_id
    ticket.spent_at = utcnow()
