"""Upload tickets end to end (docs/auth.md § 5): one upload, then reads of that one job.

No upstream traffic here either — the worker runs against the stubbed Kano Proxy from
`test_parse_end_to_end` (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from sightread.auth import upload_tickets
from sightread.db.models import Document, Job, UploadTicket, User, utcnow
from sightread.jobs.sweeper import purge_parses
from tests.conftest import KANO_CHAT_URL, mcp_running
from tests.test_parse_end_to_end import MODEL, _authorize, _drain_queue, _vision_stub
from tests.test_retention import postgres_only

# The copy is part of the contract: it is the agent's only recovery hint (docs/auth.md § 5).
EXPECTED_REJECTION = (
    "Upload ticket expired or already spent — call the parse tool again for a fresh "
    "ticket; re-uploading the same file returns the cached result instantly."
)


@pytest.fixture
async def api_client(make_client, sessionmaker) -> AsyncClient:
    return await _authorize(make_client(), sessionmaker)


async def _mint(client: AsyncClient, sessionmaker, ttl_seconds: int | None = None) -> str:
    """A ticket for the client's user, minted straight through the shared module."""
    settings = client.app.state.settings
    if ttl_seconds is not None:
        settings = settings.model_copy(update={"upload_ticket_ttl_seconds": ttl_seconds})
    async with sessionmaker() as db:
        user = (await db.execute(select(User))).scalars().one()
        _, token = await upload_tickets.mint(db, settings, user)
    return token


async def _upload(client: AsyncClient, token: str, path: Path, **fields) -> httpx.Response:
    return await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"model": MODEL, **fields},
        headers={"Authorization": f"Bearer {token}"},
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _stored_ticket(sessionmaker) -> UploadTicket:
    async with sessionmaker() as db:
        return (await db.execute(select(UploadTicket))).scalars().one()


async def test_a_ticket_uploads_once_and_binds_to_the_job_it_created(
    api_client, sessionmaker, documents
) -> None:
    token = await _mint(api_client, sessionmaker)

    accepted = await _upload(api_client, token, documents["text_pdf"])

    assert accepted.status_code == 202, accepted.text
    ticket = await _stored_ticket(sessionmaker)
    assert str(ticket.job_id) == accepted.json()["job_id"]
    assert ticket.spent_at is not None


@respx.mock
async def test_last_resolves_a_spent_ticket_to_its_own_job(
    api_client, sessionmaker, documents
) -> None:
    """The `last` aliases behind the MCP curl commands (docs/api.md § GET /v1/jobs/last*)."""
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    token = await _mint(api_client, sessionmaker)
    job_id = (await _upload(api_client, token, documents["text_pdf"])).json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = await api_client.get("/v1/jobs/last", headers=_bearer(token))
    assert status.status_code == 200
    assert status.json() == {**status.json(), "job_id": job_id, "status": "succeeded"}

    result = await api_client.get("/v1/jobs/last/result", headers=_bearer(token))
    assert result.status_code == 200
    assert result.json()["meta"]["job_id"] == job_id

    markdown = await api_client.get("/v1/jobs/last/result.md", headers=_bearer(token))
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text.startswith("<!-- page: 1 -->\n")

    # A durable credential has its job id and gets told to use it.
    refused = await api_client.get("/v1/jobs/last")
    assert refused.status_code == 400


