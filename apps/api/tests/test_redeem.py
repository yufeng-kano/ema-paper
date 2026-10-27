"""Redeem codes (docs/billing.md § Redeem codes, § Periods *Granted days*): how a typed
code is read, the period resolution with a bank of granted days, the redeem route and its
refusals, the attempt cap, what the views show, and the operator's CLI. Pay is never
configured here: a plan code works without it."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from sightread import codes as cli
from sightread.billing import codes, ledger
from sightread.billing.plans import (
    FREE,
    GRANT,
    SUBSCRIPTION,
    Grant,
    Plan,
    resolve_period,
)
from sightread.db.models import (
    CreditPack,
    CreditPeriod,
    PlanGrant,
    RedeemCode,
    Subscription,
    User,
)
from sightread.errors import ApiError
from tests.conftest import CSRF_HEADERS, PRO_PLANS_JSON, grant_plan
from tests.test_billing import _subscription
from tests.test_ledger import _user, settings_with

PLANS = {**{"kano_pay_plans_json": PRO_PLANS_JSON}}
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
REDEEM = "/api/billing/redeem"
PRINTED = re.compile(r"^EMA-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


async def _signed_in(client: AsyncClient) -> int:
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    return (await client.get("/api/me")).json()["user"]["id"]


async def _code(sessionmaker, settings, **grant) -> str:
    """One code as the operator would make it, printed as a user types it."""
    kind = codes.PLAN if "plan_id" in grant else codes.CREDITS
    async with sessionmaker() as db:
        rows = await codes.create_codes(
            db,
            settings,
            kind=kind,
            count=1,
            max_uses=grant.pop("max_uses", 1),
            redeem_by=grant.pop("redeem_by", None),
            label=grant.pop("label", ""),
            **grant,
        )
        return codes.display(rows[0].code, settings.redeem_code_prefix)


async def _redeem(client: AsyncClient, code: str):
    return await client.post(REDEEM, json={"code": code}, headers=CSRF_HEADERS)


async def _another_user(sessionmaker, sub: str) -> int:
    async with sessionmaker() as db:
        user = User(google_sub=sub, email=f"{sub}@example.test")
        db.add(user)
        await db.commit()
        return user.id


# --- the code ----------------------------------------------------------------------------


def test_a_typed_code_is_read_without_case_spaces_or_the_prefix() -> None:
    assert codes.normalize(" ema-7k3q wx9m_4htb ", "EMA") == "7K3QWX9M4HTB"
    assert codes.normalize("7K3QWX9M4HTB", "EMA") == "7K3QWX9M4HTB"
    # Only the prefix is dropped, and only in front.
    assert codes.normalize("EMAEMA7K3Q", "EMA") == "EMA7K3Q"
    assert codes.normalize("ema-7k3q", "") == "EMA7K3Q"
    assert codes.display("7K3QWX9M4HTB", "EMA") == "EMA-7K3Q-WX9M-4HTB"
    assert codes.display("7K3QWX9M4HTB", "") == "7K3Q-WX9M-4HTB"
    for _ in range(20):
        code = codes.generate(12)
        assert len(code) == 12 and set(code) <= set(codes.ALPHABET)
        assert not set(code) & set("0O1I")
        assert codes.normalize(codes.display(code, "EMA").lower(), "EMA") == code


# --- the period resolution -----------------------------------------------------------------


def test_granted_days_open_periods_from_the_bank_after_a_paid_one() -> None:
    free = Plan("free", 150, 1, False, 1)
    plans = {"pro": Plan("pro", 5000, 2, True, 2)}
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    # Nothing pays: a period of GRANT_PERIOD_DAYS opens from the bank, the rest waits.
    decided = resolve_period(None, Grant("pro", 45), plans, free, now, 30)
    assert decided.grant == Grant("pro", 15, now, now + timedelta(days=30))
    assert (decided.period.source, decided.period.plan.id, decided.period.credits) == (
        GRANT,
        "pro",
        5000,
    )
    assert (decided.period.start, decided.period.end) == (now, now + timedelta(days=30))
    # Inside it: the same period, nothing moves.
    later = now + timedelta(days=10)
    again = resolve_period(None, decided.grant, plans, free, later, 30)
    assert again.grant == decided.grant and again.period == decided.period
    # It ran out: the next period is the bank's 15 days, their credits pro-rated upwards.
    after = now + timedelta(days=30)
    short = resolve_period(None, decided.grant, plans, free, after, 30)
    assert short.grant == Grant("pro", 0, after, after + timedelta(days=15))
    assert (short.period.credits, short.period.end) == (2500, after + timedelta(days=15))
    assert resolve_period(None, Grant("pro", 1), plans, free, now, 30).period.credits == 167
    # Then the bank is empty: Free's month, the empty bank kept as it is.
    done = resolve_period(None, short.grant, plans, free, after + timedelta(days=15), 30)
    assert done.period.source == FREE and done.grant == Grant("pro", 0)
    # A bank of a plan the deployment no longer configures is inert.
    inert = resolve_period(None, Grant("max", 30), plans, free, now, 30)
    assert inert.period.source == FREE and inert.grant == Grant("max", 30)
    # A paid subscription comes first: the bank waits, untouched.
    paid = resolve_period(_subscription(), Grant("pro", 45), plans, free, now, 30)
    assert paid.period.source == SUBSCRIPTION and paid.grant == Grant("pro", 45)
    # No bank at all: the paid period, or Free, as before.
    assert resolve_period(None, None, plans, free, now, 30).period.source == FREE
    assert resolve_period(_subscription(), None, plans, free, now, 30).grant is None


def test_a_paid_period_cuts_an_open_granted_one_and_banks_its_whole_days() -> None:
    free = Plan("free", 150, 1, False, 1)
    plans = {"pro": Plan("pro", 5000, 2, True, 2)}
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    open_grant = Grant("pro", 5, now - timedelta(days=10), now + timedelta(days=20))
    # Paid since yesterday: cut there, 21 whole days (of the 20 left and the one under way)
    # go back; the paid period is the current one.
    paid = _subscription(period_start=now - timedelta(days=1), period_end=now + timedelta(days=29))
    cut = resolve_period(paid, open_grant, plans, free, now, 30)
    assert cut.period.source == SUBSCRIPTION
    assert cut.grant == Grant("pro", 26)
    # Paid since before the granted period began: every day of it goes back.
    early = _subscription(
        period_start=now - timedelta(days=15), period_end=now + timedelta(days=15)
    )
    assert resolve_period(early, open_grant, plans, free, now, 30).grant == Grant("pro", 35)
    # A part of a day is spent, not banked.
    partial = _subscription(
        period_start=now - timedelta(hours=1), period_end=now + timedelta(days=29)
    )
    assert resolve_period(partial, open_grant, plans, free, now, 30).grant == Grant("pro", 25)
    # Lapsed meanwhile with the granted period still open: it runs on, as it was.
    lapsed = _subscription(status="canceled", period_start=now - timedelta(days=1))
    kept = resolve_period(lapsed, open_grant, plans, free, now, 30)
    assert kept.grant == open_grant and kept.period.source == GRANT
    # A granted period that ended before the paid one began is simply closed.
    ended = Grant("pro", 5, now - timedelta(days=40), now - timedelta(days=10))
    assert resolve_period(paid, ended, plans, free, now, 30).grant == Grant("pro", 5)


# --- redeeming credits ---------------------------------------------------------------------


async def test_a_credits_code_becomes_a_pack_that_is_not_pays(make_client, sessionmaker) -> None:
    client = make_client(free_credits_per_period=100)
    user_id = await _signed_in(client)
    settings = client.app.state.settings
    code = await _code(sessionmaker, settings, credits=500, valid_days=365, max_uses=2)
    before = datetime.now(UTC)
    answer = await _redeem(client, code.lower().replace("-", " "))
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["kind"], body["credits"]) == ("credits", 500)
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(days=364) < expires - before < timedelta(days=366)
    billing = (await client.get("/api/billing")).json()
    assert billing["credits_available"] == 600
    assert billing["packs"]["remaining"] == 500
    (pack,) = billing["packs"]["items"]
    assert (pack["source"], pack["pack_id"], pack["credits"], pack["status"]) == (
        "code",
        "code",
        500,
        "paid",
    )
    (redemption,) = billing["redemptions"]
    assert (redemption["kind"], redemption["credits"], pack["redemption_id"]) == (
        "credits",
        500,
        redemption["id"],
    )
    assert billing["plan_grant"] is None
    assert (await client.get("/api/me")).json()["credits_remaining"] == 600
    # Once per user per code; the code's second use is another user's.
    again = await _redeem(client, code)
    assert (again.status_code, again.json()["error"]["type"]) == (409, "code_redeemed")
    other = await _another_user(sessionmaker, "second")
    async with sessionmaker() as db:
        granted = await codes.redeem(db, settings, other, code)
        assert granted.kind == "credits"
        third = await _another_user(sessionmaker, "third")
        with pytest.raises(ApiError) as refused:
            await codes.redeem(db, settings, third, code)
        assert (refused.value.status_code, refused.value.error_type) == (409, "code_exhausted")
    # A charge takes the period's credits first, then the code's, as a bought pack's
    # (docs/billing.md § Top-up packs).
    async with sessionmaker() as db:
        entry = await ledger.reserve(db, settings, user_id=user_id, kind=ledger.PARSE, amount=130)
        await db.commit()
        pack_row = await db.get(CreditPack, f"redeem-{redemption['id']}")
        assert (entry.amount, pack_row.spent) == (130, 30)
    assert (await client.get("/api/billing")).json()["credits_available"] == 470


async def test_a_code_that_is_unknown_disabled_or_past_its_date_is_refused(
    make_client, sessionmaker
) -> None:
    client = make_client()
    await _signed_in(client)
    settings = client.app.state.settings
    for typed in ("", "EMA-NOPE", "EMA-AAAA-AAAA-AAAA", "x" * 64):
        answer = await _redeem(client, typed)
        assert (answer.status_code, answer.json()["error"]["type"]) == (404, "code_invalid")
    assert (await _redeem(client, "x" * 65)).status_code == 422
    disabled = await _code(sessionmaker, settings, credits=5, valid_days=10)
    async with sessionmaker() as db:
        assert (await codes.disable(db, settings, disabled)) is not None
        assert (await codes.disable(db, settings, "EMA-AAAA-AAAA-AAAA")) is None
    answer = await _redeem(client, disabled)
    assert (answer.status_code, answer.json()["error"]["type"]) == (404, "code_invalid")
    expired = await _code(
        sessionmaker,
        settings,
        credits=5,
        valid_days=10,
        redeem_by=datetime.now(UTC) - timedelta(days=1),
    )
    answer = await _redeem(client, expired)
    assert (answer.status_code, answer.json()["error"]["type"]) == (409, "code_expired")
    # Nothing was granted or recorded.
    billing = (await client.get("/api/billing")).json()
    assert billing["packs"]["items"] == [] and billing["redemptions"] == []


async def test_refused_attempts_are_capped_per_window(sessionmaker) -> None:
    settings = settings_with(redeem_max_failures=3, redeem_failure_window_seconds=3600)
    user_id = await _user(sessionmaker)
    now = datetime(2026, 10, 27, 12, tzinfo=UTC)
    async with sessionmaker() as db:
        for _ in range(3):
            with pytest.raises(ApiError) as refused:
                await codes.redeem(db, settings, user_id, "EMA-AAAA-AAAA-AAAA", now)
            assert refused.value.error_type == "code_invalid"
        # The cap: a 429 that says when the window has passed, whatever is typed.
        good = await _code(sessionmaker, settings, credits=5, valid_days=10)
        with pytest.raises(ApiError) as capped:
            await codes.redeem(db, settings, user_id, good, now + timedelta(minutes=10))
        assert (capped.value.status_code, capped.value.error_type) == (429, "rate_limit")
        assert capped.value.headers == {"Retry-After": str(50 * 60)}
        # The window passed: a success, which clears the count.
        granted = await codes.redeem(db, settings, user_id, good, now + timedelta(hours=1))
        assert granted.credits == 5
        user = await _redeem_state(sessionmaker, user_id)
        assert user == (0, None)
        # A refusal after the success starts a new window.
        with pytest.raises(ApiError):
            await codes.redeem(db, settings, user_id, good, now + timedelta(hours=2))
        assert await _redeem_state(sessionmaker, user_id) == (1, now + timedelta(hours=2))


async def _redeem_state(sessionmaker, user_id: int) -> tuple[int, datetime | None]:
    async with sessionmaker() as db:
        user = await db.get(User, user_id)
        return user.redeem_failures, user.redeem_failed_at


# --- redeeming days of a plan --------------------------------------------------------------


async def test_a_plan_code_starts_at_once_when_nothing_else_pays(make_client, sessionmaker) -> None:
    client = make_client(**PLANS, grant_period_days=30)
    user_id = await _signed_in(client)
    settings = client.app.state.settings
    assert (await client.get("/api/me")).json()["plan"] == "free"
    code = await _code(sessionmaker, settings, plan_id="pro", days=30)
    before = datetime.now(UTC)
    answer = await _redeem(client, code)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["kind"], body["plan_id"], body["days"], body["days_banked"]) == (
        "plan",
        "pro",
        30,
        0,
    )
    period_end = datetime.fromisoformat(body["period_end"])
    assert timedelta(days=29, hours=23) < period_end - before < timedelta(days=30, hours=1)
    me = (await client.get("/api/me")).json()
    assert (me["plan"], me["credits_remaining"]) == ("pro", 5000)
    billing = (await client.get("/api/billing")).json()
    assert (billing["plan"]["id"], billing["period"]["source"]) == ("pro", "grant")
    assert billing["period"]["granted"] == 5000
    assert datetime.fromisoformat(billing["period"]["end"]) == period_end
    assert billing["subscription"] is None
    assert billing["plan_grant"] == {
        "plan_id": "pro",
        "days_banked": 0,
        "period_start": billing["period"]["start"],
        "period_end": billing["period"]["end"],
    }
    assert billing["redemptions"][0]["days"] == 30
    # Another code stacks: its days wait in the bank, the open period is unchanged.
    more = await _code(sessionmaker, settings, plan_id="pro", days=45)
    stacked = (await _redeem(client, more)).json()
    assert (stacked["days_banked"], datetime.fromisoformat(stacked["period_end"])) == (
        45,
        period_end,
    )
    # A charge lands in the granted period's row, under the plan's grant.
    async with sessionmaker() as db:
        entry = await ledger.reserve(db, settings, user_id=user_id, kind=ledger.PARSE, amount=7)
        await db.commit()
        row = await db.get(CreditPeriod, (user_id, entry.period_start))
        assert (row.plan_id, row.granted, row.spent, row.period_end) == ("pro", 5000, 7, period_end)
    # The entitlements are the plan's.
    async with sessionmaker() as db:
        plan = await ledger.plan_of(db, settings, user_id)
        assert (plan.id, plan.sandbox, plan.max_jobs) == ("pro", True, 2)


async def test_days_wait_under_a_paid_subscription_and_a_paid_period_cuts_an_open_one(
    make_client, sessionmaker
) -> None:
    client = make_client(**PLANS, grant_period_days=30)
    user_id = await _signed_in(client)
    settings = client.app.state.settings
    await grant_plan(sessionmaker, user_id)
    code = await _code(sessionmaker, settings, plan_id="pro", days=30)
    body = (await _redeem(client, code)).json()
    assert (body["days_banked"], body["period_end"]) == (30, None)
    billing = (await client.get("/api/billing")).json()
    assert billing["period"]["source"] == "subscription"
    assert billing["plan_grant"]["days_banked"] == 30
    assert billing["plan_grant"]["period_start"] is None
    # The subscription lapses: the next read opens the bank's days.
    async with sessionmaker() as db:
        await db.execute(delete(Subscription).where(Subscription.user_id == user_id))
        await db.commit()
    billing = (await client.get("/api/billing")).json()
    assert (billing["period"]["source"], billing["plan_grant"]["days_banked"]) == ("grant", 0)
    opened_start = datetime.fromisoformat(billing["plan_grant"]["period_start"])
    # Subscribed again, since yesterday: the open granted period is cut at the paid
    # one's start and its whole days go back — one read says so, and the next charge
    # agrees with it.
    await grant_plan(sessionmaker, user_id, version=2)
    billing = (await client.get("/api/billing")).json()
    assert (billing["period"]["source"], billing["plan"]["id"]) == ("subscription", "pro")
    assert billing["plan_grant"] == {
        "plan_id": "pro",
        "days_banked": 30,
        "period_start": None,
        "period_end": None,
    }
    async with sessionmaker() as db:
        grant = await db.get(PlanGrant, user_id)
        assert (grant.days_banked, grant.period_start) == (30, None)
        entry = await ledger.reserve(db, settings, user_id=user_id, kind=ledger.PARSE, amount=1)
        await db.commit()
        subscription = await db.get(Subscription, user_id)
        assert entry.period_start == subscription.period_start
        row = await db.get(CreditPeriod, (user_id, entry.period_start))
        assert (row.plan_id, row.period_end) == ("pro", subscription.period_end)
        # The cut granted period's row, if it had one, is left as it is; none was used.
        assert await db.get(CreditPeriod, (user_id, opened_start)) is None


async def test_a_code_of_another_plan_than_the_banks_is_refused(make_client, sessionmaker) -> None:
    client = make_client(kano_pay_plans_json=TWO_PLANS_JSON)
    await _signed_in(client)
    settings = client.app.state.settings
    assert (
        await _redeem(client, await _code(sessionmaker, settings, plan_id="pro", days=30))
    ).status_code == 200
    team = await _code(sessionmaker, settings, plan_id="team", days=30)
    answer = await _redeem(client, team)
    assert (answer.status_code, answer.json()["error"]["type"]) == (409, "code_plan_conflict")
    # A plan code names a configured plan, or is not made.
    async with sessionmaker() as db:
        with pytest.raises(ValueError):
            await codes.create_codes(
                db,
                settings,
                kind=codes.PLAN,
                count=1,
                max_uses=1,
                redeem_by=None,
                label="",
                plan_id="max",
                days=30,
            )


# --- the CLI ------------------------------------------------------------------------------


async def test_the_cli_creates_lists_and_disables_codes(make_client, sessionmaker, capsys) -> None:
    client = make_client(**PLANS)
    await _signed_in(client)
    settings = client.app.state.settings

    async def run(*argv: str) -> tuple[int, str, str]:
        async with sessionmaker() as db:
            status = await cli.run(cli.build_parser().parse_args(argv), settings, db)
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    status, out, _ = await run(
        "create",
        "--credits",
        "500",
        "--valid-days",
        "90",
        "--count",
        "2",
        "--redeem-by",
        "2030-12-31",
        "--label",
        "launch",
    )
    assert status == 0
    printed = out.splitlines()
    assert len(printed) == 2 and all(PRINTED.match(line) for line in printed)
    status, out, _ = await run("create", "--plan", "pro", "--days", "60", "--uses", "5")
    assert status == 0 and PRINTED.match(out.strip())
    plan_code = out.strip()
    async with sessionmaker() as db:
        rows = {row.code: row for row in (await db.execute(select(RedeemCode))).scalars()}
    assert len(rows) == 3
    first = rows[codes.normalize(printed[0], "EMA")]
    assert (first.kind, first.credits, first.valid_days, first.max_uses, first.label) == (
        "credits",
        500,
        90,
        1,
        "launch",
    )
    assert first.redeem_by == datetime(2030, 12, 31, 23, 59, 59, tzinfo=UTC)
    plan_row = rows[codes.normalize(plan_code, "EMA")]
    assert (plan_row.kind, plan_row.plan_id, plan_row.days, plan_row.max_uses) == (
        "plan",
        "pro",
        60,
        5,
    )
    # A plan the deployment does not configure, and days left out, are refused.
    assert (await run("create", "--plan", "max", "--days", "30"))[0] == 2
    assert (await run("create", "--plan", "pro"))[0] == 2
    status, out, _ = await run("list")
    assert status == 0 and len(out.splitlines()) == 3
    assert f"{plan_code}\t60 days of pro\t0/5\t-\tlive\t" in out
    assert f"{printed[0]}\t500 credits, 90 days\t0/1\t2030-12-31\tlive\tlaunch" in out
    status, out, _ = await run("disable", printed[1].lower())
    assert (status, out.strip()) == (0, f"disabled {printed[1]}")
    assert (await run("disable", "EMA-AAAA-AAAA-AAAA"))[0] == 1
    status, out, _ = await run("list")
    assert len(out.splitlines()) == 2 and printed[1] not in out
    status, out, _ = await run("list", "--all")
    assert (
        len(out.splitlines()) == 3
        and f"{printed[1]}\t500 credits, 90 days\t0/1\t2030-12-31\tdisabled" in out
    )
    answer = await _redeem(client, printed[1])
    assert (answer.status_code, answer.json()["error"]["type"]) == (404, "code_invalid")
    assert (await _redeem(client, printed[0])).status_code == 200
    _, out, _ = await run("list", "--all")
    assert f"{printed[0]}\t500 credits, 90 days\t1/1\t2030-12-31\tused up" in out
