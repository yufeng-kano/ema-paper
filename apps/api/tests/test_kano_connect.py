"""The Kano Proxy connect flow (docs/auth.md § 6), with Kano Proxy stubbed by respx.

Connect mints a single-use state bound to the signed-in user and sends the browser to
Kano Proxy's consent page; the callback consumes the state, exchanges the code with PKCE
and the client secret, and stores the key encrypted. Every outcome is a redirect back to
the settings page with a non-secret `?kano=` status.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.auth.crypto import (
    KANO_CONNECT_VERIFIER_CONTEXT,
    decrypt_kano_proxy_key,
    decrypt_secret,
    hash_token,
)
from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import KanoConnectState, KanoProxyKey, User
from sightread.upstream.kano import code_challenge
from tests.conftest import (
    CSRF_HEADERS,
    KANO_BASE,
    KANO_CLIENT_ID,
    KANO_CLIENT_SECRET,
    KANO_MODELS_URL,
    KANO_TOKEN_URL,
    TEST_SECRET_KEY,
)

REDIRECT_URI = "http://localhost:8000/api/kano/callback"
AGENTS_URL = "http://localhost:3000/settings/agent"
KEY = "sk-kano-proxy-first-key-0001"
NEW_KEY = "sk-kano-proxy-second-key-0002"


def _issued(key: str = KEY) -> httpx.Response:
    return httpx.Response(200, json={"access_token": key, "token_type": "bearer"})


async def _signed_in(client: AsyncClient) -> AsyncClient:
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    return client


async def _start(client: AsyncClient, **params) -> dict[str, str]:
    """Begin a connect and return the consent URL's query."""
    response = await client.get("/api/kano/connect", params=params)
    assert response.status_code == 302, response.text
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{KANO_BASE}/connect/authorize"
    )
    return {name: values[0] for name, values in parse_qs(location.query).items()}


async def _finish(client: AsyncClient, **params) -> str:
    response = await client.get("/api/kano/callback", params=params)
    assert response.status_code == 302, response.text
    return response.headers["location"]


async def _stored_keys(sessionmaker) -> list[KanoProxyKey]:
    async with sessionmaker() as db:
        return list((await db.execute(select(KanoProxyKey))).scalars())


async def test_connect_sends_the_browser_to_consent_with_pkce(client, sessionmaker) -> None:
    await _signed_in(client)

    query = await _start(client)

    assert query["response_type"] == "code"
    assert query["client_id"] == KANO_CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["code_challenge_method"] == "S256"
    async with sessionmaker() as db:
        row = (await db.execute(select(KanoConnectState))).scalar_one()
    # Only a hash of the state is stored; the verifier is encrypted, and the challenge
    # sent is its unpadded base64url SHA-256.
    assert row.state_hash == hash_token(query["state"])
    assert query["state"].encode() not in row.verifier_ciphertext
    verifier = decrypt_secret(
        TEST_SECRET_KEY, row.verifier_ciphertext, KANO_CONNECT_VERIFIER_CONTEXT
    )
    assert query["code_challenge"] == code_challenge(verifier)
    assert "=" not in query["code_challenge"]
    assert (row.expires_at - row.created_at).total_seconds() == 600


async def test_connect_needs_a_web_session(client) -> None:
    assert (await client.get("/api/kano/connect")).status_code == 401


@respx.mock
async def test_callback_exchanges_the_code_and_stores_the_key(client, sessionmaker) -> None:
    token = respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    await _signed_in(client)
    query = await _start(client)
    async with sessionmaker() as db:
        row = (await db.execute(select(KanoConnectState))).scalar_one()
        verifier = decrypt_secret(
            TEST_SECRET_KEY, row.verifier_ciphertext, KANO_CONNECT_VERIFIER_CONTEXT
        )

    location = await _finish(client, code="the-code", state=query["state"])

    assert location == f"{AGENTS_URL}?kano=connected"
    sent = parse_qs(token.calls[0].request.content.decode())
    assert {name: values[0] for name, values in sent.items()} == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": REDIRECT_URI,
        "client_id": KANO_CLIENT_ID,
        "client_secret": KANO_CLIENT_SECRET,
        "code_verifier": verifier,
    }
    [stored] = await _stored_keys(sessionmaker)
    assert KEY.encode() not in stored.ciphertext
    assert decrypt_kano_proxy_key(TEST_SECRET_KEY, stored.ciphertext) == KEY
    state = (await client.get("/api/kano")).json()
    assert state["available"] is True
    assert state["connected"] is True
    # Masked only: the full key never comes back.
    assert state["masked"] != KEY
    assert KEY not in (await client.get("/api/kano")).text


@respx.mock
async def test_a_state_works_once(client, sessionmaker) -> None:
    token = respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    await _signed_in(client)
    query = await _start(client)
    assert await _finish(client, code="c", state=query["state"]) == f"{AGENTS_URL}?kano=connected"

    replay = await _finish(client, code="c", state=query["state"])

    assert replay == f"{AGENTS_URL}?kano=error"
    assert token.call_count == 1


