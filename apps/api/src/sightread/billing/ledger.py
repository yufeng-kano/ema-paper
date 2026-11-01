"""The credit ledger (docs/billing.md § The ledger): the period row a charge belongs to,
the top-up packs beside it, reservations, settlements, and what the payloads show. Every
write here runs inside the caller's transaction, under the user's row lock the caller
already holds, and touches the period row, the packs and the entry together, so
`credit_periods.spent`, `credit_packs.spent` and `credit_entries` agree.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import (
    ChatToolCall,
    CreditEntry,
    CreditPack,
    CreditPackEntry,
    CreditPeriod,
    PlanGrant,
    Subscription,
    UsageLog,
    utcnow,
)
from .credits import credits_for_usd
from .plans import Decision, Grant, Period, Plan, resolve_period

logger = logging.getLogger(__name__)

PARSE = "parse"
CHAT = "chat"
RESERVED, CONSUMED, RELEASED = "reserved", "consumed", "released"
PACK_PAID, PACK_REFUNDED = "paid", "refunded"
# Where a pack came from: Pay, a redeem code, or an operator's grant in the console
# (docs/billing.md § Redeem codes, § Operator grants).
PACK_SOURCE_PAY, PACK_SOURCE_CODE, PACK_SOURCE_GRANT = "pay", "code", "grant"


class PlanConflict(Exception):
    """The bank holds days of another plan, or runs another plan's period: one plan per
    bank (docs/billing.md § Periods, *Granted days*)."""


class InsufficientCredits(Exception):
    """The balance — the period's and the active packs' together — cannot cover the
    charge (docs/billing.md § Refusals)."""

    def __init__(self, need: int, remaining: int, period_end: datetime) -> None:
        super().__init__("Not enough credits")
        self.need = need
        self.remaining = remaining
        self.period_end = period_end


@dataclass(frozen=True)
class PackBalance:
    """One top-up pack as the balance sees it (docs/billing.md § Top-up packs)."""

    id: str
    pack_id: str
    credits: int
    spent: int
    status: str
    purchased_at: datetime
    expires_at: datetime
    active: bool
    source: str = PACK_SOURCE_PAY
    redemption_id: int | None = None

    @property
    def remaining(self) -> int:
        """What the pack still gives: nothing once refunded or expired."""
        return max(0, self.credits - self.spent) if self.active else 0


@dataclass(frozen=True)
class Balance:
    period: Period
    granted: int
    spent: int
    # Every pack the user bought or redeemed, newest first.
    packs: tuple[PackBalance, ...] = ()
    # The bank of granted days, None for a user who never redeemed any.
    grant: Grant | None = None

    @property
    def remaining(self) -> int:
        """The period's own balance, negative in an overdraft."""
        return self.granted - self.spent

    @property
    def packs_remaining(self) -> int:
        return sum(pack.remaining for pack in self.packs)

    @property
    def available(self) -> int:
        """What a charge is measured against: the period's remaining, 0 when overdrawn,
        plus every active pack's (docs/billing.md § Top-up packs)."""
        return max(0, self.remaining) + self.packs_remaining

    @property
    def packs_expire_at(self) -> datetime | None:
        """The earliest expiry among the active packs with credits left."""
        dates = [pack.expires_at for pack in self.packs if pack.remaining > 0]
        return min(dates) if dates else None


def pack_active(pack: CreditPack, now: datetime) -> bool:
    """A pack counts while it is paid and not yet expired (docs/billing.md § Top-up packs)."""
    return pack.status == PACK_PAID and pack.expires_at > now


def _pack_balance(pack: CreditPack, now: datetime) -> PackBalance:
    return PackBalance(
        id=pack.id,
        pack_id=pack.pack_id,
        credits=pack.credits,
        spent=pack.spent,
        status=pack.status,
        purchased_at=pack.purchased_at,
        expires_at=pack.expires_at,
        active=pack_active(pack, now),
        source=pack.source,
        redemption_id=pack.redemption_id,
    )


def _grant_of(row: PlanGrant | None) -> Grant | None:
    if row is None:
        return None
    return Grant(row.plan_id, row.days_banked, row.period_start, row.period_end)


async def decide_period(
    db: AsyncSession, settings: Settings, user_id: int, now: datetime | None = None
) -> Decision:
    """The period a read or a charge at `now` belongs to, decided from the rows as the
    session has them, nothing written (docs/billing.md § Periods, *Granted days*): a read
    shows what the next charge will persist, and takes no lock for it."""
    return resolve_period(
        await db.get(Subscription, user_id),
        _grant_of(await db.get(PlanGrant, user_id)),
        settings.plans,
        settings.free_plan,
        now or utcnow(),
        settings.grant_period_days,
    )


