"""Upload tickets end to end (docs/auth.md § 5): one upload, then reads of that one job.

No upstream traffic here either — the worker runs against the stubbed Kano Proxy of
`tests/helpers` (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sightread.auth import upload_tickets
from sightread.billing import ledger
from sightread.config import Settings
from sightread.db.models import CreditEntry, Document, Job, UploadTicket, User, utcnow
from sightread.errors import ApiError
from sightread.jobs.intake import Submission, file_chunks, submit_parse
from sightread.jobs.sweeper import purge_parses
from tests.conftest import KANO_CHAT_URL, PARSE_MODEL, mcp_running, postgres_only
from tests.helpers import (
    Upstream,
    bearer,
    credits_spent,
    drain_queue,
    mint_upload_ticket,
    staged_uploads,
    v1_parse,
    vision_stub,
)

# The copy is part of the contract: it is the agent's only recovery hint (docs/auth.md § 5).
EXPECTED_REJECTION = (
    "Upload ticket expired or already spent — call the parse tool again for a fresh "
    "ticket. Re-uploading the same file joins its parse while it runs and returns it once "
    "it has succeeded; a failed parse is resumed with the retry command."
)


async def _submit(
    db: AsyncSession, settings: Settings, ticket: UploadTicket, user: User, path: Path, **fields
) -> Submission:
    """An upload of `path` on a ticket resolved in `db`, as `/v1/parse` submits it."""
    return await submit_parse(
        db,
        settings,
        user=user,
        chunks=file_chunks(path),
        media_type="application/pdf",
        filename=path.name,
        ticket=ticket,
        **fields,
    )


def _one_refused(answers: list[Submission | BaseException]) -> Submission:
    """The one upload that spent the ticket, once the other was the 401."""
    submissions = [answer for answer in answers if isinstance(answer, Submission)]
    refusals = [answer for answer in answers if isinstance(answer, ApiError)]
    assert len(submissions) == 1 and len(refusals) == 1, answers
    assert refusals[0].status_code == 401
    assert refusals[0].message == EXPECTED_REJECTION
    return submissions[0]


async def _failed_parse(client: AsyncClient, sessionmaker, path: Path) -> uuid.UUID:
    """A data-plane parse of `path` whose page 2 the stubbed upstream refuses: failed, its
    credits released, its upload gone."""
    respx.post(KANO_CHAT_URL).mock(
        side_effect=Upstream({(PARSE_MODEL, 2): lambda: httpx.Response(400)})
    )
    queued = await v1_parse(client, path, model=PARSE_MODEL)
    job_id = uuid.UUID(queued.json()["job_id"])
    await drain_queue(client, sessionmaker)
    async with sessionmaker() as db:
        assert (await db.get(Job, job_id)).status == "failed"
    assert staged_uploads(client) == []
    return job_id


async def _stored_ticket(sessionmaker) -> UploadTicket:
    async with sessionmaker() as db:
        return (await db.execute(select(UploadTicket))).scalars().one()


async def test_a_ticket_uploads_once_and_binds_to_the_job_it_created(
    api_client, sessionmaker, documents
) -> None:
    token = await mint_upload_ticket(api_client)

    accepted = await v1_parse(api_client, documents["text_pdf"], token)

    assert accepted.status_code == 202, accepted.text
    ticket = await _stored_ticket(sessionmaker)
    assert str(ticket.job_id) == accepted.json()["job_id"]
    assert ticket.spent_at is not None


@respx.mock
async def test_last_resolves_a_spent_ticket_to_its_own_job(
    api_client, sessionmaker, documents
) -> None:
    """The `last` aliases behind the MCP curl commands (docs/api.md § GET /v1/jobs/last*)."""
    respx.post(KANO_CHAT_URL).mock(side_effect=vision_stub)
    token = await mint_upload_ticket(api_client)
    job_id = (await v1_parse(api_client, documents["text_pdf"], token)).json()["job_id"]
    await drain_queue(api_client, sessionmaker)

    status = await api_client.get("/v1/jobs/last", headers=bearer(token))
    assert status.status_code == 200
    assert status.json() == {**status.json(), "job_id": job_id, "status": "succeeded"}

    result = await api_client.get("/v1/jobs/last/result", headers=bearer(token))
    assert result.status_code == 200
    assert result.json()["meta"]["job_id"] == job_id

    markdown = await api_client.get("/v1/jobs/last/result.md", headers=bearer(token))
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text.startswith("<!-- page: 1 -->\n")

    # A durable credential has its job id and gets told to use it.
    refused = await api_client.get("/v1/jobs/last")
    assert refused.status_code == 400


async def test_last_with_an_unspent_ticket_says_it_has_started_no_parse(api_client) -> None:
    # Not the expired copy: that would send the agent to mint again for an upload that was
    # refused, or never sent, while this ticket can still upload (docs/auth.md § 5).
    token = await mint_upload_ticket(api_client)
    for path in ("/v1/jobs/last", "/v1/jobs/last/result.md", "/v1/jobs/last/figures/1/0,0,9,9"):
        response = await api_client.get(path, headers=bearer(token))
        assert response.status_code == 409, path
        assert "has not started a parse" in response.json()["error"]["message"]


async def test_a_spent_ticket_cannot_upload_again(api_client, documents) -> None:
    token = await mint_upload_ticket(api_client)
    assert (await v1_parse(api_client, documents["text_pdf"], token)).status_code == 202

    refused = await v1_parse(api_client, documents["mixed_pdf"], token)

    assert refused.status_code == 401
    assert refused.json()["error"]["type"] == "auth"
    assert refused.json()["error"]["message"] == EXPECTED_REJECTION
    assert refused.json()["error"]["message"] == upload_tickets.REJECTION_MESSAGE


@respx.mock
async def test_a_spent_ticket_reads_its_own_job_and_nothing_else(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=vision_stub)
    token = await mint_upload_ticket(api_client)
    job_id = (await v1_parse(api_client, documents["text_pdf"], token)).json()["job_id"]
    await drain_queue(api_client, sessionmaker)

    status = await api_client.get(f"/v1/jobs/{job_id}", headers=bearer(token))
    result = await api_client.get(f"/v1/jobs/{job_id}/result", headers=bearer(token))

    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert result.status_code == 200
    assert result.json()["markdown"]

    frames = []
    async with api_client.stream(
        "GET", f"/v1/jobs/{job_id}/events", headers=bearer(token)
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            frames.append(line)
    assert "event: done" in "\n".join(frames)

    # A second job of the same user is off limits: the ticket is bound to one job.
    other = (await v1_parse(api_client, documents["mixed_pdf"], model=PARSE_MODEL)).json()["job_id"]
    stranger_read = await api_client.get(f"/v1/jobs/{other}", headers=bearer(token))
    assert stranger_read.status_code == 401
    assert stranger_read.json()["error"]["message"] == EXPECTED_REJECTION


async def test_an_unspent_ticket_cannot_read_a_job(api_client, documents) -> None:
    accepted = await v1_parse(api_client, documents["text_pdf"], model=PARSE_MODEL)
    token = await mint_upload_ticket(api_client)

    response = await api_client.get(f"/v1/jobs/{accepted.json()['job_id']}", headers=bearer(token))

    assert response.status_code == 401
    assert response.json()["error"]["message"] == EXPECTED_REJECTION


async def test_an_expired_ticket_is_refused_everywhere(api_client, documents) -> None:
    expired = await mint_upload_ticket(api_client, ttl_seconds=-60)
    accepted = await v1_parse(api_client, documents["text_pdf"], model=PARSE_MODEL)

    upload = await v1_parse(api_client, documents["mixed_pdf"], expired)
    read = await api_client.get(f"/v1/jobs/{accepted.json()['job_id']}", headers=bearer(expired))

    assert upload.status_code == 401
    assert upload.json()["error"]["message"] == EXPECTED_REJECTION
    assert read.status_code == 401


async def test_an_unknown_ticket_is_refused(api_client, documents) -> None:
    response = await v1_parse(api_client, documents["text_pdf"], "srt_not-a-real-ticket")

    assert response.status_code == 401
    assert response.json()["error"]["message"] == EXPECTED_REJECTION


async def test_a_ticket_is_not_a_credential_anywhere_else(api_client) -> None:
    token = await mint_upload_ticket(api_client)

    models = await api_client.get("/v1/models", headers=bearer(token))
    async with mcp_running(api_client):
        mcp = await api_client.post(
            "/mcp",
            headers={**bearer(token), "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert models.status_code == 401
    assert models.json()["error"]["message"] != EXPECTED_REJECTION
    assert mcp.status_code == 401
    assert 'error="invalid_token"' in mcp.headers["WWW-Authenticate"]


async def test_an_upload_that_fails_before_a_job_exists_keeps_the_ticket(
    api_client, sessionmaker, documents
) -> None:
    token = await mint_upload_ticket(api_client)

    refused = await api_client.post(
        "/v1/parse",
        files={"nope": ("text.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        headers=bearer(token),
    )
    assert refused.status_code == 400
    assert (await _stored_ticket(sessionmaker)).spent_at is None

    # Still worth exactly one upload.
    assert (await v1_parse(api_client, documents["text_pdf"], token)).status_code == 202
    assert (await _stored_ticket(sessionmaker)).spent_at is not None


@respx.mock
async def test_a_dedup_hit_binds_the_ticket_to_the_cached_job(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=vision_stub)
    first = await v1_parse(api_client, documents["text_pdf"], model=PARSE_MODEL)
    job_id = first.json()["job_id"]
    await drain_queue(api_client, sessionmaker)
    upstream_calls = respx.calls.call_count

    token = await mint_upload_ticket(api_client)
    cached = await v1_parse(api_client, documents["text_pdf"], token)

    assert cached.status_code == 200
    assert cached.json()["meta"]["cached"] is True
    assert respx.calls.call_count == upstream_calls

    ticket = await _stored_ticket(sessionmaker)
    assert str(ticket.job_id) == job_id
    assert ticket.spent_at is not None
    # The reads the ticket now allows are the cached job's.
    assert (await api_client.get(f"/v1/jobs/{job_id}", headers=bearer(token))).status_code == 200
    result = await api_client.get(f"/v1/jobs/{job_id}/result", headers=bearer(token))
    assert result.status_code == 200
    assert result.json()["markdown"]


@postgres_only
@respx.mock
async def test_a_cache_hit_outlives_the_file_that_kept_it_while_its_ticket_reads_it(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=vision_stub)
    settings = api_client.app.state.settings
    first = await v1_parse(api_client, documents["text_pdf"], model=PARSE_MODEL)
    job_id = uuid.UUID(first.json()["job_id"])
    await drain_queue(api_client, sessionmaker)
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
    token = await mint_upload_ticket(api_client)
    cached = await v1_parse(api_client, documents["text_pdf"], token)
    assert cached.status_code == 200, cached.text
    assert cached.json()["meta"]["cached"] is True

    # The file is deleted for good before the client fetches the result: the ticket still
    # reads the job, so the sweep keeps it until the ticket expires.
    async with sessionmaker() as db:
        await db.execute(delete(Document).where(Document.job_id == job_id))
        await db.commit()
    assert await purge_parses(sessionmaker, settings) == 0
    fetched = await api_client.get("/v1/jobs/last/result", headers=bearer(token))
    assert fetched.status_code == 200, fetched.text

    async with sessionmaker() as db:
        await db.execute(update(UploadTicket).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    assert await purge_parses(sessionmaker, settings) == 1
    async with sessionmaker() as db:
        assert await db.get(Job, job_id) is None


@postgres_only
async def test_two_uploads_racing_on_one_ticket_start_one_parse(
    api_client, sessionmaker, documents
) -> None:
    """Both requests passed auth on the unspent ticket before either committed: one starts
    a parse, the other is the 401 and leaves nothing behind (docs/auth.md § 5)."""
    settings = api_client.app.state.settings
    token = await mint_upload_ticket(api_client)
    authenticated = asyncio.Barrier(2)

    async def upload(path: Path) -> Submission:
        async with sessionmaker() as db:
            ticket, user = await upload_tickets.resolve(db, token)
            assert ticket.spent_at is None
            await authenticated.wait()
            return await _submit(db, settings, ticket, user, path)

    answers = await asyncio.gather(
        upload(documents["text_pdf"]), upload(documents["scanned_pdf"]), return_exceptions=True
    )

    job = _one_refused(answers).job
    async with sessionmaker() as db:
        assert (await db.execute(select(Job.id))).scalars().all() == [job.id]
        reserved = (await db.execute(select(CreditEntry.job_id))).scalars().all()
        assert reserved == [job.id]
        ticket = (await db.execute(select(UploadTicket))).scalars().one()
        assert ticket.job_id == job.id
    assert staged_uploads(api_client) == [Path(job.source_path).name]


@postgres_only
@respx.mock
async def test_two_kept_uploads_of_a_cached_parse_racing_on_one_ticket_keep_one_copy(
    api_client, sessionmaker, documents
) -> None:
    """A cache hit that keeps originals keeps its fresh copy for its own row; the 401 takes
    the loser's copy with it."""
    respx.post(KANO_CHAT_URL).mock(side_effect=vision_stub)
    settings = api_client.app.state.settings
    first = await v1_parse(api_client, documents["text_pdf"], model=PARSE_MODEL)
    assert first.status_code == 202, first.text
    await drain_queue(api_client, sessionmaker)
    before = staged_uploads(api_client)
    token = await mint_upload_ticket(api_client)
    authenticated = asyncio.Barrier(2)

    async def upload() -> Submission:
        async with sessionmaker() as db:
            ticket, user = await upload_tickets.resolve(db, token)
            await authenticated.wait()
            return await _submit(
                db,
                settings,
                ticket,
                user,
                documents["text_pdf"],
                model=PARSE_MODEL,
                retain_source=True,
            )

    kept = _one_refused(await asyncio.gather(upload(), upload(), return_exceptions=True))
    assert kept.cached is not None and kept.upload is not None
    assert staged_uploads(api_client) == sorted([*before, kept.upload.name])


