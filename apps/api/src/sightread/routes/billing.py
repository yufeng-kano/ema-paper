"""Control plane `/api/billing` — the plan, the period's balance and the rates, and the
Usage tab's credits view (docs/billing.md § Routes). The checkout, portal, webhook and
catalog routes land with the Kano Pay step of docs/billing.md § Transition.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query
from sqlalchemy import case, func, select

from ..auth.deps import AppSettings, DbSession, SessionUser
from ..billing import ledger
from ..billing.plans import FREE_PLAN_ID
from ..db.models import CreditEntry, Subscription, utcnow

router = APIRouter(prefix="/api/billing", tags=["billing"])


def rates(settings: AppSettings) -> dict:
    """Credits per parsed page and per 1K tokens of each offered chat model, derived from
    the operator's prices and the worth of a credit — never typed into the frontend."""
    return {
        "parse_per_page": settings.parse_credits_per_page,
        "chat": {
            model: {
                field: float(value)
                for field, value in settings.model_prices[model]
                .per_thousand(settings.credit_usd)
                .items()
            }
            for model in settings.chat_model_list
            if model in settings.model_prices
        },
    }


@router.get("")
async def billing(user: SessionUser, db: DbSession, settings: AppSettings):
    balance = await ledger.read_balance(db, settings, user.id)
    plan = balance.period.plan
    subscription = await db.get(Subscription, user.id)
    return {
        "plan": {
            "id": plan.id,
            "name": plan.id,
            "credits": plan.credits,
            "storage_bytes": plan.storage_bytes,
            "sandbox": plan.sandbox,
            "max_jobs": plan.max_jobs,
        },
        "subscription": (
            None
            if subscription is None
            else {
                "status": subscription.status,
                "plan_id": subscription.plan_id,
                "period_start": subscription.period_start,
                "period_end": subscription.period_end,
                "cancel_at_period_end": subscription.cancel_at_period_end,
            }
        ),
        "period": {
            "start": balance.period.start,
            "end": balance.period.end,
            "granted": balance.granted,
            "spent": balance.spent,
            "remaining": balance.remaining,
        },
        "rates": rates(settings),
        # Both say what the Kano Pay step will make them say (docs/billing.md
        # § Transition): nothing is on sale and nothing is reconciled yet.
        "checkout_available": False,
        "pay_reachable": None,
    }


@router.get("/entries")
async def entries(user: SessionUser, db: DbSession, days: int = Query(default=30, ge=1, le=365)):
    """Credits per UTC day and per agent over the window, from the entries that cost
    something: reserved and consumed, never released (docs/billing.md § Routes)."""
    since = utcnow() - timedelta(days=days)
    day = func.date(CreditEntry.created_at)
    in_window = (
        CreditEntry.user_id == user.id,
        CreditEntry.created_at >= since,
        CreditEntry.state != ledger.RELEASED,
    )
    per_day = (
        await db.execute(
            select(
                day,
                func.sum(case((CreditEntry.kind == ledger.PARSE, CreditEntry.amount), else_=0)),
                func.sum(case((CreditEntry.kind == ledger.CHAT, CreditEntry.amount), else_=0)),
            )
            .where(*in_window)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    per_agent = (
        await db.execute(
            select(CreditEntry.kind, func.sum(CreditEntry.amount))
            .where(*in_window)
            .group_by(CreditEntry.kind)
            .order_by(CreditEntry.kind)
        )
    ).all()
    return {
        "days": days,
        "per_day": [
            {"date": str(row[0])[:10], "parse": int(row[1] or 0), "chat": int(row[2] or 0)}
            for row in per_day
        ],
        "per_agent": [{"agent": row[0], "credits": int(row[1] or 0)} for row in per_agent],
    }


async def me_fields(db: DbSession, settings: AppSettings, user_id: int) -> dict:
    """What `GET /api/me` adds for the shell: the plan and the balance (docs/billing.md
    § Routes)."""
    balance = await ledger.read_balance(db, settings, user_id)
    plan = balance.period.plan
    return {
        "plan": FREE_PLAN_ID if plan.free else plan.id,
        "credits_remaining": balance.remaining,
    }
