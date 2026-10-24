"""Control plane `/api/billing` — the plan, the period's balance and the rates, the Usage
tab's credits view, and the Kano Pay surface: checkout, portal, the public catalog and
the signed webhook (docs/billing.md § Kano Pay, § Routes).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import case, func, select

from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..billing import ledger, pay
from ..billing.plans import FREE_PLAN_ID
from ..db.models import Checkout, CreditEntry, PayEvent, Subscription, User, utcnow
from ..errors import ApiError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    plan_id: str


def _catalog_cache(request: Request) -> pay.CatalogCache:
    return request.app.state.pay_catalog


async def _refresh(
    request: Request, db: DbSession, settings: AppSettings, user_id: int
) -> bool | None:
    """Reconcile the user's row with Pay at most once per `BILLING_REFRESH_SECONDS` — the
    same fetch the webhook makes — so a missed notification is recovered on the next
    visit. None: Pay is not configured; False: Pay could not be reached, and the stored
    row stays as it is (docs/billing.md § Kano Pay)."""
    if not settings.kano_pay_configured:
        return None
    stamps: dict[int, float] = request.app.state.pay_refreshed
    now = time.monotonic()
    last = stamps.get(user_id)
    if last is not None and now - last < settings.billing_refresh_seconds:
        return True
    try:
        canonical = await pay.PayClient(settings).subscription(str(user_id))
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("billing refresh of user %d failed: %s", user_id, exc)
        return False
    if canonical is not None:
        # The same write the webhook makes, under the same lock.
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
        await pay.apply_canonical(db, user_id, canonical)
        await db.commit()
    stamps[user_id] = now
    return True


async def _on_sale(
    request: Request, settings: AppSettings
) -> tuple[bool, dict[str, pay.CatalogPlan]] | None:
    """Pay's `checkoutEnabled` and the plans on sale, from the shared cache; None when
    Pay is unconfigured or unreachable."""
    if not settings.kano_pay_configured:
        return None
    try:
        catalog = await _catalog_cache(request).get(pay.PayClient(settings))
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("Kano Pay catalog unavailable: %s", exc)
        return None
    return catalog.checkout_enabled, pay.on_sale(settings, catalog)


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
async def billing(request: Request, user: SessionUser, db: DbSession, settings: AppSettings):
    reachable = await _refresh(request, db, settings, user.id)
    sale = await _on_sale(request, settings) if reachable else None
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
        # On sale: Pay configured and reachable, its checkout open, and at least one plan
        # both configured here and priced there (docs/billing.md § Plans).
        "checkout_available": bool(sale is not None and sale[0] and sale[1]),
        "pay_reachable": reachable,
    }


@router.get("/catalog")
async def catalog(request: Request, response: Response, settings: AppSettings):
    """Public: the plans on sale with Pay's prices and this deployment's grants, the Free
    plan and the credit rates, cached for five minutes; unconfigured or unreachable Pay
    answers `available: false` with no plans, never an error (docs/billing.md § Kano Pay)."""
    # The deployment's own side of the page: the Free plan and the rates, which Pay has no
    # say in, answered whether or not Pay is.
    free = settings.free_plan
    ours = {
        "free": {
            "credits": free.credits,
            "storage_bytes": free.storage_bytes,
            "sandbox": free.sandbox,
            "max_jobs": free.max_jobs,
        },
        "rates": rates(settings),
    }
    sale = await _on_sale(request, settings)
    if sale is None:
        response.headers["Cache-Control"] = "no-store"
        return {"available": False, "plans": [], **ours}
    enabled, plans = sale
    response.headers["Cache-Control"] = f"public, max-age={settings.kano_pay_catalog_ttl_seconds}"
    return {
        "available": enabled and bool(plans),
        **ours,
        "plans": [
            {
                "plan_id": plan.plan_id,
                "name": plan.name,
                "amount": plan.amount,
                "currency": plan.currency,
                "cycle": plan.cycle,
                "credits": settings.plans[plan.plan_id].credits,
                "storage_bytes": settings.plans[plan.plan_id].storage_bytes,
                "sandbox": settings.plans[plan.plan_id].sandbox,
                "max_jobs": settings.plans[plan.plan_id].max_jobs,
            }
            for plan in plans.values()
        ],
    }


def _unavailable(exc: Exception) -> ApiError:
    if isinstance(exc, pay.PayRefused):
        # Pay refused on its own terms — the plan is not sold, a subscription or an open
        # checkout stands in the way: not a checkout the user can have now.
        return ApiError(409, "checkout_unavailable", "Checkout is not available right now")
    return ApiError(503, "pay_unavailable", "The payment service is unavailable; try again")


@router.post("/checkout", dependencies=[CsrfGuard])
async def checkout(
    body: CheckoutRequest,
    request: Request,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """A Pay checkout for a plan on sale: the idempotency key is committed before Pay is
    called, so a retry after an ambiguous timeout repeats the same request and gets the
    same checkout (docs/billing.md § Kano Pay)."""
    plan_id = body.plan_id.strip()
    sale = await _on_sale(request, settings)
    if sale is None:
        raise ApiError(503, "pay_unavailable", "The payment service is unavailable; try again")
    enabled, plans = sale
    if not enabled or plan_id not in plans:
        raise ApiError(409, "checkout_unavailable", "This plan is not on sale")
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    row = await db.get(Checkout, user.id)
    if row is None or row.plan_id != plan_id or row.completed_at is not None:
        # A fresh key for a new purchase; the same plan's open checkout keeps its key.
        key = secrets.token_urlsafe(32)
        if row is None:
            row = Checkout(user_id=user.id, plan_id=plan_id, idempotency_key=key)
            db.add(row)
        else:
            row.plan_id = plan_id
            row.idempotency_key = key
            row.transaction_id = None
            row.completed_at = None
            row.created_at = utcnow()
    key = row.idempotency_key
    await db.commit()
    try:
        url, transaction_id = await pay.PayClient(settings).create_checkout(
            str(user.id), plan_id, key
        )
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("checkout of user %d failed: %s", user.id, exc)
        raise _unavailable(exc) from exc
    row = await db.get(Checkout, user.id)
    if row is not None and row.idempotency_key == key:
        row.transaction_id = transaction_id
        await db.commit()
    return {"url": url}


@router.post("/portal", dependencies=[CsrfGuard])
async def portal(user: SessionUser, db: DbSession, settings: AppSettings):
    """Pay's customer portal, fresh every time, for a user who has a subscription of
    their own, active or lapsed (docs/billing.md § Kano Pay)."""
    if not settings.kano_pay_configured:
        raise ApiError(503, "pay_unavailable", "The payment service is unavailable; try again")
    if await db.get(Subscription, user.id) is None:
        raise ApiError(404, "invalid_request", "No subscription to manage")
    try:
        url = await pay.PayClient(settings).portal(str(user.id))
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("portal of user %d failed: %s", user.id, exc)
        raise _unavailable(exc) from exc
    return {"url": url}


@router.post("/pay-webhook")
async def pay_webhook(request: Request, db: DbSession, settings: AppSettings):
    """A signed notification is a hint, never data: verified, read for its id, type and
    subject, then the subscription is fetched from Pay and written from that answer. The
    event id and the write land in one commit, so a repeated delivery changes nothing;
    2xx only once they are committed (docs/billing.md § Kano Pay)."""
    if not settings.kano_pay_configured:
        raise ApiError(503, "pay_unavailable", "Billing is not configured")
    raw = await request.body()
    if len(raw) > settings.kano_pay_event_max_bytes:
        raise ApiError(413, "invalid_request", "Event too large")
    if not pay.verify_signature(
        request.headers.get("Kano-Pay-Signature"),
        raw,
        settings.kano_pay_webhook_secret,
        time.time(),
        settings.kano_pay_signature_tolerance_seconds,
    ):
        raise ApiError(401, "auth", "Invalid signature")
    try:
        event = json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid_request", "Invalid event") from None
    subscription = event.get("subscription") if isinstance(event, dict) else None
    event_id = event.get("id") if isinstance(event, dict) else None
    event_type = event.get("type") if isinstance(event, dict) else None
    if (
        not isinstance(subscription, dict)
        or not isinstance(event_id, str)
        or not pay._ID.match(event_id)
        or not isinstance(event_type, str)
        or not 0 < len(event_type) <= 64
    ):
        raise ApiError(400, "invalid_request", "Invalid event")
    if subscription.get("appId") != settings.kano_pay_app_id:
        logger.warning("Kano Pay event %s names another App", event_id)
        raise ApiError(400, "invalid_request", "Invalid event")
    subject = subscription.get("subjectId")
    user_id = int(subject) if isinstance(subject, str) and subject.isdigit() else None
    if user_id is None or await db.get(User, user_id) is None:
        logger.warning("Kano Pay event %s names an unknown subject", event_id)
        raise ApiError(404, "invalid_request", "Unknown subject")
    if await db.get(PayEvent, event_id) is not None:
        return {}
    try:
        canonical = await pay.PayClient(settings).subscription(str(user_id))
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("Kano Pay event %s: canonical fetch failed: %s", event_id, exc)
        raise ApiError(503, "pay_unavailable", "Retry this notification") from exc
    if canonical is None:
        # Pay has no subscription for the subject yet: nothing to write, and the event
        # is not recorded, so Pay's retry lands once there is one.
        raise ApiError(503, "pay_unavailable", "Retry this notification")
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    if await db.get(PayEvent, event_id) is not None:
        await db.rollback()
        return {}
    db.add(PayEvent(id=event_id, type=event_type, user_id=user_id))
    await pay.apply_canonical(db, user_id, canonical)
    await db.commit()
    request.app.state.pay_refreshed[user_id] = time.monotonic()
    return {}


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
        "chat_min_credits": settings.chat_min_credits,
    }
