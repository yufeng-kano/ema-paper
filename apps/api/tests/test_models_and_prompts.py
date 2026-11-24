"""The models the deployment offers and the agents' model settings: the operator's key on
every upstream call and never in a response, everything stubbed (docs/testing.md § Cost
safety, docs/billing.md § Models)."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.db.models import CreditEntry, Job
from sightread.errors import ApiError
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
from sightread.upstream.client import fetch_models
from tests.conftest import CSRF_HEADERS, KANO_API_KEY, KANO_BASE, KANO_MODELS_URL, PARSE_MODEL

FALLBACK = "vendor/fallback-model"

# What Kano Proxy's catalog lists: two of the three offered chat models, and more.
MODELS_PAYLOAD = {
    "data": [
        {"id": "vendor/chat-model", "name": "Chat"},
        {"id": "grok/grok-4.5", "name": "Grok 4.5"},
        {"id": "claude-code/claude-opus-5-5", "owned_by": "claude-code"},
    ]
}


def _models_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=MODELS_PAYLOAD)


async def _settings_of(client: AsyncClient, agent: str) -> dict:
    agents = (await client.get("/api/agents")).json()["agents"]
    return next(entry for entry in agents if entry["name"] == agent)


# --- the models -----------------------------------------------------------------------


async def test_the_parse_and_check_models_are_the_deployments(signed_in, make_client) -> None:
    """Configuration, answered as it is, no catalog fetched (docs/api.md)."""
    parse = await signed_in.get("/api/agents/parse/models")
    assert parse.json() == {"data": [{"id": PARSE_MODEL, "name": None}]}
    assert (await signed_in.get("/api/agents/check/models")).json() == parse.json()

    client = make_client(parse_fallback_model=FALLBACK, check_model="vendor/chosen")
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    assert [m["id"] for m in (await client.get("/api/agents/parse/models")).json()["data"]] == [
        PARSE_MODEL,
        FALLBACK,
    ]
    assert [m["id"] for m in (await client.get("/api/agents/check/models")).json()["data"]] == [
        "vendor/chosen"
    ]
    agents = {a["name"]: a for a in (await client.get("/api/agents")).json()["agents"]}
    assert agents["parse"]["settings"]["model"] == PARSE_MODEL
    assert agents["parse"]["settings"]["fallback_model"] == FALLBACK
    assert agents["check"]["settings"]["model"] == "vendor/chosen"
    assert agents["check"]["settings"]["fallback_model"] is None

    none = make_client(parse_model="")
    assert (await none.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    for agent in ("parse", "check"):
        refused = await none.get(f"/api/agents/{agent}/models")
        assert refused.status_code == 503, agent
        assert refused.json()["error"]["type"] == "upstream"
    assert (await _settings_of(none, "parse"))["settings"]["model"] is None


async def test_v1_models_takes_a_bearer_never_the_session_cookie(signed_in) -> None:
    """The data plane is bearer-only (docs/api.md): a signed-in browser lists the parse
    models with GET /api/agents/parse/models instead."""
    refused = await signed_in.get("/v1/models")

    assert refused.status_code == 401
    assert refused.json()["error"]["message"] == "Missing bearer token"
    assert refused.headers["WWW-Authenticate"] == "Bearer"

    key = await signed_in.post("/api/keys", json={"name": "script"}, headers=CSRF_HEADERS)
    listed = await signed_in.get(
        "/v1/models", headers={"Authorization": f"Bearer {key.json()['key']}"}
    )

    assert listed.status_code == 200, listed.text
    assert listed.json() == {"data": [{"id": PARSE_MODEL, "name": None}]}


@pytest.mark.own_catalog
@respx.mock
async def test_the_chat_models_are_the_offered_ones_the_catalog_still_lists(signed_in) -> None:
    """The priced models in the price table's order, each with the catalog's name, fetched with
    the operator's key — which no response carries (docs/api.md)."""
    route = respx.get(KANO_MODELS_URL).mock(side_effect=_models_ok)

    models = await signed_in.get("/api/agents/chat/models")

    assert models.status_code == 200, models.text
    assert models.json()["data"] == [
        {"id": "claude-code/claude-opus-5-5", "name": None},
        {"id": "vendor/chat-model", "name": "Chat"},
    ]
    assert route.calls[-1].request.headers["authorization"] == f"Bearer {KANO_API_KEY}"
    assert KANO_API_KEY not in models.text
    assert KANO_API_KEY not in (await signed_in.get("/api/agents")).text


