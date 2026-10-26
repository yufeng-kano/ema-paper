"""The Kano Pay adapter (docs/billing.md § Kano Pay): the authenticated App API, the
signed notifications, and the one way a `subscriptions` row is written — from Pay's
canonical answer, never from a notification's body, and never backwards.

Everything Pay says is re-validated here: the App it names, the subject, the shape of
the ids, the origin of the URLs it hands the browser. Tests stub the transport with
`respx`; nothing here can reach the real service in a test.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Checkout, CreditPack, PackCheckout, Subscription, utcnow
from .plans import PAID_STATUSES

logger = logging.getLogger(__name__)

STATUSES = frozenset(
    {
        "active",
        "trialing",
        "pending",
        "past_due",
        "on_hold",
        "paused",
        "canceled",
        "expired",
        "failed",
    }
)
# A purchase is listed once paid, and stays listed refunded (kano-pay § Purchase payload).
PURCHASE_STATUSES = frozenset({"paid", "refunded"})
_ID = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_AMOUNT = re.compile(r"^\d{1,12}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_SIGNATURE = re.compile(r"^ts=(\d{1,20});h1=([0-9a-f]{64})$")


class PayUnavailable(Exception):
    """Pay could not be reached, answered 5xx, or answered something that did not survive
    validation: the caller gets 503 and nothing stored changes."""


class PayRefused(Exception):
    """Pay refused the request with a 4xx of its own (a plan it does not sell, a checkout
    that conflicts with a subscription): the user's request is refused, not retried."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Kano Pay refused with {status}")
        self.status = status


@dataclass(frozen=True)
class PaySubscription:
    subscription_id: str
    status: str
    plan_id: str
    period_start: datetime
    period_end: datetime
    cancel_at_period_end: bool
    version: int


@dataclass(frozen=True)
class PayPurchase:
    """One of the subject's paid or refunded purchases, as Pay lists them
    (docs/billing.md § Kano Pay, *Purchase notifications*)."""

    purchase_id: str
    pack_id: str
    status: str
    paid_at: datetime
    version: int
    amount: str | None
    currency: str | None


@dataclass(frozen=True)
class CatalogPlan:
    plan_id: str
    product_id: str
    name: str
    amount: str
    currency: str
    cycle: dict


@dataclass(frozen=True)
class CatalogPack:
    pack_id: str
    product_id: str
    name: str
    amount: str
    currency: str


@dataclass(frozen=True)
class Catalog:
    checkout_enabled: bool
    plans: dict[str, CatalogPlan]
    packs: dict[str, CatalogPack] = field(default_factory=dict)


def _str(data: dict, key: str, *, pattern: re.Pattern[str] | None = _ID) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (pattern is not None and not pattern.match(value)):
        raise PayUnavailable(f"Kano Pay's answer carries no valid {key}")
    return value


def _millis(data: dict, key: str) -> datetime:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayUnavailable(f"Kano Pay's answer carries no valid {key}")
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def parse_subscription(data: object, app_id: str, subject: str) -> PaySubscription:
    """Pay's subscription payload (`kano-pay/docs/integrating-apps.md`), re-validated:
    it must be this App's and this subject's, with Pay's status vocabulary."""
    if not isinstance(data, dict):
        raise PayUnavailable("Kano Pay's subscription is not an object")
    if data.get("appId") != app_id or data.get("subjectId") != subject:
        raise PayUnavailable("Kano Pay's subscription names another App or subject")
    status = _str(data, "status")
    if status not in STATUSES:
        raise PayUnavailable(f"Kano Pay's subscription has an unknown status {status!r}")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise PayUnavailable("Kano Pay's subscription carries no valid version")
    cancel = data.get("cancelAtPeriodEnd", False)
    if not isinstance(cancel, bool):
        raise PayUnavailable("Kano Pay's subscription carries no valid cancelAtPeriodEnd")
    # A plan Pay no longer names is kept as an empty id: stored, and it grants Free.
    plan_id = data.get("planId")
    if plan_id is None:
        plan_id = ""
    elif not isinstance(plan_id, str) or len(plan_id) > 64:
        raise PayUnavailable("Kano Pay's subscription carries no valid planId")
    period_start = _millis(data, "periodStart")
    period_end = _millis(data, "periodEnd")
    if period_end <= period_start:
        # The schema's zero example, or a subscription with no interval yet: a row the
        # database would refuse, and one that could never grant anything.
        raise PayUnavailable("Kano Pay's subscription has no billing interval")
    return PaySubscription(
        subscription_id=_str(data, "subscriptionId"),
        status=status,
        plan_id=plan_id,
        period_start=period_start,
        period_end=period_end,
        cancel_at_period_end=cancel,
        version=version,
    )


