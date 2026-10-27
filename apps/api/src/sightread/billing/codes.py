"""Redeem codes (docs/billing.md § Redeem codes): what a code looks like, how a typed one
is read, and the one transaction that redeems it — credits kept as a pack that was not
bought, or days of a paid plan banked on `plan_grants`. The code itself is never logged.

Creating and disabling are shared by the operator's two doors, `python -m sightread.codes`
and the console (docs/admin.md § Redeem codes): they write into the caller's transaction
and never commit — the CLI commits itself, the console together with its audit row.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import CreditPack, PlanGrant, RedeemCode, Redemption, User, utcnow
from ..errors import ApiError
from . import ledger
from .plans import GRANT

logger = logging.getLogger(__name__)

CREDITS, PLAN = "credits", "plan"
# What a code is now (`status`): the console's words. The CLI prints `live` for the first,
# `used up` and `plan missing` for the last two.
ACTIVE, DISABLED, EXPIRED, USED_UP, PLAN_MISSING = (
    "active",
    "disabled",
    "expired",
    "used_up",
    "plan_missing",
)
# How long a credits code's credits last once redeemed, unless the operator says.
DEFAULT_VALID_DAYS = 365
# What the integer columns hold: a count, a use or credits past it are refused before the
# insert would fail on it.
INT_MAX = 2**31 - 1
# The most `valid_days` and `days` a code gives: a hundred years. Redeeming works dates out
# of them — `now + valid_days` for a pack, a granted period's end — which a Python date
# holds only to the year 9999, and banks days in a 32-bit column.
MAX_DAYS = 36_500
# A masked code keeps at least this many characters hidden: 32⁸ ≈ 10¹² possibilities, past
# guessing under the attempt cap, whatever `REDEEM_CODE_CHARS` is.
MASK_HIDDEN_CHARS = 8
# `redeem_codes.label`.
LABEL_MAX_CHARS = 255
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


def mask(code: str, prefix: str) -> str:
    """A code as the console shows it after creation (docs/admin.md § Redeem codes): the
    prefix and the first group of four while `MASK_HIDDEN_CHARS` stay hidden, every other
    character a `•` — `EMA-7K2Q-••••-••••` for twelve. A shorter code shows none of it."""
    groups = [code[i : i + 4] for i in range(0, len(code), 4)]
    shown = groups[:1] if len(code) - 4 >= MASK_HIDDEN_CHARS else []
    hidden = ["•" * len(group) for group in groups[len(shown) :]]
    return "-".join(([prefix] if prefix else []) + shown + hidden)


def end_of_utc_day(day: date) -> datetime:
    """A redeem-by date as stored: the code can be redeemed until the last second of that
    UTC day."""
    return datetime.combine(day, time(23, 59, 59), UTC)


def status(row: RedeemCode, now: datetime, plans: Collection[str]) -> str:
    """What a code is now, in the order `redeem` refuses in: disabled, expired (past
    `redeem_by`), used up (`uses` reached `max_uses`), plan missing (a plan code whose plan
    `plans` — the configured ids — no longer names), else active — redeemable."""
    if row.disabled_at is not None:
        return DISABLED
    if row.redeem_by is not None and now > row.redeem_by:
        return EXPIRED
    if row.uses >= row.max_uses:
        return USED_UP
    if row.kind == PLAN and row.plan_id not in plans:
        return PLAN_MISSING
    return ACTIVE


def is_active(now: datetime, plans: Collection[str]) -> ColumnElement[bool]:
    """`status(row, now, plans) == ACTIVE`, as SQL, for a list of the active codes."""
    return and_(
        RedeemCode.disabled_at.is_(None),
        or_(RedeemCode.redeem_by.is_(None), RedeemCode.redeem_by >= now),
        RedeemCode.uses < RedeemCode.max_uses,
        or_(RedeemCode.kind != PLAN, RedeemCode.plan_id.in_(list(plans))),
    )


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
    grant: PlanGrant | None = None
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
            # The bank as it stands now — a period that ran out closed first — holds one
            # plan: another plan's days cannot join it.
            await ledger.settle_period(db, settings, user_id, now)
            grant = await db.get(PlanGrant, user_id)
            if (
                grant is not None
                and grant.plan_id != row.plan_id
                and (grant.days_banked > 0 or grant.period_start is not None)
            ):
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
    if grant is None:
        grant = PlanGrant(user_id=user_id, plan_id=row.plan_id, days_banked=0)
        db.add(grant)
    grant.plan_id = row.plan_id
    grant.days_banked += row.days
    grant.updated_at = now
    await db.flush()
    # Opens the granted period at once when no paid period covers now.
    decided = await ledger.settle_period(db, settings, user_id, now)
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
    """`count` fresh codes of one grant, written into the caller's transaction and flushed,
    so they have their ids; the caller commits (docs/billing.md § Redeem codes).

    The batch is checked first, for the CLI and the console alike, and refused with a
    `ValueError` naming the rule: a known kind; `count`, `max_uses` and the kind's amounts
    present, from 1 — and up to the integer columns, the days up to `MAX_DAYS`; a plan of
    `KANO_PAY_PLANS_JSON`; a label the column holds; a `redeem_by` not already past; at
    most `REDEEM_CODES_MAX_COUNT` codes. The other kind's fields are ignored."""
    if kind not in (CREDITS, PLAN):
        raise ValueError(f"kind must be {CREDITS} or {PLAN}")
    if not 1 <= count <= settings.redeem_codes_max_count:
        raise ValueError(
            f"count must be from 1 to {settings.redeem_codes_max_count} (REDEEM_CODES_MAX_COUNT)"
        )
    # Each field with the most it takes.
    if kind == CREDITS:
        amounts = {"credits": (credits, INT_MAX), "valid_days": (valid_days, MAX_DAYS)}
    else:
        amounts = {"days": (days, MAX_DAYS)}
    for name, (value, most) in {"max_uses": (max_uses, INT_MAX), **amounts}.items():
        if value is None or not 1 <= value <= most:
            raise ValueError(f"{name} must be a whole number from 1 to {most}")
    if kind == PLAN and plan_id not in settings.plans:
        raise ValueError(f"plan {plan_id!r} is not in KANO_PAY_PLANS_JSON")
    if len(label) > LABEL_MAX_CHARS:
        raise ValueError(f"label must be at most {LABEL_MAX_CHARS} characters")
    if redeem_by is not None and redeem_by.tzinfo is None:
        raise ValueError("redeem_by must carry its time zone")
    # A code born expired would be handed out and refused to everyone who types it.
    if redeem_by is not None and redeem_by < utcnow():
        raise ValueError("redeem_by must be today or later (UTC)")
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
    await db.flush()
    return rows


async def _disable(db: AsyncSession, which: ColumnElement[bool]) -> tuple[RedeemCode | None, bool]:
    row = (await db.execute(select(RedeemCode).where(which).with_for_update())).scalar_one_or_none()
    if row is None or row.disabled_at is not None:
        return row, False
    row.disabled_at = utcnow()
    await db.flush()
    return row, True


async def disable(
    db: AsyncSession, settings: Settings, typed: str
) -> tuple[RedeemCode | None, bool]:
    """Disable a code by its printed form, as the CLI does: dead from now on, its
    redemptions kept. Written into the caller's transaction under the code's row lock and
    not committed. Returns the code — None when there is no such code — and whether this
    disabled it: one already disabled is left as it is."""
    code = normalize(typed, settings.redeem_code_prefix)
    return await _disable(db, RedeemCode.code == code)


async def disable_by_id(db: AsyncSession, code_id: int) -> tuple[RedeemCode | None, bool]:
    """`disable`, by the code's id, as the console does. An id the column cannot hold
    names no code: None, without asking the database, whose driver would refuse to send
    it."""
    if not 1 <= code_id <= INT_MAX:
        return None, False
    return await _disable(db, RedeemCode.id == code_id)
