"""Control plane `/api/billing` — the plan, the period's balance, the top-up packs and
the rates, the Usage tab's credits view, and the Kano Pay surface: checkout (a plan's or a
pack's), portal, the public catalog and the signed webhook (docs/billing.md § Kano Pay,
§ Routes).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import timedelta
from typing import NamedTuple

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select

from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..billing import codes, ledger, pay
from ..billing.plans import FREE_PLAN_ID
from ..db.models import (
    Checkout,
    CreditEntry,
    CreditPack,
    PackCheckout,
    PayEvent,
    Redemption,
    Subscription,
    User,
    utcnow,
)
from ..errors import ApiError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/billing", tags=["billing"])


class CheckoutRequest(BaseModel):
    plan_id: str


class PackCheckoutRequest(BaseModel):
    pack_id: str


class RedeemRequest(BaseModel):
    # As typed; case, spaces and hyphens are dropped before the lookup.
    code: str = Field(max_length=64)


class Sale(NamedTuple):
    """Pay's `checkoutEnabled`, and the plans and the packs both configured here and
    priced there (docs/billing.md § Plans, § Top-up packs)."""

    enabled: bool
    plans: dict[str, pay.CatalogPlan]
    packs: dict[str, pay.CatalogPack]


def _catalog_cache(request: Request) -> pay.CatalogCache:
    return request.app.state.pay_catalog


async def _may_have_purchases(db: DbSession, user_id: int) -> bool:
    """Whether Pay may list purchases worth fetching for the user: a pack checkout still
    open here, or a paid pack whose refund would arrive the same way. A user with neither
    costs the refresh no second call (docs/billing.md § Kano Pay, *Refresh*)."""
    checkout = await db.get(PackCheckout, user_id)
    if checkout is not None and checkout.completed_at is None:
        return True
    paid = await db.execute(
        select(CreditPack.id)
        .where(
            CreditPack.user_id == user_id,
            CreditPack.status == ledger.PACK_PAID,
            # A code's credits are not Pay's to refund (docs/billing.md § Redeem codes).
            CreditPack.source == ledger.PACK_SOURCE_PAY,
        )
        .limit(1)
    )
    return paid.first() is not None


async def _refresh(
    request: Request, db: DbSession, settings: AppSettings, user_id: int
) -> bool | None:
    """Reconcile the user's rows with Pay at most once per `BILLING_REFRESH_SECONDS` — the
    same fetches the webhook makes, the subscription and, for a user who bought or is
    buying a pack, the purchases — so a missed notification is recovered on the next
    visit. None: Pay is not configured; False: Pay could not be reached, and the stored
    rows stay as they are (docs/billing.md § Kano Pay)."""
    if not settings.kano_pay_configured:
        return None
    stamps: dict[int, float] = request.app.state.pay_refreshed
    now = time.monotonic()
    last = stamps.get(user_id)
    if last is not None and now - last < settings.billing_refresh_seconds:
        return True
    client = pay.PayClient(settings)
    try:
        canonical = await client.subscription(str(user_id))
        purchases = (
            await client.purchases(str(user_id)) if await _may_have_purchases(db, user_id) else []
        )
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("billing refresh of user %d failed: %s", user_id, exc)
        return False
    if canonical is not None or purchases:
        # The same writes the webhook makes, under the same lock.
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())
        if canonical is not None:
            await pay.apply_canonical(db, user_id, canonical)
        if purchases:
            await pay.apply_purchases(db, settings, user_id, purchases)
        await db.commit()
    stamps[user_id] = now
    return True


async def _on_sale(request: Request, settings: AppSettings) -> Sale | None:
    """What is on sale, from the shared catalog cache; None when Pay is unconfigured or
    unreachable."""
    if not settings.kano_pay_configured:
        return None
    try:
        catalog = await _catalog_cache(request).get(pay.PayClient(settings))
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("Kano Pay catalog unavailable: %s", exc)
        return None
    return Sale(
        catalog.checkout_enabled,
        pay.on_sale(settings, catalog),
        pay.packs_on_sale(settings, catalog),
    )


def rates(settings: AppSettings) -> dict:
    """Credits per parsed page and per 1K tokens of each priced model, in the price
    table's order — the chat models among them (docs/billing.md § Models) — derived from
    the operator's prices and the worth of a credit, never typed into the frontend."""
    return {
        "parse_per_page": settings.parse_credits_per_page,
        # What a web search by the agent costs, null where the deployment offers none
        # (docs/chat.md § Tools, docs/billing.md § Routes).
        "web_search_per_call": (
            settings.web_search_credits
            if settings.chat_web_access and settings.brave_search_api_key
            else None
        ),
        "chat": {
            model: {
                field: float(value)
                for field, value in price.per_thousand(settings.credit_usd).items()
            }
            for model, price in settings.model_prices.items()
        },
    }