@pytest.mark.own_catalog
@respx.mock
async def test_an_unreadable_catalog_is_an_upstream_error(signed_in, make_client) -> None:
    for response in (
        httpx.Response(401),
        httpx.Response(500),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"not json", headers={"content-type": "application/json"}),
    ):
        respx.get(KANO_MODELS_URL).mock(return_value=response)
        refused = await signed_in.get("/api/agents/chat/models")
        assert refused.status_code == 502, response.status_code
        assert refused.json()["error"]["type"] == "upstream"
        assert KANO_API_KEY not in refused.text
    respx.get(KANO_MODELS_URL).mock(side_effect=httpx.ConnectError("down"))
    assert (await signed_in.get("/api/agents/chat/models")).status_code == 502

    client = make_client(kano_proxy_api_key="")
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    assert (await client.get("/api/agents/chat/models")).status_code == 503


@respx.mock
async def test_an_oversized_model_list_is_an_upstream_error() -> None:
    """A catalog must not be able to exhaust memory with an unbounded body
    (docs/parsing.md § Upstream usage)."""
    respx.get(KANO_MODELS_URL).mock(
        return_value=httpx.Response(
            200, content=b"x" * 1024, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(ApiError) as raised:
        await fetch_models(f"{KANO_BASE}/openai/v1", KANO_API_KEY, max_bytes=64)
    assert raised.value.status_code == 502


# --- the model settings ---------------------------------------------------------------


async def test_only_the_chat_agent_takes_a_model_and_only_an_offered_one(signed_in) -> None:
    chat = (await _settings_of(signed_in, "chat"))["settings"]
    # The default, which only the live list can name: the panel shows its first.
    assert chat["model"] is None

    picked = await signed_in.put(
        "/api/agents/chat/settings", json={"model": " vendor/chat-model "}, headers=CSRF_HEADERS
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["model"] == "vendor/chat-model"
    assert (await _settings_of(signed_in, "chat"))["settings"]["model"] == "vendor/chat-model"

    refused = await signed_in.put(
        "/api/agents/chat/settings", json={"model": "grok/grok-4.5"}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == "That model is not one the chat agent offers"

    restored = await signed_in.put(
        "/api/agents/chat/settings", json={"model": None}, headers=CSRF_HEADERS
    )
    assert restored.json()["model"] is None

    for agent in ("parse", "check"):
        refused = await signed_in.put(
            f"/api/agents/{agent}/settings", json={"model": PARSE_MODEL}, headers=CSRF_HEADERS
        )
        assert refused.status_code == 400, agent
        assert refused.json()["error"]["message"] == f"The {agent} agent takes no model"


async def test_unknown_agents_are_refused(signed_in) -> None:
    put = await signed_in.put(
        "/api/agents/summarize/settings", json={"model": "x"}, headers=CSRF_HEADERS
    )
    assert put.status_code == 404


# --- the parse model at enqueue ---------------------------------------------------------


async def _keyed(client: AsyncClient) -> None:
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    key = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {key.json()['key']}"


async def _parse(client: AsyncClient, documents, **data) -> httpx.Response:
    return await client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
        data=data,
    )


async def test_a_request_model_must_be_one_of_the_parse_agents(
    make_client, sessionmaker, documents
) -> None:
    """The deployment's parse model by default; the fallback by name; anything else
    refused before a job exists (docs/agents.md § Enqueue rules)."""
    client = make_client(parse_fallback_model=FALLBACK)
    await _keyed(client)

    default = await _parse(client, documents)
    named = await _parse(client, documents, model=FALLBACK)
    assert (default.status_code, named.status_code) == (202, 202), (default.text, named.text)
    refused = await _parse(client, documents, model="grok/grok-4.5")
    assert refused.status_code == 400
    assert refused.json()["error"]["message"] == "That model is not one the parse agent runs"

    async with sessionmaker() as db:
        first = await db.get(Job, uuid.UUID(default.json()["job_id"]))
        second = await db.get(Job, uuid.UUID(named.json()["job_id"]))
        assert (first.model, first.connection_base_url) == (PARSE_MODEL, KANO_BASE)
        assert second.model == FALLBACK
        assert sorted((await db.execute(select(Job.id))).scalars()) == sorted([first.id, second.id])


async def test_no_parse_model_or_no_kano_proxy_refuses_every_parse(
    make_client, sessionmaker, documents
) -> None:
    for overrides in ({"parse_model": ""}, {"kano_proxy_api_key": ""}, {"kano_proxy_base_url": ""}):
        client = make_client(**overrides)
        await _keyed(client)
        refused = await _parse(client, documents)
        assert refused.status_code == 503, overrides
        assert refused.json()["error"]["type"] == "upstream"
    async with sessionmaker() as db:
        assert (await db.execute(select(Job.id))).all() == []


@respx.mock
async def test_a_job_with_no_endpoint_snapshot_never_runs_on_kano_proxy(
    make_client, sessionmaker, documents
) -> None:
    """A job of an upstream the deployment no longer has — OpenRouter, a provider
    connection — has no endpoint snapshot. The worker fails it when it takes it, a retry's
    included, through the finish that releases its credits, and Kano Proxy is never
    called: it would transcribe only the pages the other upstream failed, under a Kano
    Proxy dedup key (docs/database.md § Rules)."""
    gone = "this parse ran on an upstream this deployment no longer has; upload the file again"
    client = make_client()
    await _keyed(client)
    accepted = await _parse(client, documents)
    assert accepted.status_code == 202, accepted.text
    job_id = uuid.UUID(accepted.json()["job_id"])
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        job.connection_base_url = None
        await db.commit()

    settings = client.app.state.settings
    for attempt in ("queued", "retried"):
        async with sessionmaker() as db:
            assert await claim_next_job(db, settings.max_jobs_per_user) == job_id, attempt
        await run_job(sessionmaker, settings, job_id, asyncio.Semaphore(2))

        status = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert (status["status"], status["error"]) == ("failed", gone), attempt
        async with sessionmaker() as db:
            job = await db.get(Job, job_id)
            assert job.connection_base_url is None, attempt
            entry = await db.execute(select(CreditEntry.state).where(CreditEntry.job_id == job_id))
            assert entry.scalar_one() == "released", attempt
        if attempt == "queued":
            retried = await _parse(client, documents, retry="true")
            assert retried.status_code == 202, retried.text
            assert retried.json()["job_id"] == str(job_id)
    assert respx.calls.call_count == 0


@respx.mock
async def test_a_queued_job_reconciles_its_endpoint_snapshot_at_claim_time(
    make_client, sessionmaker, documents
) -> None:
    """If KANO_PROXY_BASE_URL changes while a job sits queued, the worker runs against the
    new origin and rewrites the job's snapshot so the dedup cache is keyed by the origin
    that actually produced the result (docs/jobs.md § Dedup)."""
    moved_base = "https://moved.test"
    moved_chat = respx.post(f"{moved_base}/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Page"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    client = make_client()
    await _keyed(client)
    accepted = await _parse(client, documents)
    assert accepted.status_code == 202, accepted.text
    job_id = uuid.UUID(accepted.json()["job_id"])

    # The worker restarted on the moved origin.
    worker = make_client(kano_proxy_base_url=moved_base)
    settings = worker.app.state.settings
    async with sessionmaker() as db:
        assert await claim_next_job(db, settings.max_jobs_per_user) == job_id
    await run_job(sessionmaker, settings, job_id, asyncio.Semaphore(2))

    assert moved_chat.called
    assert moved_chat.calls[0].request.headers["authorization"] == f"Bearer {KANO_API_KEY}"
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        assert (job.status, job.connection_base_url) == ("succeeded", moved_base)
