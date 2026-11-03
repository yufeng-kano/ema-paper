"""The chat agent's `convert_file` tool (docs/chat.md § Tools, *A conversion runs inside the
turn*; docs/jobs.md § Converting a file; docs/billing.md § A chat turn): a kept PDF or image
becomes a document exactly as the Files page converts one, its parse driven inside the turn
against the stubbed vision upstream, charged on the job and never on the turn. No test
reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

from sightread.agents import chat as chat_agent
from sightread.billing import ledger
from sightread.db.models import CreditEntry, Document, Job, utcnow
from sightread.errors import ApiError
from sightread.jobs import chat_runner, runner, sweeper
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
from sightread.workspace.documents import original_of
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, KANO_CHAT_URL, convert_on_upload
from tests.test_chat import _run_queued_turn
from tests.test_chat_extensions import new_conversation, one_call, send, stub_messages
from tests.test_chat_web import recorded, tool_results, turn_credits, web_client

VISION_MARKDOWN = "## Page one\n\nBody text."
DONE = ([{"type": "text", "text": "Read."}], "end_turn")


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 90, "completion_tokens": 12},
        },
    )


async def _ready(make_client, tmp_path, **overrides) -> httpx.AsyncClient:
    """A chatting client whose format check is off: the check speaks the Messages route the
    turn is stubbed on, and a reader may switch it off (docs/parsing.md § Format check)."""
    client = await web_client(make_client, tmp_path, **overrides)
    off = await client.put(
        "/api/agents/check/settings", json={"auto_check": False}, headers=CSRF_HEADERS
    )
    assert off.status_code == 200, off.text
    return client


async def _keep(
    client: httpx.AsyncClient, name: str, data: bytes, media_type: str, folder_id: int | None
) -> dict:
    """A Files upload kept as a file: `auto_convert` is off by default (docs/agents.md)."""
    response = await client.post(
        "/api/library/documents",
        files={"file": (name, data, media_type)},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "file"
    return response.json()


async def _papers(client: httpx.AsyncClient) -> int:
    """The `Papers` folder the seeded document sits in."""
    library = (await client.get("/api/library")).json()
    return next(folder["id"] for folder in library["folders"] if folder["name"] == "Papers")


async def _run(client: httpx.AsyncClient, *calls: dict) -> tuple[int, list[dict]]:
    """One turn of the given `convert_file` calls, then an answer; its tool results."""
    stub_messages(
        [*(one_call(f"c{i}", "convert_file", call) for i, call in enumerate(calls)), DONE]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Read the other paper")
    await _run_queued_turn(client)
    return conversation_id, await tool_results(client, conversation_id)


def test_the_tool_is_declared_only_with_a_parse_model() -> None:
    """docs/chat.md § Tools: after the web tools, not strict — a path and a flag the tool
    checks itself (§ Upstream); a deployment without a parse model never declares it, and a
    history that names it is refused like a web tool's."""
    assert chat_agent.convert_tools(parse=False) == ()
    (tool,) = chat_agent.convert_tools(parse=True)
    assert tool["name"] == "convert_file" and "strict" not in tool
    assert tool["input_schema"]["additionalProperties"] is False
    assert tool["input_schema"]["required"] == ["path"]
    assert tool["input_schema"]["properties"]["retry"]["type"] == "boolean"
    assert "convert_file" in chat_runner.UNDECLARABLE_TOOLS
    assert "convert_file" in chat_runner.MUTATING_TOOLS
    rendered = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT, None, tools=["view_page", "convert_file"]
    )
    assert "- convert_file:" in rendered
    without = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT, None, tools=["view_page"]
    )
    assert "- convert_file:" not in without
    # The figure rule: a block link, and web addresses as ordinary links (docs/chat.md).
    assert "](#3-method~Figure 3: Overview of the method)" in rendered
    assert "[title](https://…)" in rendered


