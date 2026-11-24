"""Single-use upload tickets for the MCP `parse` tool (docs/auth.md § 5).

Format ``srt_<32 url-safe chars>``. Returned by the tool exactly once, stored as a SHA-256
hash. A ticket is worth one `POST /v1/parse` — a parse, or the retry of a failed one — and
then only reads of the job that upload created — never an API key's rights, never `/mcp`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import UploadTicket, User, utcnow
from .crypto import hash_token, new_token
from .tickets import check_rate, delete_expired, resolve_ticket

TICKET_PREFIX = "srt_"

# The agent's only recovery hint, so it travels in the 401 body itself (docs/auth.md § 5).
REJECTION_MESSAGE = (
    "Upload ticket expired or already spent — call the parse tool again for a fresh "
    "ticket. Re-uploading the same file joins its parse while it runs and returns it once "
    "it has succeeded; a failed parse is resumed with the retry command."
)


async def check_mint_rate(db: AsyncSession, settings: Settings, user_id: int) -> None:
    """429 once the user minted `UPLOAD_TICKET_RATE_PER_HOUR` tickets in the last hour:
    `mint`'s own check, and the parse tool's before it makes a folder for the ticket."""
    await check_rate(
        db,
        UploadTicket,
        user_id,
        settings.upload_ticket_rate_per_hour,
        "Too many upload tickets minted; retry later",
    )


async def mint(
    db: AsyncSession,
    settings: Settings,
    user: User,
    *,
    into_library: bool = False,
    folder_id: int | None = None,
) -> tuple[UploadTicket, str]:
    """Issue a ticket for this user, tidying up their dead ones on the way.

    The rate limit counts tickets minted in the last hour, expired ones included: the
    cleanup leaves those the hour still counts. Commits, and returns the row plus the
    plaintext shown exactly once.

    `into_library` and `folder_id` say where the upload is filed, decided here and never by
    the upload's own form (docs/auth.md § 5, docs/mcp.md § Filing into Files).
    """
    now = utcnow()
    await check_mint_rate(db, settings, user.id)
    await delete_expired(db, UploadTicket, user.id, now)
    token = new_token(TICKET_PREFIX)
    row = UploadTicket(
        user_id=user.id,
        token_hash=hash_token(token),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.upload_ticket_ttl_seconds),
        into_library=into_library,
        folder_id=folder_id if into_library else None,
    )
    db.add(row)
    await db.commit()
    return row, token


async def resolve(db: AsyncSession, presented: str) -> tuple[UploadTicket, User] | None:
    """Look a ticket up by its hash; None means "not a live ticket"."""
    return await resolve_ticket(db, UploadTicket, TICKET_PREFIX, presented)


def spend(ticket: UploadTicket, job_id: uuid.UUID) -> None:
    """Bind a ticket to the job its upload produced and burn it (docs/auth.md § 5)."""
    ticket.job_id = job_id
    ticket.spent_at = utcnow()
