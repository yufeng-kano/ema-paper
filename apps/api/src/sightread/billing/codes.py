"""Redeem codes (docs/billing.md § Redeem codes): what a code looks like, how a typed one
is read, and the one transaction that redeems it — credits kept as a pack that was not
bought, or days of a paid plan banked on `plan_grants`. The code itself is never logged.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import CreditPack, RedeemCode, Redemption, User, utcnow
from ..errors import ApiError
from . import ledger
from .plans import GRANT, Decision

logger = logging.getLogger(__name__)

CREDITS, PLAN = "credits", "plan"
# 32 letters: no 0, O, 1 or I, so a printed code reads back the same.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# `credit_packs.pack_id` of a code's credits: not a pack of `KANO_PAY_PACKS_JSON`.
CODE_PACK_ID = "code"
_NOT_ALNUM = re.compile(r"[^A-Z0-9]")


def generate(chars: int) -> str:
    """A fresh code: `chars` characters of the alphabet, without the prefix."""
    return "".join(secrets.choice(ALPHABET) for _ in range(chars))


def normalize(typed: str, prefix: str) -> str:
    """What a user typed, as it is stored: uppercased, everything but letters and digits
    dropped, the prefix dropped — so case, spaces and hyphens never matter."""
    code = _NOT_ALNUM.sub("", typed.upper())
    if prefix and code.startswith(prefix):
        code = code[len(prefix) :]
    return code


def display(code: str, prefix: str) -> str:
    """A code as it is printed and typed: the prefix, then groups of four."""
    groups = [code[i : i + 4] for i in range(0, len(code), 4)]
    return "-".join(([prefix] if prefix else []) + groups)


@dataclass(frozen=True)
class Redeemed:
    """What a code granted (docs/billing.md § Routes, `POST /api/billing/redeem`)."""

    kind: str
    credits: int | None = None
    expires_at: datetime | None = None
    plan_id: str | None = None
    days: int | None = None
    days_banked: int | None = None
    # The open granted period's end; None while a paid subscription runs and the days wait.
    period_end: datetime | None = None


def _refusal(code: RedeemCode | None, now: datetime) -> ApiError | None:
    """Why the code cannot be redeemed, before this user's own history is read."""
    if code is None or code.disabled_at is not None:
        return ApiError(404, "code_invalid", "This code does not exist")
    if code.redeem_by is not None and now > code.redeem_by:
        return ApiError(409, "code_expired", "This code has expired")
    if code.uses >= code.max_uses:
        return ApiError(409, "code_exhausted", "This code has been used up")
    return None


