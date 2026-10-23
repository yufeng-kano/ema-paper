"""Kano Pay (docs/billing.md § Acceptance): the signed webhook written from Pay's
canonical answer, the checkout's idempotency, the public catalog, the refresh, the
portal, and the entitlements a bought plan grants. Pay exists only inside respx."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.billing import pay
from sightread.db.models import Checkout, Job, PayEvent, Subscription, utcnow
from tests.conftest import (
    CSRF_HEADERS,
    PAY_APP_ID,
    PAY_SETTINGS,
    PAY_URL,
    PAY_WEBHOOK_SECRET,
    grant_plan,
)
from tests.test_chat import MESSAGES_URL, _seed_document, assistant_turn
from tests.test_library import MODEL, _ready_to_parse, _upload
from tests.test_parse_end_to_end import _connect_kano

SUBSCRIPTIONS_URL = f"{PAY_URL}/v1/subscriptions/"
CATALOG_URL = f"{PAY_URL}/v1/catalog"
CHECKOUTS_URL = f"{PAY_URL}/v1/checkouts"
PORTAL_URL = f"{PAY_URL}/v1/portal"


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def canonical(
    user_id: int,
    *,
    version: int = 1,
    plan_id: str | None = "pro",
    status: str = "active",
    app_id: str = PAY_APP_ID,
    cancel: bool = False,
) -> dict:
    """`GET /v1/subscriptions/:subjectId` as Pay answers it."""
    now = datetime.now(UTC)
    return {
        "subscription": {
            "appId": app_id,
            "subjectId": str(user_id),
            "subscriptionId": "sub_canonical",
            "status": status,
            "planId": plan_id,
            "periodStart": _ms(now - timedelta(days=1)),
            "periodEnd": _ms(now + timedelta(days=29)),
            "cancelAtPeriodEnd": cancel,
            "version": version,
            "active": status == "active",
        }
    }


def catalog(*, enabled: bool = True, plans: tuple[str, ...] = ("pro",)) -> dict:
    return {
        "checkoutEnabled": enabled,
        "plans": [
            {
                "planId": plan,
                "productId": f"prod_{plan}",
                "name": plan.title(),
                "amount": "1500",
                "currency": "USD",
                "cycle": {"interval": "month", "frequency": 1},
            }
            for plan in plans
        ],
    }


async def _signed_in(client: AsyncClient) -> int:
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    return (await client.get("/api/me")).json()["user"]["id"]


def _event(user_id: int, event_id: str = "evt_1", **subscription) -> bytes:
    body = {
        "id": event_id,
        "type": "subscription.updated",
        "subscription": {"appId": PAY_APP_ID, "subjectId": str(user_id), **subscription},
    }
    return json.dumps(body).encode()


async def _notify(
    client: AsyncClient,
    raw: bytes,
    *,
    timestamp: int | None = None,
    secret: str = PAY_WEBHOOK_SECRET,
) -> httpx.Response:
    ts = int(time.time()) if timestamp is None else timestamp
    return await client.post(
        "/api/billing/pay-webhook",
        content=raw,
        headers={
            "Kano-Pay-Signature": pay.sign(secret, ts, raw),
            "Content-Type": "application/json",
        },
    )


async def _row(client: AsyncClient, user_id: int) -> Subscription | None:
    async with client.app.state.sessionmaker() as db:
        return await db.get(Subscription, user_id)


async def _events(client: AsyncClient) -> list[str]:
    async with client.app.state.sessionmaker() as db:
        return list((await db.execute(select(PayEvent.id).order_by(PayEvent.id))).scalars())


async def _plan_shown(client: AsyncClient) -> str:
    return (await client.get("/api/me")).json()["plan"]


# --- the signature ----------------------------------------------------------------------


def test_the_signature_covers_the_timestamp_and_the_exact_bytes() -> None:
    raw = b'{"id":"evt"}'
    now = 1_700_000_000
    header = pay.sign("secret", now, raw)
    assert pay.verify_signature(header, raw, "secret", now + 100)
    assert pay.verify_signature(header, raw, "secret", now - 300)
    # One byte more and the signature no longer covers the body; another key, another
    # timestamp, or a timestamp past the tolerance, and it is refused.
    assert not pay.verify_signature(header, raw + b" ", "secret", now)
    assert not pay.verify_signature(header, raw, "other", now)
    assert not pay.verify_signature(header, raw, "secret", now + 301)
    assert not pay.verify_signature(
        header.replace(f"ts={now}", f"ts={now + 1}"), raw, "secret", now
    )
    assert not pay.verify_signature(None, raw, "secret", now)
    assert not pay.verify_signature("ts=1;h1=zz", raw, "secret", now)
    assert not pay.verify_signature(header.upper(), raw, "secret", now)


# --- the webhook ------------------------------------------------------------------------


@respx.mock
async def test_a_notification_is_written_from_the_canonical_fetch_not_its_body(
    make_client,
) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    assert await _plan_shown(client) == "free"
    fetch = respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        return_value=httpx.Response(200, json=canonical(user_id, version=7))
    )

    # The body claims a plan and a status of its own; neither is read.
    received = await _notify(
        client, _event(user_id, planId="enterprise", status="active", version=99)
    )

    assert received.status_code == 200, received.text
    assert fetch.call_count == 1
    assert fetch.calls[0].request.headers["Authorization"] == "Bearer pay-api-key-not-a-real-one"
    assert fetch.calls[0].request.headers["X-App-Id"] == PAY_APP_ID
    row = await _row(client, user_id)
    assert (row.plan_id, row.status, row.version, row.subscription_id) == (
        "pro",
        "active",
        7,
        "sub_canonical",
    )
    assert await _events(client) == ["evt_1"]
    assert await _plan_shown(client) == "pro"
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog()))
    assert (await client.get("/api/billing")).json()["period"]["granted"] == 5000


@respx.mock
async def test_a_wrong_or_stale_signature_is_refused_before_anything_is_read(
    make_client,
) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    fetch = respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        return_value=httpx.Response(200, json=canonical(user_id))
    )
    raw = _event(user_id)

    wrong = await _notify(client, raw, secret="another-secret-not-a-real-one")
    stale = await _notify(client, raw, timestamp=int(time.time()) - 600)
    ahead = await _notify(client, raw, timestamp=int(time.time()) + 600)
    unsigned = await client.post("/api/billing/pay-webhook", content=raw)

    assert [r.status_code for r in (wrong, stale, ahead, unsigned)] == [401, 401, 401, 401]
    assert fetch.call_count == 0
    assert await _row(client, user_id) is None
    assert await _events(client) == []


@respx.mock
async def test_a_duplicate_event_id_is_answered_without_a_second_write(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    answers = iter([canonical(user_id, version=1), canonical(user_id, version=2, cancel=True)])
    fetch = respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        side_effect=lambda request: httpx.Response(200, json=next(answers))
    )

    assert (await _notify(client, _event(user_id, "evt_1"))).status_code == 200
    # The same delivery again: 200, and Pay is not asked a second time.
    assert (await _notify(client, _event(user_id, "evt_1"))).status_code == 200
    assert fetch.call_count == 1
    assert (await _row(client, user_id)).version == 1
    # A new event id is a new fetch.
    assert (await _notify(client, _event(user_id, "evt_2"))).status_code == 200
    row = await _row(client, user_id)
    assert (row.version, row.cancel_at_period_end) == (2, True)
    assert await _events(client) == ["evt_1", "evt_2"]


@respx.mock
async def test_a_stale_version_never_rolls_access_backwards(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    answers = iter(
        [canonical(user_id, version=5), canonical(user_id, version=3, status="canceled")]
    )
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        side_effect=lambda request: httpx.Response(200, json=next(answers))
    )

    assert (await _notify(client, _event(user_id, "evt_5"))).status_code == 200
    assert (await _notify(client, _event(user_id, "evt_3"))).status_code == 200

    row = await _row(client, user_id)
    assert (row.version, row.status) == (5, "active")
    assert await _events(client) == ["evt_3", "evt_5"]
    assert await _plan_shown(client) == "pro"


@respx.mock
async def test_an_unknown_plan_is_stored_and_grants_free(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        return_value=httpx.Response(200, json=canonical(user_id, plan_id="legacy"))
    )

    assert (await _notify(client, _event(user_id))).status_code == 200

    assert (await _row(client, user_id)).plan_id == "legacy"
    assert await _plan_shown(client) == "free"
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog()))
    billing = (await client.get("/api/billing")).json()
    assert billing["plan"]["id"] == "free"
    assert billing["subscription"]["plan_id"] == "legacy"


@respx.mock
async def test_a_notification_pay_cannot_confirm_is_left_for_its_retry(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    fetch = respx.get(f"{SUBSCRIPTIONS_URL}{user_id}")

    fetch.mock(return_value=httpx.Response(500))
    down = await _notify(client, _event(user_id))
    fetch.mock(side_effect=httpx.ConnectError("down"))
    unreachable = await _notify(client, _event(user_id))
    fetch.mock(return_value=httpx.Response(200, json={"subscription": None}))
    nothing_yet = await _notify(client, _event(user_id))
    # An answer naming another subject or App is not Pay's answer about this user.
    fetch.mock(return_value=httpx.Response(200, json=canonical(user_id, app_id="other")))
    foreign = await _notify(client, _event(user_id))

    assert [r.status_code for r in (down, unreachable, nothing_yet, foreign)] == [503] * 4
    assert await _row(client, user_id) is None
    # Nothing recorded: the retry of the same id is applied once Pay answers.
    assert await _events(client) == []
    fetch.mock(return_value=httpx.Response(200, json=canonical(user_id)))
    assert (await _notify(client, _event(user_id))).status_code == 200
    assert (await _row(client, user_id)).plan_id == "pro"


@respx.mock
async def test_a_notification_that_is_not_about_a_user_of_this_app_is_refused(
    make_client,
) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    fetch = respx.get(url__startswith=SUBSCRIPTIONS_URL).mock(
        return_value=httpx.Response(200, json=canonical(user_id))
    )

    other_app = await _notify(
        client,
        json.dumps(
            {
                "id": "evt_x",
                "type": "subscription.updated",
                "subscription": {"appId": "someone-else", "subjectId": str(user_id)},
            }
        ).encode(),
    )
    unknown_subject = await _notify(client, _event(user_id + 1000))
    not_a_subject = await _notify(
        client,
        json.dumps(
            {
                "id": "evt_y",
                "type": "subscription.updated",
                "subscription": {"appId": PAY_APP_ID, "subjectId": "1; drop"},
            }
        ).encode(),
    )
    not_json = await _notify(client, b"{not json")
    no_id = await _notify(
        client,
        json.dumps({"type": "subscription.updated", "subscription": {}}).encode(),
    )
    too_big = await _notify(client, b" " * (pay.EVENT_MAX_BYTES + 1))

    assert other_app.status_code == 400
    assert unknown_subject.status_code == 404
    assert not_a_subject.status_code == 404
    assert not_json.status_code == 400
    assert no_id.status_code == 400
    assert too_big.status_code == 413
    assert fetch.call_count == 0
    assert await _events(client) == []

    unconfigured = make_client()
    assert (await _notify(unconfigured, _event(user_id))).status_code == 503


# --- checkout ---------------------------------------------------------------------------


async def _checkout_row(client: AsyncClient, user_id: int) -> Checkout | None:
    async with client.app.state.sessionmaker() as db:
        return await db.get(Checkout, user_id)


@respx.mock
async def test_a_checkout_keeps_its_idempotency_key_across_a_retry(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog()))
    create = respx.post(CHECKOUTS_URL)

    create.mock(return_value=httpx.Response(502))
    first = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert first.status_code == 503, first.text
    assert first.json()["error"]["type"] == "pay_unavailable"
    key = (await _checkout_row(client, user_id)).idempotency_key
    assert key and (await _checkout_row(client, user_id)).transaction_id is None

    create.mock(
        return_value=httpx.Response(
            200, json={"checkoutUrl": f"{PAY_URL}/checkout/cks_1", "transactionId": "cks_1"}
        )
    )
    second = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert second.status_code == 200, second.text
    assert second.json() == {"url": f"{PAY_URL}/checkout/cks_1"}
    # The same request both times: same subject, plan and key, so Pay returns the same
    # checkout rather than a second one.
    assert [json.loads(c.request.content) for c in create.calls] == [
        {"subjectId": str(user_id), "planId": "pro"}
    ] * 2
    assert {c.request.headers["Idempotency-Key"] for c in create.calls} == {key}
    row = await _checkout_row(client, user_id)
    assert (row.idempotency_key, row.transaction_id, row.completed_at) == (key, "cks_1", None)

    # Once the purchase lands, the next checkout is a new one with a key of its own.
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        return_value=httpx.Response(200, json=canonical(user_id))
    )
    assert (await _notify(client, _event(user_id))).status_code == 200
    assert (await _checkout_row(client, user_id)).completed_at is not None
    third = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert third.status_code == 200
    assert create.calls[-1].request.headers["Idempotency-Key"] != key


@respx.mock
async def test_a_plan_not_on_sale_or_a_closed_checkout_is_refused(make_client) -> None:
    create = respx.post(CHECKOUTS_URL).mock(
        return_value=httpx.Response(
            200, json={"checkoutUrl": f"{PAY_URL}/checkout/cks_2", "transactionId": "cks_2"}
        )
    )

    # Priced at Pay but not configured here: not on sale.
    client = make_client(**PAY_SETTINGS)
    await _signed_in(client)
    respx.get(CATALOG_URL).mock(
        return_value=httpx.Response(200, json=catalog(plans=("pro", "team")))
    )
    unknown = await client.post(
        "/api/billing/checkout", json={"plan_id": "team"}, headers=CSRF_HEADERS
    )
    assert unknown.status_code == 409
    assert unknown.json()["error"]["type"] == "checkout_unavailable"

    # Configured here but not priced at Pay: not on sale either.
    client = make_client(**PAY_SETTINGS)
    await _signed_in(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog(plans=("team",))))
    unpriced = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert unpriced.status_code == 409

    # Pay's checkout closed.
    client = make_client(**PAY_SETTINGS)
    await _signed_in(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog(enabled=False)))
    closed = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert closed.status_code == 409
    assert create.call_count == 0

    # Pay refuses on its own terms (a subscription already stands): refused, not retried.
    client = make_client(**PAY_SETTINGS)
    await _signed_in(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog()))
    create.mock(return_value=httpx.Response(409, json={"error": "subscription_exists"}))
    refused = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "checkout_unavailable"

    # A checkout URL off Pay's origin is not handed to the browser.
    create.mock(
        return_value=httpx.Response(
            200, json={"checkoutUrl": "https://evil.test/checkout", "transactionId": "cks_3"}
        )
    )
    elsewhere = await client.post(
        "/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS
    )
    assert elsewhere.status_code == 503

    # Pay unconfigured or unreachable: unavailable.
    bare = make_client()
    await _signed_in(bare)
    assert (
        await bare.post("/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS)
    ).status_code == 503
    client = make_client(**PAY_SETTINGS)
    await _signed_in(client)
    respx.get(CATALOG_URL).mock(side_effect=httpx.ConnectError("down"))
    down = await client.post("/api/billing/checkout", json={"plan_id": "pro"}, headers=CSRF_HEADERS)
    assert down.status_code == 503
    assert down.json()["error"]["type"] == "pay_unavailable"

    # And never without CSRF.
    nocsrf = await client.post("/api/billing/checkout", json={"plan_id": "pro"})
    assert nocsrf.status_code == 403


# --- the catalog ------------------------------------------------------------------------


@respx.mock
async def test_the_public_catalog_is_cached_and_lists_what_is_on_sale(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    listing = respx.get(CATALOG_URL).mock(
        return_value=httpx.Response(200, json=catalog(plans=("pro", "team")))
    )

    first = await client.get("/api/billing/catalog")

    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "public, max-age=300"
    assert first.json() == {
        "available": True,
        "plans": [
            {
                "plan_id": "pro",
                "name": "Pro",
                "amount": "1500",
                "currency": "USD",
                "cycle": {"interval": "month", "frequency": 1},
                "credits": 5000,
                "storage_bytes": 1_073_741_824,
                "sandbox": True,
                "max_jobs": 2,
            }
        ],
    }
    # Served from memory for five minutes: Pay is asked once.
    again = await client.get("/api/billing/catalog")
    assert again.json() == first.json()
    assert listing.call_count == 1
    client.app.state.pay_catalog.clear()
    await client.get("/api/billing/catalog")
    assert listing.call_count == 2


@respx.mock
async def test_an_unconfigured_or_unreachable_pay_gives_an_empty_catalog(make_client) -> None:
    bare = make_client()
    empty = await bare.get("/api/billing/catalog")
    assert empty.status_code == 200
    assert empty.json() == {"available": False, "plans": []}
    assert empty.headers["Cache-Control"] == "no-store"

    client = make_client(**PAY_SETTINGS)
    listing = respx.get(CATALOG_URL).mock(return_value=httpx.Response(503))
    down = await client.get("/api/billing/catalog")
    assert down.json() == {"available": False, "plans": []}
    assert down.headers["Cache-Control"] == "no-store"
    # A failure is not remembered: the next read asks again.
    await client.get("/api/billing/catalog")
    assert listing.call_count == 2

    # Checkout closed: nothing on sale, the prices still shown.
    listing.mock(return_value=httpx.Response(200, json=catalog(enabled=False)))
    closed = (await client.get("/api/billing/catalog")).json()
    assert closed["available"] is False
    assert [plan["plan_id"] for plan in closed["plans"]] == ["pro"]

    # A malformed entry leaves the catalog without hiding the others.
    client = make_client(**PAY_SETTINGS)
    broken = catalog(plans=("pro", "team"))
    broken["plans"][0]["amount"] = "free"
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=broken))
    assert (await client.get("/api/billing/catalog")).json()["plans"] == []


# --- the billing view -------------------------------------------------------------------


@respx.mock
async def test_the_billing_view_reconciles_with_pay_once_per_window(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog()))
    answers = iter([canonical(user_id, version=1), canonical(user_id, version=2, cancel=True)])
    fetch = respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        side_effect=lambda request: httpx.Response(200, json=next(answers))
    )

    first = (await client.get("/api/billing")).json()

    # A missed notification is recovered on the visit.
    assert first["plan"]["id"] == "pro"
    assert first["subscription"]["cancel_at_period_end"] is False
    assert first["pay_reachable"] is True
    assert first["checkout_available"] is True
    assert first["period"]["granted"] == 5000
    # Within the window Pay is not asked again.
    second = (await client.get("/api/billing")).json()
    assert second["subscription"] == first["subscription"]
    assert fetch.call_count == 1
    # Past it, it is.
    client.app.state.pay_refreshed.clear()
    third = (await client.get("/api/billing")).json()
    assert fetch.call_count == 2
    assert third["subscription"]["cancel_at_period_end"] is True


@respx.mock
async def test_a_pay_outage_keeps_the_stored_row(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    await grant_plan(client.app.state.sessionmaker, user_id)
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(side_effect=httpx.ConnectError("down"))
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=catalog()))

    billing = (await client.get("/api/billing")).json()

    assert billing["pay_reachable"] is False
    assert billing["checkout_available"] is False
    assert billing["plan"]["id"] == "pro"
    assert billing["subscription"]["status"] == "active"

    bare = make_client()
    await _signed_in(bare)
    unconfigured = (await bare.get("/api/billing")).json()
    assert unconfigured["pay_reachable"] is None
    assert unconfigured["checkout_available"] is False
    assert unconfigured["plan"]["id"] == "free"


# --- the portal -------------------------------------------------------------------------


@respx.mock
async def test_the_portal_needs_a_subscription_of_ones_own(make_client) -> None:
    client = make_client(**PAY_SETTINGS)
    user_id = await _signed_in(client)
    portal = respx.post(PORTAL_URL).mock(
        return_value=httpx.Response(200, json={"url": "https://portal.test/session/abc"})
    )

    none = await client.post("/api/billing/portal", headers=CSRF_HEADERS)
    assert none.status_code == 404
    assert portal.call_count == 0

    await grant_plan(client.app.state.sessionmaker, user_id)
    opened = await client.post("/api/billing/portal", headers=CSRF_HEADERS)
    assert opened.status_code == 200, opened.text
    assert opened.json() == {"url": "https://portal.test/session/abc"}
    assert json.loads(portal.calls[0].request.content) == {"subjectId": str(user_id)}

    portal.mock(return_value=httpx.Response(200, json={"url": "http://portal.test/plain"}))
    assert (await client.post("/api/billing/portal", headers=CSRF_HEADERS)).status_code == 503

    bare = make_client()
    await _signed_in(bare)
    assert (await bare.post("/api/billing/portal", headers=CSRF_HEADERS)).status_code == 503


# --- entitlements -----------------------------------------------------------------------


async def _chat_tools(client: AsyncClient) -> list[str]:
    agents = (await client.get("/api/agents")).json()["agents"]
    return next(agent["tools"] for agent in agents if agent["name"] == "chat")


async def test_the_plan_decides_whether_the_agent_gets_a_shell(make_client, tmp_path) -> None:
    runner = {"sandbox_url": "http://sandbox:8100", "sandbox_token": "runner-token-not-a-real-one"}
    client = make_client(workspaces_dir=str(tmp_path / "ws1"), **PAY_SETTINGS, **runner)
    user_id = await _signed_in(client)
    assert "bash" not in await _chat_tools(client)  # Free: a runner, no grant

    await grant_plan(client.app.state.sessionmaker, user_id)
    assert (await _chat_tools(client))[0] == "bash"

    # A deployment without a runner gives nobody a shell, whatever the plan says (the
    # same database, so the same user, still on Pro).
    bare = make_client(workspaces_dir=str(tmp_path / "ws2"), **PAY_SETTINGS)
    assert await _signed_in(bare) == user_id
    assert await _plan_shown(bare) == "pro"
    assert "bash" not in await _chat_tools(bare)


@respx.mock
async def test_a_turn_on_a_paid_plan_offers_the_shell_to_the_model(make_client, tmp_path) -> None:
    from sightread.jobs import chat_runner
    from sightread.jobs.queue import claim_next_turn

    client = make_client(
        workspaces_dir=str(tmp_path / "workspaces"),
        sandbox_url="http://sandbox:8100",
        sandbox_token="runner-token-not-a-real-one",
        **PAY_SETTINGS,
    )
    user_id = await _signed_in(client)
    await _connect_kano(client.app.state.sessionmaker, user_id)
    chosen = await client.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    assert chosen.status_code == 200, chosen.text
    document_id = await _seed_document(client, user_id)
    await grant_plan(client.app.state.sessionmaker, user_id)
    requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=assistant_turn([{"type": "text", "text": "Done."}], "end_turn")
        )

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    conversation_id = (
        await client.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 202, sent.text
    settings = client.app.state.settings
    async with client.app.state.sessionmaker() as db:
        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
    import asyncio

    await chat_runner.run_turn(
        client.app.state.sessionmaker, settings, turn_id, asyncio.Semaphore(2)
    )

    assert [tool["name"] for tool in requests[0]["tools"]][0] == "bash"
    assert "bash" in requests[0]["system"][0]["text"] or any(
        "bash" in block["text"] for block in requests[0]["system"]
    )


@pytest.mark.usefixtures("documents")
async def test_the_plan_caps_the_parses_a_user_runs_at_once(make_client, documents) -> None:
    client = make_client(**PAY_SETTINGS)
    await _ready_to_parse(client)
    user_id = (await client.get("/api/me")).json()["user"]["id"]

    async def running(count: int) -> None:
        async with client.app.state.sessionmaker() as db:
            for index in range(count):
                db.add(
                    Job(
                        user_id=user_id,
                        kind="pdf",
                        filename="busy.pdf",
                        media_type="application/pdf",
                        size_bytes=10,
                        sha256=str(index) * 64,
                        pages_spec="",
                        model=MODEL,
                        agent="parse",
                        pipeline_version=1,
                        bbox_format="yxyx_norm1000",
                        status="running",
                        page_count=1,
                        started_at=utcnow(),
                    )
                )
            await db.commit()

    # Free runs one parse at a time: with one running, the next upload waits.
    await running(1)
    refused = await _upload(client, documents)
    assert refused.status_code == 429, refused.text
    assert refused.headers["Retry-After"] == "30"

    # Pro runs two (the plan's `maxJobs`, under the deployment's ceiling).
    await grant_plan(client.app.state.sessionmaker, user_id)
    accepted = await _upload(client, documents)
    assert accepted.status_code == 201, accepted.text
    await running(1)
    async with client.app.state.sessionmaker() as db:
        rows = (await db.execute(select(Job).where(Job.user_id == user_id))).scalars().all()
        assert sum(row.status == "running" for row in rows) == 2
    assert (await _upload(client, documents, name="more.png")).status_code == 429


async def test_the_plan_sets_the_storage_the_usage_shows(make_client, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), **PAY_SETTINGS)
    user_id = await _signed_in(client)
    free = (await client.get("/api/sandbox")).json()["workspace"]["quota_bytes"]
    assert free == client.app.state.settings.free_storage_bytes == 52_428_800

    await grant_plan(client.app.state.sessionmaker, user_id)
    assert (await client.get("/api/sandbox")).json()["workspace"]["quota_bytes"] == 1_073_741_824

    # Never past the deployment's ceiling.
    client.app.state.settings.workspace_quota_bytes = 1_000_000
    assert (await client.get("/api/sandbox")).json()["workspace"]["quota_bytes"] == 1_000_000