@respx.mock
async def test_a_state_bound_to_another_user_is_rejected(make_client, sessionmaker) -> None:
    """A state is not a bearer: a callback in someone else's session cannot use it, and
    the owner's pending connect survives the attempt."""
    token = respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    owner = await _signed_in(make_client())
    query = await _start(owner)

    stranger = make_client()
    async with sessionmaker() as db:
        other = User(google_sub="stranger", email="stranger@example.com")
        db.add(other)
        await db.flush()
        session_token = await create_session(db, other)
        await db.commit()
    stranger.cookies.set(SESSION_COOKIE, session_token)

    assert await _finish(stranger, code="c", state=query["state"]) == (f"{AGENTS_URL}?kano=error")
    assert not token.called
    assert await _stored_keys(sessionmaker) == []

    # Still the owner's to finish.
    assert await _finish(owner, code="c", state=query["state"]) == (f"{AGENTS_URL}?kano=connected")
    [stored] = await _stored_keys(sessionmaker)
    assert stored.user_id != other.id


@respx.mock
async def test_an_expired_state_is_rejected(make_client, sessionmaker) -> None:
    token = respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    client = await _signed_in(make_client(kano_connect_ttl_seconds=0))
    query = await _start(client)

    assert await _finish(client, code="c", state=query["state"]) == f"{AGENTS_URL}?kano=error"
    assert not token.called


@respx.mock
async def test_a_callback_without_a_session_stores_nothing(make_client, sessionmaker) -> None:
    token = respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    query = await _start(await _signed_in(make_client()))

    anonymous = make_client()
    assert await _finish(anonymous, code="c", state=query["state"]) == (f"{AGENTS_URL}?kano=error")
    assert not token.called


@respx.mock
async def test_denied_consent_consumes_the_state(client, sessionmaker) -> None:
    token = respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    await _signed_in(client)
    query = await _start(client)

    denied = await _finish(client, error="access_denied", state=query["state"])

    assert denied == f"{AGENTS_URL}?kano=denied"
    assert not token.called
    async with sessionmaker() as db:
        assert (await db.execute(select(KanoConnectState))).scalars().all() == []


@respx.mock
async def test_reconnecting_replaces_the_stored_key(client, sessionmaker) -> None:
    respx.post(KANO_TOKEN_URL).mock(side_effect=[_issued(KEY), _issued(NEW_KEY)])
    models = respx.get(KANO_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "vendor/model"}]})
    )
    await _signed_in(client)
    first = await _start(client)
    await _finish(client, code="c1", state=first["state"])
    masked_before = (await client.get("/api/kano")).json()["masked"]

    second = await _start(client)
    assert await _finish(client, code="c2", state=second["state"]) == (
        f"{AGENTS_URL}?kano=connected"
    )

    [stored] = await _stored_keys(sessionmaker)
    assert decrypt_kano_proxy_key(TEST_SECRET_KEY, stored.ciphertext) == NEW_KEY
    assert (await client.get("/api/kano")).json()["masked"] != masked_before
    # The model picker now lists Kano Proxy's catalog with the new key.
    listed = await client.get("/api/agents/parse/models")
    assert listed.json()["data"] == [{"id": "vendor/model", "name": None}]
    assert models.calls[0].request.headers["authorization"] == f"Bearer {NEW_KEY}"


@respx.mock
async def test_a_rejected_exchange_stores_nothing(client, sessionmaker) -> None:
    respx.post(KANO_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": "invalid_grant"}),
            httpx.Response(401, json={"error": "invalid_client"}),
            # A 200 that is not a bearer key is not a key.
            httpx.Response(200, json={"access_token": KEY, "token_type": "mac"}),
            httpx.Response(200, json={"token_type": "bearer"}),
            httpx.Response(200, content=b"not json"),
        ]
    )
    await _signed_in(client)

    for _ in range(5):
        query = await _start(client)
        assert await _finish(client, code="c", state=query["state"]) == (f"{AGENTS_URL}?kano=error")

    assert await _stored_keys(sessionmaker) == []
    assert (await client.get("/api/kano")).json()["connected"] is False


@respx.mock
async def test_an_unreachable_kano_proxy_is_an_error_outcome(client, sessionmaker) -> None:
    respx.post(KANO_TOKEN_URL).mock(side_effect=httpx.ConnectError("down"))
    await _signed_in(client)
    query = await _start(client)

    assert await _finish(client, code="c", state=query["state"]) == f"{AGENTS_URL}?kano=error"


@respx.mock
async def test_the_callback_returns_to_the_locale_the_connect_started_in(client) -> None:
    respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    await _signed_in(client)
    query = await _start(client, locale="zh-TW")

    assert await _finish(client, code="c", state=query["state"]) == (
        "http://localhost:3000/zh-TW/settings/agent?kano=connected"
    )


@respx.mock
async def test_disconnect_forgets_the_key_here(client, sessionmaker) -> None:
    respx.post(KANO_TOKEN_URL).mock(return_value=_issued())
    await _signed_in(client)
    query = await _start(client)
    await _finish(client, code="c", state=query["state"])

    response = await client.delete("/api/kano", headers=CSRF_HEADERS)

    assert response.status_code == 204
    assert await _stored_keys(sessionmaker) == []
    assert (await client.get("/api/kano")).json() == {
        "available": True,
        "connected": False,
        "masked": None,
    }


async def test_an_unconfigured_deployment_reports_kano_proxy_unavailable(make_client) -> None:
    client = await _signed_in(make_client(kano_proxy_base_url=""))

    assert (await client.get("/api/kano")).json()["available"] is False
    response = await client.get("/api/kano/connect")
    assert response.status_code == 302
    assert response.headers["location"] == f"{AGENTS_URL}?kano=error"
