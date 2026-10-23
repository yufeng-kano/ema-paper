"""Plans and allowance periods (docs/billing.md § Plans, § Periods). Pure: the rows they
read are passed in, the clock too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from ..db.models import Subscription

FREE_PLAN_ID = "free"
PAID_STATUSES = frozenset({"active", "trialing"})


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


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    plan: Plan


def current_period(
    subscription: Subscription | None,
    plans: dict[str, Plan],
    free: Plan,
    now: datetime,
) -> Period:
    """Which period a charge at `now` belongs to (docs/billing.md § Periods): the paid one
    while the subscription is active or trialing, inside its period and on a plan the
    deployment knows; Free's month otherwise. Read at every charge: nothing waits for a
    webhook to expire access."""
    if (
        subscription is not None
        and subscription.status in PAID_STATUSES
        and subscription.period_start <= now < subscription.period_end
        and subscription.plan_id in plans
    ):
        return Period(
            subscription.period_start, subscription.period_end, plans[subscription.plan_id]
        )
    start, end = month_bounds(now)
    return Period(start, end, free)