async def settle_period(
    db: AsyncSession, settings: Settings, user_id: int, now: datetime | None = None
) -> Decision:
    """The same decision, its move of the bank — a granted period opened or cut — written,
    for a caller that holds the user's row lock: a charge or a redemption. The rows are
    read again under the lock, so a bank another transaction just moved is not written
    over; the caller's commit carries the move (docs/billing.md § Periods, *Granted
    days*). Nothing locks the user here: the lock is the caller's, taken in its own order."""
    now = now or utcnow()
    subscription = await db.get(Subscription, user_id)
    if subscription is not None:
        await db.refresh(subscription)
    row = await db.get(PlanGrant, user_id)
    if row is not None:
        await db.refresh(row)
    grant = _grant_of(row)
    decided = resolve_period(
        subscription, grant, settings.plans, settings.free_plan, now, settings.grant_period_days
    )
    if row is not None and decided.grant != grant:
        row.plan_id = decided.grant.plan_id
        row.days_banked = decided.grant.days_banked
        row.period_start = decided.grant.period_start
        row.period_end = decided.grant.period_end
        row.updated_at = now
        await db.flush()
    return decided


async def bank_days(
    db: AsyncSession, settings: Settings, user_id: int, plan_id: str, days: int, now: datetime
) -> Decision:
    """Add `days` of `plan_id` to the user's bank — a plan code's days or an operator's
    grant, one rule for both (docs/billing.md § Periods, *Granted days*, § Operator
    grants) — for a caller that holds the user's row lock and commits. The bank as it
    stands now, a period that ran out closed first, holds one plan: days of another raise
    `PlanConflict` before anything of them is written. Then the period is decided again,
    which opens a granted period at once when no paid one covers `now`; under a paid
    subscription the days wait."""
    await settle_period(db, settings, user_id, now)
    row = await db.get(PlanGrant, user_id)
    if (
        row is not None
        and row.plan_id != plan_id
        and (row.days_banked > 0 or row.period_start is not None)
    ):
        raise PlanConflict
    if row is None:
        row = PlanGrant(user_id=user_id, plan_id=plan_id, days_banked=0)
        db.add(row)
    row.plan_id = plan_id
    row.days_banked += days
    row.updated_at = now
    # Written before the decision, which reads the row again under the lock.
    await db.flush()
    return await settle_period(db, settings, user_id, now)


async def read_balance(
    db: AsyncSession, settings: Settings, user_id: int, now: datetime | None = None
) -> Balance:
    """The current period, its balance and the packs, without writing: a period never
    used has no row and its whole grant left."""
    now = now or utcnow()
    decided = await decide_period(db, settings, user_id, now)
    period = decided.period
    row = await db.get(CreditPeriod, (user_id, period.start))
    packs = tuple(
        _pack_balance(pack, now)
        for pack in (
            await db.execute(
                select(CreditPack)
                .where(CreditPack.user_id == user_id)
                .order_by(CreditPack.purchased_at.desc(), CreditPack.id)
            )
        ).scalars()
    )
    if row is None:
        return Balance(period, period.credits, 0, packs, decided.grant)
    return Balance(period, max(row.granted, period.credits), row.spent, packs, decided.grant)


async def plan_of(db: AsyncSession, settings: Settings, user_id: int) -> Plan:
    """The plan the user's current period belongs to, which is where their entitlements
    come from (docs/billing.md § Plans): the storage cap, the concurrency cap and
    whether the chat agent gets `bash`. Read, never written: no lock is taken."""
    return (await decide_period(db, settings, user_id)).period.plan


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
    raises its grant and never lowers it (docs/billing.md § Periods). The caller holds the
    user's row lock, under which the bank's move, if any, is written (`settle_period`)."""
    now = now or utcnow()
    period = (await settle_period(db, settings, user_id, now)).period
    plan_id = None if period.plan.free else period.plan.id
    row = await db.get(CreditPeriod, (user_id, period.start))
    if row is None:
        row = CreditPeriod(
            user_id=user_id,
            period_start=period.start,
            period_end=period.end,
            plan_id=plan_id,
            granted=period.credits,
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
    if row.granted < period.credits:
        row.granted = period.credits
        row.plan_id = plan_id
        await db.flush()
    return row, period


async def lock_packs(db: AsyncSession, *where) -> list[CreditPack]:
    """Pack rows locked in one statement, the one that expires first ahead: the one order
    for them, after the period row, for every path that writes packs — the ledger's and
    the Pay adapter's (docs/billing.md § The ledger)."""
    return list(
        (
            await db.execute(
                select(CreditPack)
                .where(*where)
                .order_by(CreditPack.expires_at, CreditPack.id)
                .with_for_update()
            )
        ).scalars()
    )


