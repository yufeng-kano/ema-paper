"""Project API keys for the data plane (docs/auth.md § 2).

Format ``sr_<32 url-safe chars>``. Shown exactly once at creation; stored as a SHA-256
hash plus a display string. Revocation is a soft delete.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ApiKey, User, utcnow
from .crypto import hash_token, new_token

KEY_PREFIX = "sr_"


async def create_api_key(db: AsyncSession, user: User, name: str) -> tuple[ApiKey, str]:
    key = new_token(KEY_PREFIX)
    row = ApiKey(
        user_id=user.id, name=name, key_hash=hash_token(key), prefix=f"{KEY_PREFIX}...{key[-4:]}"
    )
    db.add(row)
    await db.flush()
    return row, key


async def resolve_api_key(db: AsyncSession, presented: str) -> User | None:
    """Look the key up by its hash; touch `last_used_at`."""
    if not presented.startswith(KEY_PREFIX):
        return None
    key_hash = hash_token(presented)
    row = (await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    row.last_used_at = utcnow()
    await db.commit()
    return (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