@respx.mock
async def test_convert_file_turns_a_kept_file_into_a_document_inside_the_turn(
    make_client, tmp_path, documents
) -> None:
    """docs/chat.md § Tools, *A conversion runs inside the turn*: the file becomes the
    parse's document where it was, the parse runs to its end within the call, the credits
    are the job's, and the document is there to read before the next model call."""
    vision = respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    client = await _ready(make_client, tmp_path)
    settings = client.app.state.settings
    png = documents["png"].read_bytes()
    kept = await _keep(client, "tiny.png", png, "image/png", await _papers(client))
    conversation_id, (first, second) = await _run(
        client, {"path": "Papers/tiny.png"}, {"path": "/workspace/files/Papers/tiny.png"}
    )
    assert first["is_error"] is False, first["output"]
    assert first["output"].startswith(
        f"Converted tiny.png (1 page, {settings.parse_credits_per_page:,} credits) into a "
        "document at /workspace/files/Papers/tiny.png/doc.md; read it with "
        "str_replace_based_edit_tool."
    )
    # Converted, the row is a document: the second call is told where to read it.
    assert second["is_error"] is True
    assert second["output"] == (
        "Papers/tiny.png is a document already: read /workspace/files/Papers/tiny.png/doc.md."
    )
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, kept["id"])
        job = await db.get(Job, row.job_id)
        credits = (await ledger.credits_by_job(db, [job.id])).get(job.id)
    assert (row.kind, job.status, job.page_count, job.check_model) == (
        "parse",
        "succeeded",
        1,
        None,
    )
    assert job.retain_source is True
    assert credits == settings.parse_credits_per_page
    assert vision.call_count == 1
    # The document is written, under the file's own name, before the turn goes on.
    root = user_root(settings.workspaces_dir, client.user_id)
    assert "Body text." in (root / "files" / "Papers" / "tiny.png" / "doc.md").read_text()
    assert not (root / "files" / "Papers" / "tiny.png").is_file()
    assert await recorded(client, "convert_file") == 1
    # The turn is charged on its tokens alone (docs/billing.md § A chat turn).
    assert await turn_credits(client, conversation_id) == ("succeeded", 1)
    listed = (await client.get("/api/library")).json()["documents"]
    shown = next(doc for doc in listed if doc["id"] == kept["id"])
    assert (shown["kind"], shown["status"], shown["name"]) == ("image", "succeeded", "tiny.png")


@respx.mock
async def test_a_file_converted_before_costs_nothing_again(
    make_client, tmp_path, documents
) -> None:
    """docs/billing.md: the dedup cache answers a file whose bytes were converted on this
    model — no parse runs, no credits move — and the document is written all the same."""
    vision = respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    client = await _ready(make_client, tmp_path)
    settings = client.app.state.settings
    png = documents["png"].read_bytes()
    papers = await _papers(client)
    await _keep(client, "tiny.png", png, "image/png", papers)
    again = await _keep(client, "again.png", png, "image/png", None)
    _, (first, second) = await _run(client, {"path": "Papers/tiny.png"}, {"path": "again.png"})
    assert first["is_error"] is False and second["is_error"] is False, (first, second)
    assert second["output"].startswith(
        "Converted again.png (1 page, no credits: the same file was converted before) into a "
        "document at /workspace/files/again.png/doc.md;"
    )
    assert vision.call_count == 1
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, again["id"])
        jobs = (await db.execute(select(Job).where(Job.status == "succeeded"))).scalars().all()
        charged = await ledger.credits_by_job(db, [job.id for job in jobs])
    assert row.kind == "parse"
    # One parse was paid for, whatever the cache answered with.
    assert sum(value or 0 for value in charged.values()) == settings.parse_credits_per_page
    root = user_root(settings.workspaces_dir, client.user_id)
    assert "Body text." in (root / "files" / "again.png" / "doc.md").read_text()
    assert await recorded(client, "convert_file") == 2


@respx.mock
async def test_a_trashed_twin_does_not_hide_the_live_file(make_client, tmp_path, documents) -> None:
    """docs/workspace.md § Files: what is in the Trash takes no name, so a trashed row of
    the same name and place is not the file the agent names, and the live one converts."""
    respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    client = await _ready(make_client, tmp_path)
    png = documents["png"].read_bytes()
    first = await _keep(client, "tiny.png", png, "image/png", None)
    trashed = await client.post(f"/api/library/documents/{first['id']}/trash", headers=CSRF_HEADERS)
    assert trashed.status_code == 200, trashed.text
    again = await _keep(client, "tiny.png", png, "image/png", None)
    assert again["name"] == "tiny.png" and again["id"] != first["id"]
    _, (result,) = await _run(client, {"path": "tiny.png"})
    assert result["is_error"] is False, result["output"]
    async with client.app.state.sessionmaker() as db:
        live = await db.get(Document, again["id"])
        gone = await db.get(Document, first["id"])
    assert live.kind == "parse"
    assert (gone.kind, gone.trashed_at is not None) == ("file", True)


