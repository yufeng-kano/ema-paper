"""What the billing tests share: settings built without an app, the rows they charge
against, the plans they configure, and a Kano Pay that only exists inside respx
(docs/billing.md § Kano Pay)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.billing import ledger
from sightread.config import Settings
from sightread.db.models import (
    ChatTurn,
    Conversation,
    CreditPeriod,
    Document,
    Job,
    PayEvent,
    Subscription,
    UsageLog,
)
from tests.conftest import (
    PAY_APP_ID,
    PAY_URL,
    PAY_WEBHOOK_SECRET,
    PRO_PLANS_JSON,
    TEST_MODEL_PRICES,
    TEST_SECRET_KEY,
)
from tests.factories import job_row, user_row

# --- settings, plans and rows -------------------------------------------------------------


def settings_with(**overrides) -> Settings:
    """Settings for the ledger driven without an app, every test model priced."""
    return Settings(
        **{
            "app_env": "local",
            "auth_dev_mode": True,
            "secret_key": TEST_SECRET_KEY,
            "database_url": "sqlite+aiosqlite://",
            "model_prices_json": TEST_MODEL_PRICES,
            **overrides,
        }
    )


PRO_PLANS = {"kano_pay_plans_json": PRO_PLANS_JSON}
TWO_PLANS_JSON = json.dumps(
    [
        {
            "planId": "pro",
            "credits": 5000,
            "storageBytes": 1_073_741_824,
            "sandbox": True,
            "maxJobs": 2,
        },
        {
            "planId": "team",
            "credits": 9000,
            "storageBytes": 1_073_741_824,
            "sandbox": True,
            "maxJobs": 2,
        },
    ]
)


async def make_user(sessionmaker, name: str = "ledger") -> int:
    """An account of its own, signed in with Google as `name@example.test`; its id."""
    async with sessionmaker() as db:
        user = user_row(f"{name}@example.test")
        db.add(user)
        await db.commit()
        return user.id


async def credit_period(sessionmaker, user_id: int) -> CreditPeriod:
    """The user's one `credit_periods` row."""
    async with sessionmaker() as db:
        return (
            await db.execute(select(CreditPeriod).where(CreditPeriod.user_id == user_id))
        ).scalar_one()


def paid_subscription(**overrides) -> Subscription:
    """A subscription row of user 1 on Pro, active for the month from 2026-10-15."""
    base = {
        "user_id": 1,
        "subscription_id": "sub_1",
        "status": "active",
        "plan_id": "pro",
        "period_start": datetime(2026, 10, 15, tzinfo=UTC),
        "period_end": datetime(2026, 11, 15, tzinfo=UTC),
        "cancel_at_period_end": False,
        "version": 1,
    }
    return Subscription(**{**base, **overrides})


def image_job(user_id: int, **fields) -> Job:
    """A queued parse of an image, none of its pages done yet."""
    return job_row(
        user_id,
        **{
            "kind": "image",
            "filename": "a.png",
            "media_type": "image/png",
            "sha256": "x",
            "status": "queued",
            "model": "m",
            "pages_done": 0,
            **fields,
        },
    )


async def add_parses(sessionmaker, user_id: int, count: int, **fields) -> None:
    """`count` parses of the user's, each of a file of its own, in the state `fields` give."""
    async with sessionmaker() as db:
        for index in range(count):
            db.add(
                job_row(
                    user_id,
                    filename=f"paper-{index}.pdf",
                    size_bytes=10,
                    sha256=str(index) * 64,
                    pages_spec="",
                    pipeline_version=1,
                    **fields,
                )
            )
        await db.commit()


