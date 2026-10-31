"""A parse through the data plane behaves as a Files upload does (docs/mcp.md § The one tool,
docs/api.md § POST /v1/parse): checked, its reading copy returned as `markdown`, its crops
served, a failed one retried with its succeeded pages kept, and one in flight joined rather
than bought twice. The REST API and the connector's commands are the same routes; the
connector reaches them with an upload ticket and `last`, so both credentials are driven.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread.db.models import Job, UploadTicket, utcnow
from tests.conftest import CSRF_HEADERS, KANO_CHAT_URL
from tests.test_chat import MESSAGES_URL
from tests.test_format_check import READING, TRANSCRIPTION, _done, _tool, _turn, _vision
from tests.test_parse_end_to_end import _authorize, _drain_queue, _vision_stub
from tests.test_retry_failed_pages import FALLBACK, Upstream
from tests.test_retry_failed_pages import _upload as _library_upload
from tests.test_upload_tickets import _bearer, _mint

MODEL = "vendor/vision-model"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
async def api(make_client, sessionmaker, tmp_path) -> AsyncClient:
    """An API key on a deployment that names a fallback, with a library for the cases that
    need one; its session cookie reads billing and switches settings."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"), parse_fallback_model=FALLBACK)
    return await _authorize(client, sessionmaker)


async def _parse(
    client: AsyncClient, path: Path, token: str | None = None, **fields
) -> httpx.Response:
    """`POST /v1/parse` with the client's API key, or with an upload ticket."""
    return await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data=fields,
        headers=_bearer(token) if token is not None else {},
    )


async def _job(sessionmaker, job_id: str) -> Job:
    async with sessionmaker() as db:
        return await db.get(Job, uuid.UUID(job_id))


async def _spent(client: AsyncClient) -> int:
    return (await client.get("/api/billing")).json()["period"]["spent"]


def _uploads(client: AsyncClient) -> list[str]:
    upload_dir = Path(client.app.state.settings.upload_dir)
    return sorted(path.name for path in upload_dir.iterdir()) if upload_dir.exists() else []


# --- the reading copy -------------------------------------------------------------------------


@respx.mock
async def test_an_api_parse_is_checked_and_returns_the_reading_copy(
    api, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision())
    agent = respx.post(MESSAGES_URL).mock(
        side_effect=[
            _turn(
                [
                    _tool("t1", "join_pages", page=2),
                    _tool(
                        "t2",
                        "str_replace",
                        old_str="The **key idea is simple.",
                        new_str="The **key idea** is simple.",
                    ),
                ]
            ),
            _done(),
        ]
    )
    accepted = await _parse(api, documents["mixed_pdf"])
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    assert (await _job(sessionmaker, job_id)).check_model == MODEL
    await _drain_queue(api, sessionmaker)
    assert len(agent.calls) == 2

    # What the web reader opens, not the page-by-page transcription.
    markdown = await api.get(f"/v1/jobs/{job_id}/result.md")
    assert markdown.status_code == 200, markdown.text
    assert markdown.text == READING
    result = (await api.get(f"/v1/jobs/{job_id}/result")).json()
    assert result["markdown"] == READING
    assert result["meta"]["check"] == {"model": MODEL, "joins": 2, "edits": 1}
    # The stream's final event, the one the upload command waits on, carries it too.
    async with api.stream("GET", f"/v1/jobs/{job_id}/events") as response:
        body = "\n".join([line async for line in response.aiter_lines()])
    done = json.loads(body.split("event: done\ndata: ")[1].splitlines()[0])
    assert (done["markdown"], done["meta"]["check"]["joins"]) == (READING, 2)

    # The connector's path: a ticket's upload of the same file is the cached parse, and its
    # `last` reads land the same reading copy.
    token = await _mint(api, sessionmaker)
    cached = await _parse(api, documents["mixed_pdf"], token)
    assert cached.status_code == 200, cached.text
    assert (cached.json()["markdown"], cached.json()["meta"]["cached"]) == (READING, True)
    landed = await api.get("/v1/jobs/last/result.md", headers=_bearer(token))
    assert landed.text == READING
    assert (await api.get("/v1/jobs/last/result", headers=_bearer(token))).json()[
        "markdown"
    ] == READING