def parse_purchase(data: object, app_id: str, subject: str) -> PayPurchase:
    """Pay's purchase payload (`kano-pay/docs/integrating-apps.md` § Purchase payload),
    re-validated: this App's and this subject's, paid or refunded, with a paid time."""
    if not isinstance(data, dict):
        raise PayUnavailable("Kano Pay's purchase is not an object")
    if data.get("appId") != app_id or data.get("subjectId") != subject:
        raise PayUnavailable("Kano Pay's purchase names another App or subject")
    status = _str(data, "status")
    if status not in PURCHASE_STATUSES:
        raise PayUnavailable(f"Kano Pay's purchase has an unknown status {status!r}")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise PayUnavailable("Kano Pay's purchase carries no valid version")
    pack_id = data.get("packId")
    if not isinstance(pack_id, str) or not 0 < len(pack_id) <= 64:
        raise PayUnavailable("Kano Pay's purchase carries no valid packId")
    amount, currency = data.get("amount"), data.get("currency")
    return PayPurchase(
        purchase_id=_str(data, "purchaseId"),
        pack_id=pack_id,
        status=status,
        paid_at=_millis(data, "paidAt"),
        version=version,
        amount=amount if isinstance(amount, str) and _AMOUNT.match(amount) else None,
        currency=currency if isinstance(currency, str) and _CURRENCY.match(currency) else None,
    )


def _own_origin(url: str, pay_url: str) -> bool:
    """A URL Pay hands the browser must be on Pay's own origin: https (or the http
    localhost of a local setup), the same host and port."""
    try:
        given, own = urlsplit(url), urlsplit(pay_url)
    except ValueError:
        return False
    local = own.scheme == "http" and own.hostname in ("localhost", "127.0.0.1")
    return (
        given.scheme == own.scheme
        and (given.scheme == "https" or local)
        and given.netloc == own.netloc
        and bool(given.netloc)
    )


