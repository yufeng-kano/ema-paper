"""`usage_log`'s constraints (docs/database.md)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from sightread.db.models import UsageLog, User


async def test_an_unknown_cost_cannot_be_stored_as_zero(sessionmaker) -> None:
    """The check constraint holds the invariant the usage figures rely on (docs/database.md)."""
    async with sessionmaker() as db:
        user = User(google_sub="sub-1", email="a@example.com")
        db.add(user)
        await db.flush()
        db.add(
            UsageLog(
                user_id=user.id,
                model="m",
                prompt_tokens=1,
                completion_tokens=1,
                cost=Decimal("0"),
                cost_source="unknown",
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
