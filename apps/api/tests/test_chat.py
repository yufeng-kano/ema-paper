"""The chat agent end to end against a stubbed Messages stream (docs/chat.md): the routes,
the turn queue, the tool loop with the editor and page tools, the event stream, usage.
No test reaches the network or a sandbox (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.db.models import (
    AgentSetting,
    ChatEvent,
    ChatTurn,
    Conversation,
    Document,
    Folder,
    Job,
    Message,
    Result,
    UsageLog,
)
from sightread.errors import ApiError
from sightread.jobs import chat_runner, chat_tools
from sightread.jobs.queue import claim_next_turn, fail_turn
from sightread.sandbox.client import ExecResult, SandboxClient
from sightread.workspace import documents as workspace_documents
from sightread.workspace import git
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, KANO_BASE
from tests.test_parse_end_to_end import _connect_kano

MESSAGES_URL = f"{KANO_BASE}/anthropic/v1/messages"
MODELS_URL = f"{KANO_BASE}/openai/v1/models"
MARKDOWN = "<!-- page: 1 -->\n\n# Title\n\n| Policy | Return |\n| --- | --- |\n| Greedy | 3450 |\n"


def sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def assistant_turn(
    blocks: list[dict], stop_reason: str, *, output_tokens: int = 10, model: str = "claude-opus-5-5"
) -> bytes:
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "model": model,
                "usage": {"input_tokens": 300, "cache_read_input_tokens": 200},
            },
        }
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            events += [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block["text"]},
                },
                {"type": "content_block_stop", "index": index},
            ]
        elif block["type"] == "tool_use":
            events += [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                },
                {"type": "content_block_stop", "index": index},
            ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


async def _seed_document(client: AsyncClient, user_id: int) -> int:
    """A finished parse filed in the library, with a result to materialize."""
    async with client.app.state.sessionmaker() as db:
        folder = Folder(user_id=user_id, parent_id=None, name="Papers")
        db.add(folder)
        await db.flush()
        job = Job(
            id=uuid.uuid4(),
            user_id=user_id,
            agent="parse",
            kind="pdf",
            filename="paper.pdf",
            media_type="application/pdf",
            size_bytes=10,
            sha256="b" * 64,
            model="claude-x",
            bbox_format="yxyx_norm1000",
            status="succeeded",
            page_count=1,
        )
        db.add(job)
        await db.flush()
        db.add(Result(job_id=job.id, markdown=MARKDOWN, pages=[], figures=[], errors=[], meta={}))
        document = Document(user_id=user_id, folder_id=folder.id, job_id=job.id, name="paper.pdf")
        db.add(document)
        await db.commit()
        return document.id


@pytest.fixture
async def chatting(make_client, tmp_path) -> AsyncClient:
    """Signed in, Kano Proxy connected, a model chosen for the chat agent, one document."""
    client = make_client(workspaces_dir=str(tmp_path / "workspaces"))
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    await _connect_kano(client.app.state.sessionmaker, user_id)
    chosen = await client.put(
        "/api/agents/chat/settings", json={"model": "claude-opus-5-5"}, headers=CSRF_HEADERS
    )
    assert chosen.status_code == 200, chosen.text
    client.document_id = await _seed_document(client, user_id)
    client.user_id = user_id
    return client


async def _run_queued_turn(client: AsyncClient) -> None:
    settings = client.app.state.settings
    async with client.app.state.sessionmaker() as db:
        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
    assert turn_id is not None
    await chat_runner.run_turn(
        client.app.state.sessionmaker, settings, turn_id, asyncio.Semaphore(2)
    )


async def test_chat_settings_refuse_connections_and_validate_effort(chatting) -> None:
    refused = await chatting.put(
        "/api/agents/chat/settings", json={"connection_id": 1}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 400
    bad = await chatting.put(
        "/api/agents/chat/settings", json={"effort": "ultra"}, headers=CSRF_HEADERS
    )
    assert bad.status_code == 400
    ok = await chatting.put(
        "/api/agents/chat/settings", json={"effort": "high"}, headers=CSRF_HEADERS
    )
    assert ok.json()["effort"] == "high"
    agents = (await chatting.get("/api/agents")).json()["agents"]
    chat = next(agent for agent in agents if agent["name"] == "chat")
    assert chat["settings"]["effort"] == "high"
    assert chat["output"] == "edits"


@respx.mock
async def test_the_chat_catalog_lists_claude_models_only(chatting) -> None:
    respx.get(MODELS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": "claude-opus-5-5"}, {"id": "gpt-x"}, {"id": "anthropic/claude-3"}]
            },
        )
    )
    listed = await chatting.get("/api/agents/chat/models")
    assert [model["id"] for model in listed.json()["data"]] == [
        "claude-opus-5-5",
        "anthropic/claude-3",
    ]


@respx.mock
async def test_a_turn_edits_the_document_and_streams_its_events(chatting, monkeypatch) -> None:
    document_id = chatting.document_id
    created = await chatting.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]

    # The model views the page, edits the table, then finishes.
    turns = iter(
        [
            assistant_turn(
                [
                    {"type": "text", "text": "Checking the table."},
                    {"type": "tool_use", "id": "t1", "name": "view_page", "input": {"page": 1}},
                ],
                "tool_use",
            ),
            assistant_turn(
                [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "str_replace_based_edit_tool",
                        "input": {
                            "command": "str_replace",
                            "path": "/workspace/files/Papers/paper.pdf/doc.md",
                            "old_str": "| Greedy | 3450 |",
                            "new_str": "| Greedy | 3.45 |",
                        },
                    }
                ],
                "tool_use",
            ),
            assistant_turn([{"type": "text", "text": "Done: Greedy is now 3.45."}], "end_turn"),
        ]
    )
    requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(turns))

    respx.post(MESSAGES_URL).mock(side_effect=answer)

    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "Fix the Greedy row", "selection": "| Greedy | 3450 |"},
        headers=CSRF_HEADERS,
    )
    assert sent.status_code == 202, sent.text
    # A second message while the first is unanswered is refused, not queued twice.
    again = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "and?"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 409

    await _run_queued_turn(chatting)

    # The request shape: tools in their fixed order, system cached, the guard off.
    first = requests[0]
    assert [tool["name"] for tool in first["tools"]] == [
        "str_replace_based_edit_tool",
        "view_page",
        "view_figure",
    ]  # no sandbox configured → no bash
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["thinking"] == {"type": "adaptive", "display": "updates"}
    assert first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default"
    assert "selected in the document" in first["messages"][0]["content"][1]["text"]
    # The marker the stored row carries for the web app never goes upstream.
    assert "context" not in first["messages"][0]["content"][1]
    assert "files/Papers/paper.pdf" in first["system"][0]["text"]
    # The second request replays the first answer and the tool result verbatim.
    second = requests[1]
    assert second["messages"][1]["role"] == "assistant"
    assert second["messages"][1]["content"][1]["name"] == "view_page"
    result = second["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    # No source was kept for this document, so view_page answered an error result.
    assert result.get("is_error") is True

    # The document changed on disk, as a revision.
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    assert "| Greedy | 3.45 |" in content["markdown"]
    history = (await chatting.get(f"/api/library/documents/{document_id}/revisions")).json()
    assert history["revisions"][0]["author"] == f"agent:{conversation_id}"

    # The thread: user, assistant, tool results, assistant, tool results, assistant.
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert [message["role"] for message in thread["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert thread["turns"][0]["status"] == "succeeded"
    assert thread["conversation"]["title"] == "Fix the Greedy row"

    # The event stream replays everything from 0 and ends, since no turn is running.
    async with chatting.stream(
        "GET", f"/api/conversations/{conversation_id}/events?after=0"
    ) as stream:
        body = (await stream.aread()).decode()
    names = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert names[0] == "turn_start"
    assert names[-1] == "turn_done"
    assert "edit" in names and "tool_use" in names and "tool_result" in names
    edit = next(
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ") and '"diff"' in line
    )
    assert "-| Greedy | 3450 |" in edit["diff"] and "+| Greedy | 3.45 |" in edit["diff"]

    # Usage: three calls billed to the chat agent with their cache reads.
    async with chatting.app.state.sessionmaker() as db:
        rows = (await db.execute(select(UsageLog).where(UsageLog.agent == "chat"))).scalars().all()
    assert len(rows) == 3
    assert all(row.turn_id is not None and row.cache_read_tokens == 200 for row in rows)
    usage = (await chatting.get("/api/usage")).json()
    assert usage["per_agent"][0]["agent"] == "chat"
    assert usage["per_day"][0]["agents"] == ["chat"]
    per_conversation = (await chatting.get("/api/usage/conversations")).json()["conversations"]
    assert per_conversation[0]["turns"] == 1 and per_conversation[0]["cache_read_tokens"] == 600

    # Revert the agent's edit from the thread.
    reverted = await chatting.post(
        f"/api/library/documents/{document_id}/revisions/{history['revisions'][0]['commit']}/revert",
        headers=CSRF_HEADERS,
    )
    assert reverted.status_code == 200
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    assert "| Greedy | 3450 |" in content["markdown"]


@respx.mock
async def test_a_refusal_fails_the_turn_and_a_cancel_stops_it(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, content=assistant_turn([], "refusal"))
    )
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    await _run_queued_turn(chatting)
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][-1]["status"] == "failed"
    assert "declined" in thread["turns"][-1]["error"]

    await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "again"},
        headers=CSRF_HEADERS,
    )
    cancelled = await chatting.post(
        f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS
    )
    assert cancelled.status_code == 204
    async with chatting.app.state.sessionmaker() as db:
        statuses = (
            (
                await db.execute(
                    select(ChatTurn.status).where(ChatTurn.conversation_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
    assert "cancelled" in statuses


async def test_messages_need_a_model_and_a_connected_kano_proxy(make_client, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    document_id = await _seed_document(client, user_id)
    conversation_id = (
        await client.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    no_model = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert no_model.status_code == 400
    assert "No model chosen" in no_model.json()["error"]["message"]


async def test_the_editor_tool_stays_inside_the_workspace(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    (root / "files" / "d" / "doc.md").write_text("alpha\nbeta\n")
    (root / "files" / "d" / ".meta.json").write_text("{}")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")

    escape = await chat_tools.run_editor(
        root,
        {"command": "view", "path": "/workspace/../secret.txt"},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert escape.is_error
    absolute = await chat_tools.run_editor(
        root,
        {"command": "view", "path": str(outside)},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert absolute.is_error
    meta = await chat_tools.run_editor(
        root,
        {"command": "str_replace", "path": "files/d/.meta.json", "old_str": "{}", "new_str": "x"},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert meta.is_error and "read-only" in meta.content

    twice = await chat_tools.run_editor(
        root,
        {
            "command": "str_replace",
            "path": "/workspace/files/d/doc.md",
            "old_str": "a",
            "new_str": "A",
        },
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert twice.is_error and "times" in twice.content
    inserted = await chat_tools.run_editor(
        root,
        {
            "command": "insert",
            "path": "/workspace/files/d/doc.md",
            "insert_line": 1,
            "insert_text": "gamma",
        },
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert not inserted.is_error
    assert (root / "files" / "d" / "doc.md").read_text() == "alpha\ngamma\nbeta\n"
    assert inserted.edit is not None and inserted.edit["commit"]
    listing = await chat_tools.run_editor(
        root,
        {"command": "view", "path": "/workspace/files"},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert listing.content == "d/"


async def test_the_bash_tool_goes_through_the_runner_and_truncates(monkeypatch) -> None:
    calls: list[tuple] = []

    async def fake_exec(self, user_id, session, command, *, timeout_seconds):
        calls.append((user_id, session, command))
        return ExecResult(output="x" * 5000, exit_code=1, timed_out=False, truncated=False)

    monkeypatch.setattr(SandboxClient, "exec", fake_exec)
    from sightread.config import Settings

    client = SandboxClient(
        Settings(app_env="local", sandbox_url="http://sandbox:8100", sandbox_token="t")
    )
    outcome = await chat_tools.run_bash(
        client, 7, "sess", {"command": "ls"}, max_chars=400, timeout_seconds=120
    )
    assert calls == [(7, "sess", "ls")]
    assert "characters omitted" in outcome.content
    assert outcome.content.endswith("[exit code 1]")
    assert outcome.is_error  # a nonzero exit is the model's error signal


def test_truncate_keeps_head_and_tail() -> None:
    text = "".join(f"line {index}\n" for index in range(200))
    cut = chat_tools.truncate(text, 300)
    assert cut.startswith("line 0\n") and cut.rstrip().endswith("line 199")
    assert "omitted" in cut


async def test_a_finished_library_parse_keeps_its_source(chatting, tmp_path) -> None:
    """The retention exception (docs/jobs.md): the upload moves into the document dir."""
    settings = chatting.app.state.settings
    sessionmaker = chatting.app.state.sessionmaker
    async with sessionmaker() as db:
        document = await db.get(Document, chatting.document_id)
        job = await db.get(Job, document.job_id)
        upload = Path(settings.upload_dir) / f"{job.id}.pdf"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"%PDF-1.4 test")
        job.source_path = str(upload)
        job.status = "running"
        await db.commit()
        job_id = job.id
    import fcntl

    from sightread.jobs import runner
    from sightread.workspace import locks

    # Filing takes the job's row lock before it inserts: a finish that waits for it reads
    # the row it filed as a recipient rather than deleting the upload under it.
    async with sessionmaker() as holder:
        await holder.execute(select(Job.id).where(Job.id == job_id).with_for_update())
        held = asyncio.create_task(
            runner._finish(sessionmaker, job_id, status="succeeded", settings=settings)
        )
        await asyncio.sleep(0.3)
        assert not held.done()
        await holder.rollback()
    await asyncio.wait_for(held, 10)
    # Done once: the rest of the test re-stages the upload for the backup-lock check.
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        job.status = "running"
        job.source_deleted_at = None
        await db.commit()
    kept_once = (
        user_root(settings.workspaces_dir, chatting.user_id)
        / "files"
        / "Papers"
        / "paper.pdf"
        / "source.pdf"
    )
    assert kept_once.read_bytes() == b"%PDF-1.4 test"
    upload.write_bytes(b"%PDF-1.4 test")
    kept_once.unlink()

    # The terminal rows and the kept original land in one backup snapshot: a backup
    # holding the lock keeps the finish — commit included — out until it is done.
    root = user_root(settings.workspaces_dir, chatting.user_id)
    backup_path = locks._backup_lock_path(root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_path.open("a+")
    fcntl.flock(backup, fcntl.LOCK_EX)
    finishing = asyncio.create_task(
        runner._finish(sessionmaker, job_id, status="succeeded", settings=settings)
    )
    await asyncio.sleep(0.3)
    assert not finishing.done()
    async with sessionmaker() as db:
        assert (await db.get(Job, job_id)).status == "running"
    fcntl.flock(backup, fcntl.LOCK_UN)
    backup.close()
    await asyncio.wait_for(finishing, 10)
    kept = (
        user_root(settings.workspaces_dir, chatting.user_id)
        / "files"
        / "Papers"
        / "paper.pdf"
        / "source.pdf"
    )
    assert kept.read_bytes() == b"%PDF-1.4 test"
    assert not upload.exists()
    async with sessionmaker() as db:
        content = await workspace_documents.read_content(
            db, settings, await db.get(Document, chatting.document_id)
        )
    assert content.has_source
    async with sessionmaker() as db:
        assert (await db.execute(select(Message))).scalars().all() == []


async def test_view_page_and_view_figure_read_the_documents_own_files(tmp_path, documents) -> None:
    from PIL import Image

    from sightread.workspace.documents import DocumentPaths

    root = tmp_path / "ws" / "1"
    paths = DocumentPaths(root=root, relative=Path("files/paper"))
    paths.directory.mkdir(parents=True)
    shutil.copy(documents["text_pdf"], paths.directory / "source.pdf")
    paths.figures.mkdir()
    Image.new("RGB", (40, 30), "white").save(paths.figures / "p1_100_100_400_600.png")

    page = await chat_tools.view_page(
        paths,
        {"page": 1},
        document_id=5,
        page_count=3,
        render_slots=asyncio.Semaphore(1),
        decode_slot=asyncio.Semaphore(1),
        max_bytes=10_000_000,
        kind="pdf",
    )
    assert not page.is_error
    assert page.content[0]["type"] == "image"
    assert page.content[0]["source"]["media_type"] == "image/png"
    assert page.content[1]["text"] == "Page 1."
    # The original kept outside the mount is what renders, even with the shell's copy gone.
    (paths.directory / "source.pdf").unlink()
    outside = tmp_path / "ws" / ".repos" / "1.sources"
    outside.mkdir(parents=True)
    shutil.copy(documents["text_pdf"], outside / "5.pdf")
    from_original = await chat_tools.view_page(
        paths,
        {"page": 1},
        document_id=5,
        page_count=3,
        render_slots=asyncio.Semaphore(1),
        decode_slot=asyncio.Semaphore(1),
        max_bytes=10_000_000,
        kind="pdf",
    )
    assert not from_original.is_error and from_original.content[1]["text"] == "Page 1."
    too_far = await chat_tools.view_page(
        paths,
        {"page": 9},
        document_id=5,
        page_count=3,
        render_slots=asyncio.Semaphore(1),
        decode_slot=asyncio.Semaphore(1),
        max_bytes=10_000_000,
        kind="pdf",
    )
    assert too_far.is_error

    figure = await chat_tools.view_figure(
        paths, {"page": 1, "bbox": [100, 100, 400, 600]}, max_bytes=1_000_000
    )
    assert not figure.is_error and figure.content[0]["type"] == "image"
    missing = await chat_tools.view_figure(
        paths, {"page": 2, "bbox": [0, 0, 10, 10]}, max_bytes=1_000_000
    )
    # A crop the shell inflated is refused before it is read.
    (paths.figures / "p1_100_100_400_600.png").write_bytes(b"x" * 2_000_000)
    huge = await chat_tools.view_figure(
        paths, {"page": 1, "bbox": [100, 100, 400, 600]}, max_bytes=1_000_000
    )
    assert huge.is_error
    assert missing.is_error


async def test_a_shell_change_becomes_its_own_revision(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files").mkdir(parents=True)
    git.ensure_repo(root)
    (root / "files" / "made-by-bash.md").write_text("from the shell\n")
    outcome = chat_tools.ToolOutcome("ok")
    committed = await chat_tools.commit_shell_changes(
        root,
        outcome,
        {"command": "echo 'from the shell' > files/made-by-bash.md"},
        "agent:1",
        max_chars=2000,
    )
    assert committed.edit is not None
    assert committed.edit["command"] == "bash"
    assert "+from the shell" in committed.edit["diff"]
    assert git.log(root)[0].message == "bash: echo 'from the shell' > files/made-by-bash.md"
    # A command that changed nothing adds no revision.
    again = await chat_tools.commit_shell_changes(
        root, outcome, {"command": "ls"}, "agent:1", max_chars=2000
    )
    assert again.edit is None


async def test_an_editor_filesystem_failure_is_a_tool_result(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    outcome = await chat_tools.run_editor(
        root,
        {"command": "create", "path": "/workspace/files/d", "file_text": "x"},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert outcome.is_error


def test_an_interrupted_tool_call_is_answered_on_replay() -> None:
    history = [
        {"role": "user", "content": [{"type": "text", "text": "go"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "running"},
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {"command": "ls"}},
                {"type": "tool_use", "id": "t2", "name": "bash", "input": {"command": "pwd"}},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "and now?"}]},
    ]
    repaired = chat_runner.repaired(history)
    assert [message["role"] for message in repaired] == ["user", "assistant", "user", "user"]
    results = repaired[2]["content"]
    assert [block["tool_use_id"] for block in results] == ["t1", "t2"]
    assert all(block["is_error"] for block in results)
    assert chat_runner.repaired(repaired) == repaired
    # A history with every call answered is returned as it is.
    answered = history[:2] + [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "b"},
            ],
        }
    ]
    assert chat_runner.repaired(answered) == answered


async def test_a_message_belongs_to_its_turn_and_a_live_thread_cannot_be_deleted(
    chatting,
) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "hi", "locale": "zh-TW"},
        headers=CSRF_HEADERS,
    )
    assert sent.status_code == 202, sent.text
    turn_id = sent.json()["turn_id"]
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["messages"][0]["turn_id"] == turn_id
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, uuid.UUID(turn_id))
        assert turn.locale == "zh-TW"

    refused = await chatting.delete(f"/api/conversations/{conversation_id}", headers=CSRF_HEADERS)
    assert refused.status_code == 409
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    deleted = await chatting.delete(f"/api/conversations/{conversation_id}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204


async def test_a_cancel_drops_a_silent_stream(chatting, monkeypatch) -> None:
    """The upstream has sent nothing for a while; the cancel must not wait for it."""
    monkeypatch.setattr(chat_runner, "CANCEL_POLL_SECONDS", 0.05)
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    closed = asyncio.Event()
    started = asyncio.Event()

    async def silent(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            closed.set()
        yield  # pragma: no cover - never reached

    monkeypatch.setattr(chat_runner, "stream_messages", silent)

    async def cancel_soon() -> None:
        # Once the upstream call is open and silent — not before, or the loop's own
        # pre-call check would stop the turn without ever opening the stream.
        await started.wait()
        await asyncio.sleep(0.1)
        async with chatting.app.state.sessionmaker() as db:
            turn = await db.get(ChatTurn, turn_id)
            turn.status = "cancelled"
            await db.commit()

    canceller = asyncio.create_task(cancel_soon())
    async with asyncio.timeout(5):
        await _run_queued_turn(chatting)
    await canceller
    assert closed.is_set()
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "cancelled"
        stored = (
            (await db.execute(select(Message).where(Message.conversation_id == conversation_id)))
            .scalars()
            .all()
        )
    assert [row.role for row in stored] == ["user"]


async def test_a_cancelled_turn_blocks_deletion_until_the_worker_is_done(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    # The worker claimed it and is mid-stream.
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        turn.status = "running"
        await db.commit()
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    still_running = await chatting.delete(
        f"/api/conversations/{conversation_id}", headers=CSRF_HEADERS
    )
    assert still_running.status_code == 409
    # The worker wound it down.
    await chat_runner._finish(
        chatting.app.state.sessionmaker, turn_id, status="cancelled", error="cancelled"
    )
    gone = await chatting.delete(f"/api/conversations/{conversation_id}", headers=CSRF_HEADERS)
    assert gone.status_code == 204


def _round_ctx(image_budget: int = 32 << 20) -> SimpleNamespace:
    """What `_run_tools` reads from its context: the round's and the turn's image budgets."""
    return SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=image_budget, chat_image_turn_max_bytes=image_budget * 4
        ),
        image_spent=0,
    )


async def test_a_rounds_images_never_exceed_the_budget(monkeypatch) -> None:
    rendered: list[str] = []

    async def fake_view_page(paths, tool_input, **kwargs):
        rendered.append(str(tool_input["page"]))
        image = {"type": "image", "source": {"type": "base64", "data": "x" * 100}}
        return chat_tools.ToolOutcome([image, {"type": "text", "text": "Page."}])

    async def no_events(ctx, event, data):
        return None

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_tools, "view_page", fake_view_page)
    monkeypatch.setattr(chat_runner, "_emit", no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=250,
            chat_image_turn_max_bytes=1 << 30,
            chat_tool_result_max_chars=1000,
            upload_max_bytes=1,
            content_max_bytes=1,
            chat_bash_timeout_seconds=1,
        ),
        conversation=SimpleNamespace(id=1, session="s"),
        paths=None,
        document=SimpleNamespace(id=1),
        decode_slot=asyncio.Semaphore(1),
        job=SimpleNamespace(page_count=12, kind="pdf"),
        render_slots=None,
        sandbox=None,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        image_spent=0,
    )
    tool_uses = [
        {"type": "tool_use", "id": str(index), "name": "view_page", "input": {"page": index + 1}}
        for index in range(12)
    ]
    results = await chat_runner._run_tools(ctx, tool_uses, set())
    assert [result["tool_use_id"] for result in results] == [str(i) for i in range(12)]
    kept = [result for result in results if not result.get("is_error")]
    refused = [result for result in results if result.get("is_error")]
    # Admitted one at a time as each completes: two images fit, the third would cross
    # the budget and is replaced, however many rendered together.
    assert len(kept) == 2
    assert sum(chat_runner._image_bytes(result) for result in kept) <= 250
    assert len(refused) == 10
    assert all(result["content"] == chat_runner.IMAGE_BUDGET_SPENT for result in refused)


async def test_a_stop_drops_a_round_of_reads(monkeypatch) -> None:
    rendered: list[str] = []

    async def fake_run_tool(ctx, block, malformed, admit=None):
        rendered.append(block["id"])
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}

    async def cancelled(ctx):
        return True

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", cancelled)
    tool_uses = [
        {"type": "tool_use", "id": str(index), "name": "view_page", "input": {}}
        for index in range(8)
    ]
    # No write in the round to notice the Stop: the reads look for it themselves.
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    assert rendered == []


async def test_writing_tools_run_in_order_while_reads_run_together(monkeypatch) -> None:
    started: list[str] = []
    writers_running: set[str] = set()
    writer_overlap = False
    reads_overlapped = False
    reads_running: set[str] = set()

    async def fake_run_tool(ctx, block, malformed, admit=None):
        nonlocal writer_overlap, reads_overlapped
        tool_id = block["id"]
        started.append(tool_id)
        if block["name"] in chat_runner.MUTATING_TOOLS:
            if writers_running:
                writer_overlap = True
            writers_running.add(tool_id)
        else:
            if reads_running:
                reads_overlapped = True
            reads_running.add(tool_id)
        await asyncio.sleep(0.01)
        writers_running.discard(tool_id)
        reads_running.discard(tool_id)
        return {"type": "tool_result", "tool_use_id": tool_id, "content": block["name"]}

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    tool_uses = [
        {"type": "tool_use", "id": "a", "name": "view_page", "input": {}},
        {"type": "tool_use", "id": "b", "name": "bash", "input": {}},
        {"type": "tool_use", "id": "c", "name": "str_replace_based_edit_tool", "input": {}},
        {"type": "tool_use", "id": "d", "name": "view_figure", "input": {}},
    ]
    results = await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    assert [result["tool_use_id"] for result in results] == ["a", "b", "c", "d"]
    assert not writer_overlap
    assert reads_overlapped
    assert started.index("b") < started.index("c")


async def test_a_cancelled_queued_turn_ends_its_stream_and_a_winding_down_turn_blocks_sends(
    chatting,
) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "cancelled" and turn.finished_at is not None
        events = (
            (
                await db.execute(
                    select(ChatEvent).where(ChatEvent.turn_id == turn_id).order_by(ChatEvent.seq)
                )
            )
            .scalars()
            .all()
        )
    assert [event.event for event in events] == ["turn_error"]
    assert events[0].data["status"] == "cancelled"
    # Finished: a new message is accepted.
    again = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "more"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 202
    second = uuid.UUID(again.json()["turn_id"])
    # The worker took it, the user cancelled mid-tool: until the worker winds it down, no
    # further message may be queued behind it.
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, second)
        turn.status = "running"
        await db.commit()
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    blocked = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "wait"},
        headers=CSRF_HEADERS,
    )
    assert blocked.status_code == 409


async def test_a_file_with_a_live_conversation_cannot_be_deleted(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    refused = await chatting.delete(f"/api/library/documents/{document_id}", headers=CSRF_HEADERS)
    assert refused.status_code == 409
    folders = (await chatting.get("/api/library")).json()["folders"]
    folder_id = folders[0]["id"]
    refused_folder = await chatting.delete(
        f"/api/library/folders/{folder_id}", headers=CSRF_HEADERS
    )
    assert refused_folder.status_code == 409
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    deleted = await chatting.delete(f"/api/library/documents/{document_id}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204


async def test_a_worker_stopping_finishes_a_cancelled_turn(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        turn.status = "running"
        await db.commit()
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    async with chatting.app.state.sessionmaker() as db:
        await fail_turn(db, turn_id, "worker stopped")
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "cancelled" and turn.finished_at is not None
        events = (
            (await db.execute(select(ChatEvent).where(ChatEvent.turn_id == turn_id)))
            .scalars()
            .all()
        )
    assert [event.event for event in events] == ["turn_error"]
    again = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "more"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 202


async def test_an_oversized_json_body_is_refused_before_it_is_read(make_client, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), content_max_bytes=1_000)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    document_id = await _seed_document(client, user_id)
    huge = {"markdown": "x" * 200_000, "base_revision": "0" * 64}
    declared = await client.put(
        f"/api/library/documents/{document_id}/content", json=huge, headers=CSRF_HEADERS
    )
    assert declared.status_code == 413

    async def chunks():
        yield json.dumps(huge).encode()

    chunked = await client.put(
        f"/api/library/documents/{document_id}/content",
        content=chunks(),
        headers={**CSRF_HEADERS, "Content-Type": "application/json"},
    )
    assert chunked.status_code == 413
    small = await client.get(f"/api/library/documents/{document_id}/content")
    assert small.status_code == 200


async def test_the_editor_never_follows_a_link_planted_in_the_tree(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("sk-not-for-you")
    (root / "files" / "d" / "link.md").symlink_to(secret)
    viewed = await chat_tools.run_editor(
        root,
        {"command": "view", "path": "/workspace/files/d/link.md"},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert viewed.is_error and "sk-not-for-you" not in str(viewed.content)
    written = await chat_tools.run_editor(
        root,
        {
            "command": "str_replace",
            "path": "/workspace/files/d/link.md",
            "old_str": "x",
            "new_str": "y",
        },
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert written.is_error
    assert secret.read_text() == "sk-not-for-you"


async def test_a_stop_ends_the_round_before_the_next_write(monkeypatch) -> None:
    calls: list[str] = []
    cancelled = {"value": False}

    async def fake_run_tool(ctx, block, malformed, admit=None):
        calls.append(block["id"])
        cancelled["value"] = True  # the user pressed Stop while the first write ran
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}

    async def fake_cancelled(ctx):
        return cancelled["value"]

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", fake_cancelled)
    tool_uses = [
        {"type": "tool_use", "id": "a", "name": "bash", "input": {}},
        {"type": "tool_use", "id": "b", "name": "bash", "input": {}},
    ]
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    assert calls == ["a"]


async def test_the_body_cap_ignores_a_multipart_label_off_the_upload_routes(
    make_client, tmp_path
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), content_max_bytes=1_000)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    document_id = await _seed_document(client, user_id)
    spoofed = await client.put(
        f"/api/library/documents/{document_id}/content",
        content=b"x" * 200_000,
        headers={**CSRF_HEADERS, "Content-Type": "multipart/form-data; boundary=abc"},
    )
    assert spoofed.status_code == 413


async def test_the_agent_page_lists_only_the_tools_the_deployment_has(
    make_client, tmp_path
) -> None:
    with_runner = make_client(
        workspaces_dir=str(tmp_path / "ws1"),
        sandbox_url="http://sandbox:8100",
        sandbox_token="runner-token-not-a-real-one",
    )
    await with_runner.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    with_sandbox = next(
        agent
        for agent in (await with_runner.get("/api/agents")).json()["agents"]
        if agent["name"] == "chat"
    )
    assert with_sandbox["tools"] == [
        "bash",
        "str_replace_based_edit_tool",
        "view_page",
        "view_figure",
    ]
    bare = make_client(workspaces_dir=str(tmp_path / "ws2"), sandbox_url="")
    await bare.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    without = next(
        agent
        for agent in (await bare.get("/api/agents")).json()["agents"]
        if agent["name"] == "chat"
    )
    assert without["tools"] == ["str_replace_based_edit_tool", "view_page", "view_figure"]


async def test_a_file_with_a_live_conversation_cannot_be_renamed(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    refused = await chatting.put(
        f"/api/library/documents/{document_id}", json={"name": "other.pdf"}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 409
    # Nor reverted: a shell revision may have moved it, and the turn holds its path.
    refused = await chatting.post(
        f"/api/library/documents/{document_id}/revisions/abcdef1/revert", headers=CSRF_HEADERS
    )
    assert refused.status_code == 409


async def test_a_stop_seen_by_the_reads_lets_the_running_write_finish(monkeypatch) -> None:
    finished: list[str] = []

    stopped = {"value": False}

    async def fake_run_tool(ctx, block, malformed, admit=None):
        if block["name"] == "bash":
            stopped["value"] = True  # the Stop lands while the shell command runs
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0.01)  # a render takes a moment; the rest queue behind
        finished.append(block["id"])
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}

    async def cancelled(ctx):
        return stopped["value"]

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", cancelled)
    tool_uses = [{"type": "tool_use", "id": "w", "name": "bash", "input": {}}] + [
        {"type": "tool_use", "id": str(index), "name": "view_page", "input": {}}
        for index in range(8)
    ]
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    # The reads still queued saw the Stop and ended quietly; the command in flight was
    # not cancelled and finished.
    assert "w" in finished
    assert len([name for name in finished if name != "w"]) <= chat_runner.READ_CONCURRENCY


async def test_a_stop_during_backoff_and_a_running_read_are_both_cut(monkeypatch) -> None:
    flags = {"cancelled": False}

    async def fake_cancelled(ctx):
        return flags["cancelled"]

    monkeypatch.setattr(chat_runner, "_cancelled", fake_cancelled)
    monkeypatch.setattr(chat_runner, "CANCEL_POLL_SECONDS", 0.02)
    flags["cancelled"] = True
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._backoff(None, 30)

    flags["cancelled"] = False
    read_cancelled = asyncio.Event()

    async def fake_run_tool(ctx, block, malformed, admit=None):
        if block["name"] == "view_page":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                read_cancelled.set()
                raise
        # The Stop lands while the read is already running, not before it starts.
        await asyncio.sleep(0.01)
        flags["cancelled"] = True
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    tool_uses = [
        {"type": "tool_use", "id": "r", "name": "view_page", "input": {}},
        {"type": "tool_use", "id": "a", "name": "bash", "input": {}},
        {"type": "tool_use", "id": "b", "name": "bash", "input": {}},
    ]
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    assert read_cancelled.is_set()


def test_an_image_past_the_pixel_cap_is_refused_before_it_is_decoded() -> None:
    from PIL import Image

    # A 1-bit image compresses to a few kilobytes; its pixels would not.
    buffer = io.BytesIO()
    Image.new("1", (7000, 7000)).save(buffer, format="PNG")
    assert len(buffer.getvalue()) < 100_000
    with pytest.raises(chat_tools.ImageTooLarge):
        chat_tools._as_png(buffer.getvalue())
    small = io.BytesIO()
    Image.new("RGB", (20, 10), (255, 0, 0)).save(small, format="PNG")
    assert chat_tools._as_png(small.getvalue()).startswith(b"\x89PNG")


@respx.mock
async def test_a_fallback_answer_moves_the_conversation_to_its_model(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    requests: list[dict] = []
    turns = iter(
        [
            assistant_turn(
                [{"type": "text", "text": "Hi."}], "end_turn", model="claude-fallback-9"
            ),
            assistant_turn(
                [{"type": "text", "text": "Again."}], "end_turn", model="claude-fallback-9"
            ),
        ]
    )

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(turns))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(chatting)
    assert requests[0]["model"] == "claude-opus-5-5"
    async with chatting.app.state.sessionmaker() as db:
        # The safety fallback answered from another model: its thinking blocks are that
        # model's, so the conversation follows it before the answer is stored.
        assert (await db.get(Conversation, conversation_id)).model == "claude-fallback-9"
    again = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "more"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 202, again.text
    await _run_queued_turn(chatting)
    assert requests[1]["model"] == "claude-fallback-9"


async def test_the_history_cap_counts_bytes_not_characters(chatting) -> None:
    settings = chatting.app.state.settings
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    # 2000 two-byte characters: about 2040 characters stored, about 4040 bytes.
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "é" * 2000},
        headers=CSRF_HEADERS,
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    settings.chat_history_max_bytes = 3000
    await _run_queued_turn(chatting)
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed" and "start a new conversation" in turn.error
    settings.chat_history_max_bytes = 256 * 1024 * 1024


async def test_a_conversation_past_the_history_cap_is_refused(chatting) -> None:
    settings = chatting.app.state.settings
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "hello " * 20},
        headers=CSRF_HEADERS,
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    settings.chat_history_max_bytes = 50
    await _run_queued_turn(chatting)
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed"
        assert "start a new conversation" in turn.error
    settings.chat_history_max_bytes = 256 * 1024 * 1024


async def test_a_cancelled_decode_is_waited_for_before_the_slot_is_released(monkeypatch) -> None:
    import threading

    started = threading.Event()
    release = threading.Event()
    finished: list[bool] = []

    def slow_decode(raw):
        started.set()
        release.wait(5)
        finished.append(True)
        return b"\x89PNG"

    monkeypatch.setattr(chat_tools, "_as_png", slow_decode)
    slot = asyncio.Semaphore(1)
    decoding = asyncio.ensure_future(
        chat_tools._settled(asyncio.to_thread(chat_tools._as_png, b"raw"))
    )
    await asyncio.to_thread(started.wait, 5)
    decoding.cancel()
    # The cancel is not acknowledged while the thread still holds the raster.
    await asyncio.sleep(0.1)
    assert not decoding.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await decoding
    assert finished == [True]
    assert slot._value == 1


async def test_a_command_the_runner_could_not_settle_commits_no_tree(tmp_path) -> None:
    from sightread.sandbox.client import SandboxUnavailable

    class Client:
        async def exec(self, *args, **kwargs):
            raise SandboxUnavailable("the sandbox VM could not be stopped")

    outcome = await chat_tools.run_bash(
        Client(), 1, "s", {"command": "python write.py"}, max_chars=1000, timeout_seconds=5
    )
    assert outcome.is_error and outcome.settled is False
    assert "recorded with the next command" in outcome.content
    # An ordinary failure is settled: the command is over, its changes are its own.
    assert chat_tools.ToolOutcome("x", is_error=True).settled is True


async def test_a_worker_starting_records_what_an_orphaned_turn_left_unrecorded(
    chatting, monkeypatch
) -> None:
    from sightread.jobs.queue import ORPHAN_REVISION_MESSAGE, fail_orphaned_turns

    settings = chatting.app.state.settings
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        turn.status = "running"
        await db.commit()
        document = await db.get(Document, document_id)
        paths = await workspace_documents.materialize_document(db, settings, document)
    # The dead worker's shell wrote a file, and the worker died before the commit — its
    # writing marker still standing, so no host write lands on the moving tree.
    (paths.directory / "helper.py").write_text("print(1)\n")
    (paths.directory / "doc.md").write_text("# Title\n\nHalf done\n")
    from sightread.workspace.locks import mark_writing, writing_marker

    mark_writing(paths.root, conversation_id, "s")
    refused = await chatting.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": "# Mine\n", "base_revision": "x"},
        headers=CSRF_HEADERS,
    )
    assert refused.status_code == 409 and "recovering" in refused.json()["error"]["message"]

    real = git.commit_all

    def failing(*args, **kwargs):
        raise git.GitError("index.lock exists")

    # A tree that cannot be committed leaves the turn for the next pass: finishing it
    # would let the next revision carry these changes as its own.
    monkeypatch.setattr(git, "commit_all", failing)
    async with chatting.app.state.sessionmaker() as db:
        assert await fail_orphaned_turns(db, None, settings) == (0, {chatting.user_id})
        assert (await db.get(ChatTurn, turn_id)).finished_at is None
    assert writing_marker(paths.root).exists()  # still recovering: host writes wait
    monkeypatch.setattr(git, "commit_all", real)

    async with chatting.app.state.sessionmaker() as db:
        assert await fail_orphaned_turns(db, None, settings) == (1, set())
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed" and turn.finished_at is not None
    assert not writing_marker(paths.root).exists()  # recorded: host writes resume
    head = git.log(paths.root)[0]
    assert head.message == ORPHAN_REVISION_MESSAGE
    assert head.author == f"agent:{conversation_id}"
    assert set(head.files) == {"files/Papers/paper.pdf/helper.py", "files/Papers/paper.pdf/doc.md"}
    # The tree is clean: a later tool's revision carries only its own changes.
    assert git.commit_all(paths.root, "user", "anything left?") is None
    revisions = (await chatting.get(f"/api/library/documents/{document_id}/revisions")).json()
    assert revisions["revisions"][0]["message"] == ORPHAN_REVISION_MESSAGE


async def test_a_worker_starting_finishes_the_turns_a_dead_one_left(chatting) -> None:
    from sightread.jobs.queue import fail_orphaned_turns

    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        turn.status = "running"  # claimed by a worker that then died
        await db.commit()
    from sightread.sandbox.client import SandboxUnavailable

    class Runner:
        configured = True
        restarted: list[tuple[int, str]] = []
        away = True

        async def restart(self, user_id: int, session: str) -> None:
            if self.away:
                raise SandboxUnavailable("down")
            self.restarted.append((user_id, session))

    runner = Runner()
    async with chatting.app.state.sessionmaker() as db:
        # The runner does not answer: the turn is left for the next pass, since the
        # command the dead worker had running may still be writing.
        assert await fail_orphaned_turns(db, runner) == (0, {chatting.user_id})
        await db.commit()
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(ChatTurn, turn_id)).finished_at is None
        runner.away = False
        assert await fail_orphaned_turns(db, runner) == (1, set())
        turn = await db.get(ChatTurn, turn_id)
        session = (await db.get(Conversation, turn.conversation_id)).session
        assert runner.restarted == [(chatting.user_id, session)]
        assert turn.status == "failed" and turn.error == "worker restarted"
        assert turn.finished_at is not None
        events = (
            (await db.execute(select(ChatEvent).where(ChatEvent.turn_id == turn_id)))
            .scalars()
            .all()
        )
        assert [event.event for event in events] == ["turn_error"]
        assert await fail_orphaned_turns(db) == (0, set())
    # The conversation takes messages again.
    again = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "more"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 202


async def test_a_stop_before_the_first_write_starts_none(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_run_tool(ctx, block, malformed, admit=None):
        calls.append(block["id"])
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}

    async def cancelled(ctx):
        return True

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", cancelled)
    tool_uses = [{"type": "tool_use", "id": "a", "name": "bash", "input": {}}]
    # The Stop came after the round's pre-check and before the first write began.
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    assert calls == []


def test_the_selection_block_is_marked_for_the_web_and_stripped_for_the_api() -> None:
    stored = [
        {"type": "text", "text": "Fix the Greedy row"},
        {
            "type": "text",
            "text": "The passage selected in the document:\n\n| Greedy |",
            "context": "selection",
        },
    ]
    sent = chat_runner._api_blocks(stored)
    assert sent == [
        {"type": "text", "text": "Fix the Greedy row"},
        {"type": "text", "text": "The passage selected in the document:\n\n| Greedy |"},
    ]
    assert stored[1]["context"] == "selection"  # the stored row keeps its marker


async def test_the_editor_refuses_what_history_excludes(tmp_path) -> None:
    root = tmp_path / "ws" / "9"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    for path in ("files/paper/figures/p1.png", "files/paper/source.pdf", "files/paper/.sr-tmp-abc"):
        refused = await chat_tools.run_editor(
            root,
            {"command": "create", "path": f"/workspace/{path}", "file_text": "x"},
            "agent:1",
            max_chars=1000,
            max_bytes=10_000,
        )
        assert refused.is_error, path
        assert not (root / path).exists()


async def test_the_deadline_waits_for_the_write_in_flight(monkeypatch) -> None:
    done: list[str] = []

    async def slow_write() -> dict:
        await asyncio.sleep(0.05)
        done.append("w")
        return {"type": "tool_result", "tool_use_id": "w", "content": "ok"}

    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_bash_timeout_seconds=1,
            chat_writer_grace_seconds=1,
            sandbox_start_timeout_seconds=0,
            sandbox_exec_grace_seconds=0,
        ),
        writer=asyncio.ensure_future(slow_write()),
    )
    # The turn's deadline interrupted the loop while this write ran: it is waited for,
    # so its commit precedes the terminal event.
    await chat_runner._settle_writer(ctx)
    assert done == ["w"]


async def test_a_shell_commit_that_fails_is_an_error_and_leaves_the_index_clean(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "ws" / "1"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    (root / "made.txt").write_text("by the shell")

    def refuse(*_args, **_kwargs):
        raise git.GitError("disk full")

    monkeypatch.setattr(git, "_commit_staged", refuse)
    outcome = await chat_tools.commit_shell_changes(
        root,
        chat_tools.ToolOutcome("done"),
        {"command": "touch made.txt"},
        "agent:1",
        max_chars=500,
    )
    assert outcome.is_error is True
    assert outcome.content.startswith("done") and "disk full" in outcome.content
    assert outcome.edit is None
    # On disk still, staged no longer: a later path-scoped commit will not carry it.
    assert (root / "made.txt").exists()
    assert git._git(root, "diff", "--cached", "--quiet", check=False).returncode == 0


async def test_the_event_replay_is_read_in_batches(chatting, monkeypatch) -> None:
    from sightread.jobs import chat_events

    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{chatting.document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    turn_id = uuid.UUID(sent.json()["turn_id"])
    async with chatting.app.state.sessionmaker() as db:
        for index in range(7):
            await chat_events.emit(db, conversation_id, turn_id, "text_delta", {"text": str(index)})
        await fail_turn(db, turn_id, "over")
        await db.commit()
    monkeypatch.setattr(chat_events, "REPLAY_BATCH_SIZE", 3)
    async with chatting.stream(
        "GET", f"/api/conversations/{conversation_id}/events?after=0"
    ) as stream:
        body = (await stream.aread()).decode()
    ids = [int(line.split(": ", 1)[1]) for line in body.splitlines() if line.startswith("id: ")]
    assert ids == list(range(1, 9))
    names = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert names == ["text_delta"] * 7 + ["turn_error"]


async def test_a_delete_whose_commit_fails_keeps_the_edited_files(chatting, monkeypatch) -> None:
    import contextlib

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    document_id = chatting.document_id
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    saved = await chatting.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": "# Edited\n", "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text
    # The original, outside history: it must come back with the rest.
    source = (
        user_root(chatting.app.state.settings.workspaces_dir, chatting.user_id)
        / "files"
        / "Papers"
        / "paper.pdf"
        / "source.pdf"
    )
    source.write_bytes(b"%PDF-1.4 kept")

    original = AsyncSession.commit
    failing = {"on": True}

    async def commit(self):
        if failing["on"]:
            failing["on"] = False
            raise OperationalError("COMMIT", {}, Exception("the database went away"))
        await original(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with contextlib.suppress(Exception):
        await chatting.delete(f"/api/library/documents/{document_id}", headers=CSRF_HEADERS)
    assert source.read_bytes() == b"%PDF-1.4 kept"
    assert failing["on"] is False  # the commit was attempted
    monkeypatch.setattr(AsyncSession, "commit", original)

    # The row stayed, and so did the edit: not a fresh copy of the parse.
    listed = (await chatting.get("/api/library")).json()["documents"]
    assert [row["id"] for row in listed] == [document_id]
    after = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    assert after["markdown"] == "# Edited\n"
    history = (await chatting.get(f"/api/library/documents/{document_id}/revisions")).json()
    assert history["revisions"][0]["message"].startswith("restore ")


async def test_a_shell_revision_beyond_the_document_waits_for_every_turn(chatting) -> None:
    first_id = chatting.document_id
    settings = chatting.app.state.settings
    async with chatting.app.state.sessionmaker() as db:
        first = await db.get(Document, first_id)
        job_id = first.job_id
    second = await chatting.post(
        "/api/library/documents/from-job", json={"job_id": str(job_id)}, headers=CSRF_HEADERS
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["id"]
    async with chatting.app.state.sessionmaker() as db:
        paths_a = await workspace_documents.materialize_document(
            db, settings, await db.get(Document, first_id)
        )
        paths_b = await workspace_documents.materialize_document(
            db, settings, await db.get(Document, second_id)
        )
    root = paths_a.root
    # A shell revision over both documents, then one of the first document's own.
    paths_a.doc.write_text("a by the shell\n")
    paths_b.doc.write_text("b by the shell\n")
    both = git.commit_all(root, "agent:1", "bash: sed both")
    paths_a.doc.write_text("a by the user\n")
    own = git.commit_paths(root, git.USER_AUTHOR, "save", [paths_a.doc_relative])

    # A turn on the second document is unfinished.
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{second_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    refused = await chatting.post(
        f"/api/library/documents/{first_id}/revisions/{both}/revert", headers=CSRF_HEADERS
    )
    assert refused.status_code == 409, refused.text
    assert paths_b.doc.read_text() == "b by the shell\n"
    # The first document's own revision is its own business.
    allowed = await chatting.post(
        f"/api/library/documents/{first_id}/revisions/{own}/revert", headers=CSRF_HEADERS
    )
    assert allowed.status_code == 200, allowed.text
    assert paths_a.doc.read_text() == "a by the shell\n"
    # Once that turn is finished, the shell revision can go.
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    reverted = await chatting.post(
        f"/api/library/documents/{first_id}/revisions/{both}/revert", headers=CSRF_HEADERS
    )
    assert reverted.status_code == 200, reverted.text
    assert paths_b.doc.read_text() != "b by the shell\n"


async def test_a_turns_images_never_exceed_the_turn_budget(monkeypatch) -> None:
    async def fake_view_page(paths, tool_input, **kwargs):
        image = {"type": "image", "source": {"type": "base64", "data": "x" * 100}}
        return chat_tools.ToolOutcome([image, {"type": "text", "text": "Page."}])

    async def no_events(ctx, event, data):
        return None

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_tools, "view_page", fake_view_page)
    monkeypatch.setattr(chat_runner, "_emit", no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=1000,
            chat_image_turn_max_bytes=250,
            chat_tool_result_max_chars=1000,
            upload_max_bytes=1,
            content_max_bytes=1,
            chat_bash_timeout_seconds=1,
        ),
        conversation=SimpleNamespace(id=1, session="s"),
        paths=None,
        document=SimpleNamespace(id=1),
        decode_slot=asyncio.Semaphore(1),
        job=SimpleNamespace(page_count=12, kind="pdf"),
        render_slots=None,
        sandbox=None,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        image_spent=0,
    )
    one_round = [
        {"type": "tool_use", "id": str(index), "name": "view_page", "input": {"page": index + 1}}
        for index in range(3)
    ]
    # The round's budget would take all three; the turn's takes two across every round.
    first = await chat_runner._run_tools(ctx, one_round, set())
    assert len([r for r in first if not r.get("is_error")]) == 2
    assert ctx.image_spent == 200
    second = await chat_runner._run_tools(ctx, one_round, set())
    assert all(r.get("is_error") for r in second)
    assert all(r["content"] == chat_runner.IMAGE_TURN_BUDGET_SPENT for r in second)
    assert ctx.image_spent == 200


async def test_a_lost_shell_status_is_an_error(monkeypatch) -> None:
    async def fake_exec(self, user_id, session, command, *, timeout_seconds):
        return ExecResult(output="half", exit_code=None, timed_out=False, truncated=False)

    monkeypatch.setattr(SandboxClient, "exec", fake_exec)
    from sightread.config import Settings

    client = SandboxClient(
        Settings(app_env="local", sandbox_url="http://sandbox:8100", sandbox_token="t")
    )
    outcome = await chat_tools.run_bash(
        client, 7, "sess", {"command": "exit 1"}, max_chars=400, timeout_seconds=120
    )
    assert outcome.is_error
    assert "without reporting a status" in outcome.content


async def test_a_shell_command_holds_the_workspace_lock(tmp_path, monkeypatch) -> None:
    from sightread.workspace.locks import workspace_lock

    root = tmp_path / "ws" / "1"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    release = asyncio.Event()
    started = asyncio.Event()

    async def fake_run_bash(client, user_id, session, tool_input, *, max_chars, timeout_seconds):
        started.set()
        await release.wait()
        return chat_tools.ToolOutcome("done")

    async def no_events(ctx, event, data):
        return None

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_tools, "run_bash", fake_run_bash)
    monkeypatch.setattr(chat_runner, "_emit", no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=1000,
            chat_image_turn_max_bytes=1000,
            chat_tool_result_max_chars=1000,
            chat_bash_timeout_seconds=1,
        ),
        conversation=SimpleNamespace(id=1, session="s"),
        paths=SimpleNamespace(root=root),
        sandbox=object(),
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        image_spent=0,
        writer=None,
    )
    tool_uses = [{"type": "tool_use", "id": "b1", "name": "bash", "input": {"command": "sleep"}}]
    running = asyncio.create_task(chat_runner._run_tools(ctx, tool_uses, set()))
    await started.wait()

    async def take_lock() -> None:
        async with workspace_lock(root):
            pass

    # A host write waits for the running command, not for its commit alone.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(take_lock(), 0.3)
    release.set()
    (result,) = await running
    assert result["content"] == "done"
    await asyncio.wait_for(take_lock(), 5)


async def test_a_rename_whose_commit_fails_moves_the_directory_back(chatting, monkeypatch) -> None:
    import contextlib

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    document_id = chatting.document_id
    settings = chatting.app.state.settings
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    saved = await chatting.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": "# Edited\n", "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text
    root = user_root(settings.workspaces_dir, chatting.user_id)
    before = root / "files" / "Papers" / "paper.pdf"
    after = root / "files" / "Papers" / "renamed.pdf"

    original = AsyncSession.commit
    failing = {"on": True}

    async def commit(self):
        if failing["on"]:
            failing["on"] = False
            raise OperationalError("COMMIT", {}, Exception("the database went away"))
        await original(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with contextlib.suppress(Exception):
        await chatting.put(
            f"/api/library/documents/{document_id}",
            json={"name": "renamed.pdf"},
            headers=CSRF_HEADERS,
        )
    assert failing["on"] is False
    monkeypatch.setattr(AsyncSession, "commit", original)

    # The row kept its name, and the directory is back where the row says, edits and all.
    listed = (await chatting.get("/api/library")).json()["documents"]
    assert [row["name"] for row in listed] == ["paper.pdf"]
    assert (before / "doc.md").read_text() == "# Edited\n"
    assert not after.exists()
    read = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    assert read["markdown"] == "# Edited\n"


async def test_a_send_that_waited_for_a_deleted_conversation_is_a_404(
    chatting, monkeypatch
) -> None:
    from sightread.routes import chat as chat_routes

    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    original = chat_routes.owned_conversation

    async def deleted_underneath(db, user_id, cid):
        row = await original(db, user_id, cid)
        # The delete commits between the read and the lock: what a send that waited for
        # the lock behind a delete sees.
        async with chatting.app.state.sessionmaker() as other:
            await other.delete(await other.get(Conversation, cid))
            await other.commit()
        return row

    monkeypatch.setattr(chat_routes, "owned_conversation", deleted_underneath)
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 404, sent.text


def test_editor_paths_with_dot_components_are_refused(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files").mkdir(parents=True)
    for raw in ("/workspace/files/../../other/x", "files/../x", "/workspace/files/a/../b"):
        with pytest.raises(chat_tools.PathRefused):
            chat_tools.resolve_workspace_path(root, raw)
    # A `.` component is collapsed by the path itself; nothing to refuse.
    assert chat_tools.resolve_workspace_path(root, "files/./doc.md") == root / "files" / "doc.md"
    assert chat_tools.resolve_workspace_path(root, "/workspace/files/a/doc.md") == (
        root / "files" / "a" / "doc.md"
    )


async def test_a_shell_move_is_reconciled_before_the_lock_is_released(
    tmp_path, monkeypatch
) -> None:
    from sightread.workspace.locks import workspace_lock

    root = tmp_path / "ws" / "1"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    seen: list[bool] = []

    async def fake_run_bash(client, user_id, session, tool_input, *, max_chars, timeout_seconds):
        return chat_tools.ToolOutcome("moved")

    async def fake_commit(root_, outcome, tool_input, author, *, max_chars, locked=False):
        return chat_tools.ToolOutcome(outcome.content, edit={"path": "", "commit": "c", "diff": ""})

    async def fake_reconcile(db, settings, user_id):
        # Still held by the command: a waiter cannot materialize the old path meanwhile.
        async def take() -> None:
            async with workspace_lock(root):
                pass

        try:
            await asyncio.wait_for(take(), 0.2)
            seen.append(False)
        except TimeoutError:
            seen.append(True)
        return 0

    async def no_events(ctx, event, data):
        return None

    async def never_cancelled(ctx):
        return False

    class Sessions:
        def __call__(self):
            return self

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(chat_tools, "run_bash", fake_run_bash)
    monkeypatch.setattr(chat_tools, "commit_shell_changes", fake_commit)
    monkeypatch.setattr(chat_runner, "reconcile_locked", fake_reconcile)
    monkeypatch.setattr(chat_runner, "_emit", no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=1000,
            chat_image_turn_max_bytes=1000,
            chat_tool_result_max_chars=1000,
            chat_bash_timeout_seconds=1,
        ),
        conversation=SimpleNamespace(id=1, session="s"),
        paths=SimpleNamespace(root=root),
        sandbox=object(),
        sessionmaker=Sessions(),
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        image_spent=0,
        writer=None,
    )
    tool_uses = [{"type": "tool_use", "id": "b1", "name": "bash", "input": {"command": "mv a b"}}]
    await chat_runner._run_tools(ctx, tool_uses, set())
    assert seen == [True]


def test_prompt_facts_are_substituted_in_one_pass() -> None:
    from sightread.agents.chat import render_system_prompt

    rendered = render_system_prompt(
        "Edit {document_path}; answer in {locale}.",
        {"document_path": "files/{locale}/doc.md", "locale": "zh-TW"},
    )
    assert rendered == "Edit files/{locale}/doc.md; answer in zh-TW."


async def test_a_message_or_selection_past_the_deployments_cap_is_refused(chatting) -> None:
    settings = chatting.app.state.settings
    settings.chat_message_max_chars = 10
    settings.chat_selection_max_chars = 5
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{chatting.document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    long = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "x" * 11},
        headers=CSRF_HEADERS,
    )
    assert long.status_code == 400
    wide = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "hi", "selection": "y" * 6},
        headers=CSRF_HEADERS,
    )
    assert wide.status_code == 400
    fits = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "hi", "selection": "yyy"},
        headers=CSRF_HEADERS,
    )
    assert fits.status_code == 202, fits.text


async def test_a_thread_for_a_document_deleted_underneath_is_a_404(chatting, monkeypatch) -> None:
    from sightread.routes import chat as chat_routes

    document_id = chatting.document_id
    original = chat_routes.owned_document

    async def deleted_underneath(db, user_id, did):
        row = await original(db, user_id, did)
        async with chatting.app.state.sessionmaker() as other:
            await other.delete(await other.get(Document, did))
            await other.commit()
        return row

    monkeypatch.setattr(chat_routes, "owned_document", deleted_underneath)
    created = await chatting.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    assert created.status_code == 404, created.text


async def test_the_writer_is_waited_for_through_the_runner_clients_grace() -> None:
    async def slow_write() -> str:
        await asyncio.sleep(0.3)
        return "committed"

    writer = asyncio.ensure_future(slow_write())
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_bash_timeout_seconds=0,
            chat_writer_grace_seconds=0,
            sandbox_start_timeout_seconds=0,
            sandbox_exec_grace_seconds=0.5,
        ),
        writer=writer,
    )
    await chat_runner._settle_writer(ctx)
    assert writer.done() and writer.result() == "committed"


async def test_a_writer_past_its_grace_is_killed_in_the_vm_and_waited_to_its_end() -> None:
    restarted: list[tuple[int, str]] = []

    class FakeSandbox:
        async def restart(self, user_id: int, session: str) -> None:
            restarted.append((user_id, session))

    async def long_write() -> str:
        await asyncio.sleep(0.4)
        return "committed"

    writer = asyncio.ensure_future(long_write())
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_bash_timeout_seconds=0,
            chat_writer_grace_seconds=0,
            sandbox_start_timeout_seconds=0,
            sandbox_exec_grace_seconds=0.1,
        ),
        writer=writer,
        writer_tool="bash",
        sandbox=FakeSandbox(),
        turn=SimpleNamespace(id="t", user_id=7),
        conversation=SimpleNamespace(session="s"),
    )
    # Past the grace: the command is killed where it runs, and the writer is still
    # waited for to its end — the turn is never finished over a write in flight.
    await chat_runner._settle_writer(ctx)
    assert restarted == [(7, "s")]
    assert writer.done() and writer.result() == "committed"


@respx.mock
async def test_an_answer_cut_at_the_output_limit_fails_the_turn(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            content=assistant_turn(
                [
                    {"type": "text", "text": "Half an answer"},
                    {"type": "tool_use", "id": "cut", "name": "bash", "input": {}},
                ],
                "max_tokens",
            ),
        )
    )
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    await _run_queued_turn(chatting)
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][-1]["status"] == "failed"
    assert "output limit" in thread["turns"][-1]["error"]
    # What came through is kept; the tool call cut with the limit is not, and never ran.
    async with chatting.app.state.sessionmaker() as db:
        stored = (await db.execute(select(Message).order_by(Message.id))).scalars().all()
    assistant = [row for row in stored if row.role == "assistant"]
    assert len(assistant) == 1
    assert [block["type"] for block in assistant[0].content] == ["text"]
    assert all(block.get("type") != "tool_result" for row in stored for block in row.content)

    # Any other stop the loop does not know fails the turn too, rather than passing as done.
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, content=assistant_turn([{"type": "text", "text": "..."}], "pause_turn")
        )
    )
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "again"},
        headers=CSRF_HEADERS,
    )
    await _run_queued_turn(chatting)
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][-1]["status"] == "failed"
    assert "pause_turn" in thread["turns"][-1]["error"]


async def test_the_sandbox_client_takes_an_empty_success_answer(monkeypatch) -> None:
    from sightread.config import Settings
    from sightread.sandbox import client as sandbox_client

    answers: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        sandbox_client.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    client = sandbox_client.SandboxClient(
        Settings(app_env="local", sandbox_url="http://sandbox:8100", sandbox_token="t")
    )
    # Stop and reset answer 204 with no body: done, not unreadable.
    answers += [httpx.Response(204), httpx.Response(200, content=b"")]
    await client.stop(7)
    await client.reset(7)
    answers.append(httpx.Response(200, content=b"<html>"))
    with pytest.raises(sandbox_client.SandboxUnavailable):
        await client.stop(7)


async def test_a_conversation_with_a_stored_model_needs_none_on_the_agent_page(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    async with chatting.app.state.sessionmaker() as db:
        conversation = await db.get(Conversation, conversation_id)
        conversation.model = "claude-stored"
        setting = (
            await db.execute(select(AgentSetting).where(AgentSetting.agent == "chat"))
        ).scalar_one()
        setting.model = None  # "Not set" on the Agent page
        await db.commit()
    accepted = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert accepted.status_code == 202, accepted.text
    # A fresh conversation still needs one chosen.
    fresh = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    refused = await chatting.post(
        f"/api/conversations/{fresh}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 400
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][-1]["finished_at"] is None


async def test_an_editor_write_whose_commit_fails_is_undone(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    (root / "files" / "d" / "doc.md").write_text("alpha\nbeta\n")
    git.ensure_repo(root)
    git.commit_all(root, "user", "seed")

    def failing(*args, **kwargs):
        raise git.GitError("git is unwell")

    monkeypatch.setattr(git, "commit_paths", failing)
    replaced = await chat_tools.run_editor(
        root,
        {
            "command": "str_replace",
            "path": "/workspace/files/d/doc.md",
            "old_str": "beta",
            "new_str": "gamma",
        },
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert replaced.is_error
    assert (root / "files" / "d" / "doc.md").read_text() == "alpha\nbeta\n"
    created = await chat_tools.run_editor(
        root,
        {"command": "create", "path": "/workspace/files/d/new.md", "file_text": "x"},
        "agent:1",
        max_chars=1000,
        max_bytes=1_000_000,
    )
    assert created.is_error
    assert not (root / "files" / "d" / "new.md").exists()


def test_truncate_stays_within_a_small_cap() -> None:
    text = "x" * 500
    for cap in (0, 1, 10, 79, 80, 81, 200):
        cut = chat_tools.truncate(text, cap)
        assert len(cut) <= max(cap, 0) or cap >= 80, (cap, len(cut))
    assert chat_tools.truncate(text, 10) == "x" * 10
    assert chat_tools.truncate(text, 0) == ""


def test_the_prompt_lists_only_the_tools_the_turn_has() -> None:
    from sightread.agents import chat as chat_agent

    facts = {"document_path": "/workspace/files/a", "locale": "en"}
    with_bash = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT,
        facts,
        tools=["bash", "str_replace_based_edit_tool", "view_page", "view_figure"],
    )
    assert "- bash:" in with_bash and "- view_page:" in with_bash
    without = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT,
        facts,
        tools=["str_replace_based_edit_tool", "view_page", "view_figure"],
    )
    assert "- bash:" not in without and "persistent shell" not in without
    assert "- str_replace_based_edit_tool:" in without and "- view_figure:" in without
    # No tool list given: the template as written.
    assert "- bash:" in chat_agent.render_system_prompt(chat_agent.DEFAULT_SYSTEM_PROMPT, facts)


def test_the_prompt_promises_uv_add_only_where_the_vm_has_an_index() -> None:
    from sightread.agents import chat as chat_agent

    # The runner's own settings decide: `none` is the shipped default on both examples,
    # and a network without an index URL reaches no index either.
    assert "no network at all" in chat_agent.network_note("none", "http://proxy/simple")
    assert "no network at all" in chat_agent.network_note("pypi-proxy", "")
    assert "`uv add` works" not in chat_agent.network_note("none", "")
    assert "`uv add` works" in chat_agent.network_note("pypi-proxy", "http://proxy/simple")
    rendered = chat_agent.render_system_prompt(
        chat_agent.DEFAULT_SYSTEM_PROMPT, {"network": chat_agent.network_note("none", "")}
    )
    assert "no network at all" in rendered and "{network}" not in rendered


async def test_the_panel_gets_the_selection_without_its_scaffolding(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    sent = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "Fix the Greedy row", "selection": "| Greedy | 3450 |"},
        headers=CSRF_HEADERS,
    )
    assert sent.status_code == 202, sent.text
    detail = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    blocks = detail["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "Fix the Greedy row"}
    # The passage alone: the panel's note counts the selection, not the line that
    # introduces it to the model, which the stored row keeps.
    assert blocks[1] == {"type": "text", "text": "| Greedy | 3450 |", "context": "selection"}
    async with chatting.app.state.sessionmaker() as db:
        stored = (await db.execute(select(Message))).scalars().one()
        assert stored.content[1]["text"].startswith("The passage selected in the document:")


async def test_a_revert_is_refused_before_it_materializes(chatting) -> None:
    document_id = chatting.document_id
    settings = chatting.app.state.settings
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    saved = await chatting.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": "# Edited\n", "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200
    commit = (await chatting.get(f"/api/library/documents/{document_id}/revisions")).json()[
        "revisions"
    ][0]["commit"]
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    # The turn is unfinished and, between tool calls, has removed the directory.
    directory = (
        user_root(settings.workspaces_dir, chatting.user_id) / "files" / "Papers" / "paper.pdf"
    )
    shutil.rmtree(directory)
    refused = await chatting.post(
        f"/api/library/documents/{document_id}/revisions/{commit}/revert", headers=CSRF_HEADERS
    )
    assert refused.status_code == 409
    assert not directory.exists()


async def test_the_conversation_detail_carries_no_image_bytes(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "x" * 4000},
    }
    async with chatting.app.state.sessionmaker() as db:
        db.add(
            Message(
                conversation_id=conversation_id,
                role="user",
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [image, {"type": "text", "text": "Page 1."}],
                    },
                    image,
                ],
            )
        )
        await db.commit()
    detail = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    content = detail["messages"][-1]["content"]
    assert content[0]["content"][0] == {"type": "image"}
    assert content[0]["content"][1]["text"] == "Page 1."
    assert content[1] == {"type": "image"}
    assert "x" * 100 not in json.dumps(detail)


async def test_the_model_is_fixed_when_the_message_is_accepted(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = (
        await chatting.post(
            f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
        )
    ).json()["id"]
    accepted = await chatting.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert accepted.status_code == 202, accepted.text
    async with chatting.app.state.sessionmaker() as db:
        conversation = await db.get(Conversation, conversation_id)
        setting = (
            await db.execute(select(AgentSetting).where(AgentSetting.agent == "chat"))
        ).scalar_one()
        # Snapshotted at the send, before any turn ran: a change on the Agent page while
        # the turn waits in the queue does not reach it.
        assert conversation.model == setting.model
        setting.model = "claude-other"
        await db.commit()
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(Conversation, conversation_id)).model != "claude-other"


def test_the_exec_deadline_covers_a_cold_start() -> None:
    from sightread.config import Settings

    client = SandboxClient(
        Settings(
            app_env="local",
            sandbox_url="http://sandbox:8100",
            sandbox_token="t",
            sandbox_start_timeout_seconds=200,
            sandbox_exec_grace_seconds=30,
        )
    )
    assert client.exec_timeout(120) == 350


async def test_a_command_the_runner_could_not_confirm_over_keeps_the_marker_until_a_restart_does(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    from sightread.config import Settings
    from sightread.jobs.queue import UNSETTLED_REVISION_MESSAGE, recover_unsettled_commands
    from sightread.sandbox.client import SandboxUnavailable
    from sightread.workspace.locks import read_writing, writing_marker
    from tests.conftest import KANO_BASE, TEST_SECRET_KEY
    from tests.test_workspace import _seed

    settings = Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
        sandbox_url="http://sandbox:8100",
        sandbox_token="t",
    )
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await workspace_documents.materialize_document(db, settings, document)

    class Shell:
        configured = True
        restart_ok = False
        restarted: list[str] = []
        runs = 0

        async def exec(self, user_id, session, command, *, timeout_seconds):
            self.runs += 1
            (paths.directory / "half.txt").write_text(f"being written, run {self.runs}\n")
            raise SandboxUnavailable("the sandbox VM could not be stopped")

        async def restart(self, user_id, session):
            self.restarted.append(session)
            if not self.restart_ok:
                raise SandboxUnavailable("the sandbox VM could not be stopped")

    async def no_events(ctx, event, data):
        return None

    monkeypatch.setattr(chat_runner, "_emit", no_events)
    shell = Shell()
    ctx = SimpleNamespace(
        settings=settings,
        sessionmaker=sessionmaker,
        sandbox=shell,
        paths=paths,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=user.id),
        conversation=SimpleNamespace(id=3, session="s"),
    )
    block = {"id": "t1", "name": "bash", "input": {"command": "python write.py"}}
    # Neither the command nor the restart could be confirmed over: nothing is committed
    # and the marker stays, naming the shell, so host writes are refused.
    result = await chat_runner._run_tool_unguarded(ctx, block, set(), None)
    assert result["is_error"] and "takes no other write" in result["content"]
    assert shell.restarted == ["s"]
    assert read_writing(paths.root) == (3, "s")
    assert git.log(paths.root)[0].message.startswith("materialize")
    async with sessionmaker() as db:
        with pytest.raises(ApiError):
            await workspace_documents.read_content(db, settings, document)

    # The recovery a worker runs at start: the restart confirms the command dead, the
    # tree is recorded in the conversation's name, the marker goes.
    assert await recover_unsettled_commands(settings, shell) == {user.id}  # runner still away
    shell.restart_ok = True
    assert await recover_unsettled_commands(settings, shell) == set()
    assert not writing_marker(paths.root).exists()
    head = git.log(paths.root)[0]
    assert head.message == UNSETTLED_REVISION_MESSAGE and head.author == "agent:3"
    assert "files/Papers/paper.pdf/half.txt" in head.files

    # Within a turn, a restart that confirms the kill records the tree right away.
    result = await chat_runner._run_tool_unguarded(ctx, block, set(), None)
    assert result["is_error"] and "restarted" in result["content"]
    assert not writing_marker(paths.root).exists()
    assert git.log(paths.root)[0].message == "bash: python write.py"


async def test_a_shell_command_stands_marked_as_writing_for_its_whole_run(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    from sightread.config import Settings
    from sightread.workspace.locks import writing_marker
    from tests.conftest import KANO_BASE, TEST_SECRET_KEY
    from tests.test_workspace import _seed

    settings = Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
    )
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await workspace_documents.materialize_document(db, settings, document)
    seen: list[bool] = []

    class Shell:
        async def exec(self, user_id, session, command, *, timeout_seconds):
            # While the command runs, the marker stands: a worker dying now leaves it.
            seen.append(writing_marker(paths.root).exists())
            (paths.directory / "made.txt").write_text("by the shell\n")
            return ExecResult(output="ok", exit_code=0, timed_out=False, truncated=False)

    async def no_events(ctx, event, data):
        return None

    monkeypatch.setattr(chat_runner, "_emit", no_events)
    ctx = SimpleNamespace(
        settings=settings,
        sessionmaker=sessionmaker,
        sandbox=Shell(),
        paths=paths,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=user.id),
        conversation=SimpleNamespace(id=3, session="s"),
    )
    result = await chat_runner._run_tool_unguarded(
        ctx, {"id": "t1", "name": "bash", "input": {"command": "touch made.txt"}}, set(), None
    )
    assert seen == [True]
    assert not writing_marker(paths.root).exists()  # the command is over and recorded
    assert result["content"] == "ok" and "is_error" not in result
    assert git.log(paths.root)[0].message == "bash: touch made.txt"
