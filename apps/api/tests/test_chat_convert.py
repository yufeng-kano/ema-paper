"""The chat agent's `convert_file` tool (docs/chat.md § Tools, *A conversion runs inside the
turn*; docs/jobs.md § Converting a file; docs/billing.md § A chat turn): a kept PDF or image
becomes a document exactly as the Files page converts one, its parse driven inside the turn
against the stubbed vision upstream, charged on the job and never on the turn. No test
reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from sqlalchemy import select

from sightread.agents import chat as chat_agent
from sightread.billing import ledger
from sightread.db.models import Document, Job
from sightread.jobs import chat_runner
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
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
    """docs/chat.md § Tools: after the web tools, not strict — its input is one string
    (§ Upstream); a deployment without a parse model never declares it, and a history that
    names it is refused like a web tool's."""
    assert chat_agent.convert_tools(parse=False) == ()
    (tool,) = chat_agent.convert_tools(parse=True)
    assert tool["name"] == "convert_file" and "strict" not in tool
    assert tool["input_schema"]["additionalProperties"] is False
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
