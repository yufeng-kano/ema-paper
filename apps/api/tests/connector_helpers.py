"""What the connector tests share: an MCP session over ASGI with a tool's payload, and the
OAuth flow a Claude connector runs to a token pair (docs/mcp.md, docs/auth.md § 4). No
helper reaches the network."""

from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from httpx import AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import CSRF_HEADERS

BASE_URL = "https://testserver"

REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "verifier-that-is-long-enough-for-pkce-1234567890"
CHALLENGE = create_s256_code_challenge(VERIFIER)
STATE = "opaque-client-state"

CONSENT_TOKEN_RE = re.compile(r"name='consent_token' value='([^']+)'")


@asynccontextmanager
async def mcp_session(client: AsyncClient, token: str | None = None):
    """An initialized MCP session against the app under test, on `token` or else the
    client's own bearer."""
    bearer = token if token is not None else client.headers["Authorization"].removeprefix("Bearer ")
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=client.app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {bearer}"},
        ) as http,
        streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def tool_payload(result) -> dict:
    """A tool's JSON payload, as a client would read it off the content block."""
    return json.loads(result.content[0].text)


async def register_client(client: AsyncClient, redirect_uri: str = REDIRECT_URI) -> httpx.Response:
    return await client.post(
        "/oauth/register",
        json={"client_name": "Claude", "redirect_uris": [redirect_uri]},
    )


async def authorize_code(client: AsyncClient, client_id: str) -> str:
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


async def token_request(client: AsyncClient, **form) -> httpx.Response:
    return await client.post("/oauth/token", data=form)


async def oauth_connected(client: AsyncClient) -> tuple[str, dict]:
    """A signed-in browser all the way to a token pair; returns (client_id, token body)."""
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    client_id = (await register_client(client)).json()["client_id"]
    code = await authorize_code(client, client_id)
    tokens = await token_request(
        client,
        grant_type="authorization_code",
        code=code,
        code_verifier=VERIFIER,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
    )
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()
