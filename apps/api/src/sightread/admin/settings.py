"""The console's settings page (docs/admin.md § Settings): the thirteen settings of `.env`
the database may override, read with where each comes from, and a change — a value
stored, or a key restored to `.env` — checked as the API checks `.env` at start,
confirmed when it takes away what accounts have, stored and recorded.

A change writes into the caller's transaction with its audit row and never commits: the
route commits once, then empties the API's cached settings.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import CHAT
from ..billing.plans import PAID_STATUSES
from ..config import LOCAL_FALLBACK_SECRET_KEY, Settings
from ..db.models import (
    AgentSetting,
    Checkout,
    Conversation,
    PackCheckout,
    PlanGrant,
    RedeemCode,
    RuntimeSetting,
    Subagent,
    Subscription,
    User,
    utcnow,
)
from ..errors import ApiError
from ..runtime_settings import OVERRIDABLE, effective, validation_message
from . import audit

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.sql import ColumnElement

    from .deps import Operator

# The secrets of `.env`, shown only as set or unset — never a value, a length or any of
# it (docs/admin.md § What an operator sees).
SECRETS: dict[str, str] = {
    "SECRET_KEY": "secret_key",
    "GOOGLE_CLIENT_SECRET": "google_client_secret",
    "KANO_PROXY_API_KEY": "kano_proxy_api_key",
    "KANO_PAY_API_KEY": "kano_pay_api_key",
    "KANO_PAY_WEBHOOK_SECRET": "kano_pay_webhook_secret",
    "SANDBOX_TOKEN": "sandbox_token",
    "BRAVE_SEARCH_API_KEY": "brave_search_api_key",
    "OPENALEX_API_KEY": "openalex_api_key",
}
# The most a stored value may hold, in characters (docs/admin.md § Settings).
VALUE_MAX_CHARS = 65_536
# One change at a time, across every API process: a transaction-scoped advisory lock
# (docs/admin.md § Settings). The worker's own lock is 7_312_024_001.
SETTINGS_LOCK_KEY = 7_312_024_002


def env_text(base: Settings, key: str) -> str:
    """`.env`'s text for an overridable key, as the page shows it beside a stored one."""
    return str(getattr(base, OVERRIDABLE[key]))


async def payload(db: AsyncSession, base: Settings) -> dict:
    """The settings page: each overridable key with its text in effect once saved, where
    it comes from, `.env`'s text and who stored it when; whether each secret is set; the
    checks' message while the stored values no longer fit `.env`; and whether the sandbox
    has a network for its list of sites to apply to."""
    rows = (
        await db.execute(
            select(RuntimeSetting, User)
            .outerjoin(User, User.id == RuntimeSetting.updated_by)
            .where(RuntimeSetting.key.in_(OVERRIDABLE))
        )
    ).all()
    stored = {row.key: (row, who) for row, who in rows}
    items = []
    for key in OVERRIDABLE:
        row, who = stored.get(key, (None, None))
        items.append(
            {
                "key": key,
                "value": row.value if row is not None else env_text(base, key),
                "source": "database" if row is not None else "env",
                "env_value": env_text(base, key),
                "updated_at": row.updated_at if row is not None else None,
                "updated_by": None if who is None else {"id": who.id, "name": who.name},
            }
        )
    try:
        effective(base, {key: row.value for key, (row, _) in stored.items()})
        ignored = None
    except ValidationError as exc:
        ignored = validation_message(exc)
    secrets = []
    for key, field in SECRETS.items():
        value = getattr(base, field)
        is_set = value != LOCAL_FALLBACK_SECRET_KEY if key == "SECRET_KEY" else bool(value)
        secrets.append({"key": key, "set": is_set})
    return {
        "settings": items,
        "secrets": secrets,
        "ignored": ignored,
        "cache_seconds": base.runtime_settings_cache_seconds,
        "ceilings": {
            "WORKSPACE_QUOTA_BYTES": base.workspace_quota_bytes,
            "MAX_JOBS_PER_USER": base.max_jobs_per_user,
        },
        "sandbox_network": base.sandbox_network != "none",
    }


async def _count(db: AsyncSession, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where))


