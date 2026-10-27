"""Plans and allowance periods (docs/billing.md § Plans, § Periods). Pure: the rows they
read are passed in, the clock too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..db.models import Subscription

FREE_PLAN_ID = "free"
PAID_STATUSES = frozenset({"active", "trialing"})
# A subscription in one of these renews no more, whatever `cancel_at_period_end` says.
ENDED_STATUSES = frozenset({"canceled", "expired", "failed"})


@dataclass(frozen=True)
class Plan:
    """What a plan grants. `id` is `free` for the platform's own plan, else Pay's planId."""

    id: str
    credits: int
    storage_bytes: int
    sandbox: bool
    max_jobs: int

    @property
    def free(self) -> bool:
        return self.id == FREE_PLAN_ID


def parse_plans(raw: str, *, storage_ceiling: int, jobs_ceiling: int) -> dict[str, Plan]:
    """`KANO_PAY_PLANS_JSON`: `[{"planId", "credits", "storageBytes", "sandbox", "maxJobs"}]`.
    A plan past the deployment's ceilings (`WORKSPACE_QUOTA_BYTES`, `MAX_JOBS_PER_USER`)
    is refused rather than clamped: the operator meant a number the server cannot carry."""
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"KANO_PAY_PLANS_JSON is not JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("KANO_PAY_PLANS_JSON must be a list of plans")
    plans: dict[str, Plan] = {}
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("KANO_PAY_PLANS_JSON: each plan must be an object")
        plan_id = entry.get("planId")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError("KANO_PAY_PLANS_JSON: planId must be a non-empty string")
        plan_id = plan_id.strip()
        if plan_id == FREE_PLAN_ID:
            raise ValueError("KANO_PAY_PLANS_JSON: 'free' is the platform's plan, not Pay's")
        if plan_id in plans:
            raise ValueError(f"KANO_PAY_PLANS_JSON: plan {plan_id!r} listed twice")
        counts = {}
        for field in ("credits", "storageBytes", "maxJobs"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"KANO_PAY_PLANS_JSON: {plan_id!r}.{field} must be an integer >= 0"
                )
            counts[field] = value
        sandbox = entry.get("sandbox", False)
        if not isinstance(sandbox, bool):
            raise ValueError(f"KANO_PAY_PLANS_JSON: {plan_id!r}.sandbox must be true or false")
        if counts["storageBytes"] > storage_ceiling:
            raise ValueError(
                f"KANO_PAY_PLANS_JSON: {plan_id!r}.storageBytes exceeds WORKSPACE_QUOTA_BYTES"
            )
        if counts["maxJobs"] > jobs_ceiling:
            raise ValueError(f"KANO_PAY_PLANS_JSON: {plan_id!r}.maxJobs exceeds MAX_JOBS_PER_USER")
        plans[plan_id] = Plan(
            id=plan_id,
            credits=counts["credits"],
            storage_bytes=counts["storageBytes"],
            sandbox=sandbox,
            max_jobs=counts["maxJobs"],
        )
    return plans


@dataclass(frozen=True)
class Pack:
    """What a top-up pack grants (docs/billing.md § Top-up packs): `id` is Pay's packId,
    `credits` valid for `valid_days` from the day the pack was paid."""

    id: str
    credits: int
    valid_days: int


def parse_packs(raw: str) -> dict[str, Pack]:
    """`KANO_PAY_PACKS_JSON`: `[{"packId", "credits", "validDays"}]`. Empty = no pack is
    sold; the price is Pay's (docs/billing.md § Top-up packs)."""
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"KANO_PAY_PACKS_JSON is not JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("KANO_PAY_PACKS_JSON must be a list of packs")
    packs: dict[str, Pack] = {}
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError("KANO_PAY_PACKS_JSON: each pack must be an object")
        pack_id = entry.get("packId")
        if not isinstance(pack_id, str) or not pack_id.strip() or len(pack_id.strip()) > 64:
            raise ValueError(
                "KANO_PAY_PACKS_JSON: packId must be a non-empty string of at most 64 characters"
            )
        pack_id = pack_id.strip()
        if pack_id in packs:
            raise ValueError(f"KANO_PAY_PACKS_JSON: pack {pack_id!r} listed twice")
        counts = {}
        for field in ("credits", "validDays"):
            value = entry.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"KANO_PAY_PACKS_JSON: {pack_id!r}.{field} must be an integer >= 1"
                )
            counts[field] = value
        packs[pack_id] = Pack(id=pack_id, credits=counts["credits"], valid_days=counts["validDays"])
    return packs


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """The UTC calendar month holding `now`: Free's period."""
    now = now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, end


