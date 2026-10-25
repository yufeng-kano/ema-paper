"""Usage aggregates (docs/api.md § Control plane).

Nothing produces jobs or usage rows yet, so the rows are written directly — this is the
shape the job runner will write in the next phase.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

from sightread.db.models import UsageLog, User, utcnow


async def test_usage_aggregates_per_day_and_per_model(signed_in: AsyncClient, sessionmaker) -> None:
    user_id = (await signed_in.get("/api/me")).json()["user"]["id"]
    now = utcnow()

    async with sessionmaker() as db:
        db.add_all(
            [
                UsageLog(
                    user_id=user_id,
                    model="google/gemini-2.5-flash",
                    prompt_tokens=100,
                    completion_tokens=20,
                    cost=Decimal("0.001000"),
                    cost_source="openrouter",
                    created_at=now,
                ),
                UsageLog(
                    user_id=user_id,
                    model="google/gemini-2.5-flash",
                    prompt_tokens=200,
                    completion_tokens=40,
                    cost=Decimal("0.002000"),
                    cost_source="openrouter",
                    created_at=now,
                ),
                UsageLog(
                    user_id=user_id,
                    model="anthropic/claude-vision",
                    prompt_tokens=50,
                    completion_tokens=5,
                    cost=Decimal("0.000500"),
                    cost_source="openrouter",
                    created_at=now - timedelta(days=2),
                ),
                # Outside the requested window.
                UsageLog(
                    user_id=user_id,
                    model="anthropic/claude-vision",
                    prompt_tokens=999,
                    completion_tokens=999,
                    cost=Decimal("9.000000"),
                    cost_source="openrouter",
                    created_at=now - timedelta(days=40),
                ),
            ]
        )
        await db.commit()

    body = (await signed_in.get("/api/usage?days=30")).json()
    assert body["days"] == 30

    today = now.date().isoformat()
    today_bucket = next(row for row in body["per_day"] if row["date"] == today)
    assert today_bucket["prompt_tokens"] == 300
    assert today_bucket["completion_tokens"] == 60
    assert today_bucket["cost"] == 0.003

    per_model = {row["model"]: row for row in body["per_model"]}
    assert per_model["google/gemini-2.5-flash"]["prompt_tokens"] == 300
    assert per_model["anthropic/claude-vision"]["prompt_tokens"] == 50
    assert per_model["anthropic/claude-vision"]["cost"] == 0.0005


async def test_usage_keeps_billed_estimated_and_unpriced_cost_apart(
    signed_in: AsyncClient, sessionmaker
) -> None:
    """An estimate is never summed into billing and an unpriced call never reads as $0
    (docs/api.md § Usage aggregates)."""
    user_id = (await signed_in.get("/api/me")).json()["user"]["id"]
    now = utcnow()

    def row(model: str, cost: str | None, source: str) -> UsageLog:
        return UsageLog(
            user_id=user_id,
            model=model,
            prompt_tokens=10,
            completion_tokens=1,
            cost=None if cost is None else Decimal(cost),
            cost_source=source,
            created_at=now,
        )

    async with sessionmaker() as db:
        db.add_all(
            [
                row("vendor/billed", "0.010000", "openrouter"),
                row("proxy/estimated", "0.200000", "estimated"),
                row("proxy/estimated", "0.000000", "estimated"),
                row("proxy/estimated", None, "unknown"),
                row("proxy/unpriced", None, "unknown"),
            ]
        )
        await db.commit()

    body = (await signed_in.get("/api/usage?days=30")).json()

    (today,) = body["per_day"]
    assert today["calls"] == 5
    assert today["prompt_tokens"] == 50
    assert today["cost"] == 0.01
    assert today["estimated_cost"] == 0.2
    assert today["estimated_calls"] == 2
    assert today["unpriced_calls"] == 2

    per_model = {entry["model"]: entry for entry in body["per_model"]}
    assert per_model["vendor/billed"]["cost"] == 0.01
    assert per_model["vendor/billed"]["estimated_calls"] == 0
    assert per_model["proxy/estimated"]["cost"] == 0
    assert per_model["proxy/estimated"]["estimated_cost"] == 0.2
    assert per_model["proxy/estimated"]["unpriced_calls"] == 1
    # Nothing billed, nothing estimated: the client can tell this from a $0 bill.
    unpriced = per_model["proxy/unpriced"]
    assert unpriced["calls"] == unpriced["unpriced_calls"] == 1
    assert unpriced["estimated_calls"] == 0


async def test_an_unknown_cost_cannot_be_stored_as_zero(sessionmaker) -> None:
    """The check constraint holds the invariant the aggregates rely on (docs/database.md)."""
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