async def test_last_with_an_unspent_ticket_is_the_documented_401(api_client, sessionmaker) -> None:
    token = await _mint(api_client, sessionmaker)
    response = await api_client.get("/v1/jobs/last", headers=_bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["message"] == EXPECTED_REJECTION


async def test_a_spent_ticket_cannot_upload_again(api_client, sessionmaker, documents) -> None:
    token = await _mint(api_client, sessionmaker)
    assert (await _upload(api_client, token, documents["text_pdf"])).status_code == 202

    refused = await _upload(api_client, token, documents["mixed_pdf"])

    assert refused.status_code == 401
    assert refused.json()["error"]["type"] == "auth"
    assert refused.json()["error"]["message"] == EXPECTED_REJECTION
    assert refused.json()["error"]["message"] == upload_tickets.REJECTION_MESSAGE


@respx.mock
async def test_a_spent_ticket_reads_its_own_job_and_nothing_else(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    token = await _mint(api_client, sessionmaker)
    job_id = (await _upload(api_client, token, documents["text_pdf"])).json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = await api_client.get(f"/v1/jobs/{job_id}", headers=_bearer(token))
    result = await api_client.get(f"/v1/jobs/{job_id}/result", headers=_bearer(token))

    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert result.status_code == 200
    assert result.json()["markdown"]

    frames = []
    async with api_client.stream(
        "GET", f"/v1/jobs/{job_id}/events", headers=_bearer(token)
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            frames.append(line)
    assert "event: done" in "\n".join(frames)

    # A second job of the same user is off limits: the ticket is bound to one job.
    other = (
        await api_client.post(
            "/v1/parse",
            files={"file": ("mixed.pdf", documents["mixed_pdf"].read_bytes(), "application/pdf")},
            data={"model": MODEL},
        )
    ).json()["job_id"]
    stranger_read = await api_client.get(f"/v1/jobs/{other}", headers=_bearer(token))
    assert stranger_read.status_code == 401
    assert stranger_read.json()["error"]["message"] == EXPECTED_REJECTION


async def test_an_unspent_ticket_cannot_read_a_job(api_client, sessionmaker, documents) -> None:
    accepted = await api_client.post(
        "/v1/parse",
        files={"file": ("text.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        data={"model": MODEL},
    )
    token = await _mint(api_client, sessionmaker)

    response = await api_client.get(f"/v1/jobs/{accepted.json()['job_id']}", headers=_bearer(token))

    assert response.status_code == 401
    assert response.json()["error"]["message"] == EXPECTED_REJECTION


async def test_an_expired_ticket_is_refused_everywhere(api_client, sessionmaker, documents) -> None:
    expired = await _mint(api_client, sessionmaker, ttl_seconds=-60)
    accepted = await api_client.post(
        "/v1/parse",
        files={"file": ("text.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        data={"model": MODEL},
    )

    upload = await _upload(api_client, expired, documents["mixed_pdf"])
    read = await api_client.get(f"/v1/jobs/{accepted.json()['job_id']}", headers=_bearer(expired))

    assert upload.status_code == 401
    assert upload.json()["error"]["message"] == EXPECTED_REJECTION
    assert read.status_code == 401


async def test_an_unknown_ticket_is_refused(api_client, documents) -> None:
    response = await _upload(api_client, "srt_not-a-real-ticket", documents["text_pdf"])

    assert response.status_code == 401
    assert response.json()["error"]["message"] == EXPECTED_REJECTION


async def test_a_ticket_is_not_a_credential_anywhere_else(api_client, sessionmaker) -> None:
    token = await _mint(api_client, sessionmaker)

    models = await api_client.get("/v1/models", headers=_bearer(token))
    async with mcp_running(api_client):
        mcp = await api_client.post(
            "/mcp",
            headers={**_bearer(token), "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert models.status_code == 401
    assert models.json()["error"]["message"] != EXPECTED_REJECTION
    assert mcp.status_code == 401
    assert 'error="invalid_token"' in mcp.headers["WWW-Authenticate"]


async def test_an_upload_that_fails_before_a_job_exists_keeps_the_ticket(
    api_client, sessionmaker, documents
) -> None:
    token = await _mint(api_client, sessionmaker)

    refused = await api_client.post(
        "/v1/parse",
        files={"nope": ("text.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        headers=_bearer(token),
    )
    assert refused.status_code == 400
    assert (await _stored_ticket(sessionmaker)).spent_at is None

    # Still worth exactly one upload.
    assert (await _upload(api_client, token, documents["text_pdf"])).status_code == 202
    assert (await _stored_ticket(sessionmaker)).spent_at is not None


@respx.mock
async def test_a_dedup_hit_binds_the_ticket_to_the_cached_job(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    first = await api_client.post(
        "/v1/parse",
        files={"file": ("text.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        data={"model": MODEL},
    )
    job_id = first.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)
    upstream_calls = respx.calls.call_count

    token = await _mint(api_client, sessionmaker)
    cached = await _upload(api_client, token, documents["text_pdf"])

    assert cached.status_code == 200
    assert cached.json()["meta"]["cached"] is True
    assert respx.calls.call_count == upstream_calls

    ticket = await _stored_ticket(sessionmaker)
    assert str(ticket.job_id) == job_id
    assert ticket.spent_at is not None
    # The reads the ticket now allows are the cached job's.
    assert (await api_client.get(f"/v1/jobs/{job_id}", headers=_bearer(token))).status_code == 200
    result = await api_client.get(f"/v1/jobs/{job_id}/result", headers=_bearer(token))
    assert result.status_code == 200
    assert result.json()["markdown"]


@postgres_only
@respx.mock
async def test_a_cache_hit_outlives_the_file_that_kept_it_while_its_ticket_reads_it(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    settings = api_client.app.state.settings
    first = await api_client.post(
        "/v1/parse",
        files={"file": ("text.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        data={"model": MODEL},
    )
    job_id = uuid.UUID(first.json()["job_id"])
    await _drain_queue(api_client, sessionmaker)
    # Past its retention, kept only by a file of the same original.
    async with sessionmaker() as db:
        user = (await db.execute(select(User))).scalars().one()
        await db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(finished_at=utcnow() - timedelta(days=settings.parse_retention_days + 10))
        )
        db.add(Document(user_id=user.id, job_id=job_id, name="text.pdf"))
        await db.commit()
    token = await _mint(api_client, sessionmaker)
    cached = await _upload(api_client, token, documents["text_pdf"])
    assert cached.status_code == 200, cached.text
    assert cached.json()["meta"]["cached"] is True

    # The file is deleted for good before the client fetches the result: the ticket still
    # reads the job, so the sweep keeps it until the ticket expires.
    async with sessionmaker() as db:
        await db.execute(delete(Document).where(Document.job_id == job_id))
        await db.commit()
    assert await purge_parses(sessionmaker, settings) == 0
    fetched = await api_client.get("/v1/jobs/last/result", headers=_bearer(token))
    assert fetched.status_code == 200, fetched.text

    async with sessionmaker() as db:
        await db.execute(update(UploadTicket).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    assert await purge_parses(sessionmaker, settings) == 1
    async with sessionmaker() as db:
        assert await db.get(Job, job_id) is None
