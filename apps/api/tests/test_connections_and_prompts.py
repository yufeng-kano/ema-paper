"""Provider connections, prompt presets and agent settings: the control plane and parses
through a custom OpenAI-compatible endpoint, everything stubbed (docs/testing.md § Cost
safety)."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.db.models import Job, UsageLog
from sightread.errors import ApiError
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
from sightread.upstream.client import normalize_base_url
from tests.conftest import CSRF_HEADERS

BASE = "https://proxy.example/openai/v1"
MODELS_URL = f"{BASE}/models"
CHAT = f"{BASE}/chat/completions"

MODELS_PAYLOAD = {
    "data": [
        {"id": "claude-code/claude-opus-5", "owned_by": "claude-code"},
        {"id": "grok/grok-4.5", "name": "Grok 4.5"},
    ]
}


def _models_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=MODELS_PAYLOAD)


async def _create_connection(client: AsyncClient, name: str = "custom", **extra) -> dict:
    response = await client.post(
        "/api/connections",
        json={"name": name, "base_url": BASE, "api_key": "sk-custom-secret", **extra},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _run_parse_on(
    client: AsyncClient, connection_id: int | None, model: str | None = "grok/grok-4.5"
) -> dict:
    """Point the parse agent at an upstream and a model, as the settings page does."""
    response = await client.put(
        "/api/agents/parse/settings",
        json={"connection_id": connection_id, "model": model},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _parse_settings(client: AsyncClient) -> dict:
    agents = (await client.get("/api/agents")).json()["agents"]
    return next(agent for agent in agents if agent["name"] == "parse")["settings"]


# --- connections CRUD -----------------------------------------------------------------


@respx.mock
async def test_create_connection_validates_and_never_returns_the_key(signed_in) -> None:
    route = respx.get(MODELS_URL).mock(side_effect=_models_ok)

    created = await _create_connection(signed_in)

    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-custom-secret"
    assert created["name"] == "custom"
    assert created["base_url"] == BASE
    # The model is each agent's choice, not the connection's (docs/agents.md).
    assert "model" not in created
    assert "sk-custom-secret" not in json.dumps(created)
    assert created["masked"].endswith("cret")
    # Reported cost is opt-in (docs/parsing.md § Upstream usage).
    assert created["report_cost"] is False

    listed = (await signed_in.get("/api/connections")).json()["connections"]
    assert [row["id"] for row in listed] == [created["id"]]
    assert "sk-custom-secret" not in json.dumps(listed)


@respx.mock
async def test_a_rejected_key_stores_nothing(signed_in) -> None:
    respx.get(MODELS_URL).mock(return_value=httpx.Response(401))

    response = await signed_in.post(
        "/api/connections",
        json={
            "name": "custom",
            "base_url": BASE,
            "api_key": "sk-custom-wrong",
        },
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"
    assert (await signed_in.get("/api/connections")).json()["connections"] == []


@respx.mock
async def test_an_unreachable_endpoint_is_upstream_not_a_bad_key(signed_in) -> None:
    respx.get(MODELS_URL).mock(side_effect=httpx.ConnectError("boom"))

    response = await signed_in.post(
        "/api/connections",
        json={
            "name": "custom",
            "base_url": BASE,
            "api_key": "sk-custom-secret",
        },
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream"


@respx.mock
async def test_a_non_object_model_list_is_an_upstream_error(signed_in) -> None:
    """An endpoint answering `[]` (valid JSON, wrong shape) is its fault, not a 500."""
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json=[]))

    response = await signed_in.post(
        "/api/connections",
        json={
            "name": "custom",
            "base_url": BASE,
            "api_key": "sk-custom-secret",
        },
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream"


@respx.mock
async def test_a_lost_duplicate_name_race_is_a_400_not_a_500(signed_in, monkeypatch) -> None:
    """Two concurrent creates can both pass the pre-check; the loser's unique-constraint
    violation must surface as the same 400 name conflict, not a 500."""
    from sightread.routes import control

    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    await _create_connection(signed_in)

    async def race_passed_precheck(*args, **kwargs):
        return None

    monkeypatch.setattr(control, "_refuse_duplicate_connection_name", race_passed_precheck)
    duplicate = await signed_in.post(
        "/api/connections",
        json={
            "name": "custom",
            "base_url": BASE,
            "api_key": "sk-custom-secret",
        },
        headers=CSRF_HEADERS,
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["type"] == "invalid_request"


@respx.mock
async def test_duplicate_connection_names_are_refused(signed_in) -> None:
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    await _create_connection(signed_in)

    duplicate = await signed_in.post(
        "/api/connections",
        json={
            "name": "custom",
            "base_url": BASE,
            "api_key": "sk-custom-secret",
        },
        headers=CSRF_HEADERS,
    )
    assert duplicate.status_code == 400


@respx.mock
async def test_preview_models_with_a_candidate_key(signed_in) -> None:
    """The create dialog's catalog: no connection exists yet, the typed key is used."""
    route = respx.get(MODELS_URL).mock(side_effect=_models_ok)

    models = await signed_in.post(
        "/api/connections/preview-models",
        json={"base_url": BASE, "api_key": "sk-custom-secret"},
        headers=CSRF_HEADERS,
    )

    assert models.status_code == 200
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-custom-secret"
    assert models.json()["data"] == [
        {"id": "claude-code/claude-opus-5", "name": None},
        {"id": "grok/grok-4.5", "name": "Grok 4.5"},
    ]