async def impact(
    db: AsyncSession, before: Settings, after: Settings, free_accounts: ColumnElement[bool]
) -> dict | None:
    """What going from `before` to `after` takes away from accounts, counted; None when it
    takes nothing that is in use (docs/admin.md § Settings, *Confirmed when it takes away*).
    Each count is one query: conversations through `ix_conversations_model`, the rest over
    tables of a row or a few per account, and the operator's codes."""
    now = utcnow()
    # A checkout Pay opened before this can no longer be paid.
    open_since = now - timedelta(seconds=before.kano_pay_checkout_lifetime_seconds)
    models = [model for model in before.model_prices if model not in after.model_prices]
    plans = [plan for plan in before.plans if plan not in after.plans]
    packs = [pack for pack in before.packs if pack not in after.packs]
    counts: dict[str, int] = {}
    if models:
        counts["conversations"] = await _count(db, Conversation, Conversation.model.in_(models))
        counts["chat_settings"] = await _count(
            db, AgentSetting, AgentSetting.agent == CHAT.name, AgentSetting.model.in_(models)
        )
        counts["subagents"] = await _count(db, Subagent, Subagent.model.in_(models))
    if plans:
        counts["subscriptions"] = await _count(
            db,
            Subscription,
            Subscription.plan_id.in_(plans),
            Subscription.status.in_(PAID_STATUSES),
            Subscription.period_start <= now,
            Subscription.period_end > now,
        )
        counts["day_banks"] = await _count(
            db,
            PlanGrant,
            PlanGrant.plan_id.in_(plans),
            or_(PlanGrant.period_end > now, PlanGrant.days_banked > 0),
        )
        counts["plan_codes"] = await _count(
            db,
            RedeemCode,
            RedeemCode.plan_id.in_(plans),
            RedeemCode.disabled_at.is_(None),
            or_(RedeemCode.redeem_by.is_(None), RedeemCode.redeem_by >= now),
            RedeemCode.uses < RedeemCode.max_uses,
        )
        counts["plan_checkouts"] = await _count(
            db,
            Checkout,
            Checkout.plan_id.in_(plans),
            Checkout.completed_at.is_(None),
            Checkout.created_at > open_since,
        )
    if packs:
        counts["pack_checkouts"] = await _count(
            db,
            PackCheckout,
            PackCheckout.pack_id.in_(packs),
            PackCheckout.completed_at.is_(None),
            PackCheckout.created_at > open_since,
        )
    if (
        after.free_credits_per_period < before.free_credits_per_period
        or after.free_storage_bytes < before.free_storage_bytes
        or after.free_max_jobs < before.free_max_jobs
    ):
        counts["free_accounts"] = await _count(db, User, free_accounts)
    counts = {kind: count for kind, count in counts.items() if count}
    if not counts:
        return None
    return {"removed": {"models": models, "plans": plans, "packs": packs}, "counts": counts}


async def change(
    db: AsyncSession,
    base: Settings,
    operator: Operator,
    key: str,
    value: str | None,
    *,
    confirm: bool,
    free_accounts: Callable[[Settings], ColumnElement[bool]],
) -> bool:
    """Store `value` for `key`, or restore the key to `.env` when `value` is None, with its
    record, in the caller's transaction. False, and nothing written, when it changes
    nothing. 422 when the settings it makes fail the start checks — in the checks' words,
    never the settings' — except a restore while the stored values already fail them, and
    409 `impact` when it takes away what accounts have and
    `confirm` is not set. `free_accounts` says which accounts are on Free under the
    settings it is given: it is asked with what is in effect once the rows are read under
    the lock, never with a copy taken before."""
    if key not in OVERRIDABLE:
        raise ApiError(422, "invalid_request", f"key: one of {', '.join(OVERRIDABLE)}")
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(sql("SELECT pg_advisory_xact_lock(:key)"), {"key": SETTINGS_LOCK_KEY})
    rows = {
        row.key: row
        for row in (
            await db.execute(select(RuntimeSetting).where(RuntimeSetting.key.in_(OVERRIDABLE)))
        ).scalars()
    }
    row = rows.get(key)
    if row is None and value is None:
        return False
    if row is not None and row.value == value:
        return False
    stored = {name: setting.value for name, setting in rows.items()}
    proposed = dict(stored)
    if value is None:
        del proposed[key]
    else:
        proposed[key] = value
    # What is in effect now: the stored values, or `.env` alone while they do not fit it.
    try:
        before = effective(base, stored)
        ignored = False
    except ValidationError:
        before = base
        ignored = True
    try:
        after = effective(base, proposed)
    except ValidationError as exc:
        # A restore while the stored values are ignored only moves toward `.env`, which
        # passed the start checks: never refused, it leaves `.env` alone in effect until
        # the rest fit too (docs/admin.md § Settings, *When the database's values no
        # longer fit*). A new value is checked with the rest as always.
        if value is not None or not ignored:
            raise ApiError(422, "invalid_request", validation_message(exc)) from None
        after = base
    taken = await impact(db, before, after, free_accounts(before))
    if taken is not None and not confirm:
        raise ApiError(
            409,
            "admin_conflict",
            "This change takes away what accounts have; confirm it to apply it",
            details={"reason": "impact", "impact": taken},
        )

    if row is None:
        was = {"value": env_text(base, key), "source": "env"}
    else:
        was = {"value": row.value, "source": "database"}
    if value is None:
        await db.delete(row)
        action = "settings.reset"
        now = {"value": env_text(base, key), "source": "env"}
    else:
        if row is None:
            db.add(RuntimeSetting(key=key, value=value, updated_by=operator.user.id))
        else:
            row.value = value
            row.updated_by = operator.user.id
            row.updated_at = utcnow()
        action = "settings.update"
        now = {"value": value, "source": "database"}
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="settings.write",
        action=action,
        target_type="setting",
        target_id=key,
        before=was,
        after=now,
        ip=operator.ip,
    )
    return True