@respx.mock
async def test_a_document_whose_parse_still_waits_is_driven_too(
    make_client, tmp_path, documents
) -> None:
    """docs/chat.md § Tools: an upload converting on arrival sits queued behind the turn;
    named to `convert_file`, its parse is driven inside the turn the same way and read —
    neither charged nor recorded again, since the upload's conversion is the one that runs."""
    vision = respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    client = await _ready(make_client, tmp_path)
    await convert_on_upload(client)
    uploaded = await client.post(
        "/api/library/documents",
        files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
        headers=CSRF_HEADERS,
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["status"] == "queued"
    _, (result,) = await _run(client, {"path": "tiny.png"})
    assert result["is_error"] is False, result["output"]
    assert result["output"].startswith("Converted tiny.png (1 page, ")
    assert vision.call_count == 1
    assert await recorded(client, "convert_file") == 0
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, uploaded.json()["id"])
        job = await db.get(Job, row.job_id)
    assert job.status == "succeeded"
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert "Body text." in (root / "files" / "tiny.png" / "doc.md").read_text()


@respx.mock
async def test_convert_file_refuses_what_is_not_a_file_to_convert(make_client, tmp_path) -> None:
    """docs/chat.md § Tools: a path that names nothing, a file that is not there, a kind
    the parse agent does not take and a document already are each refused with nothing
    changed, and none counts as a call."""
    client = await _ready(make_client, tmp_path)
    zipped = await _keep(client, "archive.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip", None)
    _, results = await _run(
        client,
        {"path": "../etc/passwd"},
        {"path": ""},
        {"path": "Papers/nope.pdf"},
        {"path": "Nowhere/tiny.png"},
        {"path": "archive.zip"},
        {"path": "Papers/paper.pdf"},
    )
    assert [result["is_error"] for result in results] == [True] * 6
    outputs = [result["output"] for result in results]
    assert outputs[0] == outputs[1]
    assert outputs[0].startswith("The path names no file:")
    assert outputs[2] == "No file at Papers/nope.pdf in the user's files."
    assert outputs[3] == "No file at Nowhere/tiny.png in the user's files."
    assert outputs[4].startswith("archive.zip was not converted: Only a PDF or an image")
    assert outputs[5] == (
        "Papers/paper.pdf is a document already: read /workspace/files/Papers/paper.pdf/doc.md."
    )
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, zipped["id"])
        jobs = (await db.execute(select(Job))).scalars().all()
    assert (row.kind, row.job_id) == ("file", None)
    assert len(jobs) == 1  # the seeded document's
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "files" / "archive.zip").is_file()
    assert await recorded(client, "convert_file") == 0


@respx.mock
async def test_convert_file_stops_at_the_balance_and_at_the_turn_cap(
    make_client, tmp_path, documents
) -> None:
    """docs/billing.md: a conversion the balance cannot cover is refused as the Files page
    refuses it, the file left a file; docs/chat.md § Tools: `CONVERT_FILE_MAX_PER_TURN`
    counts the calls, a refused one among them."""
    vision = respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    client = await _ready(
        make_client, tmp_path, convert_file_max_per_turn=1, parse_credits_per_page=10**9
    )
    kept = await _keep(client, "tiny.png", documents["png"].read_bytes(), "image/png", None)
    _, (first, second) = await _run(client, {"path": "tiny.png"}, {"path": "tiny.png"})
    assert first["is_error"] is True and first["output"].startswith("tiny.png was not converted: ")
    assert second["is_error"] is True
    assert second["output"] == "This turn has made its 1 conversions; answer with what you have."
    assert vision.call_count == 0
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, kept["id"])
        jobs = (await db.execute(select(Job))).scalars().all()
    assert (row.kind, row.job_id) == ("file", None)
    assert len(jobs) == 1
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "files" / "tiny.png").is_file()
    assert await recorded(client, "convert_file") == 0


