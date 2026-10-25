"""The credit ledger (docs/billing.md § The ledger): the period row a charge belongs to,
reservations, settlements, and what the payloads show. Every write here runs inside the
caller's transaction, under the user's row lock the caller already holds, and touches the
period row and the entry together, so `credit_periods.spent` and `credit_entries` agree.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import CreditEntry, CreditPeriod, Subscription, UsageLog, utcnow
from .credits import credits_for_usd
from .plans import Period, Plan, current_period

logger = logging.getLogger(__name__)

PARSE = "parse"
CHAT = "chat"
RESERVED, CONSUMED, RELEASED = "reserved", "consumed", "released"


class InsufficientCredits(Exception):
    """The balance cannot cover the charge (docs/billing.md § Refusals)."""

    def __init__(self, need: int, remaining: int, period_end: datetime) -> None:
        super().__init__("Not enough credits")
        self.need = need
        self.remaining = remaining
        self.period_end = period_end


@dataclass(frozen=True)
class Balance:
    period: Period
    granted: int
    spent: int

    @property
    def remaining(self) -> int:
        return self.granted - self.spent


async def read_balance(
    db: AsyncSession, settings: Settings, user_id: int, now: datetime | None = None
) -> Balance:
    """The current period and its balance, without writing: a period never used has no
    row and its whole grant left."""
    now = now or utcnow()
    period = current_period(
        await db.get(Subscription, user_id), settings.plans, settings.free_plan, now
    )
    row = await db.get(CreditPeriod, (user_id, period.start))
    if row is None:
        return Balance(period, period.plan.credits, 0)
    return Balance(period, max(row.granted, period.plan.credits), row.spent)


async def plan_of(db: AsyncSession, settings: Settings, user_id: int) -> Plan:
    """The plan the user's current period belongs to, which is where their entitlements
    come from (docs/billing.md § Plans): the storage cap, the concurrency cap and
    whether the chat agent gets `bash`."""
    return current_period(
        await db.get(Subscription, user_id), settings.plans, settings.free_plan, utcnow()
    ).plan


def storage_limit(settings: Settings, plan: Plan) -> int:
    """The plan's storage, never past the deployment's ceiling."""
    return min(plan.storage_bytes, settings.workspace_quota_bytes)


def jobs_limit(settings: Settings, plan: Plan) -> int:
    """The plan's parses at once, never past the deployment's ceiling."""
    return min(plan.max_jobs, settings.max_jobs_per_user)


def sandbox_granted(settings: Settings, plan: Plan) -> bool:
    """`bash` for the chat agent: the plan grants it and the deployment has a runner."""
    return plan.sandbox and settings.sandbox_configured


async def period_row(
    db: AsyncSession, settings: Settings, user_id: int, now: datetime | None = None
) -> tuple[CreditPeriod, Period]:
    """The period row a charge at `now` lands in, created on first use; a plan change
    raises its grant and never lowers it (docs/billing.md § Periods)."""
    now = now or utcnow()
    period = current_period(
        await db.get(Subscription, user_id), settings.plans, settings.free_plan, now
    )
    plan_id = None if period.plan.free else period.plan.id
    row = await db.get(CreditPeriod, (user_id, period.start))
    if row is None:
        row = CreditPeriod(
            user_id=user_id,
            period_start=period.start,
            period_end=period.end,
            plan_id=plan_id,
            granted=period.plan.credits,
        )
        try:
            async with db.begin_nested():
                db.add(row)
                await db.flush()
        except IntegrityError:
            # Created meanwhile by a charge that did not hold the user's lock: use theirs.
            row = await db.get(CreditPeriod, (user_id, period.start))
            if row is None:  # pragma: no cover - the conflict was that row's insert
                raise
    if row.granted < period.plan.credits:
        row.granted = period.plan.credits
        row.plan_id = plan_id
        await db.flush()
    return row, period


async def _take(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    amount: int,
    need: int,
    now: datetime | None,
) -> Period:
    """Add `amount` to the current period's `spent`, refusing when the balance is under
    `need`. One conditional UPDATE is the guard (docs/billing.md § The ledger)."""
    row, period = await period_row(db, settings, user_id, now)
    taken = await db.execute(
        update(CreditPeriod)
        .where(
            CreditPeriod.user_id == user_id,
            CreditPeriod.period_start == period.start,
            CreditPeriod.granted - CreditPeriod.spent >= need,
        )
        .values(spent=CreditPeriod.spent + amount, updated_at=utcnow())
    )
    await db.refresh(row)
    if taken.rowcount != 1:
        raise InsufficientCredits(need, row.granted - row.spent, period.end)
    return period


async def reserve(
    db: AsyncSession,
    settings: Settings,
    *,
    user_id: int,
    kind: str,
    amount: int,
    job_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    model: str | None = None,
    minimum: int = 0,
    now: datetime | None = None,
) -> CreditEntry:
    """Reserve `amount` credits of the current period for a job or a turn, refusing when
    the balance is under `max(amount, minimum)`; the entry is inserted in the same
    transaction."""
    period = await _take(db, settings, user_id, amount, max(amount, minimum), now)
    entry = CreditEntry(
        user_id=user_id,
        period_start=period.start,
        kind=kind,
        state=RESERVED,
        amount=amount,
        job_id=job_id,
        turn_id=turn_id,
        model=model,
    )
    db.add(entry)
    await db.flush()
    return entry


async def reserve_again(
    db: AsyncSession, settings: Settings, job_id: uuid.UUID, now: datetime | None = None
) -> None:
    """A retry takes back the credits its job's failure released, from the current period
    and dated now, so a document is charged once, by the run that delivers it
    (docs/billing.md § A parse). A job with no entry — queued before the ledger — or one
    not released is left as it is."""
    entry = (
        await db.execute(select(CreditEntry).where(CreditEntry.job_id == job_id).with_for_update())
    ).scalar_one_or_none()
    if entry is None or entry.state != RELEASED:
        return
    now = now or utcnow()
    period = await _take(db, settings, entry.user_id, entry.amount, entry.amount, now)
    entry.period_start = period.start
    entry.state = RESERVED
    entry.created_at = now
    entry.settled_at = None
    await db.flush()


async def _move_spent(db: AsyncSession, entry: CreditEntry, delta: int) -> None:
    if delta:
        await db.execute(
            update(CreditPeriod)
            .where(
                CreditPeriod.user_id == entry.user_id,
                CreditPeriod.period_start == entry.period_start,
            )
            .values(spent=CreditPeriod.spent + delta, updated_at=utcnow())
        )


async def settle_parse(db: AsyncSession, job_id: uuid.UUID, status: str) -> None:
    """A job's terminal state settles its reservation: `succeeded` consumes it, anything
    else releases it. Idempotent on the entry's state (docs/billing.md § The ledger)."""
    entry = (
        await db.execute(select(CreditEntry).where(CreditEntry.job_id == job_id).with_for_update())
    ).scalar_one_or_none()
    if entry is None:
        return  # queued before the ledger existed, or a dedup answer: nothing reserved
    if entry.state != RESERVED:
        if status == "succeeded" and entry.state == RELEASED:
            logger.warning("job %s succeeded after its credits were released", job_id)
        return
    if status == "succeeded":
        entry.state = CONSUMED
    else:
        entry.state = RELEASED
        await _move_spent(db, entry, -entry.amount)
    entry.settled_at = utcnow()
    await db.flush()