# What a period is (docs/billing.md § Periods): a Pay subscription's, one of granted days,
# or Free's month.
SUBSCRIPTION, GRANT, FREE = "subscription", "grant", "free"


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    plan: Plan
    # What the period grants: the plan's credits, pro-rated for a granted period shorter
    # than `GRANT_PERIOD_DAYS` (docs/billing.md § Periods, *Granted days*).
    credits: int
    source: str = SUBSCRIPTION


@dataclass(frozen=True)
class Grant:
    """The bank of granted days as `plan_grants` holds it (docs/billing.md § Periods,
    *Granted days*): whose days, how many wait, and the open granted period if one runs."""

    plan_id: str
    days_banked: int
    period_start: datetime | None = None
    period_end: datetime | None = None

    @property
    def open(self) -> bool:
        return self.period_start is not None and self.period_end is not None


@dataclass(frozen=True)
class Decision:
    """A period decided at `now`, and what the bank should read after the decision — the
    same object when nothing moved."""

    period: Period
    grant: Grant | None


def grant_credits(plan: Plan, days: int, period_days: int) -> int:
    """A granted period's credits: the plan's for `period_days` days, pro-rated upwards
    for fewer (docs/billing.md § Periods, *Granted days*)."""
    return -(-plan.credits * days // period_days)


def _paid(subscription: Subscription | None, plans: dict[str, Plan], now: datetime) -> bool:
    return (
        subscription is not None
        and subscription.status in PAID_STATUSES
        and subscription.period_start <= now < subscription.period_end
        and subscription.plan_id in plans
    )


def resolve_period(
    subscription: Subscription | None,
    grant: Grant | None,
    plans: dict[str, Plan],
    free: Plan,
    now: datetime,
    period_days: int,
) -> Decision:
    """Which period a charge at `now` belongs to (docs/billing.md § Periods): the paid one
    while the subscription is active or trialing, inside its period and on a plan the
    deployment knows; else the open granted period, or one opened now from the bank; Free's
    month otherwise. Read at every charge and every read: nothing waits for a webhook to
    expire access, and the bank moves here — a granted period a paid one started inside
    is cut where the paid one begins and its whole days left go back to the bank; one
    that ran out closes; one is opened from the bank when nothing else pays."""
    paid = _paid(subscription, plans, now)
    if grant is not None and grant.open:
        if paid and subscription.period_start < grant.period_end:
            cut = max(grant.period_start, subscription.period_start)
            days_back = max(0, (grant.period_end - cut).days)
            grant = Grant(grant.plan_id, grant.days_banked + days_back)
        elif grant.period_end <= now:
            grant = Grant(grant.plan_id, grant.days_banked)
    if paid:
        plan = plans[subscription.plan_id]
        return Decision(
            Period(
                subscription.period_start, subscription.period_end, plan, plan.credits, SUBSCRIPTION
            ),
            grant,
        )
    if grant is not None and grant.plan_id in plans:
        plan = plans[grant.plan_id]
        # An open period runs until its end, whatever a clock a moment behind the one that
        # opened it says of its start.
        if grant.open and now < grant.period_end:
            days = (grant.period_end - grant.period_start).days
            return Decision(
                Period(
                    grant.period_start,
                    grant.period_end,
                    plan,
                    grant_credits(plan, days, period_days),
                    GRANT,
                ),
                grant,
            )
        if grant.days_banked > 0:
            days = min(grant.days_banked, period_days)
            opened = Grant(grant.plan_id, grant.days_banked - days, now, now + timedelta(days=days))
            return Decision(
                Period(now, opened.period_end, plan, grant_credits(plan, days, period_days), GRANT),
                opened,
            )
    start, end = month_bounds(now)
    return Decision(Period(start, end, free, free.credits, FREE), grant)


def current_period(
    subscription: Subscription | None,
    plans: dict[str, Plan],
    free: Plan,
    now: datetime,
) -> Period:
    """The period of a user with no granted days: the paid one or Free's month."""
    return resolve_period(subscription, None, plans, free, now, 1).period