async def _active_packs(db: AsyncSession, user_id: int, now: datetime) -> list[CreditPack]:
    """The packs a charge may take from, locked (docs/billing.md § Top-up packs)."""
    return await lock_packs(
        db,
        CreditPack.user_id == user_id,
        CreditPack.status == PACK_PAID,
        CreditPack.expires_at > now,
    )


def _left(pack: CreditPack) -> int:
    return max(0, pack.credits - pack.spent)


async def _take_from_pack(db: AsyncSession, pack: CreditPack, share: int) -> bool:
    """One pack's conditional UPDATE: the share is taken only if it is still there."""
    taken = await db.execute(
        update(CreditPack)
        .where(CreditPack.id == pack.id, CreditPack.credits - CreditPack.spent >= share)
        .values(spent=CreditPack.spent + share, updated_at=utcnow())
    )
    await db.refresh(pack)
    return taken.rowcount == 1


async def _take(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    amount: int,
    need: int,
    now: datetime | None,
) -> tuple[Period, list[tuple[str, int]]]:
    """Take `amount` from the current period first and the active packs after, in the
    order they expire, refusing when what is available is under `need`. Conditional
    UPDATEs are the guards; returns the period and each pack's share (docs/billing.md
    § The ledger, § Top-up packs)."""
    now = now or utcnow()
    row, period = await period_row(db, settings, user_id, now)
    # The period row's lock before the packs', as every path takes them (docs/billing.md
    # § The ledger): a settlement running beside this charge waits here or is waited for,
    # never both.
    await db.refresh(row, with_for_update=True)
    packs = await _active_packs(db, user_id, now)
    period_left = max(0, row.granted - row.spent)
    available = period_left + sum(_left(pack) for pack in packs)
    if available < need:
        raise InsufficientCredits(need, available, period.end)
    from_period = min(amount, period_left)
    if from_period:
        taken = await db.execute(
            update(CreditPeriod)
            .where(
                CreditPeriod.user_id == user_id,
                CreditPeriod.period_start == period.start,
                CreditPeriod.granted - CreditPeriod.spent >= from_period,
            )
            .values(spent=CreditPeriod.spent + from_period, updated_at=utcnow())
        )
        await db.refresh(row)
        if taken.rowcount != 1:
            raise InsufficientCredits(need, max(0, row.granted - row.spent), period.end)
    shares: list[tuple[str, int]] = []
    rest = amount - from_period
    for pack in packs:
        if rest <= 0:
            break
        share = min(rest, _left(pack))
        if share <= 0:
            continue
        if not await _take_from_pack(db, pack, share):
            raise InsufficientCredits(need, available, period.end)
        shares.append((pack.id, share))
        rest -= share
    if rest > 0:  # pragma: no cover - the rows are locked above; a guard only
        raise InsufficientCredits(need, available, period.end)
    return period, shares


async def _record_shares(
    db: AsyncSession, entry: CreditEntry, shares: list[tuple[str, int]]
) -> None:
    for pack_id, share in shares:
        db.add(CreditPackEntry(entry_id=entry.id, pack_id=pack_id, amount=share))
    if shares:
        await db.flush()


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
    """Reserve `amount` credits of the current period — and the packs after it — for a
    job or a turn, refusing when the balance is under `max(amount, minimum)`; the entry
    and the packs' shares are inserted in the same transaction."""
    period, shares = await _take(db, settings, user_id, amount, max(amount, minimum), now)
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
    await _record_shares(db, entry, shares)
    return entry


async def reserve_again(
    db: AsyncSession, settings: Settings, job_id: uuid.UUID, now: datetime | None = None
) -> None:
    """A retry takes back the credits its job's failure released, from the current period
    and the packs as they stand now, dated now, so a document is charged once, by the run
    that delivers it (docs/billing.md § A parse). A job with no entry — queued before the
    ledger — or one not released is left as it is."""
    entry = (
        await db.execute(select(CreditEntry).where(CreditEntry.job_id == job_id).with_for_update())
    ).scalar_one_or_none()
    if entry is None or entry.state != RELEASED:
        return
    now = now or utcnow()
    period, shares = await _take(db, settings, entry.user_id, entry.amount, entry.amount, now)
    entry.period_start = period.start
    entry.state = RESERVED
    entry.created_at = now
    entry.settled_at = None
    await db.execute(delete(CreditPackEntry).where(CreditPackEntry.entry_id == entry.id))
    await db.flush()
    await _record_shares(db, entry, shares)


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


