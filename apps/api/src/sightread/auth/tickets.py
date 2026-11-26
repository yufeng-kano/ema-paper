"""What upload tickets and document tickets share (docs/auth.md § 5, § 6): the mint rate,
the cleanup of expired rows, and the lookup of a presented token.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import DocumentTicket, UploadTicket, User, utcnow
from ..errors import ApiError
from .crypto import hash_token

MINT_WINDOW = timedelta(hours=1)


async def check_rate(
    db: AsyncSession,
    model: type[UploadTicket] | type[DocumentTicket],
    user_id: int,
    limit: int,
    message: str,
) -> None:
    """429 with `message` once the user minted `limit` tickets in the last hour. It counts
    tickets *minted*, expired ones included, so a short TTL cannot dilute it."""
    minted = await db.scalar(
        select(func.count())
        .select_from(model)
        .where(model.user_id == user_id, model.created_at > utcnow() - MINT_WINDOW)
    )
    if minted >= limit:
        raise ApiError(429, "rate_limit", message)


async def delete_expired(
    db: AsyncSession,
    model: type[UploadTicket] | type[DocumentTicket],
    user_id: int,
    now: datetime,
) -> None:
    """Opportunistic cleanup in the mint's transaction: every mint tidies up the user's
    dead tickets, so the table stays small without a sweeper. An expired ticket stays until
    it leaves the rate window, so `check_rate` still counts it."""
    await db.execute(
        delete(model).where(
            model.user_id == user_id,
            model.expires_at <= now,
            model.created_at <= now - MINT_WINDOW,
        )
    )


async def resolve_ticket[T: (UploadTicket, DocumentTicket)](
    db: AsyncSession, model: type[T], prefix: str, presented: str
) -> tuple[T, User] | None:
    """Look a ticket up by its hash; None means "not a live ticket". A spent upload ticket
    is still live here: it reads the job it is bound to (docs/auth.md § 5)."""
    if not presented.startswith(prefix):
        return None
    row = (
        await db.execute(select(model).where(model.token_hash == hash_token(presented)))
    ).scalar_one_or_none()
    if row is None or row.expires_at <= utcnow():
        return None
    user = await db.get(User, row.user_id)
    return None if user is None else (row, user)