@postgres_only
async def test_a_kept_upload_joining_a_parse_on_a_ticket_spent_meanwhile_changes_nothing(
    api_client, sessionmaker, documents
) -> None:
    """Both requests authenticated on the unspent ticket; a plain upload joins the parse in
    flight and spends it first. The kept upload's join is the 401: its copy goes and the
    job is not marked as keeping its original."""
    settings = api_client.app.state.settings
    queued = await v1_parse(api_client, documents["text_pdf"], model=PARSE_MODEL)
    job_id = uuid.UUID(queued.json()["job_id"])
    token = await mint_upload_ticket(api_client)
    async with sessionmaker() as late, sessionmaker() as early:
        stale, user = await upload_tickets.resolve(late, token)
        ticket, owner = await upload_tickets.resolve(early, token)
        joined = await _submit(
            early, settings, ticket, owner, documents["text_pdf"], model=PARSE_MODEL
        )
        assert joined.job.id == job_id
        with pytest.raises(ApiError) as refused:
            await _submit(
                late,
                settings,
                stale,
                user,
                documents["text_pdf"],
                model=PARSE_MODEL,
                retain_source=True,
            )
    assert (refused.value.status_code, refused.value.message) == (401, EXPECTED_REJECTION)
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        assert job.retain_source is False
        assert (await db.execute(select(Job.id))).scalars().all() == [job_id]
    assert staged_uploads(api_client) == [Path(job.source_path).name]