@respx.mock
async def test_preview_models_uses_the_stored_key_when_none_is_typed(signed_in) -> None:
    """The edit dialog's catalog: `connection_id` reuses the stored key, never re-typed."""
    route = respx.get(MODELS_URL).mock(side_effect=_models_ok)
    created = await _create_connection(signed_in)

    models = await signed_in.post(
        "/api/connections/preview-models",
        json={"base_url": BASE, "connection_id": created["id"]},
        headers=CSRF_HEADERS,
    )

    assert models.status_code == 200
    assert route.calls[-1].request.headers["authorization"] == "Bearer sk-custom-secret"
    assert [entry["id"] for entry in models.json()["data"]] == [
        "claude-code/claude-opus-5",
        "grok/grok-4.5",
    ]


async def test_preview_models_needs_a_key_or_a_connection(signed_in) -> None:
    response = await signed_in.post(
        "/api/connections/preview-models",
        json={"base_url": BASE},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


@respx.mock
async def test_update_revalidates_a_moved_endpoint_with_the_stored_key(signed_in) -> None:
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    created = await _create_connection(signed_in)

    moved = respx.get("https://moved.example/v1/models").mock(side_effect=_models_ok)
    response = await signed_in.put(
        f"/api/connections/{created['id']}",
        json={"base_url": "https://moved.example/v1/"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["base_url"] == "https://moved.example/v1"
    assert moved.calls[0].request.headers["authorization"] == "Bearer sk-custom-secret"


# --- agent settings ---------------------------------------------------------------------


@respx.mock
async def test_switching_the_upstream_clears_a_model_from_the_other_catalog(signed_in) -> None:
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    created = await _create_connection(signed_in)
    await _run_parse_on(signed_in, None, "kano/some-model")

    switched = await signed_in.put(
        "/api/agents/parse/settings",
        json={"connection_id": created["id"]},
        headers=CSRF_HEADERS,
    )

    assert switched.json() == {
        "connection_id": created["id"],
        "model": None,
        "prompt_preset_id": None,
        "prompt": None,
        "effort": "medium",
        # The chat agent's own settings, at their defaults: one shape for every agent.
        "instructions": None,
        "answer_language": None,
        "translate_language": None,
        "memory_auto": True,
        "memory_paused": False,
        # The parse agent's own: no fallback, and none taken on its own.
        "fallback_model": None,
        "auto_fallback": False,
        # The check agent's own: Files uploads are checked.
        "auto_check": True,
        "bbox": {"family": "default", "format": "yxyx_norm1000"},
    }
    # Sent together, the model is kept: it was picked from the new upstream's catalog.
    both = await _run_parse_on(signed_in, None, "kano/other-model")
    assert both["model"] == "kano/other-model"


@respx.mock
async def test_the_model_picker_lists_the_saved_upstreams_live_catalog(signed_in) -> None:
    route = respx.get(MODELS_URL).mock(side_effect=_models_ok)
    created = await _create_connection(signed_in)
    await _run_parse_on(signed_in, created["id"], None)

    models = await signed_in.get("/api/agents/parse/models")

    assert models.status_code == 200
    # No capability filtering and no cache: what the endpoint lists, fetched now.
    assert [entry["id"] for entry in models.json()["data"]] == [
        "claude-code/claude-opus-5",
        "grok/grok-4.5",
    ]
    assert route.calls[-1].request.headers["authorization"] == "Bearer sk-custom-secret"


async def test_unknown_agents_are_refused(signed_in) -> None:
    put = await signed_in.put(
        "/api/agents/summarize/settings", json={"model": "x"}, headers=CSRF_HEADERS
    )
    assert put.status_code == 404
    prompt = await signed_in.post(
        "/api/prompts",
        json={"agent": "summarize", "name": "x", "text": "y"},
        headers=CSRF_HEADERS,
    )
    assert prompt.status_code == 400


@respx.mock
async def test_deleting_the_selected_connection_falls_back_to_kano_proxy(signed_in) -> None:
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    created = await _create_connection(signed_in)
    await _run_parse_on(signed_in, created["id"])

    assert (
        await signed_in.delete(f"/api/connections/{created['id']}", headers=CSRF_HEADERS)
    ).status_code == 204

    settings = await _parse_settings(signed_in)
    # The model belonged to the deleted endpoint's catalog, so it goes too.
    assert settings["connection_id"] is None
    assert settings["model"] is None


def _fake_getaddrinfo(host: str, *args, **kwargs):
    """DNS stub: the test suite never resolves anything for real (docs/testing.md)."""
    resolved = {
        # 203.0.113.x (TEST-NET) would itself fail is_global — use a real global address.
        "proxy.example": "93.184.216.34",
        "ip6-localhost": "::1",
        "internal.example": "10.0.0.8",
    }
    if host not in resolved:
        raise OSError(f"unresolvable in tests: {host}")
    return [(0, 0, 0, "", (resolved[host], 0))]


async def test_base_url_rules_outside_local(monkeypatch) -> None:
    from sightread.upstream import client

    monkeypatch.setattr(client, "_getaddrinfo", _fake_getaddrinfo)
    assert await normalize_base_url(f" {BASE}/ ", "production") == BASE
    for bad in (
        "http://proxy.example/openai/v1",  # https only
        "https://localhost/v1",
        "https://127.0.0.1/v1",
        "https://10.0.0.8/v1",
        "https://[::1]/v1",
        # Non-canonical spellings the resolver accepts must be judged by the address
        # they denote, not their text (shortened / decimal / hex / octal IPv4).
        "https://127.1/v1",
        "https://2130706433/v1",
        "https://0x7f000001/v1",
        "https://017700000001/v1",
        "https://169.254.1.1/v1",
        "https://proxy.example/v1?x=1",
        "ftp://proxy.example/v1",
        "not a url",
    ):
        with pytest.raises(ApiError):
            await normalize_base_url(bad, "production")
    # Local development may point at a local endpoint over plain http.
    assert await normalize_base_url("http://127.0.0.1:8787/openai/v1", "local") is not None


async def test_base_url_hostnames_are_resolved_and_checked(monkeypatch) -> None:
    """A name whose DNS (or /etc/hosts) answer is non-global is refused at save time —
    `ip6-localhost` → ::1 and an attacker domain → 10.x both count (docs/auth.md § 3)."""
    from sightread.upstream import client

    monkeypatch.setattr(client, "_getaddrinfo", _fake_getaddrinfo)
    for bad in (
        "https://ip6-localhost/v1",
        "https://internal.example/v1",
        "https://does-not-resolve.example/v1",
    ):
        with pytest.raises(ApiError):
            await normalize_base_url(bad, "production")
    # Local skips the resolution check entirely — no DNS side effects on save.
    assert await normalize_base_url("https://whatever.internal/v1", "local") is not None


async def test_a_malformed_host_or_port_is_a_400_not_a_500() -> None:
    for env in ("local", "production"):
        for bad in (
            "https://[bad/v1",
            "https://proxy.example:bad/v1",
            "https://proxy.example:99999/v1",
        ):
            with pytest.raises(ApiError) as raised:
                await normalize_base_url(bad, env)
            assert raised.value.status_code == 400


async def test_base_url_userinfo_is_refused_everywhere() -> None:
    """`base_url` is stored and displayed in plaintext, so credentials must not ride
    inside it — in any environment (docs/auth.md § 3)."""
    for env in ("local", "production"):
        with pytest.raises(ApiError):
            await normalize_base_url("https://user:password@proxy.example/v1", env)
        with pytest.raises(ApiError):
            await normalize_base_url("https://token@proxy.example/v1", env)


@respx.mock
async def test_an_oversized_model_list_is_an_upstream_error() -> None:
    """A user-controlled endpoint must not be able to exhaust memory with an unbounded
    body (docs/parsing.md § Upstream usage)."""
    from sightread.upstream.client import fetch_connection_models

    respx.get(MODELS_URL).mock(
        return_value=httpx.Response(
            200, content=b"x" * 1024, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(ApiError) as raised:
        await fetch_connection_models(BASE, "sk-custom-secret", max_bytes=64)
    assert raised.value.status_code == 502


# --- prompt presets -------------------------------------------------------------------


async def test_prompt_preset_crud_and_selection(signed_in) -> None:
    created = await signed_in.post(
        "/api/prompts",
        json={"name": "Tables only", "text": "Only tables from page {page}."},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201
    preset = created.json()

    listed = (await signed_in.get("/api/prompts")).json()["prompts"]
    assert [row["name"] for row in listed] == ["Tables only"]

    # Existing callers that never name an agent get the parse agent's prompts.
    assert preset["agent"] == "parse"

    selected = await signed_in.put(
        "/api/agents/parse/settings",
        json={"prompt_preset_id": preset["id"]},
        headers=CSRF_HEADERS,
    )
    assert selected.json()["prompt_preset_id"] == preset["id"]
    assert (await _parse_settings(signed_in))["prompt_preset_id"] == preset["id"]

    renamed = await signed_in.put(
        f"/api/prompts/{preset['id']}", json={"name": "Tables"}, headers=CSRF_HEADERS
    )
    assert renamed.json()["name"] == "Tables"

    assert (
        await signed_in.delete(f"/api/prompts/{preset['id']}", headers=CSRF_HEADERS)
    ).status_code == 204
    # Deleting the selected preset falls back to the default prompt.
    assert (await _parse_settings(signed_in))["prompt_preset_id"] is None


async def test_prompt_text_is_capped(signed_in, make_client) -> None:
    client = make_client(system_prompt_max_chars=10)
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    response = await client.post(
        "/api/prompts",
        json={"name": "Long", "text": "x" * 11},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 400


async def test_a_foreign_preset_or_connection_cannot_be_selected(signed_in) -> None:
    for body in ({"prompt_preset_id": 999}, {"connection_id": 999}):
        response = await signed_in.put(
            "/api/agents/parse/settings", json=body, headers=CSRF_HEADERS
        )
        assert response.status_code == 404


# --- parse through a custom connection ------------------------------------------------


@respx.mock
async def test_parse_runs_on_the_selected_connection(make_client, sessionmaker, documents) -> None:
    """The whole path: settings pick a connection, the worker calls its endpoint, and the
    usage row records tokens with an unknown cost — the connection did not opt in to its
    reported one (docs/parsing.md § Upstream usage)."""
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    chat = respx.post(CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Page one"}}],
                "usage": {"prompt_tokens": 800, "completion_tokens": 90, "cost": 0.42},
            },
        )
    )

    client = make_client()
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    created = await _create_connection(client)
    await _run_parse_on(client, created["id"])
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {key.json()['key']}"

    accepted = await client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    settings = client.app.state.settings
    async with sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    assert str(claimed) == job_id
    await run_job(sessionmaker, settings, claimed, asyncio.Semaphore(2))

    status = (await client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "succeeded", status
    sent = json.loads(chat.calls[0].request.content)
    assert sent["model"] == "grok/grok-4.5"
    assert "usage" not in sent
    assert chat.calls[0].request.headers["authorization"] == "Bearer sk-custom-secret"

    async with sessionmaker() as db:
        usage = (
            (await db.execute(select(UsageLog).where(UsageLog.job_id == uuid.UUID(job_id))))
            .scalars()
            .all()
        )
    assert len(usage) == 1
    assert usage[0].prompt_tokens == 800
    assert usage[0].cost is None
    assert usage[0].cost_source == "unknown"


@respx.mock
async def test_report_cost_is_read_when_the_job_runs(make_client, sessionmaker, documents) -> None:
    """Turning the flag on while a job is queued applies to that job's calls, like a URL
    or key edit does (docs/database.md § Rules), and the estimate lands on the usage row."""
    models = respx.get(MODELS_URL).mock(side_effect=_models_ok)
    respx.post(CHAT).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Page one"}}],
                "usage": {"prompt_tokens": 800, "completion_tokens": 90, "cost": 0.0125},
            },
        )
    )

    client = make_client()
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    created = await _create_connection(client)
    await _run_parse_on(client, created["id"])
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    accepted = await client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
        headers={"Authorization": f"Bearer {key.json()['key']}"},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    validations = models.call_count
    toggled = await client.put(
        f"/api/connections/{created['id']}", json={"report_cost": True}, headers=CSRF_HEADERS
    )
    assert toggled.status_code == 200
    assert toggled.json()["report_cost"] is True
    # The flag changes what is recorded, not where calls go: nothing to re-validate.
    assert models.call_count == validations

    settings = client.app.state.settings
    async with sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    await run_job(sessionmaker, settings, claimed, asyncio.Semaphore(2))

    async with sessionmaker() as db:
        usage = (
            (await db.execute(select(UsageLog).where(UsageLog.job_id == uuid.UUID(job_id))))
            .scalars()
            .all()
        )
    assert [(row.cost_source, row.cost) for row in usage] == [("estimated", Decimal("0.0125"))]


@respx.mock
async def test_report_cost_can_be_set_at_creation_and_turned_off(signed_in) -> None:
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    created = await _create_connection(signed_in, report_cost=True)
    assert created["report_cost"] is True

    # A partial update that does not mention the flag leaves it alone.
    renamed = await signed_in.put(
        f"/api/connections/{created['id']}", json={"name": "custom 2"}, headers=CSRF_HEADERS
    )
    assert renamed.json()["report_cost"] is True

    off = await signed_in.put(
        f"/api/connections/{created['id']}", json={"report_cost": False}, headers=CSRF_HEADERS
    )
    assert off.json()["report_cost"] is False
    listed = (await signed_in.get("/api/connections")).json()["connections"]
    assert listed[0]["report_cost"] is False


@respx.mock
async def test_an_explicit_request_model_overrides_the_agents_model(
    make_client, sessionmaker, documents
) -> None:
    """The parse agent's stored model is the default, not a cage: a per-request `model`
    still wins, on the same upstream (docs/api.md § Upstreams)."""
    respx.get(MODELS_URL).mock(side_effect=_models_ok)

    client = make_client()
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    created = await _create_connection(client)
    await _run_parse_on(client, created["id"])
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {key.json()['key']}"

    accepted = await client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
        data={"model": "claude-code/claude-opus-5"},
    )
    assert accepted.status_code == 202, accepted.text

    async with sessionmaker() as db:
        job = (
            await db.execute(select(Job).where(Job.id == uuid.UUID(accepted.json()["job_id"])))
        ).scalar_one()
        assert job.model == "claude-code/claude-opus-5"
        assert job.connection_id == created["id"]


@respx.mock
async def test_deleting_a_connection_never_relabels_its_jobs_as_kano_proxy(
    make_client, sessionmaker, documents
) -> None:
    """Provider identity is immutable job history: after the connection is deleted, the
    job keeps its connection id (so it can never spend the Kano Proxy key or answer a
    Kano Proxy dedup lookup) and fails with a reason that names the real problem."""
    respx.get(MODELS_URL).mock(side_effect=_models_ok)

    client = make_client()
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    created = await _create_connection(client)
    await _run_parse_on(client, created["id"])
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {key.json()['key']}"

    accepted = await client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    assert (
        await client.delete(f"/api/connections/{created['id']}", headers=CSRF_HEADERS)
    ).status_code == 204

    async with sessionmaker() as db:
        job = (await db.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
        assert job.connection_id == created["id"]

    settings = client.app.state.settings
    async with sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    assert str(claimed) == job_id
    await run_job(sessionmaker, settings, claimed, asyncio.Semaphore(2))

    status = (await client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "failed"
    assert status["error"] == "the provider connection for this job no longer exists"


@respx.mock
async def test_a_queued_job_reconciles_its_endpoint_snapshot_at_claim_time(
    make_client, sessionmaker, documents
) -> None:
    """If the connection's URL changes while a job sits queued, the worker runs against
    the new endpoint and rewrites the job's snapshot so the dedup cache is keyed by the
    URL that actually produced the result (docs/jobs.md § Dedup)."""
    respx.get(MODELS_URL).mock(side_effect=_models_ok)
    moved_base = "https://moved.example/openai/v1"
    respx.get(f"{moved_base}/models").mock(side_effect=_models_ok)
    moved_chat = respx.post(f"{moved_base}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Page"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    client = make_client()
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    created = await _create_connection(client)
    await _run_parse_on(client, created["id"])
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {key.json()['key']}"

    accepted = await client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    moved = await client.put(
        f"/api/connections/{created['id']}",
        json={"base_url": moved_base},
        headers=CSRF_HEADERS,
    )
    assert moved.status_code == 200, moved.text

    settings = client.app.state.settings
    async with sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    assert str(claimed) == job_id
    await run_job(sessionmaker, settings, claimed, asyncio.Semaphore(2))

    assert moved_chat.called
    async with sessionmaker() as db:
        job = (await db.execute(select(Job).where(Job.id == uuid.UUID(job_id)))).scalar_one()
        assert job.status == "succeeded"
        assert job.connection_base_url == moved_base
