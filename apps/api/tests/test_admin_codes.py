"""Redeem codes in the operator console (docs/admin.md § Redeem codes, § Acceptance): a
batch created once in plaintext and listed masked after, the list's filters, disabling by
id, the rules `create_codes` holds for the console and the CLI alike, the permissions, and
what the records and the logs never hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from sightread import codes as cli
from sightread.billing import codes
from sightread.config import Settings
from sightread.db.models import RedeemCode, utcnow
from tests.conftest import CSRF_HEADERS, PRO_PLANS_JSON
from tests.test_admin import (
    _account,
    _add,
    _audit,
    _error,
    _group,
    _last_audit_id,
    _owner,
    postgres_only,
)

CODES = "/api/admin/codes"
PLANS = {"kano_pay_plans_json": PRO_PLANS_JSON}
PRINTED = re.compile(r"^EMA-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


async def _create(client, **body) -> dict:
    created = await client.post(CODES, json=body, headers=CSRF_HEADERS)
    assert created.status_code == 201, created.text
    return created.json()


async def _code_count(client) -> int:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(select(func.count()).select_from(RedeemCode))


# --- the shared helpers ---------------------------------------------------------------


def test_a_code_is_masked_to_its_prefix_and_first_group() -> None:
    assert codes.mask("7K3QWX9M4HTB", "EMA") == "EMA-7K3Q-••••-••••"
    assert codes.mask("7K3QWX9M4HTB", "") == "7K3Q-••••-••••"
    # A shorter last group keeps its length.
    assert codes.mask("7K3QWX9M4HTBR", "EMA") == "EMA-7K3Q-••••-••••-•"
    # Under twelve characters, showing four would leave fewer than eight hidden — guessable
    # under the attempt cap — so none of it shows.
    assert codes.mask("7K3QWX9M4HT", "EMA") == "EMA-••••-••••-•••"
    assert codes.mask("7K3QWX", "EMA") == "EMA-••••-••"
    assert codes.mask("7K3Q", "EMA") == "EMA-••••"
    assert codes.end_of_utc_day(date(2030, 12, 31)) == datetime(
        2030, 12, 31, 23, 59, 59, tzinfo=UTC
    )


def test_a_codes_status_is_disabled_then_expired_then_used_up_then_plan_missing() -> None:
    now = datetime(2030, 6, 1, tzinfo=UTC)
    past = now - timedelta(seconds=1)
    plans = {"pro"}

    def row(**fields) -> RedeemCode:
        return RedeemCode(
            **{
                "kind": "credits",
                "uses": 0,
                "max_uses": 1,
                "redeem_by": None,
                "disabled_at": None,
                **fields,
            }
        )

    assert codes.status(row(), now, plans) == codes.ACTIVE
    assert codes.status(row(redeem_by=now), now, plans) == codes.ACTIVE
    assert codes.status(row(kind="plan", plan_id="pro"), now, plans) == codes.ACTIVE
    # A plan the deployment no longer configures: redemption refuses it as code_invalid.
    assert codes.status(row(kind="plan", plan_id="max"), now, plans) == codes.PLAN_MISSING
    assert codes.status(row(kind="plan", plan_id="pro"), now, set()) == codes.PLAN_MISSING
    assert codes.status(row(kind="plan", plan_id="max", uses=1), now, plans) == codes.USED_UP
    assert codes.status(row(uses=1, redeem_by=past), now, plans) == codes.EXPIRED
    assert codes.status(row(uses=1, redeem_by=past, disabled_at=past), now, plans) == codes.DISABLED


def test_the_batch_cap_is_configured_within_what_one_query_binds() -> None:
    assert Settings().redeem_codes_max_count == 1000
    assert Settings(redeem_codes_max_count=10_000).redeem_codes_max_count == 10_000
    for bad in (0, 10_001):
        with pytest.raises(ValueError, match="REDEEM_CODES_MAX_COUNT"):
            Settings(redeem_codes_max_count=bad)


# --- creating and listing -------------------------------------------------------------


async def test_a_batch_is_answered_once_in_plaintext_and_listed_masked(make_client, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="sightread")
    owner = await _owner(make_client, **PLANS)
    start = await _last_audit_id(owner)

    body = await _create(
        owner,
        kind="credits",
        credits=500,
        valid_days=90,
        count=3,
        redeem_by="2030-12-31",
        label="Spring Launch",
    )
    printed = [item["code"] for item in body["codes"]]
    ids = [item["id"] for item in body["codes"]]
    assert len(printed) == 3 and all(PRINTED.match(code) for code in printed)
    assert ids == sorted(ids)
    redeem_by = datetime.fromisoformat(body.pop("redeem_by"))
    assert redeem_by == datetime(2030, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert {key: value for key, value in body.items() if key != "codes"} == {
        "kind": "credits",
        "credits": 500,
        "valid_days": 90,
        "plan_id": None,
        "days": None,
        "max_uses": 1,
        "label": "Spring Launch",
    }
    async with owner.app.state.sessionmaker() as db:
        stored = {row.id: row for row in await db.scalars(select(RedeemCode))}
    assert [codes.display(stored[i].code, "EMA") for i in ids] == printed
    assert stored[ids[0]].redeem_by == redeem_by

    listed = (await owner.get(CODES)).json()
    assert (listed["total"], listed["page"], listed["page_size"]) == (3, 1, 50)
    assert [item["id"] for item in listed["items"]] == ids[::-1]
    first = listed["items"][-1]
    assert first["masked"] == f"{printed[0][:8]}-••••-••••"
    assert {key: first[key] for key in ("kind", "credits", "valid_days", "plan_id", "days")} == {
        "kind": "credits",
        "credits": 500,
        "valid_days": 90,
        "plan_id": None,
        "days": None,
    }
    assert (first["uses"], first["max_uses"], first["label"], first["status"]) == (
        0,
        1,
        "Spring Launch",
        "active",
    )
    assert first["disabled_at"] is None and first["disabled_by"] is None
    assert listed["summary"] == {"active": 3, "labels": 1, "redeemed_this_month": 0}
    assert listed["create"] == {
        "plans": ["pro"],
        "max_count": 1000,
        "max_days": 36_500,
        "valid_days": 365,
        "credit_usd": "0.002",
    }

    # The plaintext was in the create answer and is nowhere else: not in the list, not in
    # the record, not in a log line.
    [record] = await _audit(owner, start)
    assert (record.action, record.permission, record.target_type, record.target_id) == (
        "codes.create",
        "codes.write",
        "code",
        str(ids[0]),
    )
    assert record.before is None
    assert record.after == {
        "count": 3,
        "kind": "credits",
        "credits": 500,
        "valid_days": 90,
        "max_uses": 1,
        "redeem_by": "2030-12-31T23:59:59+00:00",
        "ids": ids,
    }
    listing = json.dumps(listed)
    stored_record = json.dumps([record.before, record.after])
    audit_page = (await owner.get("/api/admin/audit")).text
    for code in printed:
        hidden = codes.normalize(code, "EMA")[4:]
        for text in (listing, stored_record, audit_page, caplog.text):
            assert code not in text and hidden not in text
        # Not even the masked form, or the label, is recorded.
        assert code[:8] not in stored_record
    assert "Launch" not in stored_record


async def test_a_plan_code_from_the_console_is_the_clis_and_redeems_as_any(
    make_client, capsys
) -> None:
    owner = await _owner(make_client, **PLANS)
    settings = owner.app.state.settings
    body = await _create(owner, kind="plan", plan_id="pro", days=30, max_uses=5, valid_days=7)
    [created] = body["codes"]
    # The other kind's fields are ignored, as the CLI's default --valid-days is.
    assert (body["kind"], body["plan_id"], body["days"], body["credits"], body["valid_days"]) == (
        "plan",
        "pro",
        30,
        None,
        None,
    )
    assert body["redeem_by"] is None and body["max_uses"] == 5

    async def run(*argv: str) -> tuple[int, str]:
        async with owner.app.state.sessionmaker() as db:
            status = await cli.run(cli.build_parser().parse_args(argv), settings, db)
        return status, capsys.readouterr().out

    status, out = await run("list")
    assert status == 0 and f"{created['code']}\t30 days of pro\t0/5\t-\tlive\t" in out

    credits = (await _create(owner, kind="credits", credits=40))["codes"][0]
    redeemed = await owner.post(
        "/api/billing/redeem", json={"code": credits["code"].lower()}, headers=CSRF_HEADERS
    )
    assert redeemed.status_code == 200 and redeemed.json()["credits"] == 40
    items = {item["id"]: item for item in (await owner.get(f"{CODES}?status=all")).json()["items"]}
    assert (items[credits["id"]]["uses"], items[credits["id"]]["status"]) == (1, "used_up")
    assert items[credits["id"]]["valid_days"] == codes.DEFAULT_VALID_DAYS

    # Disabled with the CLI: the console lists it so, and no one is named — the CLI
    # records nothing.
    start = await _last_audit_id(owner)
    assert await run("disable", created["code"]) == (0, f"disabled {created['code']}\n")
    item = (await owner.get(f"{CODES}?status=all")).json()["items"][-1]
    assert (item["id"], item["status"], item["disabled_by"]) == (created["id"], "disabled", None)
    assert await _audit(owner, start) == []


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "gift", "credits": 5},
        {"kind": "credits"},
        {"kind": "credits", "credits": 0},
        {"kind": "credits", "credits": 2**31},
        {"kind": "credits", "credits": 5, "valid_days": 0},
        {"kind": "credits", "credits": 5, "valid_days": 36_501},
        {"kind": "credits", "credits": 5, "valid_days": 2**31 - 1},
        {"kind": "credits", "credits": 5, "count": 0},
        {"kind": "credits", "credits": 5, "count": 6},
        {"kind": "credits", "credits": 5, "max_uses": 0},
        {"kind": "plan", "plan_id": "pro"},
        {"kind": "plan", "plan_id": "pro", "days": 36_501},
        {"kind": "plan", "plan_id": "max", "days": 30},
        {"kind": "credits", "credits": 5, "label": "x" * 256},
        {"kind": "credits", "credits": 5, "redeem_by": "31/12/2030"},
        {"kind": "credits", "credits": 5, "redeem_by": "2000-01-01"},
        {"credits": 5},
    ],
)
async def test_a_batch_outside_the_rules_is_422_and_writes_nothing(make_client, body) -> None:
    owner = await _owner(make_client, redeem_codes_max_count=5, **PLANS)
    start = await _last_audit_id(owner)
    refused = await owner.post(CODES, json=body, headers=CSRF_HEADERS)
    assert refused.status_code == 422
    assert _error(refused)["type"] == "invalid_request"
    assert await _code_count(owner) == 0
    assert await _audit(owner, start) == []


async def test_the_console_and_the_cli_refuse_a_batch_by_the_same_rules(
    make_client, capsys
) -> None:
    owner = await _owner(make_client, redeem_codes_max_count=5, **PLANS)
    settings = owner.app.state.settings

    async def run(*argv: str) -> tuple[int, str]:
        async with owner.app.state.sessionmaker() as db:
            status = await cli.run(cli.build_parser().parse_args(["create", *argv]), settings, db)
        return status, capsys.readouterr().err.strip()

    for body, argv in (
        ({"kind": "credits", "credits": 5, "count": 6}, ("--credits", "5", "--count", "6")),
        ({"kind": "credits", "credits": 2**31}, ("--credits", str(2**31))),
        (
            {"kind": "credits", "credits": 5, "valid_days": 36_501},
            ("--credits", "5", "--valid-days", "36501"),
        ),
        ({"kind": "plan", "plan_id": "pro", "days": 36_501}, ("--plan", "pro", "--days", "36501")),
        ({"kind": "plan", "plan_id": "max", "days": 3}, ("--plan", "max", "--days", "3")),
        (
            {"kind": "credits", "credits": 5, "redeem_by": "2000-01-01"},
            ("--credits", "5", "--redeem-by", "2000-01-01"),
        ),
        (
            {"kind": "credits", "credits": 5, "label": "x" * 256},
            ("--credits", "5", "--label", "x" * 256),
        ),
    ):
        refused = await owner.post(CODES, json=body, headers=CSRF_HEADERS)
        assert refused.status_code == 422
        assert await run(*argv) == (2, _error(refused)["message"])
    assert (
        _error(
            await owner.post(
                CODES, json={"kind": "credits", "credits": 5, "count": 6}, headers=CSRF_HEADERS
            )
        )["message"]
        == "count must be from 1 to 5 (REDEEM_CODES_MAX_COUNT)"
    )
    assert await _code_count(owner) == 0
    # At the cap, both make the batch.
    assert len((await _create(owner, kind="credits", credits=5, count=5))["codes"]) == 5
    status, _ = await run("--credits", "5", "--count", "5")
    assert status == 0 and await _code_count(owner) == 10


async def test_the_list_keeps_the_active_codes_or_all_searches_labels_and_pages(
    make_client,
) -> None:
    owner = await _owner(make_client, **PLANS)
    launch = [
        item["id"]
        for item in (await _create(owner, kind="credits", credits=5, count=5, label="launch"))[
            "codes"
        ]
    ]
    [beta] = (await _create(owner, kind="plan", plan_id="pro", days=30, label="Spring Beta"))[
        "codes"
    ]
    expired, used_up, disabled = launch[:3]
    async with owner.app.state.sessionmaker() as db:
        await db.execute(
            update(RedeemCode)
            .where(RedeemCode.id == expired)
            .values(redeem_by=utcnow() - timedelta(days=1))
        )
        await db.execute(update(RedeemCode).where(RedeemCode.id == used_up).values(uses=1))
        await db.commit()
    assert (
        await owner.post(f"{CODES}/{disabled}/disable", headers=CSRF_HEADERS)
    ).status_code == 200

    active = (await owner.get(CODES)).json()
    assert [item["id"] for item in active["items"]] == [beta["id"], launch[4], launch[3]]
    assert active["total"] == 3
    assert active["summary"] == {"active": 3, "labels": 2, "redeemed_this_month": 0}
    every = (await owner.get(f"{CODES}?status=all")).json()
    assert every["total"] == 6
    assert {item["id"]: item["status"] for item in every["items"]} == {
        beta["id"]: "active",
        launch[4]: "active",
        launch[3]: "active",
        disabled: "disabled",
        used_up: "used_up",
        expired: "expired",
    }

    def ids(answer) -> list[int]:
        return [item["id"] for item in answer.json()["items"]]

    assert ids(await owner.get(f"{CODES}?q=BETA")) == [beta["id"]]
    assert len(ids(await owner.get(f"{CODES}?status=all&q=aun"))) == 5
    assert ids(await owner.get(f"{CODES}?status=all&q=%25")) == []
    # The code itself is never searched, not even its shown group.
    assert ids(await owner.get(f"{CODES}?q={beta['code'][4:8]}")) == []
    paged = (await owner.get(f"{CODES}?status=all&page_size=4&page=2")).json()
    assert (paged["total"], paged["page"], paged["page_size"]) == (6, 2, 4)
    assert [item["id"] for item in paged["items"]] == [used_up, expired]
    assert (await owner.get(f"{CODES}?status=all&page_size=4&page=3")).json()["items"] == []
    assert (await owner.get(f"{CODES}?page_size=1000")).json()["page_size"] == 50
    for query in ("status=used", "page=0", "page_size=0"):
        assert (await owner.get(f"{CODES}?{query}")).status_code == 422


async def test_a_plan_code_whose_plan_is_gone_is_not_active(make_client, capsys) -> None:
    owner = await _owner(make_client, **PLANS)
    settings = owner.app.state.settings
    [live] = (await _create(owner, kind="plan", plan_id="pro", days=30, label="kept"))["codes"]
    # Made while `max` was configured; `KANO_PAY_PLANS_JSON` no longer names it.
    async with owner.app.state.sessionmaker() as db:
        gone = RedeemCode(
            code=codes.generate(settings.redeem_code_chars),
            kind="plan",
            plan_id="max",
            days=30,
            max_uses=1,
            label="dropped",
        )
        db.add(gone)
        await db.commit()

    active = (await owner.get(CODES)).json()
    assert [item["id"] for item in active["items"]] == [live["id"]]
    assert active["summary"] == {"active": 1, "labels": 1, "redeemed_this_month": 0}
    every = {item["id"]: item for item in (await owner.get(f"{CODES}?status=all")).json()["items"]}
    assert (every[gone.id]["status"], every[live["id"]]["status"]) == ("plan_missing", "active")

    async with owner.app.state.sessionmaker() as db:
        assert await cli.run(cli.build_parser().parse_args(["list"]), settings, db) == 0
        assert await cli.run(cli.build_parser().parse_args(["list", "--all"]), settings, db) == 0
    live_only, every_line = capsys.readouterr().out.split("kept\n", 1)
    assert "dropped" not in live_only
    assert "\tplan missing\tdropped" in every_line
    # What the list says is what redeeming does.
    redeemed = await owner.post(
        "/api/billing/redeem",
        json={"code": codes.display(gone.code, settings.redeem_code_prefix)},
        headers=CSRF_HEADERS,
    )
    assert (redeemed.status_code, _error(redeemed)["type"]) == (404, "code_invalid")


async def test_a_code_at_the_most_days_redeems(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    [credits] = (await _create(owner, kind="credits", credits=5, valid_days=codes.MAX_DAYS))[
        "codes"
    ]
    [plan] = (await _create(owner, kind="plan", plan_id="pro", days=codes.MAX_DAYS))["codes"]
    before = utcnow()
    redeemed = await owner.post(
        "/api/billing/redeem", json={"code": credits["code"]}, headers=CSRF_HEADERS
    )
    assert redeemed.status_code == 200, redeemed.text
    lasts = datetime.fromisoformat(redeemed.json()["expires_at"]) - before
    assert timedelta(days=codes.MAX_DAYS) <= lasts < timedelta(days=codes.MAX_DAYS, minutes=1)
    redeemed = await owner.post(
        "/api/billing/redeem", json={"code": plan["code"]}, headers=CSRF_HEADERS
    )
    assert redeemed.status_code == 200, redeemed.text
    # Nothing pays for the account: the first granted period opens from the bank at once.
    period_days = owner.app.state.settings.grant_period_days
    assert redeemed.json()["days_banked"] == codes.MAX_DAYS - period_days


async def test_disabling_records_once_and_the_code_stops_working(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    [created] = (await _create(owner, kind="credits", credits=5))["codes"]
    start = await _last_audit_id(owner)
    path = f"{CODES}/{created['id']}/disable"

    disabled = await owner.post(path, headers=CSRF_HEADERS)
    assert disabled.status_code == 200
    item = disabled.json()
    assert (item["id"], item["status"], item["masked"]) == (
        created["id"],
        "disabled",
        f"{created['code'][:8]}-••••-••••",
    )
    assert item["disabled_by"] == {"id": owner.user_id, "name": "Local Developer"}
    [record] = await _audit(owner, start)
    assert (record.action, record.permission, record.target_type, record.target_id) == (
        "codes.disable",
        "codes.write",
        "code",
        str(created["id"]),
    )
    assert record.before == {"disabled_at": None}
    assert datetime.fromisoformat(record.after["disabled_at"]) == datetime.fromisoformat(
        item["disabled_at"]
    )

    again = await owner.post(path, headers=CSRF_HEADERS)
    assert again.status_code == 200 and again.json()["disabled_at"] == item["disabled_at"]
    assert len(await _audit(owner, start)) == 1
    missing = await owner.post(f"{CODES}/{created['id'] + 100}/disable", headers=CSRF_HEADERS)
    assert missing.status_code == 404 and _error(missing)["type"] == "invalid_request"
    assert len(await _audit(owner, start)) == 1

    redeemed = await owner.post(
        "/api/billing/redeem", json={"code": created["code"]}, headers=CSRF_HEADERS
    )
    assert (redeemed.status_code, _error(redeemed)["type"]) == (404, "code_invalid")


@postgres_only
async def test_two_disables_at_once_record_once(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    [created] = (await _create(owner, kind="credits", credits=5))["codes"]
    start = await _last_audit_id(owner)
    path = f"{CODES}/{created['id']}/disable"
    answers = await asyncio.gather(
        owner.post(path, headers=CSRF_HEADERS), owner.post(path, headers=CSRF_HEADERS)
    )
    assert [answer.status_code for answer in answers] == [200, 200]
    assert len(await _audit(owner, start)) == 1


async def test_codes_read_lists_and_only_codes_write_changes(make_client) -> None:
    owner = await _owner(make_client, **PLANS)
    [created] = (await _create(owner, kind="credits", credits=5))["codes"]
    readers = await _group(owner, "readers", ["codes.read"])
    issuers = await _group(owner, "issuers", ["codes.write"])
    reader = await _account(make_client, "reader@example.test", **PLANS)
    writer = await _account(make_client, "writer@example.test", **PLANS)
    outsider = await _account(make_client, "outsider@example.test", **PLANS)
    assert (await _add(owner, readers["id"], email="reader@example.test")).status_code == 201
    assert (await _add(owner, issuers["id"], email="writer@example.test")).status_code == 201
    start = await _last_audit_id(owner)

    assert (await reader.get(CODES)).status_code == 200
    for refused in (
        await reader.post(CODES, json={"kind": "credits", "credits": 5}, headers=CSRF_HEADERS),
        await reader.post(f"{CODES}/{created['id']}/disable", headers=CSRF_HEADERS),
        await outsider.get(CODES),
    ):
        assert (refused.status_code, _error(refused)["type"]) == (403, "permission_denied")
    assert await _audit(owner, start) == []

    # Write brings read.
    assert (await writer.get(CODES)).status_code == 200
    made = await _create(writer, kind="credits", credits=5)
    assert (
        await writer.post(f"{CODES}/{made['codes'][0]['id']}/disable", headers=CSRF_HEADERS)
    ).status_code == 200
    assert [(row.actor_id, row.action) for row in await _audit(owner, start)] == [
        (writer.user_id, "codes.create"),
        (writer.user_id, "codes.disable"),
    ]
