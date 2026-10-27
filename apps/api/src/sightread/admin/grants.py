"""An operator's grants to an account (docs/admin.md § Users and grants): credits, as a
pack with `source = grant`, and days of a paid plan, into the bank a plan code's days go
into — through the ledger, by the rules a redeem code follows (docs/billing.md § Operator
grants).

Each takes the account's row lock, the ledger's first, writes into the caller's
transaction with its audit row, and never commits: the route commits once, the grant and
its record together.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import ledger
from ..billing.plans import Decision, Grant, Period
from ..config import Settings
from ..db.models import AuditLog, CreditPack, User, utcnow
from ..errors import ApiError
from . import audit

if TYPE_CHECKING:
    from .deps import Operator

# `credit_packs.pack_id` of an operator's grant: not a pack of `KANO_PAY_PACKS_JSON`.
GRANT_PACK_ID = "grant"


def day_bank(grant: Grant | None) -> dict | None:
    """The bank as the console shows and records it, dates as ISO 8601 strings; None for
    an account that never had one."""
    if grant is None:
        return None
    return {
        "plan_id": grant.plan_id,
        "days_banked": grant.days_banked,
        "period_start": None if grant.period_start is None else grant.period_start.isoformat(),
        "period_end": None if grant.period_end is None else grant.period_end.isoformat(),
    }


def period_summary(period: Period) -> dict:
    """What the current period is: a subscription's, granted days' or Free's, its plan and
    when it ends."""
    return {"source": period.source, "plan": period.plan.id, "end": period.end.isoformat()}


def _reused() -> ApiError:
    return ApiError(
        409,
        "admin_conflict",
        "This request id was already used for another account's grant",
        details={"reason": "request_reused"},
    )


async def grant_credits(
    db: AsyncSession,
    settings: Settings,
    operator: Operator,
    user_id: int,
    *,
    credits: int,
    valid_days: int,
    request_id: uuid.UUID,
    reason: str | None,
) -> tuple[CreditPack, int, bool]:
    """A pack of `credits` valid `valid_days` days from now, its id `grant-<request_id>`;
    returns it, the account's credits available after it, and whether it was made now. A
    request sent again finds the pack the first one made and writes nothing, its record
    included; a request id another account's pack has is refused."""
    now = utcnow()
    if await db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
        raise ApiError(404, "invalid_request", "No such account")
    existing = await db.get(CreditPack, f"grant-{request_id}")
    if existing is not None:
        if existing.user_id != user_id:
            raise _reused()
        return existing, (await ledger.read_balance(db, settings, user_id, now)).available, False
    before = await ledger.read_balance(db, settings, user_id, now)
    # The account's packs, locked in the ledger's order before one is written, as a
    # redeemed code's are (docs/billing.md § The ledger).
    await ledger.lock_packs(db, CreditPack.user_id == user_id)
    pack = CreditPack(
        id=f"grant-{request_id}",
        user_id=user_id,
        pack_id=GRANT_PACK_ID,
        credits=credits,
        status=ledger.PACK_PAID,
        purchased_at=now,
        expires_at=now + timedelta(days=valid_days),
        source=ledger.PACK_SOURCE_GRANT,
    )
    try:
        async with db.begin_nested():
            db.add(pack)
            await db.flush()
    except IntegrityError:
        # The same request id written at the same moment for another account, whose row
        # lock is not this one.
        raise _reused() from None
    available = (await ledger.read_balance(db, settings, user_id, now)).available
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="users.write",
        action="users.grant_credits",
        target_type="user",
        target_id=user_id,
        before={"credits_available": before.available},
        after={
            "pack_id": pack.id,
            "credits": credits,
            "expires_at": pack.expires_at.isoformat(),
            "credits_available": available,
            "reason": reason,
        },
        ip=operator.ip,
    )
    return pack, available, True


async def grant_days(
    db: AsyncSession,
    settings: Settings,
    operator: Operator,
    user_id: int,
    *,
    plan_id: str,
    days: int,
    request_id: uuid.UUID,
    reason: str | None,
) -> tuple[str, int, Decision, bool]:
    """`days` of `plan_id`, a configured plan, into the account's bank by the rules a plan
    code's days follow (`ledger.bank_days`); returns the plan and the days granted, the
    period decided after them, and whether they were granted now. Days of another plan
    than the bank holds are refused, nothing written. A request sent again finds the
    record the first one wrote — its `request_id` is kept there, days having no row of
    their own — and writes nothing: the plan and days that record holds, the period as
    it stands now."""
    now = utcnow()
    if await db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
        raise ApiError(404, "invalid_request", "No such account")
    # Under the account's row lock, which every grant to it takes first: a record is
    # either committed by now or not yet begun.
    recorded = await db.scalar(
        select(AuditLog.after).where(
            AuditLog.target_type == "user",
            AuditLog.target_id == str(user_id),
            AuditLog.action == "users.grant_days",
            AuditLog.after["request_id"].as_string() == str(request_id),
        )
    )
    if recorded is not None:
        return (
            recorded["plan"],
            recorded["days"],
            await ledger.decide_period(db, settings, user_id, now),
            False,
        )
    # The bank and the period as the decision leaves them before the grant — a period that
    # ran out closed, one the bank opens now opened — so the record's two sides differ by
    # the grant alone.
    before = await ledger.decide_period(db, settings, user_id, now)
    try:
        after = await ledger.bank_days(db, settings, user_id, plan_id, days, now)
    except ledger.PlanConflict:
        raise ApiError(
            409,
            "admin_conflict",
            "The account's bank holds days of another plan",
            details={"reason": "plan_conflict"},
        ) from None
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="users.write",
        action="users.grant_days",
        target_type="user",
        target_id=user_id,
        before={"day_bank": day_bank(before.grant), "period": period_summary(before.period)},
        after={
            "request_id": str(request_id),
            "plan": plan_id,
            "days": days,
            "day_bank": day_bank(after.grant),
            "period": period_summary(after.period),
            "reason": reason,
        },
        ip=operator.ip,
    )
    return plan_id, days, after, True
