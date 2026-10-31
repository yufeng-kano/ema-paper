"""`POST /v1/parse` through the worker to the result, with Kano Proxy stubbed.

This is the phase's integration test: a real PDF, real Poppler subprocesses, a real claim
cycle, and not one byte of live upstream traffic (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread.agents.parse import PIPELINE_VERSION
from sightread.db.models import Job, JobPage, Result, UsageLog, utcnow
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
from tests.conftest import CSRF_HEADERS, DATABASE_URL, KANO_API_KEY, KANO_CHAT_URL
from tests.test_jobs_queue import postgres_only

MODEL = "vendor/vision-model"
# Its figure takes the whole page: no side of its crop can cut anything, so the figure check
# (docs/parsing.md § Figure check) asks no model, whatever the fixture page holds.
VISION_MARKDOWN = (
    "## Scanned page\n\n"
    "![fig](sightread://p9/0,0,1000,1000)\n"
    "Figure 2: the photograph\n\n"
    "Body text."
)


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "cost": 0.0015},
        },
    )


def _vision_stub(request: httpx.Request) -> httpx.Response:
    """Every page is a vision transcription (docs/parsing.md § Vision-only conversion)."""
    return _completion(VISION_MARKDOWN)


async def _authorize(client: AsyncClient, sessionmaker) -> AsyncClient:
    """Give a client an API key."""
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    created = await client.post("/api/keys", json={"name": "test"}, headers=CSRF_HEADERS)
    client.headers["Authorization"] = f"Bearer {created.json()['key']}"
    return client


@pytest.fixture
async def api_client(make_client, sessionmaker) -> AsyncClient:
    return await _authorize(make_client(), sessionmaker)


async def _drain_queue(client: AsyncClient, sessionmaker) -> uuid.UUID | None:
    """One worker cycle in-process: claim a job and run it to a terminal state."""
    settings = client.app.state.settings
    async with sessionmaker() as db:
        job_id = await claim_next_job(db, settings.max_jobs_per_user)
    if job_id is not None:
        await run_job(sessionmaker, settings, job_id, asyncio.Semaphore(2))
    return job_id


async def _upload(client: AsyncClient, path: Path, **fields) -> httpx.Response:
    return await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"model": MODEL, **fields},
    )


@respx.mock
async def test_parse_a_mixed_pdf_end_to_end(api_client, sessionmaker, documents) -> None:
    route = respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)

    accepted = await _upload(api_client, documents["mixed_pdf"])
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    assert accepted.json()["status"] == "queued"

    assert str(await _drain_queue(api_client, sessionmaker)) == job_id

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "succeeded"
    assert (status["page_count"], status["pages_done"]) == (3, 3)

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert [page["method"] for page in result["pages"]] == ["vision", "vision", "vision"]
    assert [page["page"] for page in result["pages"]] == [1, 2, 3]
    assert result["pages"][0]["width_pt"] == 612.0
    assert result["errors"] == []

    # One inline figure per page, renumbered document-wide.
    assert [figure["id"] for figure in result["figures"]] == ["fig1", "fig2", "fig3"]
    assert result["figures"][0] == {
        "id": "fig1",
        "page": 1,
        "bbox": [0, 0, 1000, 1000],
        "caption": "Figure 2: the photograph",
        "check": "verified",
    }
    # The page number in a placeholder is ours, not the model's claim of "p9".
    assert "![fig2](sightread://p2/0,0,1000,1000)" in result["markdown"]
    # Every page's content sits behind its marker.
    for page_no in (1, 2, 3):
        assert f"<!-- page: {page_no} -->" in result["markdown"]

    assert result["meta"] == {
        "job_id": job_id,
        "agent": "parse",
        "model": MODEL,
        "bbox_format": "yxyx_norm1000",
        "pipeline_version": PIPELINE_VERSION,
        "sha256": result["meta"]["sha256"],
        "cached": False,
        # Checked as a Files upload is: no page boundary here needed a join or an edit.
        "check": {"model": MODEL, "joins": 0, "edits": 0},
        "figure_check": {"verified": 3, "adjusted": 0, "unverified": 0},
    }
    assert len(result["meta"]["sha256"]) == 64

    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(job_id))
        assert job.source_deleted_at is not None
        assert not Path(job.source_path).exists()
        usage = (
            (await db.execute(select(UsageLog).where(UsageLog.job_id == job.id))).scalars().all()
        )
    # One transcription call per page.
    assert len(usage) == 3
    assert {row.model for row in usage} == {MODEL}
    assert sum(row.prompt_tokens for row in usage) == 2700
    # Kano Proxy's reported cost is a list-price estimate, never billing.
    assert {row.cost_source for row in usage} == {"estimated"}
    assert float(sum(row.cost for row in usage)) == pytest.approx(0.0045)
    # The upstream is Kano Proxy's OpenAI base, called with the operator's key, and the
    # job's dedup snapshot is the Kano Proxy origin (docs/jobs.md § Dedup).
    assert route.calls[0].request.headers["authorization"] == f"Bearer {KANO_API_KEY}"
    assert (job.connection_id, job.connection_base_url) == (None, "https://kano.test")
    assert job.agent == "parse"

    # Each page's figure crop was persisted before its render was deleted, addressed by
    # our page number and the cleaned bbox (docs/parsing.md § Figure crops).
    figures_dir = Path(api_client.app.state.settings.figures_dir) / job_id
    for page_no in (1, 2, 3):
        assert (figures_dir / f"p{page_no}_0_0_1000_1000.png").is_file()


@respx.mock
async def test_parse_an_image(api_client, sessionmaker, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)

    accepted = await api_client.post(
        "/v1/parse",
        files={"file": ("tiny.png", documents["wide_png"].read_bytes(), "image/png")},
        data={"model": MODEL},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    # Page space is the original pixel size, not the downscaled copy we sent upstream.
    assert result["pages"] == [
        {
            "page": 1,
            "width_pt": 3000.0,
            "height_pt": 1000.0,
            "method": "vision",
            "model": MODEL,
            "error": None,
        }
    ]
    assert result["errors"] == []
    assert result["figures"][0]["page"] == 1
    assert "![fig1](sightread://p1/0,0,1000,1000)" in result["markdown"]

    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(job_id))
    assert job.kind == "image"
    assert not Path(job.source_path).exists()
    # The figure was cropped from the normalized image before it was deleted.
    figures_dir = Path(api_client.app.state.settings.figures_dir) / job_id
    assert (figures_dir / "p1_0_0_1000_1000.png").is_file()
    # The rendered/normalized copy is gone with the job's work directory.
    assert list(Path(api_client.app.state.settings.upload_dir).iterdir()) == []


@respx.mock
async def test_second_identical_upload_is_served_from_the_cache(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    await _upload(api_client, documents["text_pdf"])
    await _drain_queue(api_client, sessionmaker)
    calls_after_first = respx.calls.call_count

    cached = await _upload(api_client, documents["text_pdf"])

    assert cached.status_code == 200
    assert cached.json()["meta"]["cached"] is True
    assert cached.json()["markdown"]
    assert respx.calls.call_count == calls_after_first
    # The fresh upload was deleted; only the first job's (already removed) file existed.
    upload_dir = Path(api_client.app.state.settings.upload_dir)
    assert list(upload_dir.iterdir()) == []

    forced = await _upload(api_client, documents["text_pdf"], force="true")
    assert forced.status_code == 202
    assert forced.json()["job_id"] != cached.json().get("job_id")


@respx.mock
async def test_a_page_that_cannot_be_transcribed_fails_the_job(
    api_client, sessionmaker, documents
) -> None:
    # A 400 is not retried, so the second response is the other page's.
    responses = [httpx.Response(400), _completion(VISION_MARKDOWN)]
    respx.post(KANO_CHAT_URL).mock(side_effect=responses)

    accepted = await _upload(api_client, documents["mixed_pdf"], pages="1,2")
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    # Every page or nothing: no result, and the error names the page that failed and why.
    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "failed"
    assert re.fullmatch(r"1 of 2 pages failed \(page [12]: upstream call failed\)", status["error"])
    assert (await api_client.get(f"/v1/jobs/{job_id}/result")).status_code == 404
    # The page that succeeded stays on its row, for a retry to keep; the other has none.
    async with sessionmaker() as db:
        rows = (
            (await db.execute(select(JobPage).where(JobPage.job_id == uuid.UUID(job_id))))
            .scalars()
            .all()
        )
    assert sorted(row.markdown is not None for row in rows) == [False, True]


@respx.mock
async def test_a_degraded_result_is_never_served_from_the_cache(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    first = await _upload(api_client, documents["mixed_pdf"], pages="1,2")
    await _drain_queue(api_client, sessionmaker)
    # A result from before a failed page failed the job: served, with a page missing. It
    # finished before the retry window, which would otherwise join it as the same upload.
    async with sessionmaker() as db:
        await db.execute(
            update(Result)
            .where(Result.job_id == uuid.UUID(first.json()["job_id"]))
            .values(errors=[{"page": 1, "reason": "upstream call failed"}])
        )
        await db.execute(
            update(Job)
            .where(Job.id == uuid.UUID(first.json()["job_id"]))
            .values(finished_at=utcnow() - timedelta(minutes=5))
        )
        await db.commit()

    # The same upload again: the stored result carries a failed page, so no cache hit.
    retried = await _upload(api_client, documents["mixed_pdf"], pages="1,2")
    assert retried.status_code == 202
    await _drain_queue(api_client, sessionmaker)

    complete = (await api_client.get(f"/v1/jobs/{retried.json()['job_id']}/result")).json()
    assert complete["errors"] == []

    # Now that a complete result exists, the cache answers.
    cached = await _upload(api_client, documents["mixed_pdf"], pages="1,2")
    assert cached.status_code == 200
    assert cached.json()["meta"]["cached"] is True


@respx.mock
async def test_a_wholly_failed_job_names_the_page_reasons(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(400))

    accepted = await _upload(api_client, documents["text_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "failed"
    assert status["error"] == "1 of 1 pages failed (page 1: upstream call failed)"


@respx.mock
async def test_result_md_serves_the_markdown_as_a_file(api_client, sessionmaker, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    accepted = await _upload(api_client, documents["text_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    response = await api_client.get(f"/v1/jobs/{job_id}/result.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("<!-- page: 1 -->\n")


@respx.mock
async def test_a_selected_prompt_preset_is_sent_and_keyed_into_the_cache(
    api_client, sessionmaker, documents
) -> None:
    route = respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    created = await api_client.post(
        "/api/prompts",
        json={"name": "Tables only", "text": "Only tables from page {page}."},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201
    preset_id = created.json()["id"]
    selected = await api_client.put(
        "/api/agents/parse/settings", json={"prompt_preset_id": preset_id}, headers=CSRF_HEADERS
    )
    assert selected.json()["prompt_preset_id"] == preset_id

    await _upload(api_client, documents["text_pdf"])
    await _drain_queue(api_client, sessionmaker)
    sent = json.loads(route.calls[0].request.content)["messages"][0]["content"][0]["text"]
    assert sent == "Only tables from page 1."

    # Same bytes, same model — but a different prompt is a different parse.
    await api_client.put(
        f"/api/prompts/{preset_id}",
        json={"text": "Verbatim, everything."},
        headers=CSRF_HEADERS,
    )
    again = await _upload(api_client, documents["text_pdf"])
    assert again.status_code == 202


@respx.mock
async def test_a_second_402_aborts_the_job(api_client, sessionmaker, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(402))

    accepted = await _upload(api_client, documents["mixed_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "failed"
    assert status["error"] == "the upstream reported exhausted credits"
    assert (await api_client.get(f"/v1/jobs/{job_id}/result")).status_code == 404
    # A failed job keeps no crops: whatever pages managed to save one before the abort,
    # the directory goes with the result the job never wrote (docs/jobs.md § Retention).
    assert not (Path(api_client.app.state.settings.figures_dir) / job_id).exists()


@respx.mock
async def test_rate_limits_back_off_and_shrink_the_job_budget(
    api_client, sessionmaker, documents, monkeypatch
) -> None:
    monkeypatch.setattr("sightread.jobs.runner.VISION_BACKOFF_BASE_SECONDS", 0.001)
    respx.post(KANO_CHAT_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            _completion(VISION_MARKDOWN),
        ]
    )

    accepted = await _upload(api_client, documents["text_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "succeeded"
    assert respx.calls.call_count == 2


@respx.mock
async def test_a_transient_failure_is_retried_before_the_page_is_given_up(
    api_client, sessionmaker, documents, monkeypatch
) -> None:
    monkeypatch.setattr("sightread.jobs.runner.VISION_BACKOFF_BASE_SECONDS", 0.001)
    respx.post(KANO_CHAT_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.ConnectError("reset"),
            _completion(VISION_MARKDOWN),
        ]
    )

    accepted = await _upload(api_client, documents["text_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert result["errors"] == []
    assert result["markdown"]
    assert respx.calls.call_count == 3


@respx.mock
async def test_a_failure_that_stays_transient_fails_the_page_after_the_last_attempt(
    api_client, sessionmaker, documents, monkeypatch
) -> None:
    monkeypatch.setattr("sightread.jobs.runner.VISION_BACKOFF_BASE_SECONDS", 0.001)
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(500))

    accepted = await _upload(api_client, documents["text_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "failed"
    assert status["error"] == "1 of 1 pages failed (page 1: upstream call failed)"
    assert respx.calls.call_count == 4


@respx.mock
async def test_a_failure_that_would_repeat_is_not_retried(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(400))

    await _upload(api_client, documents["text_pdf"])
    await _drain_queue(api_client, sessionmaker)

    assert respx.calls.call_count == 1


@respx.mock
async def test_a_page_the_model_has_nothing_to_say_about_is_blank_not_failed(
    api_client, sessionmaker, documents, monkeypatch
) -> None:
    """The blank verso before a book chapter: the prompt drops its page number, and some
    servers say "nothing" as a null content rather than an empty string."""
    monkeypatch.setattr("sightread.jobs.runner.VISION_BACKOFF_BASE_SECONDS", 0.001)
    blank = httpx.Response(
        200, json={"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}
    )
    respx.post(KANO_CHAT_URL).mock(side_effect=[blank, _completion(VISION_MARKDOWN)])

    accepted = await _upload(api_client, documents["mixed_pdf"], pages="1,2")
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert result["errors"] == []
    assert result["markdown"]
    # A finished, empty answer is taken at its word — no retry.
    assert respx.calls.call_count == 2


@respx.mock
async def test_an_ambiguous_empty_completion_is_blank_only_after_every_attempt(
    api_client, sessionmaker, documents, monkeypatch
) -> None:
    monkeypatch.setattr("sightread.jobs.runner.VISION_BACKOFF_BASE_SECONDS", 0.001)
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json={"choices": []}))

    accepted = await _upload(api_client, documents["mixed_pdf"], pages="1")
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "succeeded"
    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert result["errors"] == []
    assert respx.calls.call_count == 4


@respx.mock
async def test_events_replay_the_final_state_of_a_terminal_job(
    api_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    accepted = await _upload(api_client, documents["text_pdf"])
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    frames = []
    async with api_client.stream("GET", f"/v1/jobs/{job_id}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            frames.append(line)

    body = "\n".join(frames)
    assert "event: progress" in body
    assert "event: done" in body
    payload = json.loads(body.split("event: done\ndata: ")[1].splitlines()[0])
    assert payload["meta"]["cached"] is False
    assert payload["markdown"]


@postgres_only
@respx.mock
async def test_the_event_stream_follows_a_job_it_is_already_watching(
    make_client, sessionmaker, documents
) -> None:
    """The LISTEN/NOTIFY wakeup path, which only exists on PostgreSQL."""
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    client = await _authorize(make_client(database_url=DATABASE_URL), sessionmaker)
    job_id = (await _upload(client, documents["text_pdf"])).json()["job_id"]

    async def collect() -> list[str]:
        frames: list[str] = []
        async with client.stream("GET", f"/v1/jobs/{job_id}/events") as response:
            async for line in response.aiter_lines():
                frames.append(line)
        return frames

    watching = asyncio.create_task(collect())
    await asyncio.sleep(0.2)
    await _drain_queue(client, sessionmaker)

    body = "\n".join(await asyncio.wait_for(watching, timeout=20))
    assert "event: progress" in body
    assert "event: done" in body


async def test_a_queued_job_fails_cleanly_when_kano_proxy_is_unconfigured_by_run_time(
    api_client, make_client, sessionmaker, documents
) -> None:
    accepted = await _upload(api_client, documents["text_pdf"])
    assert accepted.status_code == 202

    # The worker restarted without the operator's key.
    await _drain_queue(make_client(kano_proxy_api_key=""), sessionmaker)

    status = (await api_client.get(f"/v1/jobs/{accepted.json()['job_id']}")).json()
    assert status["status"] == "failed"
    assert status["error"] == "Kano Proxy is not configured on this deployment"