async def redeem(
    db: AsyncSession, settings: Settings, user_id: int, typed: str, now: datetime | None = None
) -> Redeemed:
    """Redeem a typed code for the user: one transaction under the user's row lock, the
    code row locked and checked, the redemption written and the grant made; committed
    here, the refusal's count included (docs/billing.md § Redeem codes)."""
    now = now or utcnow()
    user = (
        await db.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            # The session's `User` was loaded by the request's authentication, before this
            # lock: its counters are read again under it, so overlapping attempts count
            # each other.
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    window = timedelta(seconds=settings.redeem_failure_window_seconds)
    in_window = user.redeem_failed_at is not None and now - user.redeem_failed_at < window
    if in_window and user.redeem_failures >= settings.redeem_max_failures:
        retry_after = math.ceil((user.redeem_failed_at + window - now).total_seconds())
        raise ApiError(
            429,
            "rate_limit",
            "Too many attempts; try again later",
            headers={"Retry-After": str(max(1, retry_after))},
        )
    code = normalize(typed, settings.redeem_code_prefix)
    row = (
        (
            await db.execute(select(RedeemCode).where(RedeemCode.code == code).with_for_update())
        ).scalar_one_or_none()
        # Nothing is looked up for an empty or absurd string; a code made under another
        # `REDEEM_CODE_CHARS` still is.
        if 0 < len(code) <= 64
        else None
    )
    refusal = _refusal(row, now)
    decided: Decision | None = None
    if refusal is None:
        if row.kind == PLAN and row.plan_id not in settings.plans:
            # Created for a plan the deployment no longer configures: inert, so not given.
            logger.warning("redeem code %d names a plan not configured: %s", row.id, row.plan_id)
            refusal = ApiError(404, "code_invalid", "This code does not exist")
        elif (
            await db.execute(
                select(Redemption.id).where(
                    Redemption.code_id == row.id, Redemption.user_id == user_id
                )
            )
        ).first() is not None:
            refusal = ApiError(409, "code_redeemed", "You have already redeemed this code")
        elif row.kind == PLAN:
            # The days go into the bank by the one rule an operator's grant follows too;
            # opens the granted period at once when no paid period covers now. Another
            # plan's days are refused before anything of them is written.
            try:
                decided = await ledger.bank_days(db, settings, user_id, row.plan_id, row.days, now)
            except ledger.PlanConflict:
                refusal = ApiError(
                    409, "code_plan_conflict", "This code is for another plan than your days"
                )
    if refusal is not None:
        user.redeem_failures = user.redeem_failures + 1 if in_window else 1
        if not in_window:
            user.redeem_failed_at = now
        await db.commit()
        raise refusal
    redemption = Redemption(
        code_id=row.id,
        user_id=user_id,
        kind=row.kind,
        credits=row.credits,
        plan_id=row.plan_id,
        days=row.days,
        redeemed_at=now,
    )
    db.add(redemption)
    await db.flush()
    row.uses += 1
    user.redeem_failures = 0
    user.redeem_failed_at = None
    if row.kind == CREDITS:
        # The user's packs, locked in the ledger's order before one is written
        # (docs/billing.md § The ledger), as the Pay adapter does.
        await ledger.lock_packs(db, CreditPack.user_id == user_id)
        pack = CreditPack(
            id=f"redeem-{redemption.id}",
            user_id=user_id,
            pack_id=CODE_PACK_ID,
            credits=row.credits,
            status=ledger.PACK_PAID,
            purchased_at=now,
            expires_at=now + timedelta(days=row.valid_days),
            source=ledger.PACK_SOURCE_CODE,
            redemption_id=redemption.id,
        )
        db.add(pack)
        await db.commit()
        return Redeemed(CREDITS, credits=row.credits, expires_at=pack.expires_at)
    await db.commit()
    return Redeemed(
        PLAN,
        plan_id=row.plan_id,
        days=row.days,
        days_banked=decided.grant.days_banked,
        period_end=decided.period.end if decided.period.source == GRANT else None,
    )


async def create_codes(
    db: AsyncSession,
    settings: Settings,
    *,
    kind: str,
    count: int,
    max_uses: int,
    redeem_by: datetime | None,
    label: str,
    credits: int | None = None,
    valid_days: int | None = None,
    plan_id: str | None = None,
    days: int | None = None,
) -> list[RedeemCode]:
    """`count` fresh codes of one grant, written and committed (docs/billing.md § Redeem
    codes). A plan code names a plan of `KANO_PAY_PLANS_JSON` or is refused."""
    if kind == PLAN and plan_id not in settings.plans:
        raise ValueError(f"plan {plan_id!r} is not in KANO_PAY_PLANS_JSON")
    rows: list[RedeemCode] = []
    while len(rows) < count:
        fresh = {generate(settings.redeem_code_chars) for _ in range(count - len(rows))}
        taken = set(
            (await db.execute(select(RedeemCode.code).where(RedeemCode.code.in_(fresh)))).scalars()
        )
        for code in sorted(fresh - taken):
            rows.append(
                RedeemCode(
                    code=code,
                    kind=kind,
                    credits=credits if kind == CREDITS else None,
                    valid_days=valid_days if kind == CREDITS else None,
                    plan_id=plan_id if kind == PLAN else None,
                    days=days if kind == PLAN else None,
                    max_uses=max_uses,
                    redeem_by=redeem_by,
                    label=label,
                )
            )
    db.add_all(rows)
    await db.commit()
    return rows


async def disable(db: AsyncSession, settings: Settings, typed: str) -> RedeemCode | None:
    """Disable a code: dead from now on, its redemptions kept. None when there is no such
    code."""
    code = normalize(typed, settings.redeem_code_prefix)
    row = (
        await db.execute(select(RedeemCode).where(RedeemCode.code == code).with_for_update())
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.disabled_at is None:
        row.disabled_at = utcnow()
        await db.commit()
    return row