async def _give_back(db: AsyncSession, entry: CreditEntry) -> None:
    """A release restores each share where it came from: a pack's to that pack, expired
    or not, and the rest to the period — the period row first, then the packs in their
    one order (docs/billing.md § Top-up packs, § The ledger)."""
    shares = {
        share.pack_id: share.amount
        for share in (
            await db.execute(select(CreditPackEntry).where(CreditPackEntry.entry_id == entry.id))
        ).scalars()
    }
    await _move_spent(db, entry, -(entry.amount - sum(shares.values())))
    if not shares:
        return
    for pack in await lock_packs(db, CreditPack.id.in_(list(shares))):
        await db.execute(
            update(CreditPack)
            .where(CreditPack.id == pack.id)
            .values(spent=CreditPack.spent - shares[pack.id], updated_at=utcnow())
        )
    await db.execute(delete(CreditPackEntry).where(CreditPackEntry.entry_id == entry.id))


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
        await _give_back(db, entry)
    entry.settled_at = utcnow()
    await db.flush()


async def _spill(db: AsyncSession, entry: CreditEntry, delta: int, now: datetime) -> None:
    """A turn's further cost goes into its period up to what that has left, then into the
    active packs in the order they expire, and only then into the period past its grant —
    the overdraft (docs/billing.md § A chat turn, § Top-up packs)."""
    period = await db.get(CreditPeriod, (entry.user_id, entry.period_start))
    if period is not None:
        # Locked before its balance is read, so two turns of one user settling at once
        # see what the other left, and before the packs, as a charge locks them.
        await db.refresh(period, with_for_update=True)
    into_period = min(delta, max(0, period.granted - period.spent)) if period else 0
    rest = delta - into_period
    packs = await _active_packs(db, entry.user_id, now) if rest > 0 else []
    for pack in packs:
        if rest <= 0:
            break
        share = min(rest, _left(pack))
        if share <= 0 or not await _take_from_pack(db, pack, share):
            continue
        recorded = await db.get(CreditPackEntry, (entry.id, pack.id))
        if recorded is None:
            db.add(CreditPackEntry(entry_id=entry.id, pack_id=pack.id, amount=share))
        else:
            recorded.amount += share
        rest -= share
    await _move_spent(db, entry, into_period + rest)


# The web tool charged per call (docs/billing.md § A chat turn).
WEB_SEARCH_TOOL = "web_search"


async def settle_chat(db: AsyncSession, settings: Settings, turn_id: uuid.UUID) -> int | None:
    """Re-price a turn from every `usage_log` row it has so far — subagent calls carry its
    id and count with it — rounded up once over the whole turn, and move the difference
    into the period, the packs, then the overdraft. Returns what is left to the turn —
    the period's remaining, 0 when overdrawn, plus the active packs' — or None when the
    turn has no entry (docs/billing.md § A chat turn)."""
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
    # A web search is a flat price on top of the tokens, from the turn's own rows, added
    # after the tokens' rounding (docs/billing.md § A chat turn).
    searches = (
        await db.execute(
            select(func.count())
            .select_from(ChatToolCall)
            .where(ChatToolCall.turn_id == turn_id, ChatToolCall.tool == WEB_SEARCH_TOOL)
        )
    ).scalar_one()
    amount = (credits_for_usd(usd, settings.credit_usd) if rows else 0) + (
        int(searches) * settings.web_search_credits
    )
    now = utcnow()
    if entry.state == RESERVED:
        delta = amount - entry.amount
        if delta > 0:
            await _spill(db, entry, delta, now)
        elif delta < 0:  # pragma: no cover - a turn's cost never falls; a guard only
            await _move_spent(db, entry, delta)
        entry.amount = amount
        await db.flush()
    period = await db.get(CreditPeriod, (entry.user_id, entry.period_start))
    if period is None:  # pragma: no cover - the entry's period row outlives it
        return None
    await db.refresh(period)
    packs = await _active_packs(db, entry.user_id, now)
    return max(0, period.granted - period.spent) + sum(_left(pack) for pack in packs)


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


async def settle_call(db: AsyncSession, entry_id: int, amount: int) -> None:
    """A charge for one call made outside any turn or job — a title the summary model
    wrote (docs/billing.md § A chat turn): the entry reserved at nothing takes the call's
    credits, spilling into the period, the packs and past the grant as a turn's
    settlement does, and is consumed at once; at nothing — the upstream reported no
    usage — it is released, the reader having got nothing. Idempotent on its state."""
    entry = (
        await db.execute(select(CreditEntry).where(CreditEntry.id == entry_id).with_for_update())
    ).scalar_one_or_none()
    if entry is None or entry.state != RESERVED:
        return
    now = utcnow()
    if amount > 0:
        await _spill(db, entry, amount, now)
    entry.amount = amount
    entry.state = CONSUMED if amount > 0 else RELEASED
    entry.settled_at = now
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