@router.get("")
async def billing(request: Request, user: SessionUser, db: DbSession, settings: AppSettings):
    reachable = await _refresh(request, db, settings, user.id)
    sale = await _on_sale(request, settings) if reachable else None
    balance = await ledger.read_balance(db, settings, user.id)
    plan = balance.period.plan
    subscription = await db.get(Subscription, user.id)
    redemptions = (
        await db.execute(
            select(Redemption)
            .where(Redemption.user_id == user.id)
            .order_by(Redemption.redeemed_at.desc(), Redemption.id.desc())
        )
    ).scalars()
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
            # A subscription's, granted days', or Free's month (docs/billing.md § Periods).
            "source": balance.period.source,
        },
        # Every pack ever bought or redeemed, newest first; the totals count the active
        # ones only (docs/billing.md § Top-up packs, § Redeem codes).
        "packs": {
            "remaining": balance.packs_remaining,
            "expires_at": balance.packs_expire_at,
            "items": [
                {
                    "id": pack.id,
                    "pack_id": pack.pack_id,
                    "credits": pack.credits,
                    "spent": pack.spent,
                    "remaining": pack.remaining,
                    "status": pack.status,
                    "active": pack.active,
                    "purchased_at": pack.purchased_at,
                    "expires_at": pack.expires_at,
                    "source": pack.source,
                    "redemption_id": pack.redemption_id,
                }
                for pack in balance.packs
            ],
        },
        # The bank of granted days and its open period, null for a user who never
        # redeemed days (docs/billing.md § Periods, *Granted days*).
        "plan_grant": (
            None
            if balance.grant is None
            else {
                "plan_id": balance.grant.plan_id,
                "days_banked": balance.grant.days_banked,
                "period_start": balance.grant.period_start,
                "period_end": balance.grant.period_end,
            }
        ),
        # Every code this user redeemed, newest first (docs/billing.md § Redeem codes).
        "redemptions": [
            {
                "id": row.id,
                "kind": row.kind,
                "credits": row.credits,
                "plan_id": row.plan_id,
                "days": row.days,
                "redeemed_at": row.redeemed_at,
            }
            for row in redemptions
        ],
        "credits_available": balance.available,
        "rates": rates(settings),
        # On sale: Pay configured and reachable, its checkout open, and at least one plan
        # both configured here and priced there (docs/billing.md § Plans).
        "checkout_available": bool(sale is not None and sale.enabled and sale.plans),
        # A pack: the same, a pack on sale, and a current period that is a paid plan's.
        "pack_checkout_available": bool(
            sale is not None and sale.enabled and sale.packs and not plan.free
        ),
        "pay_reachable": reachable,
    }