@postgres_only
@respx.mock
async def test_two_retries_racing_on_one_ticket_put_the_parse_back_once(
    api_client, sessionmaker, documents
) -> None:
    """The second retry waits on the first's holds, finds the parse queued again and is the
    401: one requeue, its credits taken back once, one copy under the job's name."""
    settings = api_client.app.state.settings
    job_id = await _failed_parse(api_client, sessionmaker, documents["mixed_pdf"])
    token = await mint_upload_ticket(api_client)
    authenticated = asyncio.Barrier(2)

    async def retry() -> Submission:
        async with sessionmaker() as db:
            ticket, user = await upload_tickets.resolve(db, token)
            await authenticated.wait()
            return await _submit(db, settings, ticket, user, documents["mixed_pdf"], retry=True)

    retried = _one_refused(await asyncio.gather(retry(), retry(), return_exceptions=True))
    assert (retried.job.id, retried.job.status) == (job_id, "queued")
    async with sessionmaker() as db:
        entry = (
            await db.execute(select(CreditEntry).where(CreditEntry.job_id == job_id))
        ).scalar_one()
        assert entry.state == ledger.RESERVED
    assert await credits_spent(api_client) == 3
    assert [name.split(".retry-")[0] for name in staged_uploads(api_client)] == [str(job_id)]


