"""Settings in the database (docs/admin.md § Settings, § Acceptance): the reading layer and
its cache, the console's settings routes — checked as at start, never echoing a secret,
confirmed when they take away what accounts have, recorded — and the snapshots the worker
and the CLI take.

Nothing leaves the process: Kano Proxy is respx's, and no test calls Kano Pay.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy import text as sql

from sightread import codes as codes_cli
from sightread import runtime_settings, worker
from sightread.config import Settings
from sightread.db.models import (
    AuditLog,
    ChatTurn,
    Checkout,
    Conversation,
    Document,
    PackCheckout,
    RedeemCode,
    RuntimeSetting,
    User,
    utcnow,
)
from sightread.jobs import chat_compact, chat_runner
from sightread.jobs.queue import claim_next_turn
from sightread.runtime_settings import OVERRIDABLE, RuntimeSettings, effective
from tests.conftest import (
    CSRF_HEADERS,
    KANO_API_KEY,
    PAY_SETTINGS,
    PRO_PLANS_JSON,
    TEST_MODEL_PRICES,
    TEST_SECRET_KEY,
    grant_plan,
)
from tests.test_admin import _account, _add, _audit, _error, _group, _last_audit_id, _owner
from tests.test_chat import MESSAGES_URL, _seed_document, assistant_turn

# Every secret the tests' settings carry: none may appear in an answer or a log line.
KNOWN_SECRETS = (TEST_SECRET_KEY, KANO_API_KEY, PAY_SETTINGS["kano_pay_api_key"])


def _base(**overrides) -> Settings:
    return Settings(
        **{
            "app_env": "local",
            "secret_key": TEST_SECRET_KEY,
            "kano_proxy_api_key": KANO_API_KEY,
            "parse_model": "vendor/vision-model",
            "model_prices_json": TEST_MODEL_PRICES,
            **overrides,
        }
    )


async def _store(sessionmaker, key: str, value: str) -> None:
    async with sessionmaker() as db:
        row = await db.get(RuntimeSetting, key)
        if row is None:
            db.add(RuntimeSetting(key=key, value=value))
        else:
            row.value = value
        await db.commit()


async def _rows(sessionmaker) -> dict[str, str]:
    async with sessionmaker() as db:
        return dict((await db.execute(select(RuntimeSetting.key, RuntimeSetting.value))).all())


def _without(model: str) -> str:
    """`TEST_MODEL_PRICES` less one model, in its order."""
    prices = json.loads(TEST_MODEL_PRICES)
    del prices[model]
    return json.dumps(prices)


# --- the reading layer -----------------------------------------------------------------


def test_effective_is_the_very_object_without_rows_and_a_checked_new_one_with_them() -> None:
    base = _base()
    assert effective(base, {}) is base
    prices = json.loads(TEST_MODEL_PRICES)
    prices["vendor/new-model"] = {"input": 2, "cache_read": 2, "cache_write": 2, "output": 2}
    merged = effective(base, {"CREDIT_USD": "0.004", "MODEL_PRICES_JSON": json.dumps(prices)})
    assert merged is not base
    assert str(merged.credit_usd) == "0.004" and str(base.credit_usd) == "0.002"
    # The parsed table is the new text's, not a copy of the one cached on `.env`'s.
    assert "vendor/new-model" in merged.model_prices
    assert "vendor/new-model" not in base.model_prices
    assert list(merged.model_prices)[-1] == "vendor/new-model"
    # Everything else is `.env`'s, secrets included.
    assert (merged.secret_key, merged.kano_proxy_api_key) == (TEST_SECRET_KEY, KANO_API_KEY)
    assert sorted(OVERRIDABLE) == sorted(
        [
            "PARSE_MODEL",
            "CHECK_MODEL",
            "PARSE_FALLBACK_MODEL",
            "MODEL_PRICES_JSON",
            "CREDIT_USD",
            "PARSE_CREDITS_PER_PAGE",
            "CHAT_MIN_CREDITS",
            "FREE_CREDITS_PER_PERIOD",
            "FREE_STORAGE_BYTES",
            "FREE_MAX_JOBS",
            "KANO_PAY_PLANS_JSON",
            "KANO_PAY_PACKS_JSON",
        ]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("free_storage_bytes", -1),
        ("free_max_jobs", -1),
        ("runtime_settings_cache_seconds", 0),
        ("parse_credits_per_page", 2_147_483_648),
        ("chat_min_credits", 2_147_483_648),
        ("free_credits_per_period", 2_147_483_648),
    ],
)
def test_the_start_checks_refuse_a_negative_free_plan_credits_past_32_bits_and_no_cache(
    field: str, value: int
):
    with pytest.raises(ValueError, match=field.upper()):
        _base(**{field: value})


def test_the_most_credits_the_ledger_holds_pass_the_start_checks() -> None:
    most = 2_147_483_647
    settings = _base(
        parse_credits_per_page=most,
        chat_min_credits=most,
        free_credits_per_period=most,
        kano_pay_plans_json=json.dumps(
            [{"planId": "pro", "credits": most, "storageBytes": 1, "sandbox": True, "maxJobs": 1}]
        ),
        kano_pay_packs_json=json.dumps([{"packId": "credits", "credits": most, "validDays": 1}]),
    )
    assert settings.plans["pro"].credits == most and settings.packs["credits"].credits == most


async def test_the_rows_are_kept_for_the_cache_time_and_read_again_after_a_change(
    sessionmaker, monkeypatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(runtime_settings, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    base = _base(runtime_settings_cache_seconds=30)
    reads: list[int] = []

    def counted():
        reads.append(1)
        return sessionmaker

    runtime = RuntimeSettings(lambda: base, counted)
    assert await runtime.current() is base
    await _store(sessionmaker, "FREE_MAX_JOBS", "0")
    # Within the cache time the copy stands, read once.
    clock[0] += 29
    assert await runtime.current() is base
    assert len(reads) == 1
    # Past it, the rows are read again.
    clock[0] += 1
    current = await runtime.current()
    assert current.free_max_jobs == 0 and len(reads) == 2
    assert await runtime.current() is current
    # A change made here empties the copy at once.
    await _store(sessionmaker, "FREE_MAX_JOBS", "1")
    runtime.invalidate()
    assert (await runtime.current()).free_max_jobs == 1
    assert len(reads) == 3


async def test_one_read_at_a_time_and_the_others_wait_for_its_answer(sessionmaker) -> None:
    await _store(sessionmaker, "CHAT_MIN_CREDITS", "4")
    base = _base()
    reads: list[int] = []

    def counted():
        reads.append(1)
        return sessionmaker

    runtime = RuntimeSettings(lambda: base, counted)
    answers = await asyncio.gather(*(runtime.current() for _ in range(8)))
    assert len(reads) == 1
    assert all(answer is answers[0] for answer in answers)
    assert answers[0].chat_min_credits == 4


async def test_a_read_after_a_change_is_waited_for_and_one_that_missed_a_change_is_not_kept(
    sessionmaker,
) -> None:
    await _store(sessionmaker, "CREDIT_USD", "0.004")
    base = _base()
    gate = {"on": False}
    held = asyncio.Event()
    release = asyncio.Event()

    @contextlib.asynccontextmanager
    async def gated():
        async with sessionmaker() as db:
            yield db
        # The rows are read and the answer not given yet.
        if gate["on"]:
            held.set()
            await release.wait()

    runtime = RuntimeSettings(lambda: base, lambda: gated)
    assert str((await runtime.current()).credit_usd) == "0.004"

    await _store(sessionmaker, "CREDIT_USD", "0.008")
    runtime.invalidate()
    gate["on"] = True
    first = asyncio.create_task(runtime.current())
    await held.wait()
    # Asked while that read runs: it waits for an answer, never the copy being replaced.
    second = asyncio.create_task(runtime.current())
    await asyncio.sleep(0.05)
    assert not second.done()
    # A change written after the running read took its rows.
    await _store(sessionmaker, "CREDIT_USD", "0.016")
    runtime.invalidate()
    gate["on"] = False
    release.set()
    assert str((await first).credit_usd) == "0.008"
    # That answer was not kept: the next read finds the change, and is kept.
    newest = await second
    assert str(newest.credit_usd) == "0.016"
    assert await runtime.current() is newest


async def test_an_unreadable_database_keeps_the_last_settings_and_logs_once(
    sessionmaker, monkeypatch, caplog
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(runtime_settings, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await _store(sessionmaker, "CREDIT_USD", "0.004")
    base = _base()
    reachable = [True]

    def flaky():
        if not reachable[0]:
            raise ConnectionError("the database is down")
        return sessionmaker

    runtime = RuntimeSettings(lambda: base, flaky)
    good = await runtime.current()
    assert str(good.credit_usd) == "0.004"
    reachable[0] = False
    with caplog.at_level(logging.WARNING, logger=runtime_settings.logger.name):
        for _ in range(3):
            clock[0] += 60
            assert await runtime.current() is good
    assert [r.getMessage() for r in caplog.records if r.name == runtime_settings.logger.name] == [
        "could not read the runtime settings; keeping the settings last read"
    ]
    # Before any good read, `.env`'s.
    fresh = RuntimeSettings(lambda: base, flaky)
    assert await fresh.current() is base


async def test_a_request_reads_on_its_own_session_and_goes_on_after_a_failed_read(
    sessionmaker, monkeypatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(runtime_settings, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await _store(sessionmaker, "CHAT_MIN_CREDITS", "4")
    base = _base()

    def no_second_session():
        raise AssertionError("a request's read asked the pool for another session")

    runtime = RuntimeSettings(lambda: base, no_second_session)
    async with sessionmaker() as db:
        read = await runtime.current(db)
        assert read.chat_min_credits == 4

        # A read that fails leaves the request's session usable, as PostgreSQL would not
        # after a failed statement in its transaction.
        execute = db.execute
        broken = [True]

        async def failing(statement, *args, **kwargs):
            if broken[0]:
                broken[0] = False
                return await execute(sql("SELECT * FROM no_such_table"))
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", failing)
        clock[0] += 60
        assert await runtime.current(db) is read
        assert await db.scalar(select(func.count()).select_from(User)) is not None


async def test_rows_that_no_longer_fit_env_leave_env_alone_in_effect(
    make_client, monkeypatch, caplog
) -> None:
    owner = await _owner(make_client, max_jobs_per_user=2)
    sessionmaker = owner.app.state.sessionmaker
    # Stored when the ceiling allowed it; `.env`'s ceiling is 2 now. A row of no key the
    # database may override is ignored.
    await _store(sessionmaker, "FREE_MAX_JOBS", "3")
    await _store(sessionmaker, "CREDIT_USD", "0.004")
    await _store(sessionmaker, "SECRET_KEY", "stored-by-hand")
    runtime = owner.app.state.runtime_settings
    runtime.invalidate()
    with caplog.at_level(logging.ERROR, logger=runtime_settings.logger.name):
        assert await runtime.current() is owner.app.state.settings
        runtime.invalidate()
        assert await runtime.current() is owner.app.state.settings
    assert runtime.ignored == "FREE_MAX_JOBS exceeds MAX_JOBS_PER_USER"
    assert [r.getMessage() for r in caplog.records if r.name == runtime_settings.logger.name] == [
        "the database's settings no longer fit .env, which alone is in effect: "
        "FREE_MAX_JOBS exceeds MAX_JOBS_PER_USER"
    ]
    page = (await owner.get("/api/admin/settings")).json()
    assert page["ignored"] == "FREE_MAX_JOBS exceeds MAX_JOBS_PER_USER"
    # The API runs on `.env`'s rate meanwhile.
    users = (await owner.get("/api/admin/users")).json()
    assert users["grant"]["credit_usd"] == 0.002
    # Restoring the key that no longer fits brings the others back.
    restored = await owner.delete("/api/admin/settings/FREE_MAX_JOBS", headers=CSRF_HEADERS)
    assert restored.status_code == 200, restored.text
    assert restored.json()["ignored"] is None
    assert (await owner.get("/api/admin/users")).json()["grant"]["credit_usd"] == 0.004
    assert runtime.ignored is None


async def test_with_two_values_that_no_longer_fit_env_each_is_restored_on_its_own(
    make_client,
) -> None:
    owner = await _owner(make_client, max_jobs_per_user=2, **PAY_SETTINGS)
    sessionmaker = owner.app.state.sessionmaker
    # Both stored when the ceiling allowed them; `.env`'s is 2 now.
    plans = json.loads(PRO_PLANS_JSON)
    plans[0]["maxJobs"] = 3
    await _store(sessionmaker, "FREE_MAX_JOBS", "3")
    await _store(sessionmaker, "KANO_PAY_PLANS_JSON", json.dumps(plans))
    owner.app.state.runtime_settings.invalidate()
    start = await _last_audit_id(owner)
    plans_message = "KANO_PAY_PLANS_JSON: 'pro'.maxJobs exceeds MAX_JOBS_PER_USER"
    assert (await owner.get("/api/admin/settings")).json()["ignored"] is not None

    # A new value is checked with the rest, and the other value still fails.
    fixed = await owner.put(
        "/api/admin/settings/FREE_MAX_JOBS", json={"value": "1"}, headers=CSRF_HEADERS
    )
    assert fixed.status_code == 422
    assert _error(fixed)["message"] == plans_message
    # A restore only moves toward `.env`: never refused, `.env` alone stays in effect.
    first = await owner.delete("/api/admin/settings/FREE_MAX_JOBS", headers=CSRF_HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["ignored"] == plans_message
    assert await _rows(sessionmaker) == {"KANO_PAY_PLANS_JSON": json.dumps(plans)}
    second = await owner.delete("/api/admin/settings/KANO_PAY_PLANS_JSON", headers=CSRF_HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["ignored"] is None
    assert await _rows(sessionmaker) == {}
    assert [(record.action, record.target_id) for record in await _audit(owner, start)] == [
        ("settings.reset", "FREE_MAX_JOBS"),
        ("settings.reset", "KANO_PAY_PLANS_JSON"),
    ]


# --- the routes ------------------------------------------------------------------------


async def test_the_page_lists_the_twelve_and_only_whether_each_secret_is_set(
    make_client,
) -> None:
    owner = await _owner(make_client, **PAY_SETTINGS)
    response = await owner.get("/api/admin/settings")
    assert response.status_code == 200
    page = response.json()
    assert [item["key"] for item in page["settings"]] == list(OVERRIDABLE)
    credit = page["settings"][4]
    assert credit == {
        "key": "CREDIT_USD",
        "value": "0.002",
        "source": "env",
        "env_value": "0.002",
        "updated_at": None,
        "updated_by": None,
    }
    assert page["settings"][3]["value"] == TEST_MODEL_PRICES
    assert page["settings"][10]["value"] == PRO_PLANS_JSON
    secrets = {item["key"]: item for item in page["secrets"]}
    assert secrets["SECRET_KEY"] == {"key": "SECRET_KEY", "set": True}
    assert secrets["KANO_PROXY_API_KEY"] == {"key": "KANO_PROXY_API_KEY", "set": True}
    assert secrets["KANO_PAY_API_KEY"] == {"key": "KANO_PAY_API_KEY", "set": True}
    assert secrets["GOOGLE_CLIENT_SECRET"] == {"key": "GOOGLE_CLIENT_SECRET", "set": False}
    assert all(set(item) == {"key", "set"} for item in page["secrets"])
    for secret in KNOWN_SECRETS:
        assert secret not in response.text
    assert page["ignored"] is None
    assert page["cache_seconds"] == 30
    assert page["ceilings"] == {
        "WORKSPACE_QUOTA_BYTES": owner.app.state.settings.workspace_quota_bytes,
        "MAX_JOBS_PER_USER": owner.app.state.settings.max_jobs_per_user,
    }


async def test_a_value_stored_applies_at_once_and_is_recorded(make_client) -> None:
    owner = await _owner(make_client)
    start = await _last_audit_id(owner)
    saved = await owner.put(
        "/api/admin/settings/CREDIT_USD", json={"value": " 0.004 "}, headers=CSRF_HEADERS
    )
    assert saved.status_code == 200, saved.text
    credit = saved.json()["settings"][4]
    assert (credit["value"], credit["source"], credit["env_value"]) == (
        "0.004",
        "database",
        "0.002",
    )
    assert credit["updated_by"]["id"] == owner.user_id
    assert credit["updated_at"] is not None
    # The API answers with it at once.
    assert (await owner.get("/api/admin/users")).json()["grant"]["credit_usd"] == 0.004
    [record] = await _audit(owner, start)
    assert (
        record.actor_id,
        record.permission,
        record.action,
        record.target_type,
        record.target_id,
        record.before,
        record.after,
    ) == (
        owner.user_id,
        "settings.write",
        "settings.update",
        "setting",
        "CREDIT_USD",
        {"value": "0.002", "source": "env"},
        {"value": "0.004", "source": "database"},
    )
    assert record.ip is not None

    # The same value again changes nothing and records nothing.
    again = await owner.put(
        "/api/admin/settings/CREDIT_USD", json={"value": "0.004"}, headers=CSRF_HEADERS
    )
    assert again.status_code == 200
    assert len(await _audit(owner, start)) == 1

    # Restored to `.env`: the row goes, the record says so.
    restored = await owner.delete("/api/admin/settings/CREDIT_USD", headers=CSRF_HEADERS)
    assert restored.status_code == 200
    assert restored.json()["settings"][4]["source"] == "env"
    assert await _rows(owner.app.state.sessionmaker) == {}
    reset = (await _audit(owner, start))[-1]
    assert (reset.action, reset.before, reset.after) == (
        "settings.reset",
        {"value": "0.004", "source": "database"},
        {"value": "0.002", "source": "env"},
    )
    assert (await owner.get("/api/admin/users")).json()["grant"]["credit_usd"] == 0.002
    # Restoring a key `.env` already holds writes nothing.
    assert (
        await owner.delete("/api/admin/settings/CREDIT_USD", headers=CSRF_HEADERS)
    ).status_code == 200
    assert len(await _audit(owner, start)) == 2


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "FREE_MAX_JOBS",
            "two",
            "FREE_MAX_JOBS: Input should be a valid integer, unable to parse string as an integer",
        ),
        ("FREE_MAX_JOBS", "-1", "FREE_MAX_JOBS must be >= 0"),
        ("FREE_STORAGE_BYTES", str(10**15), "FREE_STORAGE_BYTES exceeds WORKSPACE_QUOTA_BYTES"),
        ("CREDIT_USD", "0", "CREDIT_USD must be positive"),
        ("MODEL_PRICES_JSON", "{}", "MODEL_PRICES_JSON prices none of: vendor/vision-model"),
        ("PARSE_MODEL", "vendor/unpriced", "MODEL_PRICES_JSON prices none of: vendor/unpriced"),
        (
            "FREE_CREDITS_PER_PERIOD",
            "2147483648",
            "FREE_CREDITS_PER_PERIOD must be at most 2147483647",
        ),
        (
            "KANO_PAY_PLANS_JSON",
            '[{"planId": "pro", "credits": 21474836470, "storageBytes": 1, "sandbox": true, '
            '"maxJobs": 1}]',
            "KANO_PAY_PLANS_JSON: 'pro'.credits must be at most 2147483647",
        ),
        (
            "KANO_PAY_PACKS_JSON",
            '[{"packId": "credits", "credits": 21474836470, "validDays": 365}]',
            "KANO_PAY_PACKS_JSON: 'credits'.credits must be at most 2147483647",
        ),
        (
            "KANO_PAY_PACKS_JSON",
            '[{"packId": "credits", "credits": 2000, "validDays": 36501}]',
            "KANO_PAY_PACKS_JSON: 'credits'.validDays must be at most 36500",
        ),
    ],
)
async def test_a_change_the_start_checks_refuse_is_422_in_their_words_alone(
    make_client, caplog, key: str, value: str, message: str
) -> None:
    caplog.set_level(logging.DEBUG)
    owner = await _owner(make_client, **PAY_SETTINGS)
    start = await _last_audit_id(owner)
    response = await owner.put(
        f"/api/admin/settings/{key}", json={"value": value}, headers=CSRF_HEADERS
    )
    assert response.status_code == 422
    assert _error(response) == {"type": "invalid_request", "message": message}
    for secret in KNOWN_SECRETS:
        assert secret not in response.text
        assert secret not in caplog.text
    assert await _rows(owner.app.state.sessionmaker) == {}
    assert await _audit(owner, start) == []


async def test_an_unknown_key_and_a_value_too_long_are_422(make_client) -> None:
    owner = await _owner(make_client)
    for key, value in (
        ("SECRET_KEY", "x"),
        ("WORKSPACE_QUOTA_BYTES", "1"),
        ("PARSE_MODEL", "m" * 65_537),
    ):
        response = await owner.put(
            f"/api/admin/settings/{key}", json={"value": value}, headers=CSRF_HEADERS
        )
        assert response.status_code == 422, key
    unknown = await owner.delete("/api/admin/settings/SECRET_KEY", headers=CSRF_HEADERS)
    assert unknown.status_code == 422
    assert await _rows(owner.app.state.sessionmaker) == {}


async def test_reading_takes_settings_read_and_changing_settings_write(make_client) -> None:
    owner = await _owner(make_client)
    reader = await _account(make_client, "reader@example.test")
    nobody = await _account(make_client, "nobody@example.test")
    group = await _group(owner, "readers", ["settings.read"])
    assert (await _add(owner, group["id"], user_id=reader.user_id)).status_code == 201

    assert (await reader.get("/api/admin/settings")).status_code == 200
    for response in (
        await reader.put(
            "/api/admin/settings/CREDIT_USD", json={"value": "0.004"}, headers=CSRF_HEADERS
        ),
        await reader.delete("/api/admin/settings/CREDIT_USD", headers=CSRF_HEADERS),
        await nobody.get("/api/admin/settings"),
    ):
        assert response.status_code == 403
        assert _error(response)["type"] == "permission_denied"
    # Without the custom header, before the permission.
    bare = await owner.put("/api/admin/settings/CREDIT_USD", json={"value": "0.004"})
    assert bare.status_code == 403 and _error(bare)["type"] == "auth"
    assert await _rows(owner.app.state.sessionmaker) == {}


async def test_removing_a_price_in_use_asks_first_then_applies(make_client) -> None:
    owner = await _owner(make_client)
    sessionmaker = owner.app.state.sessionmaker
    chosen = await owner.put(
        "/api/agents/chat/settings", json={"model": "codex/gpt-5.5"}, headers=CSRF_HEADERS
    )
    assert chosen.status_code == 200, chosen.text
    document_id = await _seed_document(owner, owner.user_id)
    async with sessionmaker() as db:
        db.add(
            Conversation(
                user_id=owner.user_id,
                document_id=document_id,
                title="t",
                session="s",
                model="codex/gpt-5.5",
            )
        )
        await db.commit()
    start = await _last_audit_id(owner)
    body = {"value": _without("codex/gpt-5.5")}

    asked = await owner.put(
        "/api/admin/settings/MODEL_PRICES_JSON", json=body, headers=CSRF_HEADERS
    )
    assert asked.status_code == 409
    error = _error(asked)
    assert (error["type"], error["reason"]) == ("admin_conflict", "impact")
    assert error["impact"] == {
        "removed": {"models": ["codex/gpt-5.5"], "plans": [], "packs": []},
        "counts": {"conversations": 1, "chat_settings": 1},
    }
    assert await _rows(sessionmaker) == {}
    assert await _audit(owner, start) == []

    confirmed = await owner.put(
        "/api/admin/settings/MODEL_PRICES_JSON",
        json={**body, "confirm": True},
        headers=CSRF_HEADERS,
    )
    assert confirmed.status_code == 200, confirmed.text
    # Stored as typed, its order kept.
    assert await _rows(sessionmaker) == {"MODEL_PRICES_JSON": body["value"]}
    models = (await owner.get("/api/agents/chat/models")).json()
    assert "codex/gpt-5.5" not in json.dumps(models)
    [record] = await _audit(owner, start)
    assert record.after == {"value": body["value"], "source": "database"}

    # Restoring `.env`'s table removes nothing, so it applies at once.
    assert (
        await owner.delete("/api/admin/settings/MODEL_PRICES_JSON", headers=CSRF_HEADERS)
    ).status_code == 200


async def test_removing_a_plan_or_a_pack_in_use_asks_first(make_client) -> None:
    owner = await _owner(
        make_client,
        **PAY_SETTINGS,
        kano_pay_packs_json='[{"packId": "credits", "credits": 2000, "validDays": 365}]',
    )
    sessionmaker = owner.app.state.sessionmaker
    subscriber = await _account(make_client, "subscriber@example.test")
    await grant_plan(sessionmaker, subscriber.user_id)
    async with sessionmaker() as db:
        db.add(RedeemCode(code=uuid.uuid4().hex[:12], kind="plan", plan_id="pro", days=30))
        db.add(Checkout(user_id=owner.user_id, plan_id="pro", idempotency_key=uuid.uuid4().hex))
        # Too old to be paid any more: not counted.
        db.add(
            Checkout(
                user_id=subscriber.user_id,
                plan_id="pro",
                idempotency_key=uuid.uuid4().hex,
                created_at=utcnow() - timedelta(days=2),
            )
        )
        db.add(
            PackCheckout(
                user_id=subscriber.user_id, pack_id="credits", idempotency_key=uuid.uuid4().hex
            )
        )
        # An old checkout no payment can complete any more does not count.
        db.add(
            PackCheckout(
                user_id=owner.user_id,
                pack_id="credits",
                idempotency_key=uuid.uuid4().hex,
                created_at=utcnow() - timedelta(days=2),
            )
        )
        await db.commit()

    plans = await owner.put(
        "/api/admin/settings/KANO_PAY_PLANS_JSON", json={"value": "[]"}, headers=CSRF_HEADERS
    )
    assert plans.status_code == 409
    assert _error(plans)["impact"] == {
        "removed": {"models": [], "plans": ["pro"], "packs": []},
        "counts": {"subscriptions": 1, "plan_codes": 1, "plan_checkouts": 1},
    }
    packs = await owner.put(
        "/api/admin/settings/KANO_PAY_PACKS_JSON", json={"value": "[]"}, headers=CSRF_HEADERS
    )
    assert packs.status_code == 409
    assert _error(packs)["impact"]["counts"] == {"pack_checkouts": 1}
    assert await _rows(sessionmaker) == {}
    confirmed = await owner.put(
        "/api/admin/settings/KANO_PAY_PLANS_JSON",
        json={"value": "[]", "confirm": True},
        headers=CSRF_HEADERS,
    )
    assert confirmed.status_code == 200
    # The subscriber is on Free at once.
    assert (await subscriber.get("/api/me")).json()["plan"] == "free"
    # And restoring `.env`'s plans, which takes nothing away, needs no confirmation.
    assert (
        await owner.delete("/api/admin/settings/KANO_PAY_PLANS_JSON", headers=CSRF_HEADERS)
    ).status_code == 200


async def test_lowering_the_free_plan_asks_first_and_raising_it_does_not(make_client) -> None:
    owner = await _owner(make_client)
    lowered = await owner.put(
        "/api/admin/settings/FREE_CREDITS_PER_PERIOD", json={"value": "100"}, headers=CSRF_HEADERS
    )
    assert lowered.status_code == 409
    assert _error(lowered)["impact"]["counts"] == {"free_accounts": 1}
    # A restore that lowers the allowance asks too.
    raised = await owner.put(
        "/api/admin/settings/FREE_CREDITS_PER_PERIOD", json={"value": "200"}, headers=CSRF_HEADERS
    )
    assert raised.status_code == 200
    restore = await owner.delete(
        "/api/admin/settings/FREE_CREDITS_PER_PERIOD", headers=CSRF_HEADERS
    )
    assert restore.status_code == 409
    confirmed = await owner.request(
        "DELETE",
        "/api/admin/settings/FREE_CREDITS_PER_PERIOD",
        json={"confirm": True},
        headers=CSRF_HEADERS,
    )
    assert confirmed.status_code == 200
    assert await _rows(owner.app.state.sessionmaker) == {}
    actions = [record.action for record in await _audit(owner)]
    assert actions.count("settings.update") == 1 and actions.count("settings.reset") == 1


# --- the worker and the CLI --------------------------------------------------------------


async def test_the_worker_takes_a_new_snapshot_for_each_claim(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    url = sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    base = _base(
        database_url=url,
        upload_dir=str(tmp_path / "uploads"),
        figures_dir=str(tmp_path / "figures"),
        workspaces_dir=str(tmp_path / "workspaces"),
        kano_pay_plans_json=PRO_PLANS_JSON,
    )
    stop = asyncio.Event()
    runtimes: list[RuntimeSettings] = []
    claims: list[tuple[dict, int]] = []

    class Recorded(RuntimeSettings):
        def __init__(self, *args) -> None:
            super().__init__(*args)
            runtimes.append(self)

    async def idle(*args) -> None:
        await args[-1].wait()

    async def no_turn(db, cap):
        return None

    async def claim(db, cap, excluded, generation, *, plan_jobs, free_max_jobs):
        claims.append((plan_jobs, free_max_jobs))
        if len(claims) == 1:
            # Saved on the console between two claims.
            plans = [
                {
                    "planId": "pro",
                    "credits": 5000,
                    "storageBytes": 1,
                    "sandbox": True,
                    "maxJobs": 1,
                },
                {
                    "planId": "team",
                    "credits": 9000,
                    "storageBytes": 1,
                    "sandbox": True,
                    "maxJobs": 2,
                },
            ]
            await _store(sessionmaker, "KANO_PAY_PLANS_JSON", json.dumps(plans))
            await _store(sessionmaker, "FREE_MAX_JOBS", "0")
            runtimes[0].invalidate()
        else:
            stop.set()
        return None

    monkeypatch.setattr(worker, "get_settings", lambda: base)
    monkeypatch.setattr(worker, "RuntimeSettings", Recorded)
    monkeypatch.setattr(worker, "sweep_forever", idle)
    monkeypatch.setattr(worker, "idle_compact_forever", idle)
    monkeypatch.setattr(worker, "claim_next_turn", no_turn)
    monkeypatch.setattr(worker, "claim_next_job", claim)
    monkeypatch.setattr(worker, "IDLE_POLL_SECONDS", 0.01)
    async with asyncio.timeout(20):
        await worker.run_worker(stop)
    assert claims == [({"pro": 2}, 1), ({"pro": 1, "team": 2}, 0)]


async def test_the_worker_reads_again_for_a_turn_on_a_model_priced_since_its_copy(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    url = sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    base = _base(
        database_url=url,
        upload_dir=str(tmp_path / "uploads"),
        figures_dir=str(tmp_path / "figures"),
        workspaces_dir=str(tmp_path / "workspaces"),
    )
    prices = json.loads(TEST_MODEL_PRICES)
    prices["vendor/new-model"] = {"input": 2, "cache_read": 2, "cache_write": 2, "output": 2}
    async with sessionmaker() as db:
        user = User(google_sub="worker-user", email="worker@example.test")
        db.add(user)
        await db.flush()
        document = Document(user_id=user.id, name="d.md")
        db.add(document)
        await db.flush()
        turns = []
        for model in ("vendor/new-model", "vendor/chat-model"):
            conversation = Conversation(
                user_id=user.id, document_id=document.id, session=model, model=model
            )
            db.add(conversation)
            await db.flush()
            turn = ChatTurn(
                user_id=user.id, conversation_id=conversation.id, message_id=1, status="queued"
            )
            db.add(turn)
            await db.flush()
            turns.append(turn.id)
        await db.commit()
    stop = asyncio.Event()
    invalidated: list[int] = []
    ran: list[tuple[uuid.UUID, bool]] = []
    waiting = list(turns)

    class Recorded(RuntimeSettings):
        def invalidate(self) -> None:
            invalidated.append(1)
            super().invalidate()

    async def idle(*args) -> None:
        await args[-1].wait()

    async def no_job(*args, **kwargs):
        return None

    async def claim_turn(db, cap):
        if waiting[0] == turns[0]:
            # Priced on the Settings page after the worker's copy was read: the API's
            # copy was emptied by the change, this one was not.
            await _store(sessionmaker, "MODEL_PRICES_JSON", json.dumps(prices))
        return waiting.pop(0)

    async def run(maker, settings, turn_id, slots) -> None:
        ran.append((turn_id, "vendor/new-model" in settings.model_prices))
        if not waiting:
            stop.set()

    monkeypatch.setattr(worker, "get_settings", lambda: base)
    monkeypatch.setattr(worker, "RuntimeSettings", Recorded)
    monkeypatch.setattr(worker, "sweep_forever", idle)
    monkeypatch.setattr(worker, "idle_compact_forever", idle)
    monkeypatch.setattr(worker, "claim_next_turn", claim_turn)
    monkeypatch.setattr(worker, "claim_next_job", no_job)
    monkeypatch.setattr(worker, "run_turn", run)
    monkeypatch.setattr(worker, "IDLE_POLL_SECONDS", 0.01)
    async with asyncio.timeout(20):
        await worker.run_worker(stop)
    # The first turn's model was not in the copy, so the rows were read again, once; the
    # second's was priced in the copy it was claimed with.
    assert ran == [(turns[0], True), (turns[1], True)]
    assert invalidated == [1]


async def test_idle_compaction_reads_the_settings_once_per_pass(sessionmaker, monkeypatch):
    base = _base(chat_idle_compact_sweep_seconds=1)
    runtime = RuntimeSettings(lambda: base, lambda: sessionmaker)
    stop = asyncio.Event()
    seen: list[int] = []

    async def compact_once(maker, settings):
        seen.append(settings.chat_min_credits)
        if len(seen) == 1:
            await _store(sessionmaker, "CHAT_MIN_CREDITS", "7")
            runtime.invalidate()
        else:
            stop.set()
        return 0

    monkeypatch.setattr(chat_compact, "idle_compact_once", compact_once)
    async with asyncio.timeout(10):
        await chat_compact.idle_compact_forever(sessionmaker, runtime, stop)
    assert seen == [1, 7]


@respx.mock
async def test_a_turn_keeps_the_prices_of_its_claim(make_client, tmp_path) -> None:
    """Each stubbed call is 510 tokens at US$1 per million: 6 credits at US$0.0001 a
    credit. A rate saved mid-turn — ten times cheaper — applies from the next turn."""
    client = make_client(workspaces_dir=str(tmp_path / "workspaces"), credit_usd="0.0001")
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    await client.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    document_id = await _seed_document(client, user_id)
    created = await client.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    conversation_id = created.json()["id"]
    sessionmaker = client.app.state.sessionmaker
    runtime = client.app.state.runtime_settings

    async def answer(request: httpx.Request) -> httpx.Response:
        await _store(sessionmaker, "CREDIT_USD", "0.00001")
        runtime.invalidate()
        return httpx.Response(
            200, content=assistant_turn([{"type": "text", "text": "Hi."}], "end_turn")
        )

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    sent = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 202, sent.text
    snapshot = await runtime.current()
    async with sessionmaker() as db:
        turn_id = await claim_next_turn(db, snapshot.max_chat_turns_per_user)
    await chat_runner.run_turn(sessionmaker, snapshot, turn_id, asyncio.Semaphore(2))
    [turn] = (await client.get(f"/api/conversations/{conversation_id}")).json()["turns"]
    assert (turn["status"], turn["credits"]) == ("succeeded", 6)
    assert str((await runtime.current()).credit_usd) == "0.00001"


async def test_the_codes_cli_gives_a_plan_only_the_database_configures(
    sessionmaker, monkeypatch, capsys
) -> None:
    url = sessionmaker.kw["bind"].url.render_as_string(hide_password=False)
    base = _base(database_url=url, kano_pay_plans_json=PRO_PLANS_JSON)
    team = [{"planId": "team", "credits": 9000, "storageBytes": 1, "sandbox": True, "maxJobs": 2}]
    await _store(sessionmaker, "KANO_PAY_PLANS_JSON", json.dumps(team))
    monkeypatch.setattr(codes_cli, "get_settings", lambda: base)

    assert await codes_cli.main(["create", "--plan", "team", "--days", "30"]) == 0
    assert await codes_cli.main(["create", "--plan", "pro", "--days", "30"]) == 2
    async with sessionmaker() as db:
        rows = (await db.execute(select(RedeemCode.plan_id))).scalars().all()
    assert rows == ["team"]
    assert "not in KANO_PAY_PLANS_JSON" in capsys.readouterr().err


async def test_the_record_of_a_setting_holds_its_text_and_no_secret(make_client) -> None:
    owner = await _owner(make_client)
    await owner.put("/api/admin/settings/PARSE_MODEL", json={"value": "m"}, headers=CSRF_HEADERS)
    page = (await owner.get("/api/admin/audit?area=settings")).json()
    [entry] = page["items"]
    assert (entry["target_type"], entry["target_id"], entry["target_label"]) == (
        "setting",
        "PARSE_MODEL",
        None,
    )
    async with owner.app.state.sessionmaker() as db:
        rows = (await db.execute(select(AuditLog.before, AuditLog.after))).all()
    for secret in KNOWN_SECRETS:
        assert secret not in json.dumps([tuple(row) for row in rows])