@router.get("/catalog")
async def catalog(request: Request, response: Response, settings: AppSettings):
    """Public: the plans and the packs on sale with Pay's prices and this deployment's
    grants, the Free plan and the credit rates, cached for five minutes; unconfigured or
    unreachable Pay answers `available: false` with no plans and no packs, never an error
    (docs/billing.md § Kano Pay)."""
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
        return {"available": False, "plans": [], "packs": [], **ours}
    response.headers["Cache-Control"] = f"public, max-age={settings.kano_pay_catalog_ttl_seconds}"
    return {
        "available": sale.enabled and bool(sale.plans),
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
            for plan in sale.plans.values()
        ],
        "packs": [
            {
                "pack_id": pack.pack_id,
                "name": pack.name,
                "amount": pack.amount,
                "currency": pack.currency,
                "credits": settings.packs[pack.pack_id].credits,
                "valid_days": settings.packs[pack.pack_id].valid_days,
            }
            for pack in sale.packs.values()
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
    if not sale.enabled or plan_id not in sale.plans:
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


@router.post("/packs/checkout", dependencies=[CsrfGuard])
async def pack_checkout(
    body: PackCheckoutRequest,
    request: Request,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    """A Pay checkout for a top-up pack on sale, for a user whose current period is a
    paid plan's; the `pack_checkouts` row and its idempotency key are taken as a plan's
    checkout takes `checkouts` (docs/billing.md § Top-up packs, § Kano Pay *Pack
    checkout*)."""
    pack_id = body.pack_id.strip()
    balance = await ledger.read_balance(db, settings, user.id)
    if balance.period.plan.free:
        raise ApiError(409, "plan_required", "Top-up packs are for paid plans")
    sale = await _on_sale(request, settings)
    if sale is None:
        raise ApiError(503, "pay_unavailable", "The payment service is unavailable; try again")
    if not sale.enabled or pack_id not in sale.packs:
        raise ApiError(409, "checkout_unavailable", "This pack is not on sale")
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    row = await db.get(PackCheckout, user.id)
    if (
        row is None
        or row.pack_id != pack_id
        or row.completed_at is not None
        # Its purchase is already on the books: a completed checkout whatever the row says.
        or (row.purchase_id is not None and await db.get(CreditPack, row.purchase_id) is not None)
    ):
        key = secrets.token_urlsafe(32)
        if row is None:
            row = PackCheckout(user_id=user.id, pack_id=pack_id, idempotency_key=key)
            db.add(row)
        else:
            row.pack_id = pack_id
            row.idempotency_key = key
            row.purchase_id = None
            row.completed_at = None
            row.created_at = utcnow()
    key = row.idempotency_key
    await db.commit()
    try:
        url, purchase_id = await pay.PayClient(settings).create_purchase(str(user.id), pack_id, key)
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("pack checkout of user %d failed: %s", user.id, exc)
        raise _unavailable(exc) from exc
    row = await db.get(PackCheckout, user.id)
    if row is not None and row.idempotency_key == key:
        row.purchase_id = purchase_id
        await db.commit()
    return {"url": url}


@router.post("/redeem", dependencies=[CsrfGuard])
async def redeem(body: RedeemRequest, user: SessionUser, db: DbSession, settings: AppSettings):
    """A redeem code's grant: credits as a pack, or days of a paid plan banked and, when
    nothing else pays now, started at once (docs/billing.md § Redeem codes, § Refusals).
    The code typed is never logged, refused or not."""
    granted = await codes.redeem(db, settings, user.id, body.code)
    if granted.kind == codes.CREDITS:
        return {"kind": codes.CREDITS, "credits": granted.credits, "expires_at": granted.expires_at}
    return {
        "kind": codes.PLAN,
        "plan_id": granted.plan_id,
        "days": granted.days,
        "days_banked": granted.days_banked,
        "period_end": granted.period_end,
    }


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
    subject — and the purchase id of a `purchase.updated` — then the subscription, or the
    purchases, are fetched from Pay and written from that answer. The event id and the
    write land in one commit, so a repeated delivery changes nothing; 2xx only once they
    are committed (docs/billing.md § Kano Pay)."""
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
    if not isinstance(event, dict):
        raise ApiError(400, "invalid_request", "Invalid event")
    subscription = event.get("subscription")
    purchase = event.get("purchase")
    event_id = event.get("id")
    event_type = event.get("type")
    if (
        # Exactly one of the two: a notification is a subscription's or a purchase's.
        isinstance(subscription, dict) == isinstance(purchase, dict)
        or not isinstance(event_id, str)
        or not pay._ID.match(event_id)
        or not isinstance(event_type, str)
        or not 0 < len(event_type) <= 64
    ):
        raise ApiError(400, "invalid_request", "Invalid event")
    named = subscription if isinstance(subscription, dict) else purchase
    purchase_id = named.get("purchaseId") if named is purchase else None
    if named is purchase and (not isinstance(purchase_id, str) or not pay._ID.match(purchase_id)):
        raise ApiError(400, "invalid_request", "Invalid event")
    if named.get("appId") != settings.kano_pay_app_id:
        logger.warning("Kano Pay event %s names another App", event_id)
        raise ApiError(400, "invalid_request", "Invalid event")
    subject = named.get("subjectId")
    user_id = int(subject) if isinstance(subject, str) and subject.isdigit() else None
    if user_id is None or await db.get(User, user_id) is None:
        # A deleted account's subscription still sends its ending events, and Pay retries
        # a refused delivery for ever: acknowledged, nothing fetched or recorded
        # (docs/auth.md § Deleting the account).
        # Logged as a warning: a deleted account's subscription that still bills is the
        # operator's to cancel or refund in Pay.
        logger.warning("Kano Pay event %s names a subject with no account", event_id)
        return {}
    if await db.get(PayEvent, event_id) is not None:
        return {}
    client = pay.PayClient(settings)
    canonical: pay.PaySubscription | None = None
    purchases: list[pay.PayPurchase] = []
    try:
        if named is subscription:
            canonical = await client.subscription(str(user_id))
        else:
            purchases = await client.purchases(str(user_id))
    except (pay.PayUnavailable, pay.PayRefused) as exc:
        logger.warning("Kano Pay event %s: canonical fetch failed: %s", event_id, exc)
        raise ApiError(503, "pay_unavailable", "Retry this notification") from exc
    if (named is subscription and canonical is None) or (
        named is purchase and all(row.purchase_id != purchase_id for row in purchases)
    ):
        # Pay does not list what the event names yet: nothing to write, and the event is
        # not recorded, so Pay's retry lands once it does.
        raise ApiError(503, "pay_unavailable", "Retry this notification")
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())
    if await db.get(PayEvent, event_id) is not None:
        await db.rollback()
        return {}
    db.add(PayEvent(id=event_id, type=event_type, user_id=user_id))
    if canonical is not None:
        await pay.apply_canonical(db, user_id, canonical)
    if purchases:
        await pay.apply_purchases(db, settings, user_id, purchases)
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
    """What `GET /api/me` adds for the shell: the plan, the balance a charge is measured
    against, when the period resets, and whether a pack can be offered — Pay configured, a
    pack configured and a paid plan's period, without the catalog read `GET /api/billing`
    makes (docs/billing.md § Routes)."""
    balance = await ledger.read_balance(db, settings, user_id)
    plan = balance.period.plan
    return {
        "plan": FREE_PLAN_ID if plan.free else plan.id,
        "credits_remaining": balance.available,
        "period_end": balance.period.end,
        "chat_min_credits": settings.chat_min_credits,
        "pack_checkout_available": bool(
            settings.kano_pay_configured and settings.packs and not plan.free
        ),
    }