@respx.mock
async def test_a_conversion_past_its_time_goes_back_on_the_queue(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/chat.md § Tools, *A conversion runs inside the turn*: a parse not done within
    `CONVERT_FILE_TIMEOUT_SECONDS` is put back on the queue keeping its pages, the tool
    says so, and the worker takes it after the turn — charged once."""
    monkeypatch.setattr(chat_runner, "CONVERT_POLL_SECONDS", 0.05)
    answered = 0

    async def slow_once(request: httpx.Request) -> httpx.Response:
        nonlocal answered
        answered += 1
        if answered == 1:
            await asyncio.sleep(5)
        return _completion(VISION_MARKDOWN)

    respx.post(KANO_CHAT_URL).mock(side_effect=slow_once)
    client = await _ready(make_client, tmp_path, convert_file_timeout_seconds=1.0)
    settings = client.app.state.settings
    kept = await _keep(client, "tiny.png", documents["png"].read_bytes(), "image/png", None)
    started = asyncio.get_running_loop().time()
    _, (result,) = await _run(client, {"path": "tiny.png"})
    assert asyncio.get_running_loop().time() - started < 4
    assert result["is_error"] is True
    assert result["output"].startswith(
        "The conversion of tiny.png is not finished after 1 seconds and goes on after this turn:"
    )
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, kept["id"])
        job = await db.get(Job, row.job_id)
    assert (row.kind, job.status, job.worker_generation) == ("parse", "queued", None)
    assert await recorded(client, "convert_file") == 1
    # The worker's next cycle finishes it.
    async with client.app.state.sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    assert claimed == job.id
    await run_job(client.app.state.sessionmaker, settings, claimed, asyncio.Semaphore(2))
    async with client.app.state.sessionmaker() as db:
        job = await db.get(Job, job.id)
        credits = (await ledger.credits_by_job(db, [job.id])).get(job.id)
    assert job.status == "succeeded" and answered == 2
    assert credits == settings.parse_credits_per_page


@pytest.mark.parametrize(
    ("raw", "segments"),
    [
        ("Papers/2017/attention.pdf", ["Papers", "2017", "attention.pdf"]),
        ("/workspace/files/Papers/a.pdf", ["Papers", "a.pdf"]),
        ("files/a.pdf", ["files", "a.pdf"]),
        (" Papers\\2017\\a.pdf ", ["Papers", "2017", "a.pdf"]),
        ("Papers//a.pdf/", ["Papers", "a.pdf"]),
        ("", None),
        ("/workspace/files/", None),
        ("../a.pdf", None),
        ("Papers/./a.pdf", None),
    ],
)
def test_the_path_is_read_as_the_files_page_shows_it(raw: str, segments: list[str] | None) -> None:
    assert chat_runner._library_segments(raw) == segments


async def _settled(client: httpx.AsyncClient, document_id: int) -> tuple[Document, Job, dict]:
    """The row, its job and what the library says of it."""
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Document, document_id)
        job = await db.get(Job, row.job_id)
    listed = (await client.get("/api/library")).json()["documents"]
    return row, job, next(shown for shown in listed if shown["id"] == document_id)


async def _worker_runs_next(client: httpx.AsyncClient) -> Job:
    """The worker's next claim, run to its end."""
    settings = client.app.state.settings
    async with client.app.state.sessionmaker() as db:
        claimed = await claim_next_job(db, settings.max_jobs_per_user)
    assert claimed is not None
    await run_job(client.app.state.sessionmaker, settings, claimed, asyncio.Semaphore(2))
    async with client.app.state.sessionmaker() as db:
        return await db.get(Job, claimed)


@respx.mock
async def test_a_failed_conversion_keeps_the_file_to_retry(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/jobs.md § Converting a file: a parse the upstream fails inside the turn fails
    whole, and the file is never lost to the error — kept as the failed document's original
    outside the tree, with no `source.*` left in a directory that has no `.meta.json` (the
    reconciliation would rename it and the quota count it twice) — and the Files page's
    retry makes it the document once the upstream is back, its shell copy in place."""
    monkeypatch.setattr(runner, "VISION_BACKOFF_BASE_SECONDS", 0.0)
    down = True

    def vision(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"}) if down else _completion(VISION_MARKDOWN)

    respx.post(KANO_CHAT_URL).mock(side_effect=vision)
    client = await _ready(make_client, tmp_path)
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    kept = await _keep(
        client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf", None
    )
    _, (first, second) = await _run(client, {"path": "paper.pdf"}, {"path": "paper.pdf"})
    assert first["is_error"] is True
    assert first["output"].startswith("The conversion of paper.pdf failed: ")
    assert second["output"].startswith("The conversion of paper.pdf failed earlier: ")
    row, job, shown = await _settled(client, kept["id"])
    assert (row.kind, job.status, shown["status"], shown["original"]) == (
        "parse",
        "failed",
        "failed",
        True,
    )
    assert job.source_deleted_at is not None
    assert original_of(root, kept["id"]) is not None
    # Reconciled at the turn's end: nothing of the failed parse stands in the tree.
    assert not (root / "files" / "paper.pdf").exists()

    down = False
    retried = await client.post(
        f"/api/library/documents/{kept['id']}/retry", json={}, headers=CSRF_HEADERS
    )
    assert retried.status_code == 202, retried.text
    assert (await _worker_runs_next(client)).status == "succeeded"
    names = {entry.name for entry in (root / "files" / "paper.pdf").iterdir()}
    assert {"doc.md", ".meta.json", "source.pdf"} <= names
    assert "_source.pdf" not in names


@respx.mock
async def test_the_agent_retries_a_failed_conversion_once_told_to(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/chat.md § Tools: a failed conversion answers that the original is kept and that
    a retry needs the user's yes; `retry` — a literal true only — puts the parse back
    through the Files page's own retry and drives it inside the turn, on the same job, and
    the document is charged once, by the run that delivers it. A document that stands is
    not retried."""
    monkeypatch.setattr(runner, "VISION_BACKOFF_BASE_SECONDS", 0.0)
    down = True

    def vision(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"}) if down else _completion(VISION_MARKDOWN)

    respx.post(KANO_CHAT_URL).mock(side_effect=vision)
    client = await _ready(make_client, tmp_path)
    settings = client.app.state.settings
    root = user_root(settings.workspaces_dir, client.user_id)
    kept = await _keep(
        client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf", None
    )
    _, (failed, loose) = await _run(
        client, {"path": "paper.pdf"}, {"path": "paper.pdf", "retry": "yes"}
    )
    assert failed["is_error"] is True
    assert "its original is kept" in failed["output"]
    assert "Call convert_file with retry: true only once the user says yes" in failed["output"]
    # Not a literal true: the failure is answered again, nothing queued.
    assert loose["output"].startswith("The conversion of paper.pdf failed earlier: ")
    _, job, _ = await _settled(client, kept["id"])
    assert job.status == "failed"
    job_id = job.id
    # The price of a retry is the whole conversion's, said in credits.
    async with client.app.state.sessionmaker() as db:
        reserved = (
            await db.execute(select(CreditEntry.amount).where(CreditEntry.job_id == job_id))
        ).scalar_one()
    assert f"reserves the whole conversion's {reserved:,} credits again" in failed["output"]
    assert await recorded(client, "convert_file") == 1

    down = False
    _, (retried,) = await _run(client, {"path": "paper.pdf", "retry": True})
    assert retried["is_error"] is False, retried["output"]
    assert retried["output"].startswith("Converted paper.pdf (")
    row, job, shown = await _settled(client, kept["id"])
    assert (row.job_id, job.status, shown["status"]) == (job_id, "succeeded", "succeeded")
    assert "Body text." in (root / "files" / "paper.pdf" / "doc.md").read_text()
    async with client.app.state.sessionmaker() as db:
        charged = await ledger.credits_by_job(db, [job_id])
    assert charged[job_id] == settings.parse_credits_per_page * job.page_count
    assert await recorded(client, "convert_file") == 2

    _, (again,) = await _run(client, {"path": "paper.pdf", "retry": True})
    assert again["output"] == (
        "paper.pdf is a document already: read /workspace/files/paper.pdf/doc.md."
    )
    assert await recorded(client, "convert_file") == 2


@respx.mock
async def test_a_retry_the_balance_or_the_original_cannot_carry_is_not_queued(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/billing.md § A chat turn: the agent's retry is refused as the Files page's is
    when the balance cannot cover it, the job left failed and nothing recorded; a document
    whose original is gone answers that it cannot be retried."""
    monkeypatch.setattr(runner, "VISION_BACKOFF_BASE_SECONDS", 0.0)
    vision = respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(503, json={}))
    client = await _ready(make_client, tmp_path)
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    kept = await _keep(
        client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf", None
    )
    await _run(client, {"path": "paper.pdf"})
    calls = vision.call_count

    async def broke(db, settings, job_id):
        raise ledger.InsufficientCredits(need=10, remaining=0, period_end=utcnow())

    monkeypatch.setattr(ledger, "reserve_again", broke)
    _, (refused,) = await _run(client, {"path": "paper.pdf", "retry": True})
    assert refused["is_error"] is True
    assert refused["output"].startswith("paper.pdf was not retried: Not enough credits")
    _, job, _ = await _settled(client, kept["id"])
    assert job.status == "failed"
    assert vision.call_count == calls
    assert await recorded(client, "convert_file") == 1

    original_of(root, kept["id"]).unlink()
    _, (gone,) = await _run(client, {"path": "paper.pdf"})
    assert gone["output"].endswith(
        "Its original was not kept, so it cannot be retried: the user can upload the file again."
    )


@respx.mock
async def test_a_stop_during_a_failed_finish_still_keeps_the_original(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/jobs.md § Retention: the finalization of a failed parse runs to its end like a
    result's. The reader's Stop lands while the failure files the original — a wait on the
    workspace lock, a large copy — and the file is still kept, so the retry works."""
    monkeypatch.setattr(chat_runner, "CONVERT_POLL_SECONDS", 0.05)
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
    filing = asyncio.Event()
    real_retain, real_cancelled = runner._retain_source, chat_runner._cancelled

    async def slow(*args, **kwargs) -> None:
        filing.set()
        await asyncio.sleep(0.5)
        await real_retain(*args, **kwargs)

    async def stopped(ctx) -> bool:
        # The Stop, as the turn's poll sees it, the moment the original is being filed.
        return filing.is_set() or await real_cancelled(ctx)

    monkeypatch.setattr(runner, "_retain_source", slow)
    monkeypatch.setattr(chat_runner, "_cancelled", stopped)
    client = await _ready(make_client, tmp_path)
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    kept = await _keep(
        client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf", None
    )
    _, (result,) = await _run(client, {"path": "paper.pdf"})
    assert result["output"].startswith("The conversion of paper.pdf failed: "), result["output"]
    _, job, shown = await _settled(client, kept["id"])
    assert (job.status, shown["original"]) == ("failed", True)
    assert original_of(root, kept["id"]) is not None
    retried = await client.post(
        f"/api/library/documents/{kept['id']}/retry", json={}, headers=CSRF_HEADERS
    )
    assert retried.status_code == 202, retried.text


@respx.mock
async def test_a_full_workspace_still_keeps_a_converted_files_original(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/workspace.md § Layout: the upload moved into place as the row's original is
    kept whatever the quota says — a converted file was counted in the tree, and the
    upload is the only copy there is."""
    monkeypatch.setattr(runner, "VISION_BACKOFF_BASE_SECONDS", 0.0)
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(503))
    client = await _ready(make_client, tmp_path)
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    kept = await _keep(
        client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf", None
    )

    async def full(*args, **kwargs) -> None:
        raise ApiError(409, "storage_full", "The workspace is at its quota")

    monkeypatch.setattr(runner, "ensure_within_quota", full)
    _, (result,) = await _run(client, {"path": "paper.pdf"})
    assert result["output"].startswith("The conversion of paper.pdf failed: ")
    assert original_of(root, kept["id"]) is not None


@respx.mock
async def test_the_sweep_files_an_original_the_worker_never_did(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/jobs.md § Retention: a worker that dies between a failed parse's terminal
    commit and its copies leaves the converted file in the upload directory alone; the
    sweep files it as the document's original instead of letting it age out — and its
    age sweep never takes the upload first, even when the filing fails again."""
    monkeypatch.setattr(runner, "VISION_BACKOFF_BASE_SECONDS", 0.0)
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(503))
    real = runner._retain_source

    async def died(*args, **kwargs) -> None:
        raise RuntimeError("the worker died here")

    monkeypatch.setattr(runner, "_retain_source", died)
    client = await _ready(make_client, tmp_path)
    settings = client.app.state.settings
    root = user_root(settings.workspaces_dir, client.user_id)
    kept = await _keep(
        client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf", None
    )
    await _run(client, {"path": "paper.pdf"})
    _, job, _ = await _settled(client, kept["id"])
    assert job.status == "failed" and job.source_deleted_at is None
    assert original_of(root, kept["id"]) is None

    # Two days on, the age sweep still keeps the upload of a retention still to run — one
    # that failed again in this very pass.
    upload = Path(job.source_path)
    os.utime(upload, (time.time() - 2 * settings.upload_max_age_seconds,) * 2)
    await sweeper.sweep_once(client.app.state.sessionmaker, settings)
    assert upload.is_file() and original_of(root, kept["id"]) is None

    monkeypatch.setattr(runner, "_retain_source", real)
    assert await sweeper.finish_retention(client.app.state.sessionmaker, settings) == 1
    _, job, shown = await _settled(client, kept["id"])
    assert job.source_deleted_at is not None and shown["original"] is True
    assert original_of(root, kept["id"]) is not None
    assert await sweeper.finish_retention(client.app.state.sessionmaker, settings) == 0


@respx.mock
async def test_the_sweeps_filing_leaves_no_lone_copy_in_the_tree(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/workspace.md § Layout: the sweep files the original of a succeeded parse whose
    worker died before its copies and its materialization, and puts no `source.*` in the
    tree — the directory has no `.meta.json`, and a lone copy there would turn into a
    folder's `_source.*`, counted twice; the document's first materialization makes it."""
    respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    real = runner._retain_source

    async def died(*args, **kwargs) -> None:
        raise RuntimeError("the worker died here")

    monkeypatch.setattr(runner, "_retain_source", died)
    monkeypatch.setattr(runner, "materialize_document", died)
    client = await _ready(make_client, tmp_path)
    settings = client.app.state.settings
    root = user_root(settings.workspaces_dir, client.user_id)
    kept = await _keep(client, "tiny.png", documents["png"].read_bytes(), "image/png", None)
    converted = await client.post(
        f"/api/library/documents/{kept['id']}/convert", json={}, headers=CSRF_HEADERS
    )
    assert converted.status_code == 200, converted.text
    job = await _worker_runs_next(client)
    assert job.status == "succeeded" and job.source_deleted_at is None

    monkeypatch.setattr(runner, "_retain_source", real)
    assert await sweeper.finish_retention(client.app.state.sessionmaker, settings) == 1
    assert original_of(root, kept["id"]) is not None
    assert not (root / "files" / "tiny.png").exists()
    opened = await client.post(
        f"/api/library/documents/{kept['id']}/conversations", json={}, headers=CSRF_HEADERS
    )
    assert opened.status_code == 201, opened.text
    names = {entry.name for entry in (root / "files" / "tiny.png").iterdir()}
    assert {".meta.json", "source.png"} <= names and "_source.png" not in names


@respx.mock
async def test_a_stop_before_the_claim_starts_no_run(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/chat.md § Tools, *A conversion runs inside the turn*: no job is claimed once a
    Stop or the turn's end has come — a run started then would only be cancelled, its
    first page calls paid — and the job waits on the queue for the worker."""
    vision = respx.post(KANO_CHAT_URL).mock(return_value=_completion(VISION_MARKDOWN))
    client = await _ready(make_client, tmp_path)
    kept = await _keep(client, "tiny.png", documents["png"].read_bytes(), "image/png", None)
    converted = await client.post(
        f"/api/library/documents/{kept['id']}/convert", json={}, headers=CSRF_HEADERS
    )
    assert converted.status_code == 200, converted.text
    row, job, _ = await _settled(client, kept["id"])
    claims: list[uuid.UUID] = []

    async def spy(db, job_id, generation) -> bool:
        claims.append(job_id)
        return False

    monkeypatch.setattr(chat_runner, "claim_job", spy)
    ctx = SimpleNamespace(
        settings=client.app.state.settings,
        sessionmaker=client.app.state.sessionmaker,
        convert_stop=asyncio.Event(),
        turn=SimpleNamespace(id=uuid.uuid4()),
        generation=None,
        render_slots=asyncio.Semaphore(1),
        inline_parse=None,
    )
    ctx.convert_stop.set()
    assert await chat_runner._drive_parse(ctx, job.id) is None
    assert claims == [] and vision.call_count == 0
    _, job, _ = await _settled(client, row.id)
    assert job.status == "queued"


async def test_a_failing_look_for_a_stop_cancels_the_run(monkeypatch) -> None:
    """docs/chat.md § Tools: a wait that fails in any other way than a cancel — the look for
    a Stop meets a database error — cancels the run as a Stop does, rather than leave it
    going unwatched beside the worker's next claim."""
    monkeypatch.setattr(chat_runner, "CONVERT_POLL_SECONDS", 0.01)

    async def broken(ctx) -> bool:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(chat_runner, "_cancelled", broken)
    run = asyncio.create_task(asyncio.sleep(30))
    ctx = SimpleNamespace(convert_stop=asyncio.Event())
    with pytest.raises(RuntimeError):
        await chat_runner._await_inline_parse(ctx, run, time.monotonic() + 30)
    assert run.cancelled()


async def test_a_failed_finish_outlasts_every_cancel_of_its_caller() -> None:
    """docs/jobs.md § Retention: a terminal state's finalization runs to its end whatever
    cancels its caller — a turn's end cancels the run it drives, then cancels it again as
    it stops waiting — and the caller ends only after it, so no requeue or answer comes
    before the original is filed."""
    started, release = asyncio.Event(), asyncio.Event()
    finished: list[bool] = []

    async def finish() -> None:
        started.set()
        await release.wait()
        finished.append(True)

    caller = asyncio.create_task(runner._to_the_end(finish()))
    await started.wait()
    for _ in range(2):
        caller.cancel()
        await asyncio.sleep(0)
    assert not caller.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert finished == [True]


@respx.mock
async def test_a_failing_wait_puts_the_conversion_back_on_the_queue(
    make_client, tmp_path, documents, monkeypatch
) -> None:
    """docs/chat.md § Tools: a wait that fails otherwise than by a cancel — the look for a
    Stop meets a database error — puts the job back on the queue as a Stop does, so the
    tool answers that the conversion goes on after the turn and the worker takes it."""
    monkeypatch.setattr(chat_runner, "CONVERT_POLL_SECONDS", 0.01)
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(30)
        return _completion(VISION_MARKDOWN)

    respx.post(KANO_CHAT_URL).mock(side_effect=slow)
    client = await _ready(make_client, tmp_path)
    kept = await _keep(client, "tiny.png", documents["png"].read_bytes(), "image/png", None)
    converted = await client.post(
        f"/api/library/documents/{kept['id']}/convert", json={}, headers=CSRF_HEADERS
    )
    assert converted.status_code == 200, converted.text
    row, job, _ = await _settled(client, kept["id"])

    async def broken(ctx) -> bool:
        if started.is_set():
            raise RuntimeError("the database went away")
        return False

    monkeypatch.setattr(chat_runner, "_cancelled", broken)
    ctx = SimpleNamespace(
        settings=client.app.state.settings,
        sessionmaker=client.app.state.sessionmaker,
        convert_stop=asyncio.Event(),
        turn=SimpleNamespace(id=uuid.uuid4()),
        generation=None,
        render_slots=asyncio.Semaphore(1),
        inline_parse=None,
    )
    assert await asyncio.wait_for(chat_runner._drive_parse(ctx, job.id), 10) is None
    _, job, _ = await _settled(client, row.id)
    assert job.status == "queued"
