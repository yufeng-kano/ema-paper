"""The console's Users page (docs/admin.md § Users and grants, § Acceptance): the list,
its search, plan filter and pages, one account's page without any of its content, and
the two grants — credits as a pack the ledger spends by expiry, days of a paid plan into
the bank a plan code's go to — each with its one record, its caps and its refusals.

Nothing leaves the process: Pay is not configured, and the plans are only its JSON.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from sightread.billing import ledger
from sightread.db.models import CreditPack, Job, PlanGrant, Redemption, Subscription, utcnow
from sightread.errors import ApiError
from sightread.routes import admin as admin_routes
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, grant_plan
from tests.test_admin import _account, _add, _audit, _error, _group, _last_audit_id, _owner
from tests.test_ledger import settings_with
from tests.test_redeem import TWO_PLANS_JSON

PLANS = {"kano_pay_plans_json": TWO_PLANS_JSON}
USERS = "/api/admin/users"


def _grants(user_id: int) -> str:
    return f"{USERS}/{user_id}/grants"


def _when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _job(user_id: int, **fields) -> Job:
    return Job(
        **{
            "user_id": user_id,
            "agent": "parse",
            "kind": "pdf",
            "filename": "report.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1,
            "sha256": "x",
            "status": "succeeded",
            "model": "m",
            "bbox_format": "yxyx_norm1000",
            "page_count": 3,
            "pages_done": 3,
            **fields,
        }
    )


async def _packs(client, user_id: int) -> list[CreditPack]:
    async with client.app.state.sessionmaker() as db:
        return list(
            await db.scalars(
                select(CreditPack).where(CreditPack.user_id == user_id).order_by(CreditPack.id)
            )
        )


async def _bank(client, user_id: int) -> PlanGrant | None:
    async with client.app.state.sessionmaker() as db:
        return await db.get(PlanGrant, user_id)


# --- the list -------------------------------------------------------------------------


async def test_the_list_searches_filters_by_plan_and_pages(make_client, tmp_path) -> None:
    settings = {**PLANS, "workspaces_dir": str(tmp_path / "ws")}
    owner = await _owner(make_client, **settings)
    alice = await _account(make_client, "alice@example.test", "Alice Example", **settings)
    bob = await _account(make_client, "bob@example.test", "bob_smith", **settings)
    carol = await _account(make_client, "carol@example.test", "Carol 100%", **settings)
    dave = await _account(make_client, "dave@example.test", "Dave", **settings)
    erin = await _account(make_client, "erin@example.test", None, **settings)
    sessionmaker = owner.app.state.sessionmaker
    now = utcnow()
    # bob pays for pro; erin pays for pro with team days waiting, which wait; carol has
    # team days waiting and nothing paying; dave's granted period ran out with nothing
    # banked, and his subscription is canceled: Free.
    await grant_plan(sessionmaker, bob.user_id, "pro")
    await grant_plan(sessionmaker, erin.user_id, "pro")
    async with sessionmaker() as db:
        db.add(PlanGrant(user_id=erin.user_id, plan_id="team", days_banked=4))
        db.add(PlanGrant(user_id=carol.user_id, plan_id="team", days_banked=5))
        db.add(
            PlanGrant(
                user_id=dave.user_id,
                plan_id="team",
                days_banked=0,
                period_start=now - timedelta(days=40),
                period_end=now - timedelta(days=10),
            )
        )
        db.add(
            Subscription(
                user_id=dave.user_id,
                subscription_id="sub_dave",
                status="canceled",
                plan_id="pro",
                period_start=now - timedelta(days=1),
                period_end=now + timedelta(days=29),
                cancel_at_period_end=False,
            )
        )
        db.add_all([_job(alice.user_id), _job(alice.user_id, status="failed")])
        await db.commit()
    root = user_root(tmp_path / "ws", alice.user_id)
    root.mkdir(parents=True)
    (root / "notes.md").write_bytes(b"x" * 1234)

    listed = await owner.get(USERS)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    newest_first = [erin.user_id, dave.user_id, carol.user_id, bob.user_id, alice.user_id]
    assert [row["id"] for row in page["items"]] == [*newest_first, owner.user_id]
    assert (page["total"], page["page"], page["page_size"]) == (6, 1, 50)
    rows = {row["id"]: row for row in page["items"]}
    assert rows[alice.user_id] == {
        "id": alice.user_id,
        "email": "alice@example.test",
        "name": "Alice Example",
        "plan": {"id": "free", "source": "free"},
        "credits_available": 150,
        "storage_bytes": 1234,
        "jobs": 2,
        "created_at": rows[alice.user_id]["created_at"],
    }
    assert rows[bob.user_id]["plan"] == {"id": "pro", "source": "subscription"}
    assert rows[bob.user_id]["credits_available"] == 5000
    assert rows[erin.user_id]["plan"] == {"id": "pro", "source": "subscription"}
    assert rows[carol.user_id]["plan"] == {"id": "team", "source": "grant"}
    assert rows[dave.user_id]["plan"] == {"id": "free", "source": "free"}
    assert (rows[bob.user_id]["storage_bytes"], rows[bob.user_id]["jobs"]) == (0, 0)
    assert page["plans"] == [
        {"id": "free", "accounts": 3},
        {"id": "pro", "accounts": 2},
        {"id": "team", "accounts": 1},
    ]
    assert page["grant"] == {
        "credit_usd": 0.002,
        "valid_days": 365,
        "max_credits": 1_000_000,
        "max_days": 3650,
    }

    async def ids(query: str) -> list[int]:
        response = await owner.get(f"{USERS}?{query}")
        assert response.status_code == 200, response.text
        return [row["id"] for row in response.json()["items"]]

    # The plan, as every read of the period decides it.
    assert await ids("plan=free") == [dave.user_id, alice.user_id, owner.user_id]
    assert await ids("plan=pro") == [erin.user_id, bob.user_id]
    assert await ids("plan=team") == [carol.user_id]
    # An address or a name, in any letter case; `_` and `%` match themselves.
    assert await ids("q=BOB_") == [bob.user_id]
    assert await ids("q=_") == [bob.user_id]
    assert await ids("q=%25") == [carol.user_id]
    assert await ids("q=%20%20alice%20") == [alice.user_id]
    assert await ids("q=example.test") == newest_first
    assert await ids("q=example&plan=pro") == [erin.user_id, bob.user_id]
    # The plans beside the page count every account, whatever the filters.
    filtered = await owner.get(f"{USERS}?q=bob&plan=pro")
    assert filtered.json()["total"] == 1
    assert filtered.json()["plans"] == page["plans"]

    # Pages: fewer on request, never more than ADMIN_PAGE_SIZE, empty past the end.
    second = (await owner.get(f"{USERS}?page=2&page_size=4")).json()
    assert [row["id"] for row in second["items"]] == [alice.user_id, owner.user_id]
    assert (second["total"], second["page"], second["page_size"]) == (6, 2, 4)
    past = (await owner.get(f"{USERS}?page=3&page_size=4")).json()
    assert (past["items"], past["total"]) == ([], 6)
    assert (await owner.get(f"{USERS}?page_size=999")).json()["page_size"] == 50
    for query in ("page=0", "page_size=0", f"page={2**31}", "plan=gone", "plan=FREE"):
        refused = await owner.get(f"{USERS}?{query}")
        assert refused.status_code == 422, query
        assert _error(refused)["type"] == "invalid_request"


async def test_a_workspace_that_cannot_be_measured_reads_unknown(
    make_client, tmp_path, monkeypatch
) -> None:
    owner = await _owner(make_client, workspaces_dir=str(tmp_path / "ws"))
    deep = await _account(make_client, "deep@example.test")
    measure = admin_routes.measure_storage

    async def refusing(db, settings, user_id, root):
        if user_id == deep.user_id:
            raise ApiError(409, "invalid_request", "nested too deep")
        return await measure(db, settings, user_id, root)

    monkeypatch.setattr(admin_routes, "measure_storage", refusing)
    rows = {row["id"]: row for row in (await owner.get(USERS)).json()["items"]}
    assert rows[deep.user_id]["storage_bytes"] is None
    assert rows[owner.user_id]["storage_bytes"] == 0
    detail = await owner.get(f"{USERS}/{deep.user_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["storage"] is None
    assert (await owner.get(f"{USERS}/{owner.user_id}")).json()["storage"] == {
        "bytes": 0,
        "quota_bytes": 512_000_000,
    }


# --- permissions ----------------------------------------------------------------------


async def test_the_users_routes_need_their_permissions(make_client) -> None:
    owner = await _owner(make_client)
    reader = await _account(make_client, "reader@example.test")
    writer = await _account(make_client, "writer@example.test")
    nobody = await _account(make_client, "nobody@example.test")
    readers = await _group(owner, "readers", ["users.read"])
    writers = await _group(owner, "writers", ["users.write"])
    await _add(owner, readers["id"], user_id=reader.user_id)
    await _add(owner, writers["id"], user_id=writer.user_id)
    body = {"kind": "credits", "credits": 5, "request_id": str(uuid.uuid4())}
    start = await _last_audit_id(owner)

    for path in (USERS, f"{USERS}/{nobody.user_id}"):
        refused = await nobody.get(path)
        assert refused.status_code == 403
        assert _error(refused)["type"] == "permission_denied"
        assert (await reader.get(path)).status_code == 200
    for client in (nobody, reader):
        refused = await client.post(_grants(nobody.user_id), json=body, headers=CSRF_HEADERS)
        assert refused.status_code == 403
        assert _error(refused)["type"] == "permission_denied"
    assert await _packs(owner, nobody.user_id) == []
    assert await _audit(owner, start) == []

    # `users.write` brings `users.read`; who an account's groups are takes `groups.read`.
    granted = await writer.post(_grants(nobody.user_id), json=body, headers=CSRF_HEADERS)
    assert granted.status_code == 201, granted.text
    assert (await writer.get(f"{USERS}/{reader.user_id}")).json()["groups"] is None
    assert (await owner.get(f"{USERS}/{reader.user_id}")).json()["groups"] == [
        {"id": readers["id"], "name": "readers", "builtin": False}
    ]


# --- one account ----------------------------------------------------------------------


async def test_an_accounts_page_shows_what_it_has_and_none_of_its_content(make_client) -> None:
    owner = await _owner(make_client, **PLANS, free_credits_per_period=3)
    account = await _account(make_client, "ann@example.test", "Ann", **PLANS)
    now = utcnow()
    async with owner.app.state.sessionmaker() as db:
        # Six jobs, a minute apart; the newest failed with an error naming the content.
        db.add_all(
            [
                _job(
                    account.user_id,
                    filename=f"scan-{n}.pdf",
                    created_at=now - timedelta(minutes=10 - n),
                    finished_at=now - timedelta(minutes=9 - n),
                    prompt="SECRET-PROMPT",
                )
                for n in range(5)
            ]
            + [
                _job(
                    account.user_id,
                    filename="latest.pdf",
                    status="failed",
                    error="SECRET-ERROR quoting a page",
                    page_count=4,
                    pages_done=1,
                    created_at=now,
                    finished_at=None,
                    prompt="SECRET-PROMPT",
                )
            ]
        )
        db.add(
            CreditPack(
                id="pur_soon",
                user_id=account.user_id,
                pack_id="credits",
                credits=10,
                status=ledger.PACK_PAID,
                purchased_at=now,
                expires_at=now + timedelta(days=20),
                version=1,
            )
        )
        await db.commit()
    granted = await owner.post(
        _grants(account.user_id),
        json={"kind": "credits", "credits": 7, "valid_days": 5, "request_id": str(uuid.uuid4())},
        headers=CSRF_HEADERS,
    )
    assert granted.status_code == 201, granted.text

    response = await owner.get(f"{USERS}/{account.user_id}")
    assert response.status_code == 200, response.text
    assert "SECRET" not in response.text
    detail = response.json()
    assert set(detail) == {
        "user",
        "plan",
        "subscription",
        "credits",
        "day_bank",
        "storage",
        "jobs",
        "groups",
    }
    assert set(detail["user"]) == {"id", "email", "name", "created_at"}
    assert detail["plan"]["id"] == "free"
    assert detail["plan"]["source"] == "free"
    assert (detail["subscription"], detail["day_bank"], detail["groups"]) == (None, None, [])
    sources = detail["credits"]["sources"]
    assert [(row["source"], row["remaining"]) for row in sources] == [
        ("period", 3),
        ("pay", 10),
        ("code", 0),
        ("grant", 7),
    ]
    assert _when(sources[0]["expires_at"]) == _when(detail["plan"]["end"])
    assert _when(sources[1]["expires_at"]) == now + timedelta(days=20)
    assert sources[2]["expires_at"] is None
    assert _when(sources[3]["expires_at"]) == _when(granted.json()["pack"]["expires_at"])
    assert detail["credits"]["available"] == 20
    assert detail["jobs"]["total"] == 6
    recent = detail["jobs"]["recent"]
    assert [job["filename"] for job in recent] == [
        "latest.pdf",
        "scan-4.pdf",
        "scan-3.pdf",
        "scan-2.pdf",
        "scan-1.pdf",
    ]
    assert set(recent[0]) == {
        "id",
        "filename",
        "status",
        "page_count",
        "pages_done",
        "created_at",
        "finished_at",
    }
    assert (recent[0]["status"], recent[0]["page_count"], recent[0]["pages_done"]) == (
        "failed",
        4,
        1,
    )
    assert recent[0]["finished_at"] is None

    # The owner's own page: the built-in group, read with `groups.read`.
    mine = (await owner.get(f"{USERS}/{owner.user_id}")).json()
    assert [group["builtin"] for group in mine["groups"]] == [True]
    missing = await owner.get(f"{USERS}/{account.user_id + 1000}")
    assert missing.status_code == 404
    assert _error(missing)["type"] == "invalid_request"


# --- credits --------------------------------------------------------------------------


async def test_granted_credits_are_a_pack_the_ledger_spends_by_expiry(make_client) -> None:
    owner = await _owner(make_client, free_credits_per_period=3)
    account = await _account(make_client, "ann@example.test", "Ann", free_credits_per_period=3)
    now = utcnow()
    async with owner.app.state.sessionmaker() as db:
        db.add(
            CreditPack(
                id="pur_later",
                user_id=account.user_id,
                pack_id="credits",
                credits=5,
                status=ledger.PACK_PAID,
                purchased_at=now,
                expires_at=now + timedelta(days=30),
                version=1,
            )
        )
        await db.commit()
    request_id = uuid.uuid4()
    start = await _last_audit_id(owner)

    granted = await owner.post(
        _grants(account.user_id),
        json={
            "kind": "credits",
            "credits": 5,
            "valid_days": 10,
            "request_id": str(request_id),
            "reason": "  Make-good for the outage of 2026-10-20  ",
        },
        headers=CSRF_HEADERS,
    )
    assert granted.status_code == 201, granted.text
    answer = granted.json()
    pack_id = f"grant-{request_id}"
    assert answer["kind"] == "credits"
    assert (answer["pack"]["id"], answer["pack"]["credits"]) == (pack_id, 5)
    expires_at = _when(answer["pack"]["expires_at"])
    assert timedelta(days=10) <= expires_at - now < timedelta(days=10, minutes=1)
    assert answer["credits_available"] == 13
    stored = {pack.id: pack for pack in await _packs(owner, account.user_id)}
    assert set(stored) == {"pur_later", pack_id}
    assert (stored[pack_id].source, stored[pack_id].pack_id, stored[pack_id].spent) == (
        "grant",
        "grant",
        0,
    )
    assert (stored[pack_id].redemption_id, stored[pack_id].status) == (None, "paid")

    # One record, its two sides numbers and dates only; the reason trimmed.
    [record] = await _audit(owner, start)
    assert (record.actor_id, record.permission, record.action) == (
        owner.user_id,
        "users.write",
        "users.grant_credits",
    )
    assert (record.target_type, record.target_id) == ("user", str(account.user_id))
    assert record.before == {"credits_available": 8}
    assert record.after == {
        "pack_id": pack_id,
        "credits": 5,
        "expires_at": expires_at.isoformat(),
        "credits_available": 13,
        "reason": "Make-good for the outage of 2026-10-20",
    }
    assert "@" not in json.dumps([record.before, record.after])

    # The account sees a pack among its others, and never the reason.
    billing = await account.get("/api/billing")
    assert billing.status_code == 200, billing.text
    assert "outage" not in billing.text
    items = {item["id"]: item for item in billing.json()["packs"]["items"]}
    assert (items[pack_id]["source"], items[pack_id]["remaining"]) == ("grant", 5)
    assert billing.json()["credits_available"] == 13

    # A charge of 10: the period's 3, the granted pack that expires first, then the
    # bought one — a grant is spent like any other pack, by its expiry.
    async with owner.app.state.sessionmaker() as db:
        await ledger.reserve(
            db,
            owner.app.state.settings,
            user_id=account.user_id,
            kind=ledger.PARSE,
            amount=10,
            job_id=None,
        )
        await db.commit()
    spent = {pack.id: pack.spent for pack in await _packs(owner, account.user_id)}
    assert spent == {pack_id: 5, "pur_later": 2}

    # Without `valid_days`, ADMIN_GRANT_VALID_DAYS.
    default = await owner.post(
        _grants(account.user_id),
        json={"kind": "credits", "credits": 1, "request_id": str(uuid.uuid4()), "reason": " "},
        headers=CSRF_HEADERS,
    )
    assert default.status_code == 201, default.text
    lasts = _when(default.json()["pack"]["expires_at"]) - utcnow()
    assert timedelta(days=364, hours=23) < lasts <= timedelta(days=365)
    assert (await _audit(owner, start))[-1].after["reason"] is None


async def test_a_credit_grant_sent_again_writes_nothing(make_client) -> None:
    owner = await _owner(make_client)
    account = await _account(make_client, "ann@example.test")
    other = await _account(make_client, "ben@example.test")
    body = {"kind": "credits", "credits": 5, "request_id": str(uuid.uuid4())}
    start = await _last_audit_id(owner)

    first = await owner.post(_grants(account.user_id), json=body, headers=CSRF_HEADERS)
    assert first.status_code == 201, first.text
    # A retry, even one whose numbers changed on the way, answers the first pack.
    again = await owner.post(
        _grants(account.user_id), json={**body, "credits": 50}, headers=CSRF_HEADERS
    )
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert [pack.credits for pack in await _packs(owner, account.user_id)] == [5]
    assert len(await _audit(owner, start)) == 1

    # The same id for another account is refused, nothing written.
    reused = await owner.post(_grants(other.user_id), json=body, headers=CSRF_HEADERS)
    assert reused.status_code == 409
    assert _error(reused) == {
        "type": "admin_conflict",
        "message": "This request id was already used for another account's grant",
        "reason": "request_reused",
    }
    assert await _packs(owner, other.user_id) == []
    assert len(await _audit(owner, start)) == 1


# --- days -----------------------------------------------------------------------------


async def test_granted_days_open_a_period_when_nothing_pays(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    account = await _account(make_client, "ann@example.test", **PLANS)
    start = await _last_audit_id(owner)
    month_end = (await owner.get(f"{USERS}/{account.user_id}")).json()["plan"]["end"]
    request_id = str(uuid.uuid4())

    granted = await owner.post(
        _grants(account.user_id),
        json={
            "kind": "days",
            "plan": "pro",
            "days": 40,
            "request_id": request_id,
            "reason": "Pilot school",
        },
        headers=CSRF_HEADERS,
    )
    assert granted.status_code == 201, granted.text
    answer = granted.json()
    # A granted period lasts GRANT_PERIOD_DAYS at most; the rest waits in the bank.
    opened, ends = _when(answer["day_bank"]["period_start"]), _when(answer["period"]["end"])
    assert ends - opened == timedelta(days=30)
    assert answer == {
        "kind": "days",
        "plan": "pro",
        "days": 40,
        "day_bank": {
            "plan_id": "pro",
            "days_banked": 10,
            "period_start": answer["day_bank"]["period_start"],
            "period_end": answer["period"]["end"],
        },
        "period": {"source": "grant", "plan": "pro", "end": answer["period"]["end"]},
    }
    # The account sees the plan as it sees a redeemed code's days; no code, no redemption.
    billing = (await account.get("/api/billing")).json()
    assert (billing["plan"]["id"], billing["period"]["source"]) == ("pro", "grant")
    assert billing["plan_grant"]["days_banked"] == 10
    async with owner.app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Redemption)) == 0

    [record] = await _audit(owner, start)
    assert (record.permission, record.action, record.target_id) == (
        "users.write",
        "users.grant_days",
        str(account.user_id),
    )
    assert record.before == {
        "day_bank": None,
        "period": {"source": "free", "plan": "free", "end": _when(month_end).isoformat()},
    }
    assert record.after == {
        "request_id": request_id,
        "plan": "pro",
        "days": 40,
        "day_bank": {
            "plan_id": "pro",
            "days_banked": 10,
            "period_start": opened.isoformat(),
            "period_end": ends.isoformat(),
        },
        "period": {"source": "grant", "plan": "pro", "end": ends.isoformat()},
        "reason": "Pilot school",
    }


async def test_granted_days_wait_under_a_paid_subscription(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    account = await _account(make_client, "ann@example.test", **PLANS)
    await grant_plan(owner.app.state.sessionmaker, account.user_id, "pro")
    async with owner.app.state.sessionmaker() as db:
        paid_until = (await db.get(Subscription, account.user_id)).period_end

    granted = await owner.post(
        _grants(account.user_id),
        json={"kind": "days", "plan": "pro", "days": 10, "request_id": str(uuid.uuid4())},
        headers=CSRF_HEADERS,
    )
    assert granted.status_code == 201, granted.text
    answer = granted.json()
    assert (answer["period"]["source"], answer["period"]["plan"]) == ("subscription", "pro")
    assert _when(answer["period"]["end"]) == paid_until
    assert answer["day_bank"] == {
        "plan_id": "pro",
        "days_banked": 10,
        "period_start": None,
        "period_end": None,
    }
    settings = owner.app.state.settings
    sessionmaker = owner.app.state.sessionmaker
    # A charge while the subscription pays lands in its period and leaves the bank alone.
    async with sessionmaker() as db:
        entry = await ledger.reserve(
            db, settings, user_id=account.user_id, kind=ledger.PARSE, amount=1
        )
        await db.commit()
        subscription = await db.get(Subscription, account.user_id)
        assert (subscription.status, subscription.period_end) == ("active", paid_until)
        assert entry.period_start == subscription.period_start
    billing = (await account.get("/api/billing")).json()
    assert billing["period"]["source"] == "subscription"
    bank = await _bank(owner, account.user_id)
    assert (bank.days_banked, bank.period_start, bank.period_end) == (10, None, None)

    # Once it lapses, the days open: a read shows them, the next charge writes them.
    async with sessionmaker() as db:
        (await db.get(Subscription, account.user_id)).status = "canceled"
        await db.commit()
    billing = (await account.get("/api/billing")).json()
    assert (billing["plan"]["id"], billing["period"]["source"]) == ("pro", "grant")
    assert billing["plan_grant"]["days_banked"] == 0
    async with sessionmaker() as db:
        entry = await ledger.reserve(
            db, settings, user_id=account.user_id, kind=ledger.PARSE, amount=1
        )
        await db.commit()
    bank = await _bank(owner, account.user_id)
    assert (bank.plan_id, bank.days_banked, bank.period_start) == ("pro", 0, entry.period_start)
    assert bank.period_end - bank.period_start == timedelta(days=10)


async def test_a_day_grant_sent_again_writes_nothing(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    account = await _account(make_client, "ann@example.test", **PLANS)
    other = await _account(make_client, "ben@example.test", **PLANS)
    body = {"kind": "days", "plan": "pro", "days": 40, "request_id": str(uuid.uuid4())}
    start = await _last_audit_id(owner)

    first = await owner.post(_grants(account.user_id), json=body, headers=CSRF_HEADERS)
    assert first.status_code == 201, first.text
    # A retry, even one whose numbers changed on the way, answers the days the first
    # banked, with the bank as it stands: nothing is banked twice.
    again = await owner.post(
        _grants(account.user_id), json={**body, "days": 400}, headers=CSRF_HEADERS
    )
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    bank = await _bank(owner, account.user_id)
    assert (bank.plan_id, bank.days_banked) == ("pro", 10)
    assert len(await _audit(owner, start)) == 1

    # The id is looked for among this account's day grants: another account's is its own.
    elsewhere = await owner.post(_grants(other.user_id), json=body, headers=CSRF_HEADERS)
    assert elsewhere.status_code == 201, elsewhere.text
    assert (await _bank(owner, other.user_id)).days_banked == 10
    assert len(await _audit(owner, start)) == 2


async def test_days_of_another_plan_than_the_bank_holds_are_refused(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    account = await _account(make_client, "ann@example.test", **PLANS)
    first = await owner.post(
        _grants(account.user_id),
        json={"kind": "days", "plan": "team", "days": 5, "request_id": str(uuid.uuid4())},
        headers=CSRF_HEADERS,
    )
    assert first.status_code == 201, first.text
    bank = await _bank(owner, account.user_id)
    start = await _last_audit_id(owner)

    refused = await owner.post(
        _grants(account.user_id),
        json={"kind": "days", "plan": "pro", "days": 5, "request_id": str(uuid.uuid4())},
        headers=CSRF_HEADERS,
    )
    assert refused.status_code == 409
    assert _error(refused)["type"] == "admin_conflict"
    assert _error(refused)["reason"] == "plan_conflict"
    after = await _bank(owner, account.user_id)
    assert (after.plan_id, after.days_banked, after.period_start, after.period_end) == (
        bank.plan_id,
        bank.days_banked,
        bank.period_start,
        bank.period_end,
    )
    assert await _audit(owner, start) == []


# --- refusals -------------------------------------------------------------------------


async def test_a_grant_outside_its_rules_is_refused(make_client) -> None:
    caps = {"admin_grant_max_credits": 100, "admin_grant_max_days": 50}
    owner = await _owner(make_client, **PLANS, **caps, admin_grant_valid_days=30)
    account = await _account(make_client, "ann@example.test")
    credits = {"kind": "credits", "credits": 5, "request_id": str(uuid.uuid4())}
    days = {"kind": "days", "plan": "pro", "days": 5, "request_id": str(uuid.uuid4())}
    start = await _last_audit_id(owner)

    for body in (
        {**credits, "credits": 0},
        {**credits, "credits": 101},
        {**credits, "credits": 2.5},
        {**credits, "valid_days": 0},
        {**credits, "valid_days": 51},
        {**credits, "request_id": "not-a-uuid"},
        {key: value for key, value in credits.items() if key != "request_id"},
        {**credits, "reason": "r" * 201},
        {**credits, "kind": "coins"},
        {key: value for key, value in credits.items() if key != "kind"},
        {**days, "days": 0},
        {**days, "days": 51},
        {**days, "plan": "free"},
        {**days, "plan": "gone"},
        {**days, "reason": "r" * 201},
        {key: value for key, value in days.items() if key != "request_id"},
    ):
        refused = await owner.post(_grants(account.user_id), json=body, headers=CSRF_HEADERS)
        assert refused.status_code == 422, body
        assert _error(refused)["type"] == "invalid_request"
    # The body is read before the account is looked up.
    assert (
        await owner.post(_grants(10**6), json={**credits, "credits": 0}, headers=CSRF_HEADERS)
    ).status_code == 422
    # An id `users.id` cannot hold is refused before the database is asked.
    for user_id in (0, 2**31):
        refused = await owner.post(_grants(user_id), json=credits, headers=CSRF_HEADERS)
        assert refused.status_code == 422, user_id
        assert _error(refused)["type"] == "invalid_request"
        assert (await owner.get(f"{USERS}/{user_id}")).status_code == 422
    assert (await owner.get(f"{USERS}/{2**31 - 1}")).status_code == 404
    assert await _packs(owner, account.user_id) == []
    assert await _bank(owner, account.user_id) is None
    assert await _audit(owner, start) == []

    # The caps themselves are allowed; the default validity is ADMIN_GRANT_VALID_DAYS.
    edge = await owner.post(
        _grants(account.user_id),
        json={**credits, "credits": 100, "reason": "r" * 200},
        headers=CSRF_HEADERS,
    )
    assert edge.status_code == 201, edge.text
    lasts = _when(edge.json()["pack"]["expires_at"]) - utcnow()
    assert timedelta(days=29, hours=23) < lasts <= timedelta(days=30)
    assert (
        await owner.post(_grants(account.user_id), json={**days, "days": 50}, headers=CSRF_HEADERS)
    ).status_code == 201
    missing = await owner.post(
        _grants(account.user_id + 1000),
        json={**days, "days": 1, "request_id": str(uuid.uuid4())},
        headers=CSRF_HEADERS,
    )
    assert missing.status_code == 404
    assert _error(missing)["type"] == "invalid_request"

    # With no paid plan configured, there are no days to give.
    bare = await _owner(make_client)
    refused = await bare.post(_grants(account.user_id), json=days, headers=CSRF_HEADERS)
    assert refused.status_code == 422


def test_the_grant_limits_are_validated() -> None:
    for overrides in (
        {"admin_grant_max_credits": 0},
        {"admin_grant_max_credits": 2**31},
        {"admin_grant_max_days": 0},
        {"admin_grant_max_days": 36_501},
        {"admin_grant_valid_days": 0},
        {"admin_grant_valid_days": 31, "admin_grant_max_days": 30},
    ):
        with pytest.raises(ValueError, match="ADMIN_GRANT_"):
            settings_with(**overrides)
    settings = settings_with()
    assert (
        settings.admin_grant_valid_days,
        settings.admin_grant_max_credits,
        settings.admin_grant_max_days,
    ) == (365, 1_000_000, 3650)


async def test_a_grant_record_is_read_on_the_audit_page(make_client) -> None:
    owner = await _owner(make_client)
    account = await _account(make_client, "ann@example.test")
    await owner.post(
        _grants(account.user_id),
        json={"kind": "credits", "credits": 5, "request_id": str(uuid.uuid4())},
        headers=CSRF_HEADERS,
    )
    entries = (await owner.get("/api/admin/audit?area=users")).json()["items"]
    # The address is read now, from `users`, for the page: the record holds none.
    assert [(entry["action"], entry["target_label"]) for entry in entries] == [
        ("users.grant_credits", "ann@example.test")
    ]
    assert "ann@" not in json.dumps([entries[0]["before"], entries[0]["after"]])
