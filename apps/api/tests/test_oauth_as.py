"""The OAuth 2.1 authorization server (docs/auth.md § 4).

The whole connector path with no network at all: register a client, consent in the browser
session, exchange the code, then use the token where a project API key would go.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from httpx import AsyncClient
from sqlalchemy import select

from sightread.auth.crypto import hash_token
from sightread.db.models import OAuthGrant, utcnow
from tests.conftest import CSRF_HEADERS, mcp_running

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "verifier-that-is-long-enough-for-pkce-1234567890"
CHALLENGE = create_s256_code_challenge(VERIFIER)
STATE = "opaque-client-state"

CONSENT_TOKEN_RE = re.compile(r"name='consent_token' value='([^']+)'")


async def _register(client: AsyncClient, redirect_uri: str = REDIRECT_URI) -> httpx.Response:
    return await client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": [redirect_uri]},
    )


async def _authorize(client: AsyncClient, client_id: str) -> str:
    """Run the browser half of the flow and return the issued authorization code."""
    consent = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": STATE,
        },
    )
    assert consent.status_code == 200, consent.text
    match = CONSENT_TOKEN_RE.search(consent.text)
    assert match is not None

    granted = await client.post(
        "/oauth/authorize",
        data={
            "consent_token": match.group(1),
            "decision": "approve",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "state": STATE,
        },
    )
    assert granted.status_code == 302
    location = urlparse(granted.headers["location"])
    query = parse_qs(location.query)
    assert query["state"] == [STATE]
    return query["code"][0]


async def _token(client: AsyncClient, **form) -> httpx.Response:
    return await client.post("/oauth/token", data=form)


async def _connected(client: AsyncClient) -> tuple[str, dict]:
    """A signed-in browser all the way to a token pair; returns (client_id, token body)."""
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    client_id = (await _register(client)).json()["client_id"]
    code = await _authorize(client, client_id)
    tokens = await _token(
        client,
        grant_type="authorization_code",
        code=code,
        code_verifier=VERIFIER,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
    )
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


async def test_discovery_documents_point_at_this_deployment(client: AsyncClient) -> None:
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == "http://localhost:8000"
    assert metadata["authorization_endpoint"] == "http://localhost:8000/oauth/authorize"
    assert metadata["token_endpoint"] == "http://localhost:8000/oauth/token"
    assert metadata["registration_endpoint"] == "http://localhost:8000/oauth/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["grant_types_supported"] == ["authorization_code", "refresh_token"]

    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ):
        resource = (await client.get(path)).json()
        assert resource["resource"] == "http://localhost:8000/mcp"
        assert resource["authorization_servers"] == ["http://localhost:8000"]


async def test_the_connector_flow_ends_in_a_working_bearer(client: AsyncClient) -> None:
    _, tokens = await _connected(client)

    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == "parse"

    # Same rights as a project API key: the data plane accepts either (docs/auth.md).
    # An unknown job is a 404 to an authenticated caller and a 401 to anyone else.
    job = await client.get(
        f"/v1/jobs/{uuid.uuid4()}", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert job.status_code == 404

    async with mcp_running(client):
        mcp = await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert mcp.status_code == 200


async def test_refresh_rotates_and_burns_the_old_token(client: AsyncClient) -> None:
    client_id, tokens = await _connected(client)

    refreshed = await _token(
        client,
        grant_type="refresh_token",
        refresh_token=tokens["refresh_token"],
        client_id=client_id,
    )
    assert refreshed.status_code == 200
    rotated = refreshed.json()
    assert rotated["refresh_token"] != tokens["refresh_token"]
    assert rotated["access_token"] != tokens["access_token"]

    replayed = await _token(
        client,
        grant_type="refresh_token",
        refresh_token=tokens["refresh_token"],
        client_id=client_id,
    )
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"


async def test_registration_refuses_a_redirect_uri_that_is_not_https(client: AsyncClient) -> None:
    for uri in ("http://evil.example.com/callback", "not-a-url", ""):
        response = await _register(client, uri)
        assert response.status_code == 400, uri
        assert response.json()["error"] == "invalid_redirect_uri"

    # Local deployments may point a connector at a loopback listener.
    assert (await _register(client, "http://localhost:6274/callback")).status_code == 201


async def test_authorize_refuses_an_unregistered_redirect_uri(client: AsyncClient) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client_id = (await _register(client)).json()["client_id"]

    response = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://attacker.example.com/callback",
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    # Never a redirect: an unverified redirect URI must not receive anything.
    assert response.status_code == 400
    assert "location" not in response.headers


async def test_a_wrong_pkce_verifier_is_refused(client: AsyncClient) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client_id = (await _register(client)).json()["client_id"]
    code = await _authorize(client, client_id)

    wrong = await _token(
        client,
        grant_type="authorization_code",
        code=code,
        code_verifier="a-different-verifier-that-is-long-enough-1234",
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"

    # The code survived the failed attempt, and the right verifier still works.
    right = await _token(
        client,
        grant_type="authorization_code",
        code=code,
        code_verifier=VERIFIER,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
    )
    assert right.status_code == 200


async def test_a_code_cannot_be_exchanged_twice(client: AsyncClient) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client_id = (await _register(client)).json()["client_id"]
    code = await _authorize(client, client_id)
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": VERIFIER,
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
    }

    assert (await _token(client, **form)).status_code == 200
    replayed = await _token(client, **form)
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"


@pytest.mark.parametrize("kill", ["expire", "revoke"])
async def test_a_dead_access_token_is_a_401(client: AsyncClient, sessionmaker, kill: str) -> None:
    _, tokens = await _connected(client)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    probe = f"/v1/jobs/{uuid.uuid4()}"
    assert (await client.get(probe, headers=headers)).status_code == 404

    async with sessionmaker() as db:
        grant = (
            await db.execute(
                select(OAuthGrant).where(
                    OAuthGrant.token_hash == hash_token(tokens["access_token"])
                )
            )
        ).scalar_one()
        if kill == "expire":
            grant.expires_at = utcnow() - timedelta(seconds=1)
        else:
            grant.revoked_at = utcnow()
        await db.commit()

    assert (await client.get(probe, headers=headers)).status_code == 401


async def test_consent_needs_the_form_token_from_this_session(client: AsyncClient) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client_id = (await _register(client)).json()["client_id"]

    forged = await client.post(
        "/oauth/authorize",
        data={
            "consent_token": "guessed",
            "decision": "approve",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
        },
    )
    assert forged.status_code == 400
    assert "location" not in forged.headers


async def test_denying_consent_redirects_with_access_denied(client: AsyncClient) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client_id = (await _register(client)).json()["client_id"]
    consent = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": STATE,
        },
    )
    token = CONSENT_TOKEN_RE.search(consent.text).group(1)

    denied = await client.post(
        "/oauth/authorize",
        data={
            "consent_token": token,
            "decision": "deny",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "state": STATE,
        },
    )
    assert denied.status_code == 302
    assert parse_qs(urlparse(denied.headers["location"]).query)["error"] == ["access_denied"]


async def test_authorize_without_a_session_asks_for_sign_in(make_client) -> None:
    local = make_client()
    client_id = (await _register(local)).json()["client_id"]
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
    }

    # No Google credentials configured: the page has to say how to sign in instead.
    unconfigured = await local.get("/oauth/authorize", params=params)
    assert unconfigured.status_code == 401
    assert "dev login" in unconfigured.text

    google = make_client(google_client_id="id", google_client_secret="secret")
    client_id = (await _register(google)).json()["client_id"]
    redirected = await google.get("/oauth/authorize", params={**params, "client_id": client_id})
    assert redirected.status_code == 302
    assert redirected.headers["location"] == "/api/auth/login"