async def running_turns(
    sessionmaker, settings: Settings, user_id: int, count: int = 1, *, settle: bool = True
) -> tuple[int, list[uuid.UUID], list[int]]:
    """A conversation with `count` running turns, each with its reserved chat entry:
    `(conversation_id, turn_ids, entry_ids)`. With `settle`, each turn has made one call of
    2,000 tokens, settled."""
    async with sessionmaker() as db:
        # The turns' foreign keys, which PostgreSQL enforces: a document and its thread.
        document = Document(user_id=user_id, name="d.md")
        db.add(document)
        await db.flush()
        conversation = Conversation(user_id=user_id, document_id=document.id, session="s")
        db.add(conversation)
        await db.flush()
        turn_ids: list[uuid.UUID] = []
        entry_ids: list[int] = []
        for message_id in range(1, count + 1):
            turn = ChatTurn(
                user_id=user_id,
                conversation_id=conversation.id,
                message_id=message_id,
                status="running",
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
            if settle:
                db.add(
                    UsageLog(
                        user_id=user_id,
                        turn_id=turn.id,
                        agent="chat",
                        model="m",
                        prompt_tokens=1000,
                        completion_tokens=1000,
                        cost_source="unknown",
                    )
                )
                await db.flush()
                await ledger.settle_chat(db, settings, turn.id)
            turn_ids.append(turn.id)
            entry_ids.append(entry.id)
        await db.commit()
    return conversation.id, turn_ids, entry_ids


# --- Kano Pay -----------------------------------------------------------------------------

SUBSCRIPTIONS_URL = f"{PAY_URL}/v1/subscriptions/"
CATALOG_URL = f"{PAY_URL}/v1/catalog"
CHECKOUTS_URL = f"{PAY_URL}/v1/checkouts"
PORTAL_URL = f"{PAY_URL}/v1/portal"
PURCHASES_URL = f"{PAY_URL}/v1/purchases"


def ms(when: datetime) -> int:
    """A time as Pay writes it: milliseconds since the epoch."""
    return int(when.timestamp() * 1000)


# Pay's `version` is the subscription's last update in milliseconds. A test's `version`
# is a step of this many from a minute ahead of now, so an answer completes the checkout
# opened moments before it, versions keep their order across one test, and a negative
# step lands before anything the test opened.
VERSION_STEP = 1_000_000


def pay_version(step: int) -> int:
    return ms(datetime.now(UTC)) + 60_000 + step * VERSION_STEP


def canonical(
    user_id: int,
    *,
    version: int = 1,
    plan_id: str | None = "pro",
    status: str = "active",
    app_id: str = PAY_APP_ID,
    cancel: bool = False,
) -> dict:
    """`GET /v1/subscriptions/:subjectId` as Pay answers it, at `pay_version(version)`."""
    now = datetime.now(UTC)
    return {
        "subscription": {
            "appId": app_id,
            "subjectId": str(user_id),
            "subscriptionId": "sub_canonical",
            "status": status,
            "planId": plan_id,
            "periodStart": ms(now - timedelta(days=1)),
            "periodEnd": ms(now + timedelta(days=29)),
            "cancelAtPeriodEnd": cancel,
            "version": pay_version(version),
            "active": status == "active",
        }
    }


def catalog(
    *, enabled: bool = True, plans: tuple[str, ...] = ("pro",), packs: tuple[str, ...] = ()
) -> dict:
    """`GET /v1/catalog` as Pay answers it: each plan at US$15 a month, each pack at US$5."""
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
        "packs": [
            {
                "packId": pack,
                "productId": f"prod_{pack}",
                "name": pack.title(),
                "amount": "500",
                "currency": "USD",
            }
            for pack in packs
        ],
    }


def subscription_event(user_id: int, event_id: str = "evt_1", **subscription) -> bytes:
    """A `subscription.updated` notification's body, with whatever claims it is given."""
    body = {
        "id": event_id,
        "type": "subscription.updated",
        "subscription": {"appId": PAY_APP_ID, "subjectId": str(user_id), **subscription},
    }
    return json.dumps(body).encode()


def purchase_event(user_id: int, event_id: str = "evt_p1", purchase_id: str = "pur_1") -> bytes:
    """A `purchase.updated` notification's body."""
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


def sign(secret: str, timestamp: int, raw: bytes) -> str:
    """`Kano-Pay-Signature` as Pay writes it, independent of the code that verifies it."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256)
    return f"ts={timestamp};h1={digest.hexdigest()}"


async def notify(
    client: AsyncClient,
    raw: bytes,
    *,
    timestamp: int | None = None,
    secret: str = PAY_WEBHOOK_SECRET,
) -> httpx.Response:
    """Deliver `raw` to the webhook, signed as Pay signs it."""
    ts = int(time.time()) if timestamp is None else timestamp
    return await client.post(
        "/api/billing/pay-webhook",
        content=raw,
        headers={
            "Kano-Pay-Signature": sign(secret, ts, raw),
            "Content-Type": "application/json",
        },
    )


async def pay_events(client: AsyncClient) -> list[str]:
    """The ids of the notifications recorded, in order."""
    async with client.app.state.sessionmaker() as db:
        return list((await db.execute(select(PayEvent.id).order_by(PayEvent.id))).scalars())
