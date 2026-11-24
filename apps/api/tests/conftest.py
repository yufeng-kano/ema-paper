"""Shared test fixtures.

Database: set `TEST_DATABASE_URL` to run the suite against a real PostgreSQL. It is the
only variable read here (never the app's `DATABASE_URL`). A run drops and re-creates every
table in the database it names, once, and empties every table before each test, so it
must name a database of its own, never the app's. On the compose server, for example,
create one once:

    docker compose up -d pg
    docker compose exec pg createdb -U sightread sightread_test
    TEST_DATABASE_URL=postgresql+asyncpg://sightread:sightread@127.0.0.1:5432/sightread_test \
        uv run pytest

Without it the suite falls back to a throwaway SQLite file so `uv run pytest` works with
no services and no network. The models carry SQLite variants for the two PostgreSQL-only
types (JSONB and timestamptz) precisely so this fallback stays honest.

Settings: no test reads a setting from the caller's environment. Every variable that names
a `Settings` field is removed before each test, so an exported `.env` changes nothing;
`TEST_DATABASE_URL` is not one, and it is read once, at import.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient

from sightread.config import Settings, get_settings
from sightread.db.models import WORKER_GENERATION_SEQ, Base
from sightread.db.session import create_engine, create_sessionmaker
from sightread.jobs.chat_tools import ImageView
from sightread.main import create_app
from sightread.mcp import mcp_session_manager
from sightread.workspace.layout import user_root
from tests.fixtures.documents import (
    build_corrupt_pdf,
    build_image,
    build_pdf,
    build_rotated_jpeg,
    scanned_page,
    text_layer_page,
    two_column_page,
)

TEST_SECRET_KEY = "test-secret-key-not-a-real-one"
CSRF_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


async def convert_on_upload(client: AsyncClient) -> None:
    """Turn the parse agent's `auto_convert` on for the signed-in user. It is off by
    default (docs/agents.md), so a Files upload of a PDF or an image is kept as a file
    unless a test asks for the parse."""
    saved = await client.put(
        "/api/agents/parse/settings", json={"auto_convert": True}, headers=CSRF_HEADERS
    )
    assert saved.status_code == 200, saved.text


# A Kano Proxy that only exists inside respx: no test ever reaches a real one
# (docs/testing.md § Cost safety).
KANO_BASE = "https://kano.test"
KANO_CHAT_URL = f"{KANO_BASE}/openai/v1/chat/completions"
KANO_MODELS_URL = f"{KANO_BASE}/openai/v1/models"
# The operator's key, the parse model, and what the stub catalog lists: the chat agent
# offers the ones `TEST_MODEL_PRICES` prices, in its order (docs/billing.md § Models).
KANO_API_KEY = "sk-kano-proxy-test-not-a-real-one"
PARSE_MODEL = "vendor/vision-model"
CATALOG_MODELS = "claude-code/claude-opus-5-5,codex/gpt-5.5,vendor/chat-model"

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
POSTGRES = (TEST_DATABASE_URL or "").startswith("postgresql")
postgres_only = pytest.mark.skipif(not POSTGRES, reason="needs TEST_DATABASE_URL on PostgreSQL")

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


def path_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings for code driven without an app: its workspaces and figures under
    `tmp_path`, Kano Proxy at `KANO_BASE`."""
    return Settings(
        **{
            "app_env": "local",
            "secret_key": TEST_SECRET_KEY,
            "workspaces_dir": str(tmp_path / "workspaces"),
            "figures_dir": str(tmp_path / "figures"),
            "kano_proxy_base_url": KANO_BASE,
            **overrides,
        }
    )


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every `Settings` a test builds, bare `Settings()` and the cached `get_settings()`
    included, sees only its own arguments and the defaults. The names match without regard
    to case, as the settings read them."""
    fields = set(Settings.model_fields)
    for name in list(os.environ):
        if name.lower() in fields:
            monkeypatch.delenv(name)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _rebuild_schema(url: str, *, create: bool) -> None:
    engine = create_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        if create:
            await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture(scope="session")
def postgres_schema() -> Iterator[None]:
    """On PostgreSQL the schema is made once per run, from a clean database, and dropped at
    its end; `sessionmaker` empties it before each test."""
    if not POSTGRES:
        yield
        return
    asyncio.run(_rebuild_schema(TEST_DATABASE_URL, create=True))
    yield
    asyncio.run(_rebuild_schema(TEST_DATABASE_URL, create=False))


@pytest_asyncio.fixture
async def sessionmaker(postgres_schema, tmp_path):
    """A database no earlier test has written to. On PostgreSQL every table is truncated
    and every sequence restarted before the test, so ids count from 1 as on a fresh schema;
    on SQLite the schema is made in a new file under `tmp_path`."""
    # The app's own engine factory, so a PostgreSQL run has production's session settings.
    engine = create_engine(TEST_DATABASE_URL or f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with engine.begin() as connection:
        if POSTGRES:
            quote = engine.dialect.identifier_preparer.quote
            tables = ", ".join(quote(table.name) for table in Base.metadata.sorted_tables)
            await connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
            await connection.exec_driver_sql(
                f"ALTER SEQUENCE {quote(WORKER_GENERATION_SEQ.name)} RESTART"
            )
        else:
            await connection.run_sync(Base.metadata.create_all)
    yield create_sessionmaker(engine)
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
                "workspaces_dir": str(tmp_path / "workspaces"),
                "kano_proxy_base_url": KANO_BASE,
                "kano_proxy_api_key": KANO_API_KEY,
                "parse_model": PARSE_MODEL,
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


@pytest_asyncio.fixture
async def chat_client(make_client) -> AsyncClient:
    """Signed in, a chat model chosen, one document (tests/chat_helpers.py)."""
    # Imported here: chat_helpers imports this module.
    from tests.chat_helpers import chatting_client

    return await chatting_client(make_client())


@pytest.fixture(autouse=True)
def kano_catalog(request: pytest.FixtureRequest):
    """Kano Proxy's catalog, listing `CATALOG_MODELS`: the chat agent's models are read
    from it (docs/billing.md § Models). The router started
    first answers first, so a test that needs another catalog opts out with the
    `own_catalog` marker and stubs the URL itself; nothing else is mocked here, and an
    unstubbed request still fails (docs/testing.md)."""
    if request.node.get_closest_marker("own_catalog"):
        yield
        return
    router = respx.mock(assert_all_called=False)
    router.get(KANO_MODELS_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": model} for model in CATALOG_MODELS.split(",")]}
        )
    )
    with router:
        yield


@pytest.fixture(scope="session")
def documents(tmp_path_factory) -> dict[str, Path]:
    """Every fixture document, built once per session (tests/fixtures/documents.py)."""
    directory = tmp_path_factory.mktemp("documents")
    built = {
        "text_pdf": build_pdf(directory / "text.pdf", text_layer_page),
        "scanned_pdf": build_pdf(directory / "scanned.pdf", scanned_page),
        "mixed_pdf": build_pdf(
            directory / "mixed.pdf", text_layer_page, scanned_page, two_column_page
        ),
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


async def sign_in(client: AsyncClient) -> AsyncClient:
    """Sign `client` in as the dev user and note who that is: `client.user_id`, and
    `client.root`, the user's workspace."""
    response = await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    assert response.status_code == 200, response.text
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


@pytest_asyncio.fixture
async def signed_in(client: AsyncClient) -> AsyncClient:
    return await sign_in(client)


@pytest_asyncio.fixture
async def writer(make_client) -> AsyncClient:
    """A signed-in client of its own, apart from the `client` fixture."""
    return await sign_in(make_client())


async def api_key_client(client: AsyncClient) -> AsyncClient:
    """Sign `client` in as the dev user and send a fresh API key of theirs on every
    request; its session cookie stays, for the routes a key does not open."""
    response = await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    assert response.status_code == 200, response.text
    created = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {created.json()['key']}"
    return client


@pytest_asyncio.fixture
async def api_client(make_client) -> AsyncClient:
    return await api_key_client(make_client())


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


def image_view(*, max_bytes: int = 32 << 20, shell: bool = True) -> ImageView:
    """What the editor may show of an image, made fresh per call: its semaphore binds to
    the event loop of the test that first waits on it."""
    return ImageView(
        max_bytes=max_bytes,
        max_pixels=40_000_000,
        long_edge=1568,
        decode_slot=asyncio.Semaphore(1),
        shell=shell,
    )