@respx.mock
async def test_with_the_check_switched_off_the_transcription_is_returned(
    api, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision())
    off = await api.put(
        "/api/agents/check/settings", json={"auto_check": False}, headers=CSRF_HEADERS
    )
    assert off.status_code == 200, off.text

    accepted = await _parse(api, documents["mixed_pdf"])
    job_id = accepted.json()["job_id"]
    assert (await _job(sessionmaker, job_id)).check_model is None
    await _drain_queue(api, sessionmaker)

    assert (await api.get(f"/v1/jobs/{job_id}/result.md")).text == TRANSCRIPTION
    assert "check" not in (await api.get(f"/v1/jobs/{job_id}/result")).json()["meta"]


# --- the crops --------------------------------------------------------------------------------


@respx.mock
async def test_a_figure_is_served_as_the_crop_the_web_reader_shows(
    api, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    token = await _mint(api, sessionmaker)
    accepted = await _parse(api, documents["mixed_pdf"], token)
    job_id = accepted.json()["job_id"]
    await _drain_queue(api, sessionmaker)
    stored = Path(api.app.state.settings.figures_dir) / job_id / "p2_0_0_1000_1000.png"

    # By the ticket's `last`, as the connector's figures command fetches it.
    crop = await api.get("/v1/jobs/last/figures/2/0,0,1000,1000", headers=_bearer(token))
    assert crop.status_code == 200, crop.text
    assert crop.headers["content-type"] == "image/png"
    assert crop.headers["cache-control"] == "private, no-store"
    assert crop.content == stored.read_bytes()
    assert crop.content.startswith(PNG_MAGIC)
    # By id, with the API key.
    by_id = await api.get(f"/v1/jobs/{job_id}/figures/1/0,0,1000,1000")
    assert by_id.status_code == 200
    assert by_id.content.startswith(PNG_MAGIC)
    # A box the parse holds no crop for, and one that is no box at all.
    for path in (f"/v1/jobs/{job_id}/figures/1/0,0,500,500", f"/v1/jobs/{job_id}/figures/1/x"):
        assert (await api.get(path)).status_code == 404
    # The ticket reads its own job's crops and no other job's.
    other = f"/v1/jobs/{uuid.uuid4()}/figures/1/0,0,1000,1000"
    assert (await api.get(other, headers=_bearer(token))).status_code == 401


# --- a failed parse ---------------------------------------------------------------------------


@respx.mock
async def test_a_failed_parse_is_retried_by_sending_its_file_again(
    api, sessionmaker, documents
) -> None:
    upstream = Upstream({(MODEL, 2): lambda: httpx.Response(400)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    first = await _mint(api, sessionmaker)
    job_id = (await _parse(api, documents["mixed_pdf"], first)).json()["job_id"]
    await _drain_queue(api, sessionmaker)

    # Final, not pending: the read says the parse failed and why.
    missing = await api.get("/v1/jobs/last/result.md", headers=_bearer(first))
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == (
        "This parse failed (1 of 3 pages failed (page 2: upstream call failed)) and has no result"
    )
    assert await _spent(api) == 0

    upstream.calls.clear()
    second = await _mint(api, sessionmaker)
    # Another selection of pages is another parse: nothing to retry. A flag that is no
    # bool is refused rather than read as false, which would start a full parse.
    other = await _parse(api, documents["mixed_pdf"], second, retry="true", pages="1-2")
    assert other.status_code == 409, other.text
    assert "with these pages" in other.json()["error"]["message"]
    unclear = await _parse(api, documents["mixed_pdf"], second, retry="please")
    assert unclear.status_code == 400, unclear.text
    assert (upstream.calls, await _spent(api), _uploads(api)) == ([], 0, [])
    # Any case, as JSON's bool takes it.
    retried = await _parse(api, documents["mixed_pdf"], second, retry="True", model=FALLBACK)
    assert retried.status_code == 202, retried.text
    assert retried.json() == {"job_id": job_id, "status": "queued"}
    job = await _job(sessionmaker, job_id)
    assert (job.status, job.pages_done, job.retry_model, job.check_model) == (
        "queued",
        2,
        FALLBACK,
        FALLBACK,
    )
    # The file again is the job's source, under the job's own name; credits taken back.
    assert [name.split(".retry-")[0] for name in _uploads(api)] == [job_id]
    assert await _spent(api) == 3

    # Sent once more while it waits: the same retry, joined — nothing reserved twice.
    third = await _mint(api, sessionmaker)
    again = await _parse(api, documents["mixed_pdf"], third, retry="true", model=FALLBACK)
    assert again.json() == {"job_id": job_id, "status": "queued"}
    assert await _spent(api) == 3
    assert len(_uploads(api)) == 1

    await _drain_queue(api, sessionmaker)
    # Only the failed page went upstream, to the model the retry named.
    assert upstream.calls == [(FALLBACK, 2)]
    result = (await api.get("/v1/jobs/last/result", headers=_bearer(second))).json()
    assert [page["model"] for page in result["pages"]] == [MODEL, FALLBACK, MODEL]
    assert result["markdown"].count("<!-- page:") == 3
    assert (await api.get(f"/v1/jobs/{job_id}")).json()["credits"] == 3
    assert await _spent(api) == 3
    assert _uploads(api) == []


@respx.mock
async def test_a_retry_with_nothing_to_retry_is_refused_and_spends_nothing(
    api, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=Upstream({(MODEL, 2): lambda: httpx.Response(400)}))
    token = await _mint(api, sessionmaker)

    nothing = await _parse(api, documents["mixed_pdf"], token, retry="true")
    assert nothing.status_code == 409, nothing.text
    assert "No parse of this file with these pages to retry" in nothing.json()["error"]["message"]
    both = await _parse(api, documents["mixed_pdf"], token, retry="true", force="true")
    assert both.status_code == 400, both.text
    async with sessionmaker() as db:
        ticket = (await db.execute(select(UploadTicket))).scalars().one()
    assert (ticket.spent_at, ticket.job_id) == (None, None)
    assert _uploads(api) == []

    # A failed parse behind a library document is the library's to retry, not the API's.
    filed = await _library_upload(api, documents["mixed_pdf"])
    await _drain_queue(api, sessionmaker)
    assert (await _job(sessionmaker, filed["job_id"])).status == "failed"
    refused = await _parse(api, documents["mixed_pdf"], retry="true")
    assert refused.status_code == 409, refused.text
    assert "a Files document's" in refused.json()["error"]["message"]
    assert (await _job(sessionmaker, filed["job_id"])).status == "failed"


@respx.mock
async def test_a_retry_the_balance_cannot_cover_is_refused_and_changes_nothing(
    make_client, sessionmaker, documents, tmp_path
) -> None:
    client = make_client(
        workspaces_dir=str(tmp_path / "ws"),
        parse_fallback_model=FALLBACK,
        free_credits_per_period=3,
    )
    api = await _authorize(client, sessionmaker)
    respx.post(KANO_CHAT_URL).mock(side_effect=Upstream({(MODEL, 2): lambda: httpx.Response(400)}))
    job_id = (await _parse(api, documents["mixed_pdf"])).json()["job_id"]
    await _drain_queue(api, sessionmaker)
    # The failure gave its credits back; a one-page parse takes one of the three.
    assert (await _parse(api, documents["text_pdf"])).status_code == 202
    await _drain_queue(api, sessionmaker)
    assert await _spent(api) == 1

    token = await _mint(api, sessionmaker)
    refused = await _parse(api, documents["mixed_pdf"], token, retry="true")
    assert refused.status_code == 402, refused.text
    assert (refused.json()["error"]["need"], refused.json()["error"]["remaining"]) == (3, 2)
    assert (await _job(sessionmaker, job_id)).status == "failed"
    assert await _spent(api) == 1
    async with sessionmaker() as db:
        ticket = (await db.execute(select(UploadTicket))).scalars().one()
    assert (ticket.spent_at, ticket.job_id) == (None, None)
    assert _uploads(api) == []


@respx.mock
async def test_a_retry_after_the_file_parsed_again_answers_that_parse(
    api, sessionmaker, documents
) -> None:
    upstream = Upstream({(MODEL, 2): lambda: httpx.Response(400)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    failed = (await _parse(api, documents["mixed_pdf"])).json()["job_id"]
    await _drain_queue(api, sessionmaker)
    # Past the in-flight window, so the file sent again is a parse of its own.
    async with sessionmaker() as db:
        await db.execute(
            update(Job)
            .where(Job.id == uuid.UUID(failed))
            .values(finished_at=utcnow() - timedelta(minutes=5))
        )
        await db.commit()
    upstream.answers.clear()
    parsed = (await _parse(api, documents["mixed_pdf"])).json()["job_id"]
    await _drain_queue(api, sessionmaker)
    assert parsed != failed
    assert await _spent(api) == 3
    upstream.calls.clear()

    # The newer parse answers: the older failure is neither run nor billed again.
    token = await _mint(api, sessionmaker)
    retried = await _parse(api, documents["mixed_pdf"], token, retry="true")
    assert retried.status_code == 202, retried.text
    assert retried.json() == {"job_id": parsed, "status": "succeeded"}
    assert (await _job(sessionmaker, failed)).status == "failed"
    assert upstream.calls == []
    assert await _spent(api) == 3
    assert (await api.get("/v1/jobs/last/result.md", headers=_bearer(token))).status_code == 200
    assert _uploads(api) == []


@respx.mock
async def test_a_retry_after_the_file_parsed_in_files_answers_that_parse(
    api, sessionmaker, documents
) -> None:
    upstream = Upstream({(MODEL, 2): lambda: httpx.Response(400)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    failed = (await _parse(api, documents["mixed_pdf"])).json()["job_id"]
    await _drain_queue(api, sessionmaker)
    async with sessionmaker() as db:
        await db.execute(
            update(Job)
            .where(Job.id == uuid.UUID(failed))
            .values(finished_at=utcnow() - timedelta(minutes=5))
        )
        await db.commit()
    upstream.answers.clear()
    filed = await _library_upload(api, documents["mixed_pdf"])
    await _drain_queue(api, sessionmaker)
    assert (await _job(sessionmaker, filed["job_id"])).status == "succeeded"
    assert await _spent(api) == 3
    upstream.calls.clear()

    # The Files upload is the newest parse of the file: the retry is answered with it.
    token = await _mint(api, sessionmaker)
    retried = await _parse(api, documents["mixed_pdf"], token, retry="true")
    assert retried.json() == {"job_id": filed["job_id"], "status": "succeeded"}
    assert (await _job(sessionmaker, failed)).status == "failed"
    assert (upstream.calls, await _spent(api)) == ([], 3)


# --- a parse in flight ------------------------------------------------------------------------


@respx.mock
async def test_the_same_file_again_joins_the_parse_in_flight(api, sessionmaker, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    first = await _parse(api, documents["mixed_pdf"])
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    assert await _spent(api) == 3

    # A command the shell cut off, run again with a fresh ticket: the same parse, bound to
    # the ticket, no second reservation.
    token = await _mint(api, sessionmaker)
    joined = await _parse(api, documents["mixed_pdf"], token)
    assert joined.json() == {"job_id": job_id, "status": "queued"}
    assert (await api.get("/v1/jobs/last", headers=_bearer(token))).json()["job_id"] == job_id
    assert await _spent(api) == 3

    # `force` is how a caller asks for a second parse.
    forced = await _parse(api, documents["mixed_pdf"], force="true")
    assert forced.status_code == 202
    assert forced.json()["job_id"] != job_id
    assert await _spent(api) == 6

    # At the plan's cap (Free: one running), the same file still joins a parse of it in
    # flight: the cap refuses only an upload that would start one.
    in_flight = {uuid.UUID(job_id), uuid.UUID(forced.json()["job_id"])}
    async with sessionmaker() as db:
        await db.execute(update(Job).where(Job.id.in_(in_flight)).values(status="running"))
        await db.commit()
    again = await _parse(api, documents["mixed_pdf"], await _mint(api, sessionmaker))
    assert again.status_code == 202, again.text
    assert uuid.UUID(again.json()["job_id"]) in in_flight
    capped = await _parse(api, documents["text_pdf"])
    assert capped.status_code == 429, capped.text
    assert await _spent(api) == 6
    assert sorted(name.split(".")[0] for name in _uploads(api)) == sorted(
        [job_id, forced.json()["job_id"]]
    )