@postgres_only
@respx.mock
async def test_a_retry_on_a_ticket_spent_meanwhile_leaves_the_parse_failed(
    api_client, sessionmaker, documents
) -> None:
    """Both requests authenticated on the unspent ticket; another file's upload spends it
    first. The retry got as far as its copy under the job's name and its credits taken
    back: the 401 removes the copy and the parse stays failed, its credits released."""
    settings = api_client.app.state.settings
    job_id = await _failed_parse(api_client, sessionmaker, documents["mixed_pdf"])
    token = await mint_upload_ticket(api_client)
    async with sessionmaker() as late, sessionmaker() as early:
        stale, user = await upload_tickets.resolve(late, token)
        ticket, owner = await upload_tickets.resolve(early, token)
        other = await _submit(
            early, settings, ticket, owner, documents["text_pdf"], model=PARSE_MODEL
        )
        with pytest.raises(ApiError) as refused:
            await _submit(late, settings, stale, user, documents["mixed_pdf"], retry=True)
    assert (refused.value.status_code, refused.value.message) == (401, EXPECTED_REJECTION)
    async with sessionmaker() as db:
        assert (await db.get(Job, job_id)).status == "failed"
        entry = (
            await db.execute(select(CreditEntry).where(CreditEntry.job_id == job_id))
        ).scalar_one()
        assert entry.state == ledger.RELEASED
    assert staged_uploads(api_client) == [Path(other.job.source_path).name]
