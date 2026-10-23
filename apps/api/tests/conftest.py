"""Shared test fixtures.

Database: set `TEST_DATABASE_URL` (or `DATABASE_URL`) to run the suite against a real
PostgreSQL — e.g. `docker compose up -d pg` then

    TEST_DATABASE_URL=postgresql+asyncpg://sightread:sightread@127.0.0.1:5432/sightread \
        uv run pytest

Without it the suite falls back to a throwaway SQLite file so `uv run pytest` works with
no services and no network. The models carry SQLite variants for the two PostgreSQL-only
types (JSONB and timestamptz) precisely so this fallback stays honest.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from sightread.config import Settings
from sightread.db.models import Base
from sightread.db.session import create_sessionmaker
from sightread.main import create_app
from sightread.mcp import mcp_session_manager
from tests.fixtures.documents import (
    build_corrupt_pdf,
    build_image,
    build_mixed_pdf,
    build_rotated_jpeg,
    build_scanned_pdf,
    build_table_pdf,
    build_text_layer_pdf,
    build_two_column_pdf,
)

TEST_SECRET_KEY = "test-secret-key-not-a-real-one"
CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}

# A Kano Proxy that only exists inside respx: no test ever reaches a real one
# (docs/testing.md § Cost safety).
KANO_BASE = "https://kano.test"
KANO_CHAT_URL = f"{KANO_BASE}/openai/v1/chat/completions"
KANO_MODELS_URL = f"{KANO_BASE}/openai/v1/models"
# The operator's key, and what the deployment offers (docs/billing.md § Models).
KANO_API_KEY = "sk-kano-proxy-test-not-a-real-one"
PARSE_MODEL = "vendor/vision-model"
CHAT_MODELS = "claude-code/claude-opus-5-5,codex/gpt-5.5,vendor/chat-model"

DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

# USD per 1M tokens, every kind alike, for the models the tests chat on.
TEST_MODEL_PRICES = json.dumps(
    {
        model: {"input": 1, "cache_read": 1, "cache_write": 1, "output": 1}
        for model in (
            "claude-code/claude-opus-5-5",
            "claude-code/claude-opus-5",
            "claude-code/claude-fallback-9",
            "claude-code/claude-haiku-5-5",
            "claude-opus-5-5",
            "codex/gpt-5.5",
            "vendor/chat-model",
            "vendor/chosen",
            "vendor/fallback-model",
            "vendor/vision-model",
            "m",
        )
    }
)


@pytest_asyncio.fixture
async def sessionmaker(tmp_path):
    url = DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield create_sessionmaker(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def make_client(sessionmaker, tmp_path) -> AsyncIterator[Callable[..., AsyncClient]]:
    """Build a client for an app with the given settings overrides.

    The app's sessionmaker is injected directly, so no lifespan and no live database are
    needed. `https://` base URL so the `Secure` session cookie is stored by httpx.
    """
    opened: list[AsyncClient] = []

    def _make(**overrides) -> AsyncClient:
        settings = Settings(
            **{
                "app_env": "local",
                "auth_dev_mode": True,
                "secret_key": TEST_SECRET_KEY,
                "database_url": "sqlite+aiosqlite://",
                "upload_dir": str(tmp_path / "uploads"),
                "figures_dir": str(tmp_path / "figures"),
                "kano_proxy_base_url": KANO_BASE,
                "kano_proxy_api_key": KANO_API_KEY,
                "parse_model": PARSE_MODEL,
                "chat_models": CHAT_MODELS,
                # Every model a test turn runs on is priced, as the deployment requires
                # of its offered models (docs/billing.md § Models): a flat list price so
                # the credits a test sees are predictable. Tests of the arithmetic set
                # their own.
                "model_prices_json": TEST_MODEL_PRICES,
                **overrides,
            }
        )
        app = create_app(settings)
        app.state.sessionmaker = sessionmaker
        client = AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver")
        # Tests that drive the worker in-process need the same settings the app used.
        client.app = app
        opened.append(client)
        return client

    yield _make
    for client in opened:
        await client.aclose()


@pytest.fixture
def client(make_client) -> AsyncClient:
    return make_client()


@pytest.fixture(scope="session")
def documents(tmp_path_factory) -> dict[str, Path]:
    """Every fixture document, built once per session (tests/fixtures/documents.py)."""
    directory = tmp_path_factory.mktemp("documents")
    built = {
        "text_pdf": build_text_layer_pdf(directory / "text.pdf"),
        "scanned_pdf": build_scanned_pdf(directory / "scanned.pdf"),
        "two_column_pdf": build_two_column_pdf(directory / "two-column.pdf"),
        "table_pdf": build_table_pdf(directory / "table.pdf"),
        "mixed_pdf": build_mixed_pdf(directory / "mixed.pdf"),
        "corrupt_pdf": build_corrupt_pdf(directory / "corrupt.pdf"),
        "jpg": build_image(directory / "tiny.jpg", "JPEG"),
        "png": build_image(directory / "tiny.png", "PNG"),
        "webp": build_image(directory / "tiny.webp", "WEBP"),
        "rotated_jpg": build_rotated_jpeg(directory / "rotated.jpg"),
        "wide_png": build_image(directory / "wide.png", "PNG", size=(3000, 1000)),
    }
    # Some pillow-heif wheels ship a decoder but no encoder; the HEIC test skips then.
    with contextlib.suppress(OSError, ValueError, KeyError):
        built["heic"] = build_image(directory / "tiny.heic", "HEIF")
    return built


@pytest_asyncio.fixture
async def signed_in(client: AsyncClient) -> AsyncClient:
    response = await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    assert response.status_code == 200
    return client


def mcp_running(client: AsyncClient):
    """Run the MCP session manager around a block of requests.

    The app starts it in its lifespan, which `ASGITransport` never runs, so a test that
    touches `/mcp` enters it explicitly.
    """
    return mcp_session_manager(client.app)


# A Kano Pay that only exists inside respx (docs/billing.md § Kano Pay).
PAY_URL = "https://pay.test"
PAY_APP_ID = "ema-paper-test"
PAY_WEBHOOK_SECRET = "pay-webhook-secret-not-a-real-one"
PRO_PLANS_JSON = json.dumps(
    [
        {
            "planId": "pro",
            "credits": 5000,
            "storageBytes": 1_073_741_824,
            "sandbox": True,
            "maxJobs": 2,
        }
    ]
)
PAY_SETTINGS = {
    "kano_pay_url": PAY_URL,
    "kano_pay_app_id": PAY_APP_ID,
    "kano_pay_api_key": "pay-api-key-not-a-real-one",
    "kano_pay_webhook_secret": PAY_WEBHOOK_SECRET,
    "kano_pay_plans_json": PRO_PLANS_JSON,
}


async def grant_plan(sessionmaker, user_id: int, plan_id: str = "pro", *, version: int = 1):
    """A subscription row as the webhook would write it: the user is on `plan_id` for a
    period around now (docs/billing.md § Periods)."""
    from datetime import UTC, datetime, timedelta

    from sightread.db.models import Subscription

    now = datetime.now(UTC)
    async with sessionmaker() as db:
        db.add(
            Subscription(
                user_id=user_id,
                subscription_id=f"sub_{user_id}",
                status="active",
                plan_id=plan_id,
                period_start=now - timedelta(days=1),
                period_end=now + timedelta(days=29),
                cancel_at_period_end=False,
                version=version,
            )
        )
        await db.commit()