async def settle_chat(db: AsyncSession, settings: Settings, turn_id: uuid.UUID) -> int | None:
    """Re-price a turn from every `usage_log` row it has so far — subagent calls carry its
    id and count with it — rounded up once over the whole turn, and move the period's
    `spent` by the difference. Returns the period's remaining balance, negative in an
    overdraft; None when the turn has no entry (docs/billing.md § A chat turn)."""
    entry = (
        await db.execute(
            select(CreditEntry).where(CreditEntry.turn_id == turn_id).with_for_update()
        )
    ).scalar_one_or_none()
    if entry is None:
        return None
    rows = (
        await db.execute(
            select(
                UsageLog.model,
                func.sum(UsageLog.prompt_tokens),
                func.sum(UsageLog.completion_tokens),
                func.sum(UsageLog.cache_read_tokens),
                func.sum(UsageLog.cache_write_tokens),
            )
            .where(UsageLog.turn_id == turn_id)
            .group_by(UsageLog.model)
        )
    ).all()
    usd = settings.credit_usd * 0
    for model, prompt, completion, cache_read, cache_write in rows:
        price = settings.model_prices.get(model)
        if price is None:
            # The turn was refused before its first call unless its model was priced; a
            # fallback that answered from an unpriced model is the proxy's choice, logged
            # rather than charged at a number nobody set.
            logger.warning("turn %s ran on unpriced model %s", turn_id, model)
            continue
        usd += price.usd(
            prompt_tokens=int(prompt or 0),
            completion_tokens=int(completion or 0),
            cache_read_tokens=int(cache_read or 0),
            cache_write_tokens=int(cache_write or 0),
        )
    amount = credits_for_usd(usd, settings.credit_usd) if rows else 0
    if entry.state == RESERVED:
        await _move_spent(db, entry, amount - entry.amount)
        entry.amount = amount
        await db.flush()
    period = await db.get(CreditPeriod, (entry.user_id, entry.period_start))
    if period is None:  # pragma: no cover - the entry's period row outlives it
        return None
    await db.refresh(period)
    return period.granted - period.spent


async def close_chat(db: AsyncSession, turn_id: uuid.UUID) -> None:
    """The turn is over: what it was charged so far is what it cost."""
    entry = (
        await db.execute(
            select(CreditEntry).where(CreditEntry.turn_id == turn_id).with_for_update()
        )
    ).scalar_one_or_none()
    if entry is None or entry.state != RESERVED:
        return
    entry.state = CONSUMED
    entry.settled_at = utcnow()
    await db.flush()


def shown(entry: CreditEntry | None) -> int | None:
    """What a job or turn payload shows as `credits`: the amount once settled, 0 for a
    released reservation, null while one is pending (docs/billing.md § Routes)."""
    if entry is None or entry.state == RESERVED:
        return None
    return 0 if entry.state == RELEASED else entry.amount


async def credits_by_job(db: AsyncSession, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, int | None]:
    if not job_ids:
        return {}
    rows = (await db.execute(select(CreditEntry).where(CreditEntry.job_id.in_(job_ids)))).scalars()
    return {entry.job_id: shown(entry) for entry in rows}


async def credits_by_turn(
    db: AsyncSession, turn_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int | None]:
    if not turn_ids:
        return {}
    rows = (
        await db.execute(select(CreditEntry).where(CreditEntry.turn_id.in_(turn_ids)))
    ).scalars()
    return {entry.turn_id: shown(entry) for entry in rows}