class PayClient:
    """`/v1/*` of Kano Pay with the App's service key (docs/billing.md § Kano Pay)."""

    def __init__(self, settings: Settings) -> None:
        self.url = settings.kano_pay_url
        self.app_id = settings.kano_pay_app_id
        self.timeout = settings.kano_pay_timeout_seconds
        self.catalog_ttl = settings.kano_pay_catalog_ttl_seconds
        self._headers = {
            "Authorization": f"Bearer {settings.kano_pay_api_key}",
            "X-App-Id": settings.kano_pay_app_id,
        }

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, headers: dict | None = None
    ) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(
                    method,
                    f"{self.url}{path}",
                    json=json,
                    headers={**self._headers, **(headers or {})},
                )
        except httpx.HTTPError as exc:
            # The exception's text can carry the URL; the path is enough to find it.
            raise PayUnavailable(f"Kano Pay unreachable on {path}") from exc
        if 400 <= response.status_code < 500:
            raise PayRefused(response.status_code)
        if response.status_code >= 500:
            raise PayUnavailable(f"Kano Pay answered {response.status_code} on {path}")
        try:
            data = response.json()
        except ValueError as exc:
            raise PayUnavailable(f"Kano Pay answered no JSON on {path}") from exc
        if not isinstance(data, dict):
            raise PayUnavailable(f"Kano Pay answered no object on {path}")
        return data

    async def subscription(self, subject: str) -> PaySubscription | None:
        data = await self._request("GET", f"/v1/subscriptions/{subject}")
        if data.get("subscription") is None:
            return None
        return parse_subscription(data["subscription"], self.app_id, subject)

    async def create_checkout(
        self, subject: str, plan_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        """`(checkoutUrl, transactionId)`: the URL validated to Pay's own origin."""
        data = await self._request(
            "POST",
            "/v1/checkouts",
            json={"subjectId": subject, "planId": plan_id},
            headers={"Idempotency-Key": idempotency_key},
        )
        url = _str(data, "checkoutUrl", pattern=None)
        if not _own_origin(url, self.url):
            raise PayUnavailable("Kano Pay's checkout URL is not on Pay's origin")
        return url, _str(data, "transactionId")

    async def purchases(self, subject: str) -> list[PayPurchase]:
        """The subject's paid and refunded purchases, oldest first, each re-validated."""
        data = await self._request("GET", f"/v1/purchases/{subject}")
        rows = data.get("purchases")
        if not isinstance(rows, list):
            raise PayUnavailable("Kano Pay's purchases are not a list")
        return [parse_purchase(row, self.app_id, subject) for row in rows]

    async def create_purchase(
        self, subject: str, pack_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        """`(checkoutUrl, purchaseId)` of a pack's checkout: the URL validated to Pay's
        own origin (docs/billing.md § Kano Pay, *Pack checkout*)."""
        data = await self._request(
            "POST",
            "/v1/purchases",
            json={"subjectId": subject, "packId": pack_id},
            headers={"Idempotency-Key": idempotency_key},
        )
        url = _str(data, "checkoutUrl", pattern=None)
        if not _own_origin(url, self.url):
            raise PayUnavailable("Kano Pay's checkout URL is not on Pay's origin")
        return url, _str(data, "purchaseId")

    async def portal(self, subject: str) -> str:
        data = await self._request("POST", "/v1/portal", json={"subjectId": subject})
        url = _str(data, "url", pattern=None)
        if not url.startswith("https://"):
            raise PayUnavailable("Kano Pay's portal URL is not https")
        return url

    async def catalog(self) -> Catalog:
        data = await self._request("GET", "/v1/catalog")
        plans: dict[str, CatalogPlan] = {}
        for entry in data.get("plans") or []:
            if not isinstance(entry, dict):
                continue
            cycle = entry.get("cycle")
            if not isinstance(cycle, dict):
                continue
            try:
                plan = CatalogPlan(
                    plan_id=_str(entry, "planId"),
                    product_id=_str(entry, "productId"),
                    name=_str(entry, "name", pattern=None),
                    amount=_str(entry, "amount", pattern=re.compile(r"^\d{1,12}$")),
                    currency=_str(entry, "currency", pattern=re.compile(r"^[A-Z]{3}$")),
                    cycle={
                        "interval": _str(cycle, "interval"),
                        "frequency": int(cycle.get("frequency") or 1),
                    },
                )
            except (PayUnavailable, TypeError, ValueError):
                # One malformed entry leaves the catalog; it does not hide the others.
                continue
            plans[plan.plan_id] = plan
        packs: dict[str, CatalogPack] = {}
        for entry in data.get("packs") or []:
            if not isinstance(entry, dict):
                continue
            try:
                pack = CatalogPack(
                    pack_id=_str(entry, "packId"),
                    product_id=_str(entry, "productId"),
                    name=_str(entry, "name", pattern=None),
                    amount=_str(entry, "amount", pattern=_AMOUNT),
                    currency=_str(entry, "currency", pattern=_CURRENCY),
                )
            except PayUnavailable:
                continue
            packs[pack.pack_id] = pack
        return Catalog(
            checkout_enabled=data.get("checkoutEnabled") is True, plans=plans, packs=packs
        )


class CatalogCache:
    """Pay's catalog remembered for `KANO_PAY_CATALOG_TTL_SECONDS`, shared by the public
    catalog route and the signed-in billing view; an answer that failed is not remembered."""

    def __init__(self) -> None:
        self._value: Catalog | None = None
        self._until = 0.0

    async def get(self, client: PayClient) -> Catalog:
        now = time.monotonic()
        if self._value is not None and now < self._until:
            return self._value
        self._value = await client.catalog()
        self._until = now + client.catalog_ttl
        return self._value

    def clear(self) -> None:
        self._value = None
        self._until = 0.0


def on_sale(settings: Settings, catalog: Catalog) -> dict[str, CatalogPlan]:
    """The plans both configured here and priced at Pay: only that intersection is on
    sale (docs/billing.md § Plans)."""
    return {plan_id: plan for plan_id, plan in catalog.plans.items() if plan_id in settings.plans}


def packs_on_sale(settings: Settings, catalog: Catalog) -> dict[str, CatalogPack]:
    """The packs both configured here and priced at Pay (docs/billing.md § Top-up packs)."""
    return {pack_id: pack for pack_id, pack in catalog.packs.items() if pack_id in settings.packs}


def sign(secret: str, timestamp: int, raw: bytes) -> str:
    """`Kano-Pay-Signature` as Pay writes it, for tests and for the docs' example."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256)
    return f"ts={timestamp};h1={digest.hexdigest()}"


def verify_signature(
    header: str | None, raw: bytes, secret: str, now: float, tolerance: int
) -> bool:
    """`ts=<unix seconds>;h1=<hex HMAC-SHA256>` over `<ts>.<raw body>`, compared in
    constant time, the timestamp within `tolerance` seconds of `now` (docs/billing.md
    § Kano Pay)."""
    match = _SIGNATURE.match(header or "")
    if match is None:
        return False
    timestamp = int(match.group(1))
    if abs(now - timestamp) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256)
    return hmac.compare_digest(expected.hexdigest(), match.group(2))


async def write_subscription(db: AsyncSession, user_id: int, canonical: PaySubscription) -> bool:
    """Upsert the user's row from Pay's canonical answer, only when its version is newer
    than the stored one: an out-of-order notification never rolls access backwards.
    Returns whether anything was written. The caller commits."""
    row = await db.get(Subscription, user_id, with_for_update=True)
    if row is not None and row.version >= canonical.version:
        return False
    if row is None:
        row = Subscription(user_id=user_id, subscription_id=canonical.subscription_id)
        db.add(row)
    row.subscription_id = canonical.subscription_id
    row.status = canonical.status
    row.plan_id = canonical.plan_id
    row.period_start = canonical.period_start
    row.period_end = canonical.period_end
    row.cancel_at_period_end = canonical.cancel_at_period_end
    row.version = canonical.version
    row.updated_at = utcnow()
    await db.flush()
    return True


async def apply_canonical(db: AsyncSession, user_id: int, canonical: PaySubscription) -> bool:
    """What Pay's canonical answer changes here, whichever way it arrived — the webhook
    or a refresh: the subscription row (`write_subscription`), and the user's open
    checkout marked completed when the answer is a paid subscription updated after the
    checkout was opened — the purchase that checkout started. The caller holds the
    user's row lock and commits. Returns whether the subscription row changed."""
    written = await write_subscription(db, user_id, canonical)
    checkout = await db.get(Checkout, user_id)
    if (
        checkout is not None
        and checkout.completed_at is None
        and canonical.status in PAID_STATUSES
        and checkout.created_at <= datetime.fromtimestamp(canonical.version / 1000, tz=UTC)
    ):
        checkout.completed_at = utcnow()
        await db.flush()
    return written


async def apply_purchases(
    db: AsyncSession, settings: Settings, user_id: int, purchases: list[PayPurchase]
) -> bool:
    """What Pay's canonical purchases change here, whichever way they arrived — the
    webhook or a refresh (docs/billing.md § Kano Pay, *Purchase notifications*): a
    `credit_packs` row created for a purchase not yet known, with the credits and the
    validity its pack is configured with now; an existing row's status following Pay's
    only when Pay's version is newer, never backwards, its credits never lowered; the
    pack checkout the purchase came from marked completed. A pack this deployment does
    not configure is logged and skipped. The caller holds the user's row lock and
    commits. Returns whether anything was written."""
    written = False
    checkout = await db.get(PackCheckout, user_id)
    for purchase in purchases:
        row = await db.get(CreditPack, purchase.purchase_id, with_for_update=True)
        if row is None:
            config = settings.packs.get(purchase.pack_id)
            if config is None:
                logger.warning(
                    "Kano Pay purchase %s is of a pack this deployment does not configure: %s",
                    purchase.purchase_id,
                    purchase.pack_id,
                )
                continue
            db.add(
                CreditPack(
                    id=purchase.purchase_id,
                    user_id=user_id,
                    pack_id=purchase.pack_id,
                    credits=config.credits,
                    status=purchase.status,
                    purchased_at=purchase.paid_at,
                    expires_at=purchase.paid_at + timedelta(days=config.valid_days),
                    version=purchase.version,
                )
            )
            written = True
        elif row.user_id != user_id:
            logger.warning("Kano Pay purchase %s belongs to another user", purchase.purchase_id)
            continue
        elif purchase.version > row.version:
            row.status = purchase.status
            row.version = purchase.version
            row.updated_at = utcnow()
            written = True
        if (
            checkout is not None
            and checkout.completed_at is None
            and (
                checkout.purchase_id == purchase.purchase_id
                # Pay answered, the app did not get to keep the id: the purchase paid
                # after the checkout opened is the one it started.
                or (checkout.purchase_id is None and purchase.paid_at >= checkout.created_at)
            )
        ):
            checkout.completed_at = utcnow()
            written = True
    if written:
        await db.flush()
    return written
