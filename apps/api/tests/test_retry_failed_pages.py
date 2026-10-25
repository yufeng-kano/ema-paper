"""Failed pages: every page or nothing, a blocked page on the fallback model, and a retry
that sends only the pages without a result (docs/jobs.md § Retrying failed pages,
docs/parsing.md § Fallback model). Every upstream call is stubbed (docs/testing.md § Cost
safety).
"""

from __future__ import annotations

import io
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select, update

from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import CreditPeriod, Document, Job, JobPage, UsageLog, User
from sightread.workspace.documents import original_of
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, KANO_CHAT_URL
from tests.test_library import MODEL, _ready_to_parse
from tests.test_library_trash import _fail_next_commit
from tests.test_parse_end_to_end import VISION_MARKDOWN, _drain_queue

FALLBACK = "vendor/fallback-model"


def _answer(content: str | None = VISION_MARKDOWN, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "cost": 0.0015},
        },
    )


class Upstream:
    """The stubbed upstream: an answer per (model, page), a transcription for the rest,
    and every call it took, in order. The page is read from the prompt's placeholder."""

    def __init__(self, answers: dict[tuple[str, int], Callable[[], httpx.Response]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"][0]["text"]
        page = int(re.search(r"sightread://p(\d+)/", prompt).group(1))
        self.calls.append((body["model"], page))
        answer = self.answers.get((body["model"], page))
        return answer() if answer else _answer()


@pytest.fixture
async def library(make_client, tmp_path) -> AsyncClient:
    """A signed-in client that can parse, whose library keeps originals under tmp_path."""
    # The deployment names a fallback, the model a retry may send failed pages to.
    client = make_client(workspaces_dir=str(tmp_path / "ws"), parse_fallback_model=FALLBACK)
    await _ready_to_parse(client)
    return client


async def _upload(client: AsyncClient, path: Path, media_type: str = "application/pdf") -> dict:
    response = await client.post(
        "/api/library/documents",
        files={"file": (path.name, path.read_bytes(), media_type)},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _row(client: AsyncClient, document_id: int) -> dict:
    documents = (await client.get("/api/library")).json()["documents"]
    return next(row for row in documents if row["id"] == document_id)


async def _retry(client: AsyncClient, document_id: int, **body) -> httpx.Response:
    return await client.post(
        f"/api/library/documents/{document_id}/retry", json=body, headers=CSRF_HEADERS
    )


async def _with_fallback(make_client, tmp_path, fallback: str | None, auto_fallback: bool):
    """A library on a deployment that names `fallback`, the reader's switch set."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"), parse_fallback_model=fallback or "")
    await _ready_to_parse(client)
    switched = await client.put(
        "/api/agents/parse/settings", json={"auto_fallback": auto_fallback}, headers=CSRF_HEADERS
    )
    assert switched.status_code == 200, switched.text
    return client


# --- every page or nothing, and the retry ---------------------------------------------------


@respx.mock
async def test_a_retry_sends_only_the_failed_pages_and_keeps_the_rest(
    library, sessionmaker, documents
) -> None:
    upstream = Upstream({(MODEL, 2): lambda: httpx.Response(400)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    uploaded = await _upload(library, documents["mixed_pdf"])
    await _drain_queue(library, sessionmaker)

    # One page failed, so the document failed: nothing to open, and the row says how many
    # pages a retry would send.
    row = await _row(library, uploaded["id"])
    assert (row["status"], row["failed_pages"]) == ("failed", 1)
    assert row["error"] == "1 of 3 pages failed (page 2: upstream call failed)"
    result = await library.get(f"/api/library/documents/{uploaded['id']}/result")
    assert result.status_code == 404
    pages = (await library.get(f"/api/library/documents/{uploaded['id']}/pages")).json()
    assert pages["pages"] == [
        {"page": 1, "status": "succeeded", "reason": None, "model": MODEL},
        {"page": 2, "status": "failed", "reason": "upstream call failed", "model": MODEL},
        {"page": 3, "status": "succeeded", "reason": None, "model": MODEL},
    ]
    # A failed attempt's crops are discarded with it.
    figures = Path(library.app.state.settings.figures_dir) / uploaded["job_id"]
    assert not figures.exists()

    upstream.calls.clear()
    retried = await _retry(library, uploaded["id"], model=FALLBACK)
    assert retried.status_code == 202, retried.text
    assert retried.json() == {"job_id": uploaded["job_id"], "status": "queued"}
    row = await _row(library, uploaded["id"])
    assert (row["status"], row["pages_done"], row["error"], row["failed_pages"]) == (
        "queued",
        2,
        None,
        None,
    )
    await _drain_queue(library, sessionmaker)

    # Only the failed page went upstream, to the model the retry named.
    assert upstream.calls == [(FALLBACK, 2)]
    assert (await _row(library, uploaded["id"]))["status"] == "succeeded"
    result = (await library.get(f"/api/library/documents/{uploaded['id']}/result")).json()
    assert [page["model"] for page in result["pages"]] == [MODEL, FALLBACK, MODEL]
    assert result["errors"] == []
    assert result["markdown"].count("<!-- page:") == 3
    # The kept pages were cropped again from their stored markdown, beside the new one.
    for page_no in (1, 2, 3):
        assert (figures / f"p{page_no}_0_0_1000_1000.png").is_file()
    # The copy the retry ran on went with its terminal state; the original stays.
    assert list(Path(library.app.state.settings.upload_dir).iterdir()) == []
    async with sessionmaker() as db:
        document = await db.get(Document, uploaded["id"])
        root = user_root(library.app.state.settings.workspaces_dir, document.user_id)
        usage = (
            await db.execute(
                select(UsageLog.model).where(UsageLog.job_id == uuid.UUID(uploaded["job_id"]))
            )
        ).all()
    assert original_of(root, uploaded["id"]) is not None
    # The 400 billed nothing; each answer is logged under the model that gave it.
    assert sorted(model for (model,) in usage) == [FALLBACK, MODEL, MODEL]


@respx.mock
async def test_a_retry_takes_the_credits_back_and_the_document_is_charged_once(
    make_client, tmp_path, sessionmaker, documents
) -> None:
    """docs/billing.md § A parse: a failure releases the parse's credits, and a retry
    reserves them again — refused 402, with nothing requeued, when the balance cannot
    cover them — so a job that keeps failing costs nothing and the run that succeeds is
    the one charge."""
    client = make_client(
        workspaces_dir=str(tmp_path / "ws"),
        parse_fallback_model=FALLBACK,
        free_credits_per_period=3,
    )
    await _ready_to_parse(client)
    respx.post(KANO_CHAT_URL).mock(side_effect=Upstream({(MODEL, 2): lambda: httpx.Response(400)}))
    uploaded = await _upload(client, documents["mixed_pdf"])
    await _drain_queue(client, sessionmaker)

    async def spent() -> int:
        return (await client.get("/api/billing")).json()["period"]["spent"]

    async def spend(credits: int) -> None:
        async with sessionmaker() as db:
            await db.execute(update(CreditPeriod).values(spent=credits))
            await db.commit()

    assert (await _row(client, uploaded["id"]))["status"] == "failed"
    assert await spent() == 0
    # A retry on the same model takes the three pages' credits back; failing again, it
    # releases them again.
    assert (await _retry(client, uploaded["id"])).status_code == 202
    assert await spent() == 3
    await _drain_queue(client, sessionmaker)
    assert (await _row(client, uploaded["id"]))["status"] == "failed"
    assert await spent() == 0

    # With one credit spent elsewhere the balance cannot cover the parse: refused, and the
    # job, its credits and the upload directory are as they were.
    await spend(1)
    refused = await _retry(client, uploaded["id"], model=FALLBACK)
    assert refused.status_code == 402, refused.text
    assert (refused.json()["error"]["need"], refused.json()["error"]["remaining"]) == (3, 2)
    assert (await _row(client, uploaded["id"]))["status"] == "failed"
    assert await spent() == 1
    assert list(Path(client.app.state.settings.upload_dir).iterdir()) == []

    await spend(0)
    assert (await _retry(client, uploaded["id"], model=FALLBACK)).status_code == 202
    await _drain_queue(client, sessionmaker)
    row = await _row(client, uploaded["id"])
    assert (row["status"], row["credits"]) == ("succeeded", 3)
    assert await spent() == 3


@respx.mock
async def test_only_a_failed_parse_with_its_original_can_be_retried(
    library, make_client, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=Upstream({}))
    uploaded = await _upload(library, documents["png"], "image/png")
    # Queued, then succeeded: neither has failed pages to send.
    assert (await _retry(library, uploaded["id"])).status_code == 409
    await _drain_queue(library, sessionmaker)
    assert (await _retry(library, uploaded["id"])).status_code == 409

    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(uploaded["job_id"]))
        job.status = "failed"
        root = user_root(library.app.state.settings.workspaces_dir, job.user_id)
        other = User(google_sub="stranger", email="stranger@example.com")
        db.add(other)
        await db.flush()
        token = await create_session(db, other)
        await db.commit()
    # Another account's document is not there at all.
    stranger = make_client(workspaces_dir=library.app.state.settings.workspaces_dir)
    stranger.cookies.set(SESSION_COOKIE, token)
    assert (await _retry(stranger, uploaded["id"])).status_code == 404
    assert (await stranger.get(f"/api/library/documents/{uploaded['id']}/pages")).status_code == 404
    # A failed parse whose original is gone has nothing to parse again.
    original = original_of(root, uploaded["id"])
    assert original is not None
    original.unlink()
    refused = await _retry(library, uploaded["id"])
    assert refused.status_code == 409
    assert "upload the file again" in refused.text
    # A markdown document has no parse at all.
    written = await library.post(
        "/api/library/documents/new", json={"name": "notes"}, headers=CSRF_HEADERS
    )
    assert written.status_code == 201, written.text
    assert (await _retry(library, written.json()["id"])).status_code == 404
    assert (await _row(library, written.json()["id"]))["failed_pages"] is None


@respx.mock
async def test_a_failed_parse_in_the_trash_is_retried_only_once_it_is_put_back(
    library, sessionmaker, documents
) -> None:
    """A file in the Trash is not opened or parsed again (docs/api.md): its pages, their
    pictures and the retry answer 404 until Put Back. Every answer that carries the file
    still says how many of its pages failed."""
    respx.post(KANO_CHAT_URL).mock(side_effect=Upstream({(MODEL, 2): lambda: httpx.Response(400)}))
    uploaded = await _upload(library, documents["mixed_pdf"])
    await _drain_queue(library, sessionmaker)
    document_id = uploaded["id"]

    trashed = await library.post(
        f"/api/library/documents/{document_id}/trash", headers=CSRF_HEADERS
    )
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["failed_pages"] == 1
    trash = (await library.get("/api/library")).json()["trash"]["documents"]
    assert [(row["id"], row["failed_pages"]) for row in trash] == [(document_id, 1)]
    for path in ("pages", "pages/2/image"):
        answer = await library.get(f"/api/library/documents/{document_id}/{path}")
        assert answer.status_code == 404, path
    assert (await _retry(library, document_id)).status_code == 404

    restored = await library.post(
        f"/api/library/documents/{document_id}/restore", headers=CSRF_HEADERS
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["failed_pages"] == 1
    assert (await _retry(library, document_id, model=FALLBACK)).status_code == 202
    await _drain_queue(library, sessionmaker)
    assert (await _row(library, document_id))["status"] == "succeeded"


@respx.mock
@pytest.mark.parametrize("landed", [True, False])
async def test_a_retry_whose_commit_raised_keeps_its_copy_only_if_it_landed(
    library, sessionmaker, documents, monkeypatch, landed
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=Upstream({(MODEL, 2): lambda: httpx.Response(400)}))
    uploaded = await _upload(library, documents["mixed_pdf"])
    await _drain_queue(library, sessionmaker)

    _fail_next_commit(monkeypatch, landed=landed)
    response = await _retry(library, uploaded["id"])
    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(uploaded["job_id"]))
    copies = list(Path(library.app.state.settings.upload_dir).glob(f"{job.id}.retry-*"))
    if landed:
        # The acknowledgement was lost, not the retry: it stands, on its copy.
        assert response.status_code == 202, response.text
        assert job.status == "queued"
        assert [str(copy) for copy in copies] == [job.source_path]
    else:
        assert response.status_code == 409
        assert response.json()["error"]["message"] == (
            "The retry could not be queued; nothing changed"
        )
        assert job.status == "failed"
        assert copies == []


@respx.mock
async def test_a_page_already_transcribed_is_not_sent_again(
    library, sessionmaker, documents
) -> None:
    """A requeued attempt — a shutdown, a dead worker — keeps the pages it finished: the
    next one transcribes only the rest, and crops the kept ones again
    (docs/jobs.md § Queue)."""
    upstream = Upstream({})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    uploaded = await _upload(library, documents["mixed_pdf"])
    async with sessionmaker() as db:
        job_id = uuid.UUID(uploaded["job_id"])
        db.add_all(
            [
                JobPage(
                    job_id=job_id,
                    page_no=page_no,
                    method="vision",
                    status="succeeded",
                    markdown=VISION_MARKDOWN.replace("sightread://p9/", f"sightread://p{page_no}/"),
                    model=MODEL,
                )
                for page_no in (1, 3)
            ]
        )
        await db.execute(update(Job).where(Job.id == job_id).values(pages_done=2))
        await db.commit()
    await _drain_queue(library, sessionmaker)

    assert upstream.calls == [(MODEL, 2)]
    result = (await library.get(f"/api/library/documents/{uploaded['id']}/result")).json()
    assert result["markdown"].count("Body text.") == 3
    figures = Path(library.app.state.settings.figures_dir) / uploaded["job_id"]
    for page_no in (1, 2, 3):
        assert (figures / f"p{page_no}_0_0_1000_1000.png").is_file()


# --- the fallback model ---------------------------------------------------------------------


@respx.mock
async def test_a_blocked_page_goes_to_the_fallback_when_it_is_switched_on(
    make_client, tmp_path, sessionmaker, documents
) -> None:
    upstream = Upstream({(MODEL, 1): lambda: _answer("", "content_filter")})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    library = await _with_fallback(make_client, tmp_path, FALLBACK, True)
    uploaded = await _upload(library, documents["png"], "image/png")
    await _drain_queue(library, sessionmaker)

    assert upstream.calls == [(MODEL, 1), (FALLBACK, 1)]
    result = (await library.get(f"/api/library/documents/{uploaded['id']}/result")).json()
    assert [page["model"] for page in result["pages"]] == [FALLBACK]
    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(uploaded["job_id"]))
        usage = (await db.execute(select(UsageLog.model).where(UsageLog.job_id == job.id))).all()
    # The snapshot the job ran with, and both calls billed: the block was answered too.
    assert (job.model, job.fallback_model) == (MODEL, FALLBACK)
    assert sorted(model for (model,) in usage) == [FALLBACK, MODEL]


@respx.mock
@pytest.mark.parametrize(
    ("finish_reason", "reason", "settings"),
    [
        # Blocked, with no fallback switched on: the page fails, and the call is billed.
        ("content_filter", "blocked", (FALLBACK, False)),
        # Blocked, with the deployment naming no fallback: nowhere else to go.
        ("content_filter", "blocked", (None, True)),
        # Blocked, with the fallback the model the job already runs on: not asked twice.
        ("content_filter", "blocked", (MODEL, True)),
        # Cut off is the page's length, not the model's refusal: no fallback for it.
        ("length", "cut off", (FALLBACK, True)),
    ],
)
async def test_a_page_the_model_stopped_fails_the_job_after_one_call(
    make_client, tmp_path, sessionmaker, documents, finish_reason, reason, settings
) -> None:
    upstream = Upstream({(MODEL, 1): lambda: _answer("half a page", finish_reason)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    library = await _with_fallback(make_client, tmp_path, *settings)
    uploaded = await _upload(library, documents["png"], "image/png")
    await _drain_queue(library, sessionmaker)

    assert upstream.calls == [(MODEL, 1)]
    row = await _row(library, uploaded["id"])
    assert (row["status"], row["error"]) == ("failed", f"1 of 1 pages failed (page 1: {reason})")
    async with sessionmaker() as db:
        billed = (
            await db.execute(
                select(UsageLog.model).where(UsageLog.job_id == uuid.UUID(uploaded["job_id"]))
            )
        ).all()
    assert billed == [(MODEL,)]


@respx.mock
async def test_a_blocked_call_stays_billed_when_its_fallback_aborts_the_job(
    make_client, tmp_path, sessionmaker, documents
) -> None:
    upstream = Upstream(
        {
            (MODEL, 1): lambda: _answer("", "content_filter"),
            (FALLBACK, 1): lambda: httpx.Response(401),
        }
    )
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    library = await _with_fallback(make_client, tmp_path, FALLBACK, True)
    uploaded = await _upload(library, documents["png"], "image/png")
    await _drain_queue(library, sessionmaker)

    assert upstream.calls == [(MODEL, 1), (FALLBACK, 1)]
    row = await _row(library, uploaded["id"])
    assert (row["status"], row["error"]) == ("failed", "the upstream rejected the stored key")
    async with sessionmaker() as db:
        billed = (
            await db.execute(
                select(UsageLog.model).where(UsageLog.job_id == uuid.UUID(uploaded["job_id"]))
            )
        ).all()
    # The rejected key answered nothing; the block was an answer, and the key paid for it.
    assert billed == [(MODEL,)]


# --- settings --------------------------------------------------------------------------------


async def test_the_fallback_switch_is_the_parse_agents_alone(
    library, make_client, tmp_path
) -> None:
    """The switch is the reader's; the fallback model is the deployment's, a fact
    (docs/api.md § GET /api/agents)."""
    saved = await library.put(
        "/api/agents/parse/settings", json={"auto_fallback": True}, headers=CSRF_HEADERS
    )
    assert (saved.json()["fallback_model"], saved.json()["auto_fallback"]) == (FALLBACK, True)
    for agent in ("chat", "check"):
        refused = await library.put(
            f"/api/agents/{agent}/settings", json={"auto_fallback": True}, headers=CSRF_HEADERS
        )
        assert refused.status_code == 400, agent
    switch = await library.put(
        "/api/agents/parse/settings", json={"auto_fallback": None}, headers=CSRF_HEADERS
    )
    assert switch.status_code == 400

    # A deployment naming no fallback: the switch is kept, with nowhere to send a page.
    unnamed = await _with_fallback(make_client, tmp_path, None, True)
    parse = next(
        a for a in (await unnamed.get("/api/agents")).json()["agents"] if a["name"] == "parse"
    )
    assert (parse["settings"]["fallback_model"], parse["settings"]["auto_fallback"]) == (None, True)


# --- the page previews -----------------------------------------------------------------------


async def test_a_page_of_the_original_is_drawn_small_for_the_retry_dialog(
    library, sessionmaker, documents
) -> None:
    uploaded = await _upload(library, documents["mixed_pdf"])
    # The upload is the job's until it runs; the second row of the same bytes keeps its
    # own original at once (docs/workspace.md § Layout).
    uploaded = await _upload(library, documents["mixed_pdf"])
    response = await library.get(f"/api/library/documents/{uploaded['id']}/pages/2/image")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    with Image.open(io.BytesIO(response.content)) as drawn:
        assert max(drawn.size) <= library.app.state.settings.page_preview_long_edge
    for page in (0, 4):
        missing = await library.get(f"/api/library/documents/{uploaded['id']}/pages/{page}/image")
        assert missing.status_code == 404
