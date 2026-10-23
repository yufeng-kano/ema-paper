"""Credits, plans and periods (docs/billing.md § Acceptance): pure arithmetic and the
configuration that feeds it. The ledger itself arrives with the next step."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sightread.billing.credits import (
    ModelPrice,
    credits_for_usd,
    parse_credits,
    parse_model_prices,
)
from sightread.billing.plans import (
    FREE_PLAN_ID,
    Plan,
    current_period,
    month_bounds,
    parse_plans,
)
from sightread.config import Settings
from sightread.db.models import Subscription

LUNA = '{"input": 0.10, "cache_read": 0.01, "cache_write": 0.10, "output": 0.50}'
OPUS = '{"input": 4, "cache_read": 0.20, "cache_write": 5, "output": 20}'
CREDIT = Decimal("0.002")


def test_a_parse_is_a_flat_rate_per_page_parsed() -> None:
    assert parse_credits(20, 1) == 20
    assert parse_credits(3, 2) == 6
    assert parse_credits(0, 1) == 0
    with pytest.raises(ValueError):
        parse_credits(-1, 1)


def test_a_turn_is_priced_from_its_tokens_and_rounded_up_once() -> None:
    """The measured average turn on Opus (docs/billing.md § Rates): 11 uncached input,
    104K cache reads, 11K cache writes, 2,958 output — about $0.135, 68 credits."""
    opus = parse_model_prices(f'{{"claude-opus-5-5": {OPUS}}}')["claude-opus-5-5"]
    usd = opus.usd(
        prompt_tokens=11,
        cache_read_tokens=103_909,
        cache_write_tokens=11_078,
        completion_tokens=2958,
    )
    assert Decimal("0.13") < usd < Decimal("0.14")
    assert credits_for_usd(usd, CREDIT) == 68
    # Summed over the turn's calls before rounding: two calls of $0.0011 are 2 credits
    # rounded separately and 1 together.
    half = Decimal("0.0011")
    assert credits_for_usd(half, CREDIT) + credits_for_usd(half, CREDIT) == 2
    assert credits_for_usd(half + half, CREDIT) == 2  # 0.0022 / 0.002 = 1.1 → 2
    assert credits_for_usd(Decimal("0.004"), CREDIT) == 2
    # Never under the floor, even for a free call.
    assert credits_for_usd(Decimal(0), CREDIT) == 1
    assert credits_for_usd(Decimal(0), CREDIT, minimum=0) == 0
    with pytest.raises(ValueError):
        credits_for_usd(Decimal(1), Decimal(0))


def test_the_rate_per_thousand_tokens_is_the_price_over_the_credit() -> None:
    luna = parse_model_prices(f'{{"gpt-6-luna": {LUNA}}}')["gpt-6-luna"]
    rates = luna.per_thousand(CREDIT)
    assert rates == {
        "input": Decimal("0.05"),
        "cache_read": Decimal("0.005"),
        "cache_write": Decimal("0.05"),
        "output": Decimal("0.25"),
    }


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"m": {"input": 1}}',
        '{"m": {"input": 1, "cache_read": 1, "cache_write": 1, "output": -1}}',
        '{"m": {"input": "x", "cache_read": 1, "cache_write": 1, "output": 1}}',
        '{"m": {"input": true, "cache_read": 1, "cache_write": 1, "output": 1}}',
        '{"": {"input": 1, "cache_read": 1, "cache_write": 1, "output": 1}}',
    ],
)
def test_a_malformed_price_table_is_refused_whole(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_model_prices(raw)


def test_an_offered_model_without_a_price_refuses_to_start() -> None:
    with pytest.raises(ValueError, match="MODEL_PRICES_JSON prices none of: gpt-6-luna"):
        Settings(parse_model="gpt-6-luna", model_prices_json="{}")
    settings = Settings(
        parse_model="gpt-6-luna",
        chat_models="gpt-6-luna, claude-opus-5-5,gpt-6-luna",
        model_prices_json=f'{{"gpt-6-luna": {LUNA}, "claude-opus-5-5": {OPUS}}}',
    )
    assert settings.chat_model_list == ["gpt-6-luna", "claude-opus-5-5"]
    assert settings.offered_models == {"gpt-6-luna", "claude-opus-5-5"}
    assert settings.model_prices["gpt-6-luna"] == ModelPrice(
        Decimal("0.10"), Decimal("0.01"), Decimal("0.10"), Decimal("0.50")
    )
    # Nothing offered, nothing required: the pre-transition deployment starts as before.
    assert Settings().offered_models == set()


def test_plans_are_read_from_the_pay_configuration_and_bounded_by_the_deployment() -> None:
    plans = parse_plans(
        '[{"planId": "pro", "credits": 5000, "storageBytes": 10737418240, "sandbox": true, '
        '"maxJobs": 2}]',
        storage_ceiling=10_737_418_240,
        jobs_ceiling=2,
    )
    assert plans == {"pro": Plan("pro", 5000, 10_737_418_240, True, 2)}
    assert not plans["pro"].free
    for raw in (
        '[{"planId": "pro", "credits": 5000, "storageBytes": 1, "sandbox": true, "maxJobs": 3}]',
        '[{"planId": "pro", "credits": 5000, "storageBytes": 99, "sandbox": true, "maxJobs": 1}]',
        '[{"planId": "free", "credits": 1, "storageBytes": 1, "sandbox": false, "maxJobs": 1}]',
        '[{"planId": "pro", "credits": -1, "storageBytes": 1, "sandbox": false, "maxJobs": 1}]',
        '[{"planId": "pro", "credits": 1, "storageBytes": 1, "sandbox": "yes", "maxJobs": 1}]',
        '[{"planId": "pro", "credits": 1, "storageBytes": 1, "sandbox": true, "maxJobs": 1}, '
        '{"planId": "pro", "credits": 1, "storageBytes": 1, "sandbox": true, "maxJobs": 1}]',
        "{}",
    ):
        with pytest.raises(ValueError):
            parse_plans(raw, storage_ceiling=10, jobs_ceiling=2)


def test_the_free_plan_is_policy_and_bounded_too() -> None:
    settings = Settings()
    free = settings.free_plan
    assert free.id == FREE_PLAN_ID and free.free
    assert (free.credits, free.storage_bytes, free.sandbox, free.max_jobs) == (
        150,
        52_428_800,
        False,
        1,
    )
    with pytest.raises(ValueError, match="FREE_STORAGE_BYTES"):
        Settings(free_storage_bytes=1, workspace_quota_bytes=0)
    with pytest.raises(ValueError, match="FREE_MAX_JOBS"):
        Settings(free_max_jobs=3, max_jobs_per_user=2)
    with pytest.raises(ValueError, match="CREDIT_USD"):
        Settings(credit_usd="0")
    assert not settings.kano_pay_configured
    assert Settings(
        kano_pay_url="https://pay.example.com/",
        kano_pay_app_id="ema-paper",
        kano_pay_api_key="k",
        kano_pay_webhook_secret="s",
    ).kano_pay_configured


def test_frees_period_is_the_utc_month() -> None:
    start, end = month_bounds(datetime(2026, 12, 31, 23, 59, tzinfo=UTC))
    assert (start, end) == (datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    # A clock in another zone is still the UTC month.
    local = datetime(2026, 11, 1, 7, 30, tzinfo=UTC).astimezone(
        __import__("datetime").timezone(timedelta(hours=8))
    )
    assert month_bounds(local)[0] == datetime(2026, 11, 1, tzinfo=UTC)


def _subscription(**overrides) -> Subscription:
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


def test_the_current_period_is_the_paid_one_only_while_it_holds() -> None:
    free = Plan(FREE_PLAN_ID, 150, 1, False, 1)
    plans = {"pro": Plan("pro", 5000, 2, True, 2)}
    now = datetime(2026, 10, 23, tzinfo=UTC)
    paid = current_period(_subscription(), plans, free, now)
    assert (paid.start, paid.end, paid.plan.id) == (
        datetime(2026, 10, 15, tzinfo=UTC),
        datetime(2026, 11, 15, tzinfo=UTC),
        "pro",
    )
    # A scheduled cancellation changes nothing until the period ends.
    assert (
        current_period(_subscription(cancel_at_period_end=True), plans, free, now).plan.id == "pro"
    )
    month = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    for lapsed in (
        None,
        _subscription(status="past_due"),
        _subscription(status="canceled"),
        _subscription(plan_id="max"),  # not a plan this deployment knows
        _subscription(period_end=datetime(2026, 10, 23, tzinfo=UTC)),  # ended at `now`
        _subscription(period_start=datetime(2026, 10, 24, tzinfo=UTC)),  # not yet
    ):
        period = current_period(lapsed, plans, free, now)
        assert (period.start, period.end, period.plan.id) == (*month, FREE_PLAN_ID)
