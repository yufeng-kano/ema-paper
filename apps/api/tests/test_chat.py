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
    ChatEvent,
    ChatTurn,
    Document,
    Folder,
    Job,
    Message,
    Result,
    UsageLog,
)
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


def assistant_turn(blocks: list[dict], stop_reason: str, *, output_tokens: int = 10) -> bytes:
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "model": "claude-opus-5-5",
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
    assert not outcome.is_error


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
    from sightread.jobs import runner

    await runner._finish(sessionmaker, job_id, status="succeeded", settings=settings)
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
        page_count=3,
        render_slots=asyncio.Semaphore(1),
        max_bytes=10_000_000,
        kind="pdf",
    )
    assert not page.is_error
    assert page.content[0]["type"] == "image"
    assert page.content[0]["source"]["media_type"] == "image/png"
    assert page.content[1]["text"] == "Page 1."
    too_far = await chat_tools.view_page(
        paths,
        {"page": 9},
        page_count=3,
        render_slots=asyncio.Semaphore(1),
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
    """What `_run_tools` reads from its context: the round's image budget."""
    return SimpleNamespace(settings=SimpleNamespace(chat_image_round_max_bytes=image_budget))


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
            chat_tool_result_max_chars=1000,
            upload_max_bytes=1,
            content_max_bytes=1,
            chat_bash_timeout_seconds=1,
        ),
        conversation=SimpleNamespace(id=1, session="s"),
        paths=None,
        job=SimpleNamespace(page_count=12, kind="pdf"),
        render_slots=None,
        sandbox=None,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
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

    async def fake_run_tool(ctx, block, malformed, admit=None):
        if block["name"] == "bash":
            await asyncio.sleep(0.05)  # the shell command is still running
        finished.append(block["id"])
        return {"type": "tool_result", "tool_use_id": block["id"], "content": "ok"}

    async def cancelled(ctx):
        return True

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", cancelled)
    tool_uses = [{"type": "tool_use", "id": "w", "name": "bash", "input": {}}] + [
        {"type": "tool_use", "id": str(index), "name": "view_page", "input": {}}
        for index in range(8)
    ]
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._run_tools(_round_ctx(), tool_uses, set())
    # The reads saw the Stop and ended quietly; the command in flight was not cancelled.
    assert finished == ["w"]


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
    async with chatting.app.state.sessionmaker() as db:
        assert await fail_orphaned_turns(db) == 1
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed" and turn.error == "worker restarted"
        assert turn.finished_at is not None
        events = (
            (await db.execute(select(ChatEvent).where(ChatEvent.turn_id == turn_id)))
            .scalars()
            .all()
        )
        assert [event.event for event in events] == ["turn_error"]
        assert await fail_orphaned_turns(db) == 0
    # The conversation takes messages again.
    again = await chatting.post(
        f"/api/conversations/{conversation_id}/messages",
        json={"text": "more"},
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 202
