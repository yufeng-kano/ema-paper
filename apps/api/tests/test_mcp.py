"""The MCP endpoint (docs/mcp.md), driven by the official SDK client over ASGI.

One tool, `parse`, whose whole job is minting an upload ticket and formatting the curl
commands that carry it. What those commands then do to `/v1` is tested in
`test_upload_tickets.py`. `mcp_running` stands in for the app lifespan, which
`ASGITransport` never runs.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx2
import pytest
from httpx import AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from sightread.auth.upload_tickets import TICKET_PREFIX
from sightread.db.models import UploadTicket, User, utcnow
from tests.conftest import mcp_running
from tests.test_parse_end_to_end import _authorize

BASE_URL = "https://testserver"


@pytest.fixture
async def api_client(make_client, sessionmaker) -> AsyncClient:
    """A client holding an API key and a connected Kano Proxy, exactly like REST tests."""
    return await _authorize(make_client(), sessionmaker)


@asynccontextmanager
async def _mcp_session(client: AsyncClient, token: str | None = None):
    """An initialized MCP session against the app under test."""
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


def _payload(result) -> dict:
    """The tool's JSON payload, as a client would read it off the content block."""
    return json.loads(result.content[0].text)


async def _mint(session: ClientSession) -> dict:
    """Call the tool and read the ticket it hands back."""
    result = await session.call_tool("parse", {})
    assert result.is_error is False, result.content
    return _payload(result)


async def test_initialize_and_list_tools(api_client: AsyncClient) -> None:
    async with mcp_running(api_client), _mcp_session(api_client) as session:
        listing = await session.list_tools()

    tools = {tool.name: tool for tool in listing.tools}
    assert set(tools) == {"parse"}
    assert "yxyx_norm1000" in tools["parse"].description
    assert "sightread://" in tools["parse"].description
    assert not tools["parse"].input_schema.get("required")


async def test_an_unauthenticated_call_points_at_the_protected_resource_metadata(
    client: AsyncClient,
) -> None:
    async with mcp_running(client):
        response = await client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )

    assert response.status_code == 401
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    # The pointer a Claude connector follows to find the authorization server (RFC 9728).
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource"'
        in challenge
    )
    assert response.json()["error"]["type"] == "auth"


async def test_parse_mints_a_ticket_and_ready_to_run_commands(api_client, sessionmaker) -> None:
    async with mcp_running(api_client), _mcp_session(api_client) as session:
        payload = await _mint(session)
    token = payload["token"]
    base = api_client.app.state.settings.app_url

    assert token.startswith(TICKET_PREFIX)
    assert payload["max_upload_bytes"] == api_client.app.state.settings.upload_max_bytes
    assert payload["page_cap"] == api_client.app.state.settings.page_cap
    assert payload["expires_at"].endswith("Z")

    assert payload["upload"] == (
        f"f=doc.pdf; curl -sN -H 'Authorization: Bearer {token}' "
        f"-H 'Accept: text/event-stream' -F file=@\"$f\" {base}/v1/parse -o progress.sse "
        f"&& curl -s -H 'Authorization: Bearer {token}' {base}/v1/jobs/last/result.md "
        f'-o "${{f%.*}}.md"'
    )
    assert payload["markdown"] == (
        f"f=doc.pdf; curl -s -H 'Authorization: Bearer {token}' {base}/v1/jobs/last/result.md "
        f'-o "${{f%.*}}.md"'
    )
    assert payload["metadata"] == (
        f"f=doc.pdf; curl -s -H 'Authorization: Bearer {token}' {base}/v1/jobs/last/result "
        f'-o "${{f%.*}}.meta.json"'
    )
    assert payload["status"] == (f"curl -s -H 'Authorization: Bearer {token}' {base}/v1/jobs/last")
    # The notes are the agent's whole manual: form fields, both input kinds, markers,
    # recovery.
    for fragment in (
        "model=",
        "pages=1-5,8",
        "force=true",
        "jpg/png/webp/heic",
        "<!-- page: N -->",
        "NEVER print",
        "sightread://pPAGE/YMIN,XMIN,YMAX,XMAX",
    ):
        assert fragment in payload["notes"]
    assert "cached result comes back instantly while the parse is kept" in payload["notes"]
    # The parse is deleted after the retention: the landed file is what lasts.
    assert "kept 30 days after it finishes and then deleted" in payload["notes"]
    # Profiles are gone; an agent told to send one would get a silently ignored field.
    assert "profile" not in payload["notes"]

    # Stored hashed, never in plaintext, and unspent until an upload uses it.
    async with sessionmaker() as db:
        rows = (await db.execute(select(UploadTicket))).scalars().all()
    assert len(rows) == 1
    assert rows[0].prefix == f"{TICKET_PREFIX}...{token[-4:]}"
    assert token not in rows[0].token_hash
    assert (rows[0].spent_at, rows[0].job_id) == (None, None)


async def test_every_call_mints_a_fresh_ticket(api_client: AsyncClient) -> None:
    async with mcp_running(api_client), _mcp_session(api_client) as session:
        assert (await _mint(session))["token"] != (await _mint(session))["token"]


async def test_the_mint_rate_limit_trips_at_the_configured_count(make_client, sessionmaker) -> None:
    client = await _authorize(make_client(upload_ticket_rate_per_hour=2), sessionmaker)

    async with mcp_running(client), _mcp_session(client) as session:
        await _mint(session)
        await _mint(session)
        refused = await session.call_tool("parse", {})

    assert refused.is_error is True
    assert "retry later" in refused.content[0].text


async def test_minting_cleans_up_this_users_expired_tickets(api_client, sessionmaker) -> None:
    async with sessionmaker() as db:
        user = (await db.execute(select(User))).scalars().one()
        db.add(
            UploadTicket(
                user_id=user.id,
                token_hash="dead" * 16,
                prefix=f"{TICKET_PREFIX}...dead",
                created_at=utcnow() - timedelta(hours=2),
                expires_at=utcnow() - timedelta(hours=1),
            )
        )
        await db.commit()

    async with mcp_running(api_client), _mcp_session(api_client) as session:
        await _mint(session)

    async with sessionmaker() as db:
        prefixes = {row.prefix for row in (await db.execute(select(UploadTicket))).scalars()}
    assert f"{TICKET_PREFIX}...dead" not in prefixes
    assert len(prefixes) == 1


async def test_an_invalid_token_never_reaches_a_tool(api_client: AsyncClient) -> None:
    async with mcp_running(api_client):
        response = await api_client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer sr_not-a-real-key",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]
