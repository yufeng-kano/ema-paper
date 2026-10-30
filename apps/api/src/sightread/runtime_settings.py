"""The settings in effect: `.env`'s, with the database's overrides on top
(docs/admin.md § Settings).

The API and the worker start from `.env` alone and check it as they always have; the rows
of `runtime_settings` are applied after start, through `RuntimeSettings`, one reading layer
the API, the worker and the CLI share. A key without a row is `.env`'s, and with no row at
all the settings in effect are the very object the process started with.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .db.models import RuntimeSetting

logger = logging.getLogger(__name__)

# The keys the database may override, as `.env` names them, in the settings page's order,
# each with the field it sets. Everything else is `.env`'s alone: every secret and
# connection, every path, every other `SANDBOX_*`, the ceilings every plan must fit under
# (docs/admin.md § Settings).
OVERRIDABLE: dict[str, str] = {
    "PARSE_MODEL": "parse_model",
    "CHECK_MODEL": "check_model",
    "PARSE_FALLBACK_MODEL": "parse_fallback_model",
    "MODEL_PRICES_JSON": "model_prices_json",
    "CREDIT_USD": "credit_usd",
    "PARSE_CREDITS_PER_PAGE": "parse_credits_per_page",
    "CHAT_MIN_CREDITS": "chat_min_credits",
    "FREE_CREDITS_PER_PERIOD": "free_credits_per_period",
    "FREE_STORAGE_BYTES": "free_storage_bytes",
    "FREE_MAX_JOBS": "free_max_jobs",
    "SANDBOX_EGRESS_HOSTS": "sandbox_egress_hosts",
    "KANO_PAY_PLANS_JSON": "kano_pay_plans_json",
    "KANO_PAY_PACKS_JSON": "kano_pay_packs_json",
}


def effective(base: Settings, overrides: dict[str, str]) -> Settings:
    """`base` itself when nothing overrides it; otherwise new settings — `base`'s values
    with `overrides` (keys of `OVERRIDABLE`, the text `.env` would hold) on top — checked
    whole by the checks the API and the worker run at start.

    Built with `model_validate`, which reads no environment, and never by copying `base`:
    its parsed prices, plans and packs are cached on it and would outlive the change.
    Raises `ValidationError`, whose string form holds every setting, secrets included:
    only `validation_message` of it may be shown or logged."""
    if not overrides:
        return base
    mapped = {OVERRIDABLE[key]: value for key, value in overrides.items()}
    return Settings.model_validate({**base.model_dump(), **mapped})


def validation_message(exc: ValidationError) -> str:
    """What the failed checks say, from their messages alone — never the settings they
    checked: `CHAT_MIN_CREDITS: Input should be a valid integer, …` for a field, the
    check's own words for a rule across fields."""
    parts = []
    for error in exc.errors(include_input=False, include_url=False):
        message = error["msg"].removeprefix("Value error, ")
        if error["loc"]:
            parts.append(f"{str(error['loc'][0]).upper()}: {message}")
        else:
            parts.append(message)
    return "; ".join(parts)


class RuntimeSettings:
    """The settings in effect, read from the database at most once every
    `RUNTIME_SETTINGS_CACHE_SECONDS` and kept in memory between reads.

    `base` and `sessionmaker` are asked for at each read, not at construction: the API's
    sessionmaker exists only once its lifespan starts, and tests replace both. One read
    runs at a time; whoever asks meanwhile waits for its answer. A request passes its own
    session, connected before it waits for the lock, and the read runs on it: the requests
    waiting meanwhile each hold a connection of the pool, so a read that asked the pool for
    one under the lock could wait out its timeout with all of them. The worker and the CLI
    pass none and read on a session of their own. It never raises: a database it cannot
    read keeps the settings last read (`.env`'s before any), and rows that no longer fit
    `.env` leave `.env` alone in effect, with the checks' message in `ignored` for the
    settings page."""

    def __init__(
        self,
        base: Callable[[], Settings],
        sessionmaker: Callable[[], async_sessionmaker[AsyncSession]],
    ) -> None:
        self._base = base
        self._sessionmaker = sessionmaker
        self._lock = asyncio.Lock()
        self._built_on: Settings | None = None
        self._value: Settings | None = None
        self._expires = 0.0
        # Moved on by every `invalidate()`: a read that began before it keeps nothing.
        self._generation = 0
        self._read_failed = False
        self.ignored: str | None = None

    async def current(self, db: AsyncSession | None = None) -> Settings:
        base = self._base()
        if self._built_on is base and time.monotonic() < self._expires:
            return self._value
        if db is not None:
            # A database it cannot connect to is the read's to report, and leaves the
            # session usable.
            with contextlib.suppress(Exception):
                await db.connection()
        async with self._lock:
            base = self._base()
            if self._built_on is not base or time.monotonic() >= self._expires:
                await self._read(base, db)
            return self._value

    def invalidate(self) -> None:
        """Read the rows again at the next `current()`: a change was just written. A read
        under way may have missed it, so what it answers is not kept."""
        self._expires = 0.0
        self._generation += 1

    async def _read(self, base: Settings, db: AsyncSession | None) -> None:
        generation = self._generation
        statement = select(RuntimeSetting.key, RuntimeSetting.value).where(
            RuntimeSetting.key.in_(OVERRIDABLE)
        )
        try:
            if db is not None:
                # In a savepoint: a failed read is undone alone, leaving the request's
                # transaction usable — PostgreSQL refuses every later statement of one a
                # statement failed in — and what it loaded before, such as the signed-in
                # account, unexpired.
                async with db.begin_nested():
                    rows = (await db.execute(statement)).all()
            else:
                async with self._sessionmaker()() as own:
                    rows = (await own.execute(statement)).all()
        except Exception:  # noqa: BLE001 - a settings read never fails a request
            if not self._read_failed:
                logger.warning(
                    "could not read the runtime settings; keeping the settings last read",
                    exc_info=True,
                )
                self._read_failed = True
            if self._built_on is not base:
                self._built_on, self._value = base, base
        else:
            if self._read_failed:
                logger.info("the runtime settings are readable again")
                self._read_failed = False
            try:
                value = effective(base, dict(rows))
            except ValidationError as exc:
                message = validation_message(exc)
                if message != self.ignored:
                    logger.error(
                        "the database's settings no longer fit .env, which alone is in effect: %s",
                        message,
                    )
                self.ignored = message
                value = base
            else:
                self.ignored = None
            self._built_on, self._value = base, value
        # Kept from the end of the read, never its start: whoever asks while it runs waits
        # for its answer rather than take the copy it replaces, and a read cut short (its
        # request cancelled) keeps nothing. Not kept when a change was written meanwhile.
        if generation == self._generation:
            self._expires = time.monotonic() + base.runtime_settings_cache_seconds
