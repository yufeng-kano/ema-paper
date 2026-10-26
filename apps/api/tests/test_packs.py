"""Top-up packs (docs/billing.md § Top-up packs, § Acceptance): the configuration, the
ledger's order — the period first, then the packs by expiry — the refusal on the sum,
releases and retries, a turn's spill into the packs before the overdraft, the pack
checkout, Pay's purchase notifications, the refresh and what the views show. Pay exists
only inside respx."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.billing import ledger
from sightread.billing.plans import Pack, parse_packs
from sightread.db.models import (
    ChatTurn,
    Conversation,
    CreditEntry,
    CreditPack,
    CreditPackEntry,
    Document,
    Job,
    PackCheckout,
    UsageLog,
)
from tests.conftest import CSRF_HEADERS, PAY_APP_ID, PAY_SETTINGS, PAY_URL, grant_plan
from tests.test_ledger import _period, _user, settings_with
from tests.test_pay import (
    CATALOG_URL,
    SUBSCRIPTIONS_URL,
    _events,
    _ms,
    _notify,
    _signed_in,
    canonical,
    catalog,
)

PURCHASES_URL = f"{PAY_URL}/v1/purchases"
PACKS_JSON = json.dumps([{"packId": "credits", "credits": 2000, "validDays": 365}])
PACK_SETTINGS = {**PAY_SETTINGS, "kano_pay_packs_json": PACKS_JSON}
CHECKOUT = "/api/billing/packs/checkout"


# --- configuration ----------------------------------------------------------------------


def test_the_packs_configuration_is_validated() -> None:
    assert parse_packs("") == {}
    assert parse_packs(PACKS_JSON) == {"credits": Pack(id="credits", credits=2000, valid_days=365)}
    for raw in (
        "{",
        '{"packId": "a"}',
        "[1]",
        '[{"credits": 1, "validDays": 1}]',
        '[{"packId": "", "credits": 1, "validDays": 1}]',
        f'[{{"packId": "{"p" * 65}", "credits": 1, "validDays": 1}}]',
        '[{"packId": "a", "credits": 0, "validDays": 1}]',
        '[{"packId": "a", "credits": 1, "validDays": "7"}]',
        '[{"packId": "a", "credits": true, "validDays": 1}]',
        '[{"packId": "a", "credits": 1, "validDays": 1}, '
        '{"packId": "a", "credits": 2, "validDays": 1}]',
    ):
        with pytest.raises(ValueError, match="KANO_PAY_PACKS_JSON"):
            parse_packs(raw)
    # Settings read it at start, so a bad value fails the boot, not a purchase.
    with pytest.raises(ValueError, match="KANO_PAY_PACKS_JSON"):
        settings_with(kano_pay_packs_json='[{"packId": "a", "credits": 0, "validDays": 1}]')
    assert settings_with(kano_pay_packs_json=PACKS_JSON).packs["credits"].valid_days == 365


# --- the ledger -------------------------------------------------------------------------


async def _pack(
    sessionmaker,
    user_id: int,
    *,
    credits: int,
    days_left: int = 365,
    status: str = ledger.PACK_PAID,
) -> str:
    """A `credit_packs` row as the webhook writes it, valid a year from its purchase."""
    expires_at = datetime.now(UTC) + timedelta(days=days_left)
    purchase_id = f"pur_{uuid.uuid4().hex[:8]}"
    async with sessionmaker() as db:
        db.add(
            CreditPack(
                id=purchase_id,
                user_id=user_id,
                pack_id="credits",
                credits=credits,
                status=status,
                purchased_at=expires_at - timedelta(days=365),
                expires_at=expires_at,
                version=1,
            )
        )
        await db.commit()
    return purchase_id


def _job(user_id: int) -> Job:
    return Job(
        user_id=user_id,
        agent="parse",
        kind="image",
        filename="a.png",
        media_type="image/png",
        size_bytes=1,
        sha256="x",
        status="queued",
        model="m",
        bbox_format="yxyx_norm1000",
        page_count=1,
        pages_done=0,
    )


async def _spent(sessionmaker, purchase_id: str) -> int:
    async with sessionmaker() as db:
        return (await db.get(CreditPack, purchase_id)).spent


async def _shares(sessionmaker, entry_id: int) -> dict[str, int]:
    async with sessionmaker() as db:
        rows = await db.execute(
            select(CreditPackEntry.pack_id, CreditPackEntry.amount).where(
                CreditPackEntry.entry_id == entry_id
            )
        )
        return dict(rows.all())


async def test_a_charge_takes_the_period_first_and_the_packs_by_expiry(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=3)
    user_id = await _user(sessionmaker)
    later = await _pack(sessionmaker, user_id, credits=5, days_left=30)
    sooner = await _pack(sessionmaker, user_id, credits=5, days_left=10)

    async with sessionmaker() as db:
        job = _job(user_id)
        db.add(job)
        await db.flush()
        entry = await ledger.reserve(
            db, settings, user_id=user_id, kind=ledger.PARSE, amount=10, job_id=job.id
        )
        await db.commit()
        entry_id, job_id = entry.id, job.id

    # 3 from the period, then the pack that expires first, then the other.
    assert (await _period(sessionmaker, user_id)).spent == 3
    assert (await _spent(sessionmaker, sooner), await _spent(sessionmaker, later)) == (5, 2)
    assert await _shares(sessionmaker, entry_id) == {sooner: 5, later: 2}
    async with sessionmaker() as db:
        balance = await ledger.read_balance(db, settings, user_id)
        # Null while the reservation is pending its settlement, as a parse's always is.
        assert await ledger.credits_by_job(db, [job_id]) == {job_id: None}
    assert (balance.remaining, balance.packs_remaining, balance.available) == (0, 3, 3)
    assert [pack.remaining for pack in balance.packs] == [0, 3] or [
        pack.remaining for pack in balance.packs
    ] == [3, 0]
    # The earliest expiry among the packs with credits left: `sooner` has none.
    async with sessionmaker() as db:
        assert balance.packs_expire_at == (await db.get(CreditPack, later)).expires_at

    # Refused on the sum: 3 left across the period and the packs, 4 asked.
    async with sessionmaker() as db:
        with pytest.raises(ledger.InsufficientCredits) as refused:
            await ledger.reserve(
                db, settings, user_id=user_id, kind=ledger.PARSE, amount=4, job_id=None
            )
    assert (refused.value.need, refused.value.remaining) == (4, 3)


async def test_a_release_restores_each_share_and_a_retry_takes_again(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=2)
    user_id = await _user(sessionmaker)
    first = await _pack(sessionmaker, user_id, credits=5, days_left=300)
    async with sessionmaker() as db:
        job = _job(user_id)
        db.add(job)
        await db.flush()
        entry = await ledger.reserve(
            db, settings, user_id=user_id, kind=ledger.PARSE, amount=6, job_id=job.id
        )
        await db.commit()
        entry_id, job_id = entry.id, job.id
    assert (await _period(sessionmaker, user_id)).spent == 2
    assert await _spent(sessionmaker, first) == 4

    # The parse fails: the period gets its 2 back, the pack its 4.
    async with sessionmaker() as db:
        await ledger.settle_parse(db, job_id, "failed")
        await db.commit()
    assert (await _period(sessionmaker, user_id)).spent == 0
    assert await _spent(sessionmaker, first) == 0
    assert await _shares(sessionmaker, entry_id) == {}

    # The retry takes again, from the packs as they stand now: a pack bought since,
    # expiring sooner, is taken before the first.
    second = await _pack(sessionmaker, user_id, credits=5, days_left=100)
    async with sessionmaker() as db:
        await ledger.reserve_again(db, settings, job_id)
        await db.commit()
    assert (await _period(sessionmaker, user_id)).spent == 2
    assert (await _spent(sessionmaker, second), await _spent(sessionmaker, first)) == (4, 0)
    assert await _shares(sessionmaker, entry_id) == {second: 4}
    # Succeeding consumes it: every share stays where it was taken.
    async with sessionmaker() as db:
        await ledger.settle_parse(db, job_id, "succeeded")
        await db.commit()
        assert await ledger.credits_by_job(db, [job_id]) == {job_id: 6}
    assert await _spent(sessionmaker, second) == 4


async def test_a_turn_spills_into_the_packs_before_the_overdraft(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=1, credit_usd="0.001")
    user_id = await _user(sessionmaker)
    pack = await _pack(sessionmaker, user_id, credits=4)
    async with sessionmaker() as db:
        document = Document(user_id=user_id, name="d.md")
        db.add(document)
        await db.flush()
        conversation = Conversation(user_id=user_id, document_id=document.id, session="s")
        db.add(conversation)
        await db.flush()
        turn = ChatTurn(
            user_id=user_id, conversation_id=conversation.id, message_id=1, status="running"
        )
        db.add(turn)
        await db.flush()
        entry = await ledger.reserve(
            db,
            settings,
            user_id=user_id,
            kind=ledger.CHAT,
            amount=0,
            turn_id=turn.id,
            minimum=settings.chat_min_credits,
        )
        await db.commit()
        turn_id, entry_id = turn.id, entry.id

    async def call() -> int | None:
        # 3,000 tokens at US$1 per million = US$0.003: 3 credits per call.
        async with sessionmaker() as db:
            db.add(
                UsageLog(
                    user_id=user_id,
                    turn_id=turn_id,
                    agent="chat",
                    model="m",
                    prompt_tokens=1000,
                    completion_tokens=1000,
                    cache_read_tokens=500,
                    cache_write_tokens=500,
                    cost_source="unknown",
                )
            )
            await db.flush()
            left = await ledger.settle_chat(db, settings, turn_id)
            await db.commit()
            return left

    # 3 credits: the period's 1, then 2 of the pack; 2 are left to the turn.
    assert await call() == 2
    assert (await _period(sessionmaker, user_id)).spent == 1
    assert await _spent(sessionmaker, pack) == 2
    assert await _shares(sessionmaker, entry_id) == {pack: 2}
    # 3 more: the pack's last 2, and only then 1 into the period past its grant.
    assert await call() == 0
    assert (await _period(sessionmaker, user_id)).spent == 2
    assert await _spent(sessionmaker, pack) == 4
    assert await _shares(sessionmaker, entry_id) == {pack: 4}
    async with sessionmaker() as db:
        balance = await ledger.read_balance(db, settings, user_id)
        await ledger.close_chat(db, turn_id)
        await db.commit()
        assert await ledger.credits_by_turn(db, [turn_id]) == {turn_id: 6}
        with pytest.raises(ledger.InsufficientCredits) as refused:
            await ledger.reserve(
                db, settings, user_id=user_id, kind=ledger.CHAT, amount=0, turn_id=None, minimum=1
            )
    assert (balance.remaining, balance.packs_remaining, balance.available) == (-1, 0, 0)
    assert (refused.value.need, refused.value.remaining) == (1, 0)


async def test_two_turns_settling_at_once_share_the_period_before_the_pack(sessionmaker) -> None:
    """The period row is locked before its balance is read: a turn settling while another
    turn's settlement is open takes only what that one left of the period and spills the
    rest into the pack, instead of both charging the period from the same stale remainder
    (docs/billing.md § The ledger)."""
    settings = settings_with(free_credits_per_period=10, credit_usd="0.001")
    user_id = await _user(sessionmaker)
    pack = await _pack(sessionmaker, user_id, credits=20)
    async with sessionmaker() as db:
        document = Document(user_id=user_id, name="d.md")
        db.add(document)
        await db.flush()
        conversation = Conversation(user_id=user_id, document_id=document.id, session="s")
        db.add(conversation)
        await db.flush()
        turn_ids = []
        for message_id in (1, 2):
            turn = ChatTurn(
                user_id=user_id,
                conversation_id=conversation.id,
                message_id=message_id,
                status="running",
            )
            db.add(turn)
            await db.flush()
            await ledger.reserve(
                db,
                settings,
                user_id=user_id,
                kind=ledger.CHAT,
                amount=0,
                turn_id=turn.id,
                minimum=1,
            )
            turn_ids.append(turn.id)
        await db.commit()

    async def settle(db, turn_id: uuid.UUID) -> int | None:
        # 8,000 tokens at US$1 per million = US$0.008: 8 credits a turn.
        db.add(
            UsageLog(
                user_id=user_id,
                turn_id=turn_id,
                agent="chat",
                model="m",
                prompt_tokens=4000,
                completion_tokens=4000,
                cost_source="unknown",
            )
        )
        await db.flush()
        return await ledger.settle_chat(db, settings, turn_id)

    async def second() -> int | None:
        async with sessionmaker() as db:
            left = await settle(db, turn_ids[1])
            await db.commit()
            return left

    async with sessionmaker() as first:
        # 8 of the period's 10; the pack's 20 untouched: 22 left to the turn.
        assert await settle(first, turn_ids[0]) == 22
        # The second turn settles while the first's settlement is still open: it waits
        # for the period row, then takes the 2 the first left of it and 6 of the pack.
        waiting = asyncio.ensure_future(second())
        await asyncio.sleep(0.3)
        assert not waiting.done()
        await first.commit()
    assert await waiting == 14
    assert (await _period(sessionmaker, user_id)).spent == 10
    assert await _spent(sessionmaker, pack) == 6
    async with sessionmaker() as db:
        entries = (
            (
                await db.execute(
                    select(CreditEntry)
                    .where(CreditEntry.turn_id.in_(turn_ids))
                    .order_by(CreditEntry.id)
                )
            )
            .scalars()
            .all()
        )
    assert [entry.amount for entry in entries] == [8, 8]
    assert [await _shares(sessionmaker, entry.id) for entry in entries] == [{}, {pack: 6}]


async def test_an_expired_or_refunded_pack_counts_nothing(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=0)
    user_id = await _user(sessionmaker)
    expired = await _pack(sessionmaker, user_id, credits=5, days_left=-1)
    refunded = await _pack(sessionmaker, user_id, credits=5, status=ledger.PACK_REFUNDED)
    active = await _pack(sessionmaker, user_id, credits=2)

    async with sessionmaker() as db:
        balance = await ledger.read_balance(db, settings, user_id)
    assert (balance.packs_remaining, balance.available) == (2, 2)
    assert {pack.id: (pack.active, pack.remaining) for pack in balance.packs} == {
        expired: (False, 0),
        refunded: (False, 0),
        active: (True, 2),
    }
    async with sessionmaker() as db:
        with pytest.raises(ledger.InsufficientCredits) as refused:
            await ledger.reserve(db, settings, user_id=user_id, kind=ledger.PARSE, amount=3)
    assert (refused.value.need, refused.value.remaining) == (3, 2)
    async with sessionmaker() as db:
        entry = await ledger.reserve(db, settings, user_id=user_id, kind=ledger.PARSE, amount=2)
        await db.commit()
        entry_id = entry.id
    assert await _shares(sessionmaker, entry_id) == {active: 2}
    assert (await _spent(sessionmaker, expired), await _spent(sessionmaker, refunded)) == (0, 0)


# --- Pay --------------------------------------------------------------------------------


def _catalog(*, enabled: bool = True, packs: tuple[str, ...] = ("credits",)) -> dict:
    return {
        **catalog(enabled=enabled),
        "packs": [
            {
                "packId": pack,
                "productId": f"prod_{pack}",
                "name": "Credits",
                "amount": "500",
                "currency": "USD",
            }
            for pack in packs
        ],
    }


def _purchase(
    user_id: int,
    purchase_id: str = "pur_1",
    *,
    pack_id: str = "credits",
    status: str = "paid",
    version: int = 1,
    paid_at: datetime | None = None,
) -> dict:
    paid = paid_at or datetime.now(UTC)
    return {
        "appId": PAY_APP_ID,
        "subjectId": str(user_id),
        "purchaseId": purchase_id,
        "packId": pack_id,
        "productId": f"prod_{pack_id}",
        "paymentId": "pay_1",
        "status": status,
        "amount": "500",
        "currency": "USD",
        "createdAt": _ms(paid),
        "paidAt": _ms(paid),
        "version": version,
    }


def _purchase_event(user_id: int, event_id: str = "evt_p1", purchase_id: str = "pur_1") -> bytes:
    body = {
        "id": event_id,
        "type": "purchase.updated",
        "purchase": {
            "appId": PAY_APP_ID,
            "subjectId": str(user_id),
            "purchaseId": purchase_id,
            # The body's own claims, never read.
            "packId": "mystery",
            "status": "refunded",
        },
    }
    return json.dumps(body).encode()


def _created(purchase_id: str = "pur_1") -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "checkoutUrl": f"{PAY_URL}/checkout/{purchase_id}",
            "purchaseId": purchase_id,
            "sessionId": "ses_1",
        },
    )


async def _pro(client: AsyncClient) -> int:
    user_id = await _signed_in(client)
    await grant_plan(client.app.state.sessionmaker, user_id)
    return user_id


async def _checkout_row(client: AsyncClient, user_id: int) -> PackCheckout | None:
    async with client.app.state.sessionmaker() as db:
        return await db.get(PackCheckout, user_id)


async def _pack_rows(client: AsyncClient, user_id: int) -> list[CreditPack]:
    async with client.app.state.sessionmaker() as db:
        rows = await db.execute(
            select(CreditPack).where(CreditPack.user_id == user_id).order_by(CreditPack.id)
        )
        return list(rows.scalars())


async def _buy(client: AsyncClient, pack_id: str = "credits") -> httpx.Response:
    return await client.post(CHECKOUT, json={"pack_id": pack_id}, headers=CSRF_HEADERS)


@respx.mock
async def test_a_pack_checkout_is_for_a_paid_plan_and_a_pack_on_sale(make_client) -> None:
    client = make_client(**PACK_SETTINGS)
    user_id = await _signed_in(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_catalog()))
    create = respx.post(PURCHASES_URL).mock(return_value=_created())

    # Free: the plan comes first, whatever is on sale.
    free = await _buy(client)
    assert free.status_code == 409, free.text
    assert free.json()["error"]["type"] == "plan_required"
    me = (await client.get("/api/me")).json()
    assert (me["plan"], me["pack_checkout_available"]) == ("free", False)

    await grant_plan(client.app.state.sessionmaker, user_id)
    assert (await client.get("/api/me")).json()["pack_checkout_available"] is True
    other = await _buy(client, "other")
    assert (other.status_code, other.json()["error"]["type"]) == (409, "checkout_unavailable")
    client.app.state.pay_catalog.clear()
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_catalog(enabled=False)))
    closed = await _buy(client)
    assert (closed.status_code, closed.json()["error"]["type"]) == (409, "checkout_unavailable")
    client.app.state.pay_catalog.clear()
    respx.get(CATALOG_URL).mock(side_effect=httpx.ConnectError("down"))
    down = await _buy(client)
    assert (down.status_code, down.json()["error"]["type"]) == (503, "pay_unavailable")
    assert create.call_count == 0
    assert await _checkout_row(client, user_id) is None

    # A deployment that configures no pack sells none, whatever Pay prices.
    bare = make_client(**PAY_SETTINGS)
    await _signed_in(bare)  # the same user, on Pro already
    bare.app.state.pay_catalog.clear()
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_catalog()))
    none = await _buy(bare)
    assert (none.status_code, none.json()["error"]["type"]) == (409, "checkout_unavailable")
    assert (await bare.get("/api/me")).json()["pack_checkout_available"] is False
    assert (await bare.get("/api/billing/catalog")).json()["packs"] == []


@respx.mock
async def test_a_pack_checkout_keeps_its_key_across_a_retry(make_client) -> None:
    client = make_client(**PACK_SETTINGS)
    user_id = await _pro(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_catalog()))
    create = respx.post(PURCHASES_URL)

    create.mock(return_value=httpx.Response(502))
    first = await _buy(client)
    assert first.status_code == 503, first.text
    row = await _checkout_row(client, user_id)
    key = row.idempotency_key
    assert key and (row.pack_id, row.purchase_id, row.completed_at) == ("credits", None, None)

    create.mock(return_value=_created())
    second = await _buy(client)
    assert second.status_code == 200, second.text
    assert second.json() == {"url": f"{PAY_URL}/checkout/pur_1"}
    assert [json.loads(c.request.content) for c in create.calls] == [
        {"subjectId": str(user_id), "packId": "credits"}
    ] * 2
    assert {c.request.headers["Idempotency-Key"] for c in create.calls} == {key}
    row = await _checkout_row(client, user_id)
    assert (row.idempotency_key, row.purchase_id, row.completed_at) == (key, "pur_1", None)
    # Asked again before paying: the same open checkout, the same key.
    assert (await _buy(client)).status_code == 200
    assert create.calls[-1].request.headers["Idempotency-Key"] == key

    # Once the purchase lands, the next pack is a new checkout with a key of its own.
    respx.get(f"{PURCHASES_URL}/{user_id}").mock(
        return_value=httpx.Response(200, json={"purchases": [_purchase(user_id)]})
    )
    assert (await _notify(client, _purchase_event(user_id))).status_code == 200
    assert (await _checkout_row(client, user_id)).completed_at is not None
    create.mock(return_value=_created("pur_2"))
    assert (await _buy(client)).status_code == 200
    assert create.calls[-1].request.headers["Idempotency-Key"] != key
    assert (await _checkout_row(client, user_id)).purchase_id == "pur_2"


@respx.mock
async def test_a_purchase_notification_is_written_from_the_canonical_purchases(
    make_client,
) -> None:
    client = make_client(**PACK_SETTINGS)
    user_id = await _pro(client)
    paid_at = datetime.now(UTC) - timedelta(hours=1)
    answer = {"purchases": [_purchase(user_id, paid_at=paid_at, version=1)]}
    fetch = respx.get(f"{PURCHASES_URL}/{user_id}").mock(
        return_value=httpx.Response(200, json=answer)
    )

    received = await _notify(client, _purchase_event(user_id))

    assert received.status_code == 200, received.text
    assert fetch.call_count == 1
    (row,) = await _pack_rows(client, user_id)
    # The credits and the validity are this deployment's, not Pay's amount.
    assert (row.pack_id, row.credits, row.spent, row.status, row.version) == (
        "credits",
        2000,
        0,
        "paid",
        1,
    )
    assert row.expires_at - row.purchased_at == timedelta(days=365)
    assert abs(row.purchased_at - paid_at) < timedelta(seconds=1)
    assert await _events(client) == ["evt_p1"]
    me = (await client.get("/api/me")).json()
    assert (me["credits_remaining"], me["pack_checkout_available"]) == (7000, True)

    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_catalog()))
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        return_value=httpx.Response(200, json=canonical(user_id))
    )
    billing = (await client.get("/api/billing")).json()
    assert billing["period"]["remaining"] == 5000
    assert billing["credits_available"] == 7000
    assert billing["pack_checkout_available"] is True
    assert billing["packs"]["remaining"] == 2000
    assert billing["packs"]["expires_at"] == billing["packs"]["items"][0]["expires_at"]
    item = billing["packs"]["items"][0]
    assert {k: item[k] for k in ("id", "pack_id", "credits", "spent", "remaining", "status")} == {
        "id": "pur_1",
        "pack_id": "credits",
        "credits": 2000,
        "spent": 0,
        "remaining": 2000,
        "status": "paid",
    }
    assert item["active"] is True
    public = (await client.get("/api/billing/catalog")).json()
    assert public["packs"] == [
        {
            "pack_id": "credits",
            "name": "Credits",
            "amount": "500",
            "currency": "USD",
            "credits": 2000,
            "valid_days": 365,
        }
    ]

    # The same delivery again: nothing fetched, nothing written.
    assert (await _notify(client, _purchase_event(user_id))).status_code == 200
    assert fetch.call_count == 1
    # A refund: applied once, and a stale answer never brings the pack back.
    fetch.mock(
        return_value=httpx.Response(
            200,
            json={"purchases": [_purchase(user_id, status="refunded", version=2, paid_at=paid_at)]},
        )
    )
    assert (await _notify(client, _purchase_event(user_id, "evt_p2"))).status_code == 200
    fetch.mock(return_value=httpx.Response(200, json=answer))
    assert (await _notify(client, _purchase_event(user_id, "evt_p3"))).status_code == 200
    (row,) = await _pack_rows(client, user_id)
    assert (row.status, row.version, row.credits) == ("refunded", 2, 2000)
    assert (await client.get("/api/me")).json()["credits_remaining"] == 5000
    client.app.state.pay_refreshed.clear()
    billing = (await client.get("/api/billing")).json()
    assert (billing["packs"]["remaining"], billing["packs"]["expires_at"]) == (0, None)
    assert (billing["packs"]["items"][0]["remaining"], billing["packs"]["items"][0]["active"]) == (
        0,
        False,
    )
    assert await _events(client) == ["evt_p1", "evt_p2", "evt_p3"]


@respx.mock
async def test_a_purchase_pay_cannot_confirm_or_does_not_configure(make_client) -> None:
    client = make_client(**PACK_SETTINGS)
    user_id = await _pro(client)
    fetch = respx.get(f"{PURCHASES_URL}/{user_id}")

    fetch.mock(return_value=httpx.Response(500))
    down = await _notify(client, _purchase_event(user_id))
    fetch.mock(return_value=httpx.Response(200, json={"purchases": []}))
    not_yet = await _notify(client, _purchase_event(user_id))
    fetch.mock(return_value=httpx.Response(200, json={"purchases": [_purchase(user_id, "pur_2")]}))
    another = await _notify(client, _purchase_event(user_id, purchase_id="pur_1"))
    assert [r.status_code for r in (down, not_yet, another)] == [503, 503, 503]
    assert await _pack_rows(client, user_id) == []
    assert await _events(client) == []

    # Malformed: no purchase id, or a subscription and a purchase in one event.
    no_id = await _notify(
        client,
        json.dumps(
            {
                "id": "evt_x",
                "type": "purchase.updated",
                "purchase": {"appId": PAY_APP_ID, "subjectId": str(user_id)},
            }
        ).encode(),
    )
    both = await _notify(
        client,
        json.dumps(
            {
                "id": "evt_y",
                "type": "purchase.updated",
                "purchase": {"appId": PAY_APP_ID, "subjectId": str(user_id), "purchaseId": "p"},
                "subscription": {"appId": PAY_APP_ID, "subjectId": str(user_id)},
            }
        ).encode(),
    )
    assert [r.status_code for r in (no_id, both)] == [400, 400]
    assert fetch.call_count == 3

    # A pack this deployment does not configure: acknowledged, logged, no row.
    fetch.mock(
        return_value=httpx.Response(
            200, json={"purchases": [_purchase(user_id, "pur_9", pack_id="mystery")]}
        )
    )
    assert (await _notify(client, _purchase_event(user_id, "evt_9", "pur_9"))).status_code == 200
    assert await _pack_rows(client, user_id) == []
    assert await _events(client) == ["evt_9"]
    assert (await client.get("/api/me")).json()["credits_remaining"] == 5000


@respx.mock
async def test_a_refresh_recovers_a_missed_purchase(make_client) -> None:
    client = make_client(**PACK_SETTINGS)
    user_id = await _pro(client)
    respx.get(CATALOG_URL).mock(return_value=httpx.Response(200, json=_catalog()))
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(
        return_value=httpx.Response(200, json=canonical(user_id))
    )
    fetch = respx.get(f"{PURCHASES_URL}/{user_id}").mock(
        return_value=httpx.Response(200, json={"purchases": []})
    )

    # Nothing bought or being bought: the purchases are not asked for.
    assert (await client.get("/api/billing")).json()["packs"]["items"] == []
    assert fetch.call_count == 0

    respx.post(PURCHASES_URL).mock(return_value=_created())
    assert (await _buy(client)).status_code == 200
    # The purchase lands, the webhook never arrives: the next visit writes the pack and
    # closes the checkout.
    fetch.mock(return_value=httpx.Response(200, json={"purchases": [_purchase(user_id)]}))
    client.app.state.pay_refreshed.clear()
    billing = (await client.get("/api/billing")).json()
    assert fetch.call_count == 1
    assert (billing["packs"]["remaining"], billing["credits_available"]) == (2000, 7000)
    assert (await _checkout_row(client, user_id)).completed_at is not None
    # Within the window Pay is not asked again; past it, a paid pack keeps the fetch on
    # — a refund arrives the same way.
    assert (await client.get("/api/billing")).json()["packs"]["remaining"] == 2000
    assert fetch.call_count == 1
    client.app.state.pay_refreshed.clear()
    fetch.mock(
        return_value=httpx.Response(
            200, json={"purchases": [_purchase(user_id, status="refunded", version=2)]}
        )
    )
    assert (await client.get("/api/billing")).json()["packs"]["remaining"] == 0
    assert fetch.call_count == 2
    # Refunded and nothing open: no more purchase fetches.
    client.app.state.pay_refreshed.clear()
    assert (await client.get("/api/billing")).json()["credits_available"] == 5000
    assert fetch.call_count == 2
    # A Pay outage leaves the rows as they are and says so.
    client.app.state.pay_refreshed.clear()
    respx.get(f"{SUBSCRIPTIONS_URL}{user_id}").mock(side_effect=httpx.ConnectError("down"))
    outage = (await client.get("/api/billing")).json()
    assert (outage["pay_reachable"], outage["pack_checkout_available"]) == (False, False)
    assert outage["packs"]["items"][0]["status"] == "refunded"
