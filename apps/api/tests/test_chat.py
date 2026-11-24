"""The chat agent end to end against a stubbed Messages stream (docs/chat.md): the routes,
the turn queue, the tool loop with the editor and page tools, the event stream, usage.
No test reaches the network or a sandbox (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import io
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import httpx
import pytest
import respx
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from sightread.agents import CHAT
from sightread.agents import chat as chat_agent
from sightread.config import Settings
from sightread.db.models import (
    AgentSetting,
    ChatEvent,
    ChatTurn,
    Conversation,
    Document,
    Job,
    Message,
    UsageLog,
)
from sightread.errors import ApiError
from sightread.jobs import chat_events, chat_runner, chat_tools
from sightread.jobs import runner as parse_runner
from sightread.jobs.events import open_listener
from sightread.jobs.queue import (
    ORPHAN_REVISION_MESSAGE,
    UNSETTLED_REVISION_MESSAGE,
    fail_orphaned_turns,
    fail_turn,
    recover_unsettled_commands,
)
from sightread.routes import chat as chat_routes
from sightread.sandbox.client import (
    ExecResult,
    SandboxClient,
    SandboxRejected,
    SandboxUnavailable,
)
from sightread.upstream.anthropic import ChatUsage
from sightread.upstream.client import RateLimited
from sightread.workspace import documents as workspace_documents
from sightread.workspace import git, safe_io
from sightread.workspace.layout import sources_dir, user_root
from sightread.workspace.locks import (
    _backup_lock_path,
    mark_writing,
    read_writing,
    workspace_lock,
    writing_marker,
)
from tests.chat_helpers import (
    MESSAGES_URL,
    assistant_turn,
    chatting_client,
    new_conversation,
    run_queued_turn,
    seed_document,
    send,
)
from tests.conftest import (
    CSRF_HEADERS,
    KANO_MODELS_URL,
    PAY_SETTINGS,
    grant_plan,
    image_view,
    path_settings,
    postgres_only,
    sign_in,
)
from tests.factories import user_row
from tests.test_workspace import _seed


@pytest.fixture
async def chatting(make_client) -> AsyncClient:
    """Signed in, a chat model chosen, one document; the setting stored directly, so a test
    with its own catalog stubs it only once the turn runs."""
    return await chatting_client(make_client(), store_directly=True)


def _paper_history(client: AsyncClient) -> list[git.Revision]:
    """The commits that touched the seeded document's directory, newest first."""
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return git.log(root, Path("files/Papers/paper.pdf"))


def _sandbox_client(**overrides) -> SandboxClient:
    """A runner client for a deployment with a sandbox configured; nothing it sends leaves
    the test, which stubs `exec` or routes the runner's URL through respx."""
    return SandboxClient(
        Settings(app_env="local", sandbox_url="http://sandbox:8100", sandbox_token="t", **overrides)
    )


async def _edit(
    root: Path, tool_input: dict, *, max_bytes: int = 1_000_000
) -> chat_tools.ToolOutcome:
    """One editor call as conversation 1's agent, with the caps the editor tests share and
    a fresh image view."""
    return await chat_tools.run_editor(
        root, tool_input, "agent:1", max_chars=1000, max_bytes=max_bytes, images=image_view()
    )


def test_strict_tool_schemas_hold_only_what_strict_accepts() -> None:
    """A strict schema with a numeric bound or an array length is a 400 on every request
    (docs/chat.md § Tools): the tools check those themselves."""
    refused = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }

    def walk(node: object, where: str) -> list[str]:
        if isinstance(node, list):
            return [hit for i, item in enumerate(node) for hit in walk(item, f"{where}[{i}]")]
        if not isinstance(node, dict):
            return []
        hits = [f"{where}.{key}" for key in node if key in refused]
        if node.get("type") == "object" and node.get("additionalProperties") is not False:
            hits.append(f"{where}: additionalProperties must be false")
        return hits + [hit for key, value in node.items() for hit in walk(value, f"{where}.{key}")]

    strict = [tool for tool in chat_agent.TOOLS if tool.get("strict")]
    assert strict
    assert [hit for tool in strict for hit in walk(tool["input_schema"], tool["name"])] == []


async def test_the_chat_effort_is_validated_and_echoed(chatting) -> None:
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
async def test_a_turn_edits_the_document_and_streams_its_events(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = await new_conversation(chatting)

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

    sent = await send(
        chatting, conversation_id, text="Fix the Greedy row", selection="| Greedy | 3450 |"
    )
    assert sent.status_code == 202, sent.text
    # A second message while the first is unanswered is refused, not queued twice.
    again = await send(chatting, conversation_id, text="and?")
    assert again.status_code == 409

    await run_queued_turn(chatting)

    # The request shape: tools in their fixed order, system cached, the guard off.
    first = requests[0]
    assert [tool["name"] for tool in first["tools"]] == [
        "str_replace_based_edit_tool",
        "view_page",
        "view_figure",
        "skill",
        "subagent",
        "memory",
        "read_url",
        "download_file",
        "convert_file",
        "insert_image",
    ]  # no sandbox configured → no bash; no keys → no searches (docs/chat.md § Tools)
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert first["thinking"] == {
        "type": "adaptive",
        "display": "updates",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
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
    history = _paper_history(chatting)
    assert history[0].author == f"agent:{conversation_id}"

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

    # Revert the agent's edit from the thread.
    reverted = await chatting.post(
        f"/api/library/documents/{document_id}/revisions/{history[0].commit}/revert",
        headers=CSRF_HEADERS,
    )
    assert reverted.status_code == 200
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    assert "| Greedy | 3450 |" in content["markdown"]


@respx.mock
async def test_a_refusal_fails_the_turn(chatting) -> None:
    conversation_id = await new_conversation(chatting)
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(200, content=assistant_turn([], "refusal"))
    )
    await send(chatting, conversation_id, text="hi")
    await run_queued_turn(chatting)
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][-1]["status"] == "failed"
    assert "declined" in thread["turns"][-1]["error"]


@pytest.mark.own_catalog
@respx.mock
async def test_a_first_message_needs_a_model_the_catalog_still_serves(chatting) -> None:
    """The pick was offered when it was saved but Kano Proxy dropped it: refused before it
    is fixed on the conversation; a catalog that cannot be read is the upstream's failure."""
    conversation_id = await new_conversation(chatting)
    respx.get(KANO_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "codex/gpt-5.5"}]})
    )
    dropped = await send(chatting, conversation_id, text="hi")
    assert dropped.status_code == 400
    assert dropped.json()["error"]["message"] == (
        "That model is no longer served: pick another on the Agent page"
    )
    respx.get(KANO_MODELS_URL).mock(return_value=httpx.Response(500))
    unreadable = await send(chatting, conversation_id, text="hi")
    assert unreadable.status_code == 502
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(Conversation, conversation_id)).model is None
        assert (await db.execute(select(ChatTurn.id))).all() == []


async def test_messages_need_an_offered_model_and_a_configured_kano_proxy(make_client) -> None:
    """A deployment offering no chat model — no catalog model priced — or no Kano Proxy
    refuses the send with 503 before a turn exists (docs/agents.md § Enqueue rules)."""
    document_id: int | None = None
    parse_only = json.dumps(
        {"vendor/vision-model": {"input": 1, "cache_read": 1, "cache_write": 1, "output": 1}}
    )
    for overrides in ({"model_prices_json": parse_only}, {"kano_proxy_api_key": ""}):
        client = make_client(**overrides)
        await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
        if document_id is None:
            user_id = (await client.get("/api/me")).json()["user"]["id"]
            document_id = await seed_document(client, user_id)
        conversation_id = (
            await client.post(
                f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
            )
        ).json()["id"]
        refused = await send(client, conversation_id, text="hi")
        assert refused.status_code == 503, overrides
        assert refused.json()["error"]["type"] == "upstream"


async def test_the_editor_tool_stays_inside_the_workspace(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    (root / "files" / "d" / "doc.md").write_text("alpha\nbeta\n")
    (root / "files" / "d" / ".meta.json").write_text("{}")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")

    escape = await _edit(root, {"command": "view", "path": "/workspace/../secret.txt"})
    assert escape.is_error
    absolute = await _edit(root, {"command": "view", "path": str(outside)})
    assert absolute.is_error
    meta = await _edit(
        root,
        {"command": "str_replace", "path": "files/d/.meta.json", "old_str": "{}", "new_str": "x"},
    )
    assert meta.is_error and "read-only" in meta.content

    twice = await _edit(
        root,
        {
            "command": "str_replace",
            "path": "/workspace/files/d/doc.md",
            "old_str": "a",
            "new_str": "A",
        },
    )
    assert twice.is_error and "times" in twice.content
    inserted = await _edit(
        root,
        {
            "command": "insert",
            "path": "/workspace/files/d/doc.md",
            "insert_line": 1,
            "insert_text": "gamma",
        },
    )
    assert not inserted.is_error
    assert (root / "files" / "d" / "doc.md").read_text() == "alpha\ngamma\nbeta\n"
    assert inserted.edit is not None and inserted.edit["commit"]
    listing = await _edit(root, {"command": "view", "path": "/workspace/files"})
    assert listing.content == "d/"


async def test_the_bash_tool_goes_through_the_runner_and_truncates(monkeypatch) -> None:
    calls: list[tuple] = []

    async def fake_exec(self, user_id, session, command, *, timeout_seconds):
        calls.append((user_id, session, command))
        return ExecResult(output="x" * 5000, exit_code=1, timed_out=False, truncated=False)

    monkeypatch.setattr(SandboxClient, "exec", fake_exec)
    outcome = await chat_tools.run_bash(
        _sandbox_client(), 7, "sess", {"command": "ls"}, max_chars=400, timeout_seconds=120
    )
    assert calls == [(7, "sess", "ls")]
    assert "characters omitted" in outcome.content
    assert outcome.content.endswith("[exit code 1]")
    assert outcome.is_error  # a nonzero exit is the model's error signal


def test_truncate() -> None:
    lines = "".join(f"line {index}\n" for index in range(200))
    cut = chat_tools.truncate(lines, 300)
    assert cut.startswith("line 0\n") and cut.rstrip().endswith("line 199")
    assert "omitted" in cut
    # Never past the cap: 81 is the last cap too small for the marker, 82 the first with it.
    text = "x" * 500
    for cap in (0, 10, 81, 82, 200):
        assert len(chat_tools.truncate(text, cap)) <= cap, cap
    assert chat_tools.truncate(text, 81) == "x" * 81
    assert "omitted" in chat_tools.truncate(text, 82)
    assert "omitted" in chat_tools.truncate(text, 200)
    # A cap of nothing is nothing, never `text[-0:]`, the whole text.
    assert chat_tools.truncate(text, 0) == ""


async def _running_library_parse(client: AsyncClient) -> SimpleNamespace:
    """A document's parse still running with its upload staged: the document, its job, the
    upload, and where the finished parse keeps it in the document's directory."""
    settings = client.app.state.settings
    document_id = await seed_document(client, client.user_id)
    async with client.app.state.sessionmaker() as db:
        job = await db.get(Job, (await db.get(Document, document_id)).job_id)
        upload = Path(settings.upload_dir) / f"{job.id}.pdf"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_bytes(b"%PDF-1.4 test")
        job.source_path = str(upload)
        job.status = "running"
        await db.commit()
        return SimpleNamespace(
            document_id=document_id,
            job_id=job.id,
            upload=upload,
            kept=client.root / "files" / "Papers" / "paper.pdf" / "source.pdf",
        )


@postgres_only
async def test_a_finished_library_parse_waits_for_the_filing_row_lock(signed_in) -> None:
    """Filing takes the job's row lock before it inserts: a finish that waits for it reads
    the row it filed as a recipient rather than deleting the upload under it (docs/jobs.md
    § Retention). PostgreSQL only: SQLite takes no row lock."""
    settings = signed_in.app.state.settings
    sessionmaker = signed_in.app.state.sessionmaker
    parse = await _running_library_parse(signed_in)
    async with sessionmaker() as holder:
        await holder.execute(select(Job.id).where(Job.id == parse.job_id).with_for_update())
        held = asyncio.create_task(
            parse_runner._finish(sessionmaker, parse.job_id, status="succeeded", settings=settings)
        )
        await asyncio.sleep(0.3)
        assert not held.done()
        await holder.rollback()
    await asyncio.wait_for(held, 10)
    assert parse.kept.read_bytes() == b"%PDF-1.4 test"


async def test_a_finished_library_parse_keeps_its_source_in_one_backup_snapshot(
    signed_in,
) -> None:
    """The retention exception (docs/jobs.md): the upload moves into the document dir. The
    terminal rows and the kept original land in one backup snapshot: a backup holding the
    lock keeps the finish — commit included — out until it is done."""
    settings = signed_in.app.state.settings
    sessionmaker = signed_in.app.state.sessionmaker
    parse = await _running_library_parse(signed_in)
    backup_path = _backup_lock_path(signed_in.root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_path.open("a+")
    fcntl.flock(backup, fcntl.LOCK_EX)
    finishing = asyncio.create_task(
        parse_runner._finish(sessionmaker, parse.job_id, status="succeeded", settings=settings)
    )
    await asyncio.sleep(0.3)
    assert not finishing.done()
    async with sessionmaker() as db:
        assert (await db.get(Job, parse.job_id)).status == "running"
    fcntl.flock(backup, fcntl.LOCK_UN)
    backup.close()
    await asyncio.wait_for(finishing, 10)
    assert parse.kept.read_bytes() == b"%PDF-1.4 test"
    assert not parse.upload.exists()
    original = workspace_documents.original_of(signed_in.root, parse.document_id)
    assert original is not None and original.read_bytes() == b"%PDF-1.4 test"


async def test_view_page_and_view_figure_read_the_documents_own_files(tmp_path, documents) -> None:
    root = tmp_path / "ws" / "1"
    paths = workspace_documents.DocumentPaths(root=root, relative=Path("files/paper"))
    paths.directory.mkdir(parents=True)
    # Only the shell's copy: the shell can plant one, and it is never what renders.
    shutil.copy(documents["text_pdf"], paths.directory / "source.pdf")
    paths.figures.mkdir()
    Image.new("RGB", (40, 30), "white").save(paths.figures / "p1_100_100_400_600.png")

    async def view_page(page: int) -> chat_tools.ToolOutcome:
        return await chat_tools.view_page(
            paths,
            {"page": page},
            document_id=5,
            page_count=3,
            render_slots=asyncio.Semaphore(1),
            decode_slot=asyncio.Semaphore(1),
            max_bytes=10_000_000,
            max_pixels=40_000_000,
            kind="pdf",
        )

    # The shell's `source.*` is never rendered: without the original, there is no page.
    unkept = await view_page(1)
    assert unkept.is_error and "kept no original" in unkept.content
    # The original kept outside the mount is what renders, even with the shell's copy gone.
    (paths.directory / "source.pdf").unlink()
    outside = sources_dir(root)
    outside.mkdir(parents=True)
    shutil.copy(documents["text_pdf"], outside / "5.pdf")
    from_original = await view_page(1)
    assert not from_original.is_error
    assert from_original.content[0]["type"] == "image"
    assert from_original.content[0]["source"]["media_type"] == "image/png"
    assert from_original.content[1]["text"] == "Page 1."
    too_far = await view_page(9)
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
    # A coordinate past five digits is no box, though clamping it would name a stored crop.
    (paths.figures / "p1_0_0_1000_1000.png").write_bytes(b"png")
    oversized = await chat_tools.view_figure(
        paths, {"page": 1, "bbox": [0, 0, 100_000, 1000]}, max_bytes=1_000_000
    )
    assert oversized.is_error
    assert oversized.content == "that bbox is not a usable box"


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
    assert committed.edit["path"] == "" and committed.edit["commit"]
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
    outcome = await _edit(
        root, {"command": "create", "path": "/workspace/files/d", "file_text": "x"}
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


def test_thinking_made_under_another_prompt_is_left_out_from_the_start() -> None:
    """docs/chat.md § History is append-only: every thinking block up to and including the
    last one bound to another system prompt or tool list — or to none, stored before blocks
    carried one — or before the last answer stored with a cutoff is left out, never one
    from the middle; the blocks made since are kept, and an assistant message left empty
    goes with them."""

    def thinking(text: str, binding: str | None = None) -> dict:
        block = {"type": "thinking", "thinking": text, "signature": f"sig-{text}"}
        return block if binding is None else {**block, "binding": binding}

    def text(value: str) -> dict:
        return {"type": "text", "text": value}

    use = {"type": "tool_use", "id": "t1", "name": "view_page", "input": {"page": 1}}
    result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
    history = [
        {"role": "user", "content": [text("one")]},
        {"role": "assistant", "content": [thinking("before bindings"), text("a")]},
        {"role": "user", "content": [text("two")]},
        # Bound to today's prompt, but before a stale block: it goes too.
        {"role": "assistant", "content": [thinking("now", "now"), use]},
        {"role": "user", "content": [result]},
        {
            "role": "assistant",
            "content": [
                thinking("then", "then"),
                {"type": "redacted_thinking", "data": "x", "binding": "then"},
            ],
        },
        {"role": "user", "content": [text("three")]},
        {"role": "assistant", "content": [thinking("since", "now"), text("b")]},
        {"role": "user", "content": [text("four")]},
    ]
    assert chat_runner.stale_thinking_left_out(history, "now") == [
        history[0],
        {"role": "assistant", "content": [text("a")]},
        history[2],
        {"role": "assistant", "content": [use]},
        history[4],
        history[6],
        history[7],
        history[8],
    ]
    # A history bound throughout to the request's prompt goes back as it is, and the key
    # that says so never reaches the upstream.
    current = [history[0], history[7], history[8]]
    assert chat_runner.stale_thinking_left_out(current, "now") == current
    assert chat_runner._api_blocks(history[7]["content"])[0] == {
        "type": "thinking",
        "thinking": "since",
        "signature": "sig-since",
    }
    # The answer to a call whose thinking the upstream dropped carries the cutoff: every
    # block before it goes, though bound to today's prompt; its own and later ones stay.
    cut = {**thinking("after", "now"), "thinking_cutoff": True}
    dropped = [
        history[0],
        {"role": "assistant", "content": [thinking("old", "now"), text("a")]},
        history[2],
        {"role": "assistant", "content": [cut, text("b")]},
        history[6],
        history[7],
    ]
    assert chat_runner.stale_thinking_left_out(dropped, "now") == [
        history[0],
        {"role": "assistant", "content": [text("a")]},
        history[2],
        dropped[3],
        history[6],
        history[7],
    ]
    assert "thinking_cutoff" not in chat_runner._api_blocks([cut])[0]


async def test_a_message_belongs_to_its_turn_and_a_live_thread_cannot_be_deleted(
    chatting,
) -> None:
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi", locale="zh-TW")
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
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
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
        await run_queued_turn(chatting)
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
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
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


def _round_ctx(
    *, round_budget: int = 32 << 20, turn_budget: int = 128 << 20, **fields
) -> SimpleNamespace:
    """What `_run_tools` reads from its context, made fresh per call: the turn's image
    budgets for the round and the whole turn, the document a page renders from, the
    conversation's shell, and the turn's writes in flight with the gate that starts them
    one at a time. `fields` replace any of these or add more."""
    return SimpleNamespace(
        **{
            "settings": SimpleNamespace(
                upload_max_bytes=1,
                chat_page_image_max_pixels=1,
                chat_tool_result_max_chars=1000,
                chat_bash_timeout_seconds=1,
            ),
            "turn": SimpleNamespace(id=uuid.uuid4(), user_id=1),
            "images": chat_runner.ImageBudget(
                round_budget, turn_budget, chat_runner.IMAGE_TURN_BUDGET_SPENT
            ),
            "round_admission": lambda calls: lambda index: True,
            "tools": [{"name": "view_page"}, {"name": "view_figure"}],
            "paths": None,
            "document": SimpleNamespace(id=1),
            "job": SimpleNamespace(page_count=12, kind="pdf"),
            "render_slots": None,
            "decode_slot": asyncio.Semaphore(1),
            "conversation": SimpleNamespace(id=1, session="s"),
            "sandbox": None,
            "writers": {},
            "write_gate": asyncio.Lock(),
            **fields,
        }
    )


async def _no_events(ctx, event, data) -> None:
    return None


async def _never_cancelled(ctx) -> bool:
    return False


async def test_a_rounds_images_never_exceed_the_budget(monkeypatch) -> None:
    async def fake_view_page(paths, tool_input, **kwargs):
        image = {"type": "image", "source": {"type": "base64", "data": "x" * 100}}
        return chat_tools.ToolOutcome([image, {"type": "text", "text": "Page."}])

    monkeypatch.setattr(chat_tools, "view_page", fake_view_page)
    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", _never_cancelled)
    ctx = _round_ctx(round_budget=250, turn_budget=1 << 30)
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
    assert sum(chat_runner._image_bytes(result["content"]) for result in kept) <= 250
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

    monkeypatch.setattr(chat_runner, "_run_tool", fake_run_tool)
    monkeypatch.setattr(chat_runner, "_cancelled", _never_cancelled)
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
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
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
    again = await send(chatting, conversation_id, text="more")
    assert again.status_code == 202
    second = uuid.UUID(again.json()["turn_id"])
    # The worker took it, the user cancelled mid-tool: until the worker winds it down, no
    # further message may be queued behind it.
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, second)
        turn.status = "running"
        await db.commit()
    await chatting.post(f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS)
    blocked = await send(chatting, conversation_id, text="wait")
    assert blocked.status_code == 409


async def test_a_file_with_a_live_conversation_cannot_be_deleted(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = await new_conversation(chatting)
    await send(chatting, conversation_id, text="hi")
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
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
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
    again = await send(chatting, conversation_id, text="more")
    assert again.status_code == 202


async def test_an_oversized_json_body_is_refused_before_it_is_read(make_client) -> None:
    client = make_client(content_max_bytes=1_000)
    user_id = (await sign_in(client)).user_id
    document_id = await seed_document(client, user_id)
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
    viewed = await _edit(root, {"command": "view", "path": "/workspace/files/d/link.md"})
    assert viewed.is_error and "sk-not-for-you" not in str(viewed.content)
    written = await _edit(
        root,
        {
            "command": "str_replace",
            "path": "/workspace/files/d/link.md",
            "old_str": "x",
            "new_str": "y",
        },
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


async def test_the_body_cap_ignores_a_multipart_label_off_the_upload_routes(make_client) -> None:
    client = make_client(content_max_bytes=1_000)
    user_id = (await sign_in(client)).user_id
    document_id = await seed_document(client, user_id)
    spoofed = await client.put(
        f"/api/library/documents/{document_id}/content",
        content=b"x" * 200_000,
        headers={**CSRF_HEADERS, "Content-Type": "multipart/form-data; boundary=abc"},
    )
    assert spoofed.status_code == 413


async def test_the_agent_page_lists_only_the_tools_the_deployment_has(make_client) -> None:
    # A shell needs both a runner and a plan that grants one (docs/billing.md § Plans).
    with_runner = make_client(
        sandbox_url="http://sandbox:8100",
        sandbox_token="runner-token-not-a-real-one",
        **PAY_SETTINGS,
    )
    user_id = (await sign_in(with_runner)).user_id
    await grant_plan(with_runner.app.state.sessionmaker, user_id)
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
        "skill",
        "subagent",
        "memory",
        "read_url",
        "download_file",
        "convert_file",
        "insert_image",
    ]


async def test_a_file_with_a_live_conversation_cannot_be_renamed(chatting) -> None:
    document_id = chatting.document_id
    conversation_id = await new_conversation(chatting)
    await send(chatting, conversation_id, text="hi")
    refused = await chatting.put(
        f"/api/library/documents/{document_id}", json={"name": "other.pdf"}, headers=CSRF_HEADERS
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
    # A 1-bit image compresses to a few kilobytes; its pixels would not.
    buffer = io.BytesIO()
    Image.new("1", (7000, 7000)).save(buffer, format="PNG")
    assert len(buffer.getvalue()) < 100_000
    with pytest.raises(chat_tools.ImageTooLarge):
        chat_tools._model_image(buffer.getvalue(), 40_000_000)
    small = io.BytesIO()
    Image.new("RGB", (20, 10), (255, 0, 0)).save(small, format="PNG")
    assert chat_tools._model_image(small.getvalue(), 40_000_000).data.startswith(b"\x89PNG")
    # The cap is the deployment's (`CHAT_PAGE_IMAGE_MAX_PIXELS`), not a constant.
    with pytest.raises(chat_tools.ImageTooLarge):
        chat_tools._model_image(small.getvalue(), 100)


@respx.mock
async def test_a_kano_proxy_model_answered_under_its_bare_id_is_not_a_fallback(chatting) -> None:
    """Kano Proxy routes `provider/model`; the upstream answers with the bare id. That is
    the same model, and a real fallback is followed under the provider's name."""
    conversation_id = await new_conversation(chatting)
    requests: list[dict] = []
    turns = iter(
        [
            assistant_turn([{"type": "text", "text": "Hi."}], "end_turn", model="claude-opus-5-5"),
            assistant_turn([{"type": "text", "text": "Again."}], "end_turn", model="claude-opus-5"),
            assistant_turn([{"type": "text", "text": "More."}], "end_turn", model="claude-opus-5"),
        ]
    )

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(turns))

    respx.post(MESSAGES_URL).mock(side_effect=answer)

    async def answered_on(text: str) -> str:
        sent = await send(chatting, conversation_id, text=text)
        assert sent.status_code == 202, sent.text
        await run_queued_turn(chatting)
        async with chatting.app.state.sessionmaker() as db:
            return (await db.get(Conversation, conversation_id)).model

    assert await answered_on("hi") == "claude-code/claude-opus-5-5"
    assert await answered_on("again") == "claude-code/claude-opus-5"
    assert await answered_on("more") == "claude-code/claude-opus-5"
    assert [request["model"] for request in requests] == [
        "claude-code/claude-opus-5-5",
        "claude-code/claude-opus-5-5",
        "claude-code/claude-opus-5",
    ]


@respx.mock
async def test_an_answer_under_an_unpriced_name_is_kept_as_the_model_asked_for(
    chatting,
) -> None:
    """A group alias the upstream answers under the target's own id (`claude-sonnet-5.5`
    answered as `claude-sonnet-5-5`): following it would strand the conversation on a name
    no turn may run, and bill the call nothing."""
    conversation_id = await new_conversation(chatting)
    requests: list[dict] = []
    turns = iter(
        [
            assistant_turn(
                [{"type": "text", "text": "Hi."}], "end_turn", model="claude-unpriced-7"
            ),
            assistant_turn(
                [{"type": "text", "text": "Again."}], "end_turn", model="claude-unpriced-7"
            ),
        ]
    )

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(turns))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    for text in ("hi", "more"):
        sent = await send(chatting, conversation_id, text=text)
        assert sent.status_code == 202, sent.text
        await run_queued_turn(chatting)
    assert [request["model"] for request in requests] == ["claude-code/claude-opus-5-5"] * 2
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(Conversation, conversation_id)).model == "claude-code/claude-opus-5-5"
        logged = (await db.execute(select(UsageLog.model))).scalars().all()
    assert set(logged) == {"claude-code/claude-opus-5-5"}
    turns_now = (await chatting.get(f"/api/conversations/{conversation_id}")).json()["turns"]
    assert [turn["status"] for turn in turns_now] == ["succeeded", "succeeded"]
    assert all(turn["credits"] > 0 for turn in turns_now)


@postgres_only
async def test_the_history_cap_counts_bytes_not_characters(chatting) -> None:
    """PostgreSQL only: SQLite stores non-ASCII JSON escaped, so there a message's bytes
    equal its characters and the test could not tell the two apart."""
    settings = chatting.app.state.settings
    conversation_id = await new_conversation(chatting)
    # 2000 two-byte characters: about 2040 characters stored, about 4040 bytes.
    sent = await send(chatting, conversation_id, text="é" * 2000)
    turn_id = uuid.UUID(sent.json()["turn_id"])
    settings.chat_history_max_bytes = 3000
    await run_queued_turn(chatting)
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed" and "start a new conversation" in turn.error


async def test_a_conversation_past_the_history_cap_is_refused(chatting) -> None:
    settings = chatting.app.state.settings
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hello " * 20)
    turn_id = uuid.UUID(sent.json()["turn_id"])
    settings.chat_history_max_bytes = 50
    await run_queued_turn(chatting)
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed"
        assert "start a new conversation" in turn.error
    # Past the cap, the conversation takes no further message: the overflow is not grown
    # by capped-size sends that each fail a turn (docs/chat.md § History is append-only).
    refused = await send(chatting, conversation_id, text="once more")
    assert refused.status_code == 409
    assert "start a new conversation" in refused.json()["error"]["message"]
    detail = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert len(detail["messages"]) == 1 and len(detail["turns"]) == 1


async def test_a_command_the_runner_could_not_settle_commits_no_tree() -> None:
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
    settings = chatting.app.state.settings
    document_id = chatting.document_id
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
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

    class Runner:
        restarted: list[tuple[int, str]] = []

        async def restart(self, user_id: int, session: str) -> None:
            self.restarted.append((user_id, session))

    runner = Runner()
    # No runner to stop the shell the marker names: the VM may still run the command,
    # so the turn and the marker wait rather than the tree being declared safe.
    async with chatting.app.state.sessionmaker() as db:
        assert await fail_orphaned_turns(db, None, settings) == (0, {chatting.user_id})
        assert (await db.get(ChatTurn, turn_id)).finished_at is None
    assert writing_marker(paths.root).exists()
    # A tree that cannot be committed leaves the turn for the next pass: finishing it
    # would let the next revision carry these changes as its own.
    monkeypatch.setattr(git, "commit_all", failing)
    async with chatting.app.state.sessionmaker() as db:
        assert await fail_orphaned_turns(db, runner, settings) == (0, {chatting.user_id})
        assert (await db.get(ChatTurn, turn_id)).finished_at is None
    assert writing_marker(paths.root).exists()  # still recovering: host writes wait
    monkeypatch.setattr(git, "commit_all", real)

    async with chatting.app.state.sessionmaker() as db:
        assert await fail_orphaned_turns(db, runner, settings) == (1, set())
        assert runner.restarted and runner.restarted[-1][0] == chatting.user_id
        turn = await db.get(ChatTurn, turn_id)
        assert turn.status == "failed" and turn.finished_at is not None
    assert not writing_marker(paths.root).exists()  # recorded: host writes resume
    head = git.log(paths.root)[0]
    assert head.message == ORPHAN_REVISION_MESSAGE
    assert head.author == f"agent:{conversation_id}"
    assert set(head.files) == {"files/Papers/paper.pdf/helper.py", "files/Papers/paper.pdf/doc.md"}
    # The tree is clean: a later tool's revision carries only its own changes.
    assert git.commit_all(paths.root, "user", "anything left?") is None
    assert _paper_history(chatting)[0].message == ORPHAN_REVISION_MESSAGE


async def test_a_worker_starting_finishes_the_turns_a_dead_one_left(chatting) -> None:
    settings = chatting.app.state.settings
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
    turn_id = uuid.UUID(sent.json()["turn_id"])
    async with chatting.app.state.sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        turn.status = "running"  # claimed by a worker that then died
        await db.commit()

    class Runner:
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
        assert await fail_orphaned_turns(db, runner, settings) == (0, {chatting.user_id})
        await db.commit()
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(ChatTurn, turn_id)).finished_at is None
        runner.away = False
        assert await fail_orphaned_turns(db, runner, settings) == (1, set())
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
        assert await fail_orphaned_turns(db, None, settings) == (0, set())
    # The conversation takes messages again.
    again = await send(chatting, conversation_id, text="more")
    assert again.status_code == 202


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
    # Beneath an excluded name as well as at it: git ignores these at any depth.
    for nested in (
        "/workspace/files/x/source.pdf/child.txt",
        "/workspace/files/.sr-tmp-abc/doc.md",
        "/workspace/files/x/figures/deep/note.md",
    ):
        refused = await _edit(
            root, {"command": "create", "path": nested, "file_text": "x"}, max_bytes=10_000
        )
        assert refused.is_error, nested
    git.ensure_repo(root)
    for path in ("files/paper/figures/p1.png", "files/paper/source.pdf", "files/paper/.sr-tmp-abc"):
        refused = await _edit(
            root,
            {"command": "create", "path": f"/workspace/{path}", "file_text": "x"},
            max_bytes=10_000,
        )
        assert refused.is_error, path
        assert not (root / path).exists()


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
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
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


async def test_the_conversation_is_read_in_batches(chatting, monkeypatch) -> None:
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
    turn_id = uuid.UUID(sent.json()["turn_id"])
    async with chatting.app.state.sessionmaker() as db:
        for index in range(5):
            db.add(
                Message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=[{"type": "text", "text": str(index)}],
                    turn_id=turn_id,
                )
            )
        await fail_turn(db, turn_id, "over")
        await db.commit()
    # Six rows in batches of three: the last batch is full, and an empty one ends the read.
    monkeypatch.setattr(chat_events, "REPLAY_BATCH_SIZE", 3)
    body = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert [message["role"] for message in body["messages"]] == ["user"] + ["assistant"] * 5
    assert [message["content"][0]["text"] for message in body["messages"][1:]] == [
        str(index) for index in range(5)
    ]
    ids = [message["id"] for message in body["messages"]]
    assert ids == sorted(ids) and len(set(ids)) == 6
    assert body["conversation"]["compactable"] is True


async def test_a_delete_whose_commit_fails_keeps_the_edited_files(chatting, monkeypatch) -> None:
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
    assert _paper_history(chatting)[0].message.startswith("restore ")


async def test_a_shell_revision_beyond_the_document_waits_for_every_turn(chatting) -> None:
    first_id = chatting.document_id
    settings = chatting.app.state.settings
    async with chatting.app.state.sessionmaker() as db:
        first = await db.get(Document, first_id)
        job_id, folder_id = first.job_id, first.folder_id
    # A copy: a second document on the same parse.
    second = await chatting.post(
        f"/api/library/documents/{first_id}/copy",
        json={"folder_id": folder_id},
        headers=CSRF_HEADERS,
    )
    assert second.status_code == 201, second.text
    assert second.json()["job_id"] == str(job_id)
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
    await send(chatting, conversation_id, text="hi")
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

    monkeypatch.setattr(chat_tools, "view_page", fake_view_page)
    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", _never_cancelled)
    ctx = _round_ctx(round_budget=1000, turn_budget=250)
    one_round = [
        {"type": "tool_use", "id": str(index), "name": "view_page", "input": {"page": index + 1}}
        for index in range(3)
    ]
    # The round's budget would take all three; the turn's takes two across every round.
    first = await chat_runner._run_tools(ctx, one_round, set())
    assert len([r for r in first if not r.get("is_error")]) == 2
    assert ctx.images.total == 200
    second = await chat_runner._run_tools(ctx, one_round, set())
    assert all(r.get("is_error") for r in second)
    assert all(r["content"] == chat_runner.IMAGE_TURN_BUDGET_SPENT for r in second)
    assert ctx.images.total == 200


async def test_a_lost_shell_status_is_an_error(monkeypatch) -> None:
    async def fake_exec(self, user_id, session, command, *, timeout_seconds):
        return ExecResult(output="half", exit_code=None, timed_out=False, truncated=False)

    monkeypatch.setattr(SandboxClient, "exec", fake_exec)
    outcome = await chat_tools.run_bash(
        _sandbox_client(), 7, "sess", {"command": "exit 1"}, max_chars=400, timeout_seconds=120
    )
    assert outcome.is_error
    assert "without reporting a status" in outcome.content


async def test_a_shell_command_holds_the_workspace_lock(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws" / "1"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    release = asyncio.Event()
    started = asyncio.Event()

    async def fake_run_bash(client, user_id, session, tool_input, *, max_chars, timeout_seconds):
        started.set()
        await release.wait()
        return chat_tools.ToolOutcome("done")

    monkeypatch.setattr(chat_tools, "run_bash", fake_run_bash)
    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", _never_cancelled)
    ctx = _round_ctx(paths=SimpleNamespace(root=root), sandbox=object())
    tool_uses = [{"type": "tool_use", "id": "b1", "name": "bash", "input": {"command": "sleep"}}]
    running = asyncio.create_task(chat_runner._run_tools(ctx, tool_uses, set()))
    # Bounded: a run that fails before the command starts must fail the test, not hang it.
    await asyncio.wait_for(started.wait(), 5)

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
    conversation_id = await new_conversation(chatting)
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
    sent = await send(chatting, conversation_id, text="hi")
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
    root = tmp_path / "ws" / "1"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    seen: list[bool] = []

    async def fake_run_bash(client, user_id, session, tool_input, *, max_chars, timeout_seconds):
        return chat_tools.ToolOutcome("moved")

    async def fake_commit(root_, outcome, tool_input, author, *, max_chars):
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
    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", _never_cancelled)
    ctx = _round_ctx(paths=SimpleNamespace(root=root), sandbox=object(), sessionmaker=Sessions())
    tool_uses = [{"type": "tool_use", "id": "b1", "name": "bash", "input": {"command": "mv a b"}}]
    await chat_runner._run_tools(ctx, tool_uses, set())
    assert seen == [True]


def test_prompt_facts_are_substituted_in_one_pass() -> None:
    rendered = chat_agent.render_system_prompt(
        "Edit {document_path}; network: {network}.",
        {"document_path": "files/{network}/doc.md", "network": "none"},
    )
    assert rendered == "Edit files/{network}/doc.md; network: none."


async def test_a_message_or_selection_past_the_deployments_cap_is_refused(chatting) -> None:
    settings = chatting.app.state.settings
    settings.chat_message_max_chars = 10
    settings.chat_selection_max_chars = 5
    conversation_id = await new_conversation(chatting)
    long = await send(chatting, conversation_id, text="x" * 11)
    assert long.status_code == 400
    wide = await send(chatting, conversation_id, text="hi", selection="y" * 6)
    assert wide.status_code == 400
    fits = await send(chatting, conversation_id, text="hi", selection="yyy")
    assert fits.status_code == 202, fits.text


async def test_a_thread_for_a_document_deleted_underneath_is_a_404(chatting, monkeypatch) -> None:
    document_id = chatting.document_id
    original = chat_routes.live_text_document

    async def deleted_underneath(db, user_id, did):
        row = await original(db, user_id, did)
        async with chatting.app.state.sessionmaker() as other:
            await other.delete(await other.get(Document, did))
            await other.commit()
        return row

    monkeypatch.setattr(chat_routes, "live_text_document", deleted_underneath)
    created = await chatting.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    assert created.status_code == 404, created.text


async def test_the_turns_writes_run_one_at_a_time_and_none_starts_after_a_stop(
    monkeypatch,
) -> None:
    stop = asyncio.Event()

    async def cancelled(ctx) -> bool:
        return stop.is_set()

    monkeypatch.setattr(chat_runner, "_cancelled", cancelled)
    ctx = SimpleNamespace(writers={}, write_gate=asyncio.Lock())
    running: list[str] = []
    done: list[str] = []

    async def write(name: str) -> str:
        running.append(name)
        # The agent's own and every subagent's writes share the gate: never two at once.
        assert running == [name]
        await asyncio.sleep(0.01)
        running.remove(name)
        done.append(name)
        return name

    written = await asyncio.gather(
        *(chat_runner.run_writer(ctx, "bash", lambda name=name: write(name)) for name in "abc")
    )
    assert written == ["a", "b", "c"] and done == ["a", "b", "c"]
    assert ctx.writers == {}
    stop.set()
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner.run_writer(ctx, "bash", lambda: write("d"))
    assert done == ["a", "b", "c"]


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
        writers={writer: "str_replace_based_edit_tool"},
    )
    await chat_runner._settle_writers(ctx)
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
        writers={writer: "bash"},
        sandbox=FakeSandbox(),
        turn=SimpleNamespace(id="t", user_id=7),
        conversation=SimpleNamespace(session="s"),
    )
    # Past the grace: the command is killed where it runs, and the writer is still
    # waited for to its end — the turn is never finished over a write in flight.
    await chat_runner._settle_writers(ctx)
    assert restarted == [(7, "s")]
    assert writer.done() and writer.result() == "committed"


@respx.mock
async def test_an_answer_cut_at_the_output_limit_fails_the_turn(chatting) -> None:
    conversation_id = await new_conversation(chatting)
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
    await send(chatting, conversation_id, text="hi")
    await run_queued_turn(chatting)
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
    await send(chatting, conversation_id, text="again")
    await run_queued_turn(chatting)
    thread = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["turns"][-1]["status"] == "failed"
    assert "pause_turn" in thread["turns"][-1]["error"]


@respx.mock
async def test_the_sandbox_client_takes_an_empty_success_answer() -> None:
    client = _sandbox_client()
    stop = respx.post("http://sandbox:8100/sandboxes/7/stop")
    respx.post("http://sandbox:8100/sandboxes/7/reset").mock(
        return_value=httpx.Response(200, content=b"")
    )
    # Stop and reset answer with no body: done, not unreadable.
    stop.mock(return_value=httpx.Response(204))
    await client.stop(7)
    await client.reset(7)
    stop.mock(return_value=httpx.Response(200, content=b"<html>"))
    with pytest.raises(SandboxUnavailable):
        await client.stop(7)


async def test_a_conversation_with_a_stored_model_keeps_it_whatever_the_page_says(
    chatting,
) -> None:
    """The conversation's model is its first turn's, though the reader's pick is no longer
    offered; a fresh conversation with no pick starts on the first offered (docs/chat.md,
    docs/agents.md)."""
    conversation_id = await new_conversation(chatting)
    async with chatting.app.state.sessionmaker() as db:
        conversation = await db.get(Conversation, conversation_id)
        conversation.model = "claude-stored"
        setting = (
            await db.execute(select(AgentSetting).where(AgentSetting.agent == "chat"))
        ).scalar_one()
        setting.model = "vendor/delisted"  # no longer offered
        await db.commit()
    accepted = await send(chatting, conversation_id, text="hi")
    assert accepted.status_code == 202, accepted.text
    fresh = await new_conversation(chatting)
    restored = await chatting.put(
        "/api/agents/chat/settings", json={"model": None}, headers=CSRF_HEADERS
    )
    assert restored.status_code == 200, restored.text
    started = await send(chatting, fresh, text="hi")
    assert started.status_code == 202, started.text
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(Conversation, conversation_id)).model == "claude-stored"
        assert (await db.get(Conversation, fresh)).model == "claude-code/claude-opus-5-5"
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
    replaced = await _edit(
        root,
        {
            "command": "str_replace",
            "path": "/workspace/files/d/doc.md",
            "old_str": "beta",
            "new_str": "gamma",
        },
    )
    assert replaced.is_error
    assert (root / "files" / "d" / "doc.md").read_text() == "alpha\nbeta\n"


def test_the_prompt_lists_only_the_tools_the_turn_has() -> None:
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


def test_the_prompt_names_the_sites_and_promises_an_install_only_with_pypi() -> None:
    pypi = ("pypi.org", "files.pythonhosted.org")
    # No network — the shipped default on both examples — or an empty list reaches nothing.
    assert chat_agent.network_note("none", pypi) == chat_agent.NETWORK_NONE
    assert chat_agent.network_note("ema-paper-sandbox", ()) == chat_agent.NETWORK_NONE
    note = chat_agent.network_note("ema-paper-sandbox", (*pypi, "github.com"))
    assert "pypi.org, files.pythonhosted.org, github.com" in note
    assert "`uv pip install` works" in note
    # The index without the host its files come from installs nothing; a wildcard covers
    # the names under it, never the name itself.
    for hosts in (("pypi.org", "github.com"), ("*.pypi.org", "files.pythonhosted.org")):
        other = chat_agent.network_note("ema-paper-sandbox", hosts)
        assert "`uv pip install`" not in other and "fails at once" in other
    assert "`uv pip install` works" in chat_agent.network_note(
        "ema-paper-sandbox", ("pypi.org", "*.pythonhosted.org")
    )
    rendered = chat_agent.render_system_prompt(chat_agent.DEFAULT_SYSTEM_PROMPT, {"network": note})
    assert "through a proxy: pypi.org" in rendered and "{network}" not in rendered


async def test_the_panel_gets_the_selection_without_its_scaffolding(chatting) -> None:
    conversation_id = await new_conversation(chatting)
    sent = await send(
        chatting, conversation_id, text="Fix the Greedy row", selection="| Greedy | 3450 |"
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
    commit = _paper_history(chatting)[0].commit
    conversation_id = await new_conversation(chatting)
    await send(chatting, conversation_id, text="hi")
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
    conversation_id = await new_conversation(chatting)
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


def test_the_exec_deadline_covers_a_cold_start() -> None:
    client = _sandbox_client(sandbox_start_timeout_seconds=200, sandbox_exec_grace_seconds=30)
    assert client.exec_timeout(120) == 350


async def test_a_command_the_runner_could_not_confirm_over_keeps_the_marker_until_a_restart_does(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = path_settings(tmp_path, sandbox_url="http://sandbox:8100", sandbox_token="t")
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await workspace_documents.materialize_document(db, settings, document)

    class Shell:
        restart_ok = False
        restarted: list[str] = []
        stopped: list[int] = []
        runs = 0

        async def stop(self, user_id):
            self.stopped.append(user_id)

        async def exec(self, user_id, session, command, *, timeout_seconds):
            self.runs += 1
            (paths.directory / "half.txt").write_text(f"being written, run {self.runs}\n")
            raise SandboxUnavailable("the sandbox VM could not be stopped")

        async def restart(self, user_id, session):
            self.restarted.append(session)
            if not self.restart_ok:
                raise SandboxUnavailable("the sandbox VM could not be stopped")

    monkeypatch.setattr(chat_runner, "_emit", _no_events)
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
    result = await chat_runner._run_tool_unguarded(ctx, block, set(), lambda outcome: outcome)
    assert result["is_error"] and "takes no other write" in result["content"]
    assert shell.restarted == ["s"]
    assert read_writing(paths.root) == (3, "s")
    assert git.log(paths.root)[0].message.startswith("materialize")
    async with sessionmaker() as db:
        with pytest.raises(ApiError):
            await workspace_documents.read_content(db, settings, document)

    # The recovery a worker runs at start: the restart confirms the command dead, the
    # tree is recorded in the conversation's name, the marker goes. No runner to ask, or
    # one that does not answer, leaves it for the next pass.
    assert await recover_unsettled_commands(settings, None) == {user.id}
    assert await recover_unsettled_commands(settings, shell) == {user.id}  # runner still away
    shell.restart_ok = True
    assert await recover_unsettled_commands(settings, shell) == set()
    assert not writing_marker(paths.root).exists()
    head = git.log(paths.root)[0]
    assert head.message == UNSETTLED_REVISION_MESSAGE and head.author == "agent:3"
    assert "files/Papers/paper.pdf/half.txt" in head.files

    # Within a turn, a restart that confirms the kill records the tree right away.
    result = await chat_runner._run_tool_unguarded(ctx, block, set(), lambda outcome: outcome)
    assert result["is_error"] and "restarted" in result["content"]
    assert not writing_marker(paths.root).exists()
    assert git.log(paths.root)[0].message == "bash: python write.py"

    # A marker that stands but names no shell is not a clean tree: the VM is stopped
    # whole before the tree is recorded, and without a runner the workspace stays closed.
    writing_marker(paths.root).write_text("")
    (paths.directory / "late.txt").write_text("from a shell unknown\n")
    assert await recover_unsettled_commands(settings, None) == {user.id}
    assert writing_marker(paths.root).exists()
    assert await recover_unsettled_commands(settings, shell) == set()
    assert shell.stopped == [user.id]
    assert not writing_marker(paths.root).exists()
    head = git.log(paths.root)[0]
    assert head.message == UNSETTLED_REVISION_MESSAGE and head.author == "user"
    assert "files/Papers/paper.pdf/late.txt" in head.files


async def test_a_figure_read_is_bounded_by_the_image_budget_not_the_text_cap(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "ws" / "1"
    paths = workspace_documents.DocumentPaths(root=root, relative=Path("files/paper"))
    paths.figures.mkdir(parents=True)
    # A legitimate crop far larger than the cap on doc.md and notes.md.
    Image.radial_gradient("L").resize((900, 900)).save(paths.figures / "p1_0_0_1000_1000.png")
    crop_bytes = (paths.figures / "p1_0_0_1000_1000.png").stat().st_size
    assert crop_bytes > 4096

    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            chat_tool_result_max_chars=1000,
            content_max_bytes=4096,
            chat_image_round_max_bytes=32 << 20,
        ),
        paths=paths,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        conversation=SimpleNamespace(id=1, session="s"),
        tools=[{"name": "view_figure"}],
    )
    result = await chat_runner._run_tool_unguarded(
        ctx,
        {"id": "f1", "name": "view_figure", "input": {"page": 1, "bbox": [0, 0, 1000, 1000]}},
        set(),
        lambda outcome: outcome,
    )
    assert "is_error" not in result
    assert result["content"][0]["type"] == "image"


async def test_a_shell_command_stands_marked_as_writing_for_its_whole_run(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = path_settings(tmp_path)
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

    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    ctx = SimpleNamespace(
        settings=settings,
        sessionmaker=sessionmaker,
        sandbox=Shell(),
        paths=paths,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=user.id),
        conversation=SimpleNamespace(id=3, session="s"),
    )
    result = await chat_runner._run_tool_unguarded(
        ctx,
        {"id": "t1", "name": "bash", "input": {"command": "touch made.txt"}},
        set(),
        lambda outcome: outcome,
    )
    assert seen == [True]
    assert not writing_marker(paths.root).exists()  # the command is over and recorded
    assert result["content"] == "ok" and "is_error" not in result
    assert git.log(paths.root)[0].message == "bash: touch made.txt"


@respx.mock
async def test_a_request_the_runner_refuses_is_settled_not_unconfirmed() -> None:
    client = _sandbox_client()
    route = respx.post("http://sandbox:8100/sandboxes/7/exec")
    # A 4xx is the runner's own refusal, before anything ran: the token, a command past
    # its validation cap (docs/chat.md § Tools).
    route.mock(return_value=httpx.Response(422, json={"detail": "command too long"}))
    with pytest.raises(SandboxRejected):
        await client.exec(7, "s", "x" * 10, timeout_seconds=5)
    # A 5xx leaves it unknown whether the command ran.
    route.mock(return_value=httpx.Response(503, json={"detail": "unstoppable"}))
    with pytest.raises(SandboxUnavailable) as caught:
        await client.exec(7, "s", "x", timeout_seconds=5)
    assert not isinstance(caught.value, SandboxRejected)

    class Client:
        async def exec(self, *args, **kwargs):
            raise SandboxRejected("the sandbox runner refused the request (422)")

    outcome = await chat_tools.run_bash(
        Client(), 1, "s", {"command": "x"}, max_chars=200, timeout_seconds=5
    )
    # Settled: no marker stays, no later host write is refused over a command that never ran.
    assert outcome.is_error and outcome.settled is True
    assert "rejected the command" in outcome.content


async def test_a_turn_another_worker_finished_is_left_as_it_is(chatting) -> None:
    """The worker that lost the lock finds its turn finished by the one that took it
    (docs/jobs.md § Queue): its loop stops, and its own terminal write changes nothing."""
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="hi")
    turn_id = uuid.UUID(sent.json()["turn_id"])
    sessionmaker = chatting.app.state.sessionmaker
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        turn.status = "running"
        await db.commit()
    stale = SimpleNamespace(sessionmaker=sessionmaker, turn=SimpleNamespace(id=turn_id))
    assert await chat_runner._cancelled(stale) is False
    async with sessionmaker() as db:
        await fail_turn(db, turn_id, "worker restarted")
    assert await chat_runner._cancelled(stale) is True
    # Nor does it store an answer, or move the conversation to a fallback model: the
    # check and the write are one transaction (docs/jobs.md § Queue).
    stale.conversation = SimpleNamespace(id=conversation_id, model="m")
    stale.model = "m"
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._store(
            stale, "assistant", [{"type": "text", "text": "late"}], model="fallback"
        )
    with pytest.raises(chat_runner.TurnCancelled):
        await chat_runner._follow_model(stale, "fallback")
    await chat_runner._finish(sessionmaker, turn_id, status="succeeded")
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert (turn.status, turn.error) == ("failed", "worker restarted")
        stored = (
            await db.execute(
                select(Message.id).where(Message.turn_id == turn_id, Message.role == "assistant")
            )
        ).all()
        assert stored == []
        assert (await db.get(Conversation, conversation_id)).model != "fallback"
        events = (
            (await db.execute(select(ChatEvent).where(ChatEvent.turn_id == turn_id)))
            .scalars()
            .all()
        )
    assert [event.event for event in events] == ["turn_error"]


async def test_a_failed_create_leaves_no_directory_behind(tmp_path, monkeypatch) -> None:
    """A create whose revision cannot be recorded is undone whole: the file and the
    directories made for it, so reconciliation finds no folder the tool reported it
    could not make (docs/workspace.md § Revisions)."""
    root = tmp_path / "ws" / "1"
    (root / "files" / "kept").mkdir(parents=True)
    git.ensure_repo(root)

    def refusing(*args, **kwargs):
        raise git.GitError("disk full")

    monkeypatch.setattr(chat_tools.git, "commit_paths", refusing)
    outcome = await _edit(
        root,
        {"command": "create", "path": "/workspace/files/kept/New/deeper/a.md", "file_text": "x"},
    )
    assert outcome.is_error
    assert not (root / "files" / "kept" / "New").exists()
    assert (root / "files" / "kept").is_dir()


async def test_a_message_carries_the_documents_revision_and_the_locale(chatting) -> None:
    """The per-turn context rides in the user message, marked, so the model learns of an
    edit made meanwhile and of the reader's language without the system prompt — the
    cached prefix — changing (docs/chat.md § Prompt caching); the thread never shows it."""
    document_id = chatting.document_id
    conversation_id = await new_conversation(chatting)
    content = (await chatting.get(f"/api/library/documents/{document_id}/content")).json()
    sent = await send(chatting, conversation_id, text="hi", locale="zh-TW")
    assert sent.status_code == 202, sent.text
    async with chatting.app.state.sessionmaker() as db:
        stored = (
            await db.execute(
                select(Message).where(
                    Message.conversation_id == conversation_id, Message.role == "user"
                )
            )
        ).scalar_one()
    turn_blocks = [
        block for block in stored.content if block.get("context") == chat_routes.TURN_CONTEXT
    ]
    assert len(turn_blocks) == 1
    assert f"document revision: {content['revision']}" in turn_blocks[0]["text"]
    assert "reader locale: zh-TW" in turn_blocks[0]["text"]
    # Nothing of it reaches the thread.
    detail = (await chatting.get(f"/api/conversations/{conversation_id}")).json()
    user_blocks = detail["messages"][0]["content"]
    assert all(block.get("context") != chat_routes.TURN_CONTEXT for block in user_blocks)
    # The system prompt carries no locale: a switch of language leaves the prefix alone.
    assert "{locale}" not in CHAT.default_prompt_template
    assert "{locale}" not in chat_agent.PROMPT_TOKENS


async def test_a_listener_refused_listen_is_closed(monkeypatch) -> None:
    """A connection that opened but could not LISTEN (a proxy in between) is closed on the
    way to polling, or every stream opened under that condition would leak one."""
    closed: list[bool] = []

    class Refusing:
        async def add_listener(self, channel, callback):
            raise asyncpg.PostgresError("LISTEN is not supported here")

        async def close(self):
            closed.append(True)

    async def connect(dsn):
        return Refusing()

    monkeypatch.setattr(asyncpg, "connect", connect)
    listener = await open_listener(
        "postgresql+asyncpg://u:p@db/sightread", "sr_conv_7", asyncio.Event(), "conversation"
    )
    assert listener is None and closed == [True]


async def test_an_accepted_message_moves_the_conversation_up_the_list(chatting) -> None:
    conversation_id = await new_conversation(chatting)
    sent = await send(chatting, conversation_id, text="one")
    before = (await chatting.get(f"/api/conversations/{conversation_id}")).json()["conversation"][
        "updated_at"
    ]
    async with chatting.app.state.sessionmaker() as db:
        await fail_turn(db, uuid.UUID(sent.json()["turn_id"]), "worker stopped")
    again = await send(chatting, conversation_id, text="two")
    assert again.status_code == 202, again.text
    after = (await chatting.get(f"/api/conversations/{conversation_id}")).json()["conversation"][
        "updated_at"
    ]
    # The second message changed neither title nor model; the timestamp moves all the same.
    assert after > before


async def test_the_list_holds_only_conversations_with_a_message(chatting) -> None:
    """A conversation is listed once it holds a message. An empty one — a first send that
    was refused — stays readable by its id and takes the retry, but is no thread to pick
    (docs/chat.md § Conversations, turns, events)."""
    listing = f"/api/library/documents/{chatting.document_id}/conversations"
    empty = (await chatting.post(listing, headers=CSRF_HEADERS)).json()["id"]
    talked = (await chatting.post(listing, headers=CSRF_HEADERS)).json()["id"]
    sent = await send(chatting, talked, text="Summarize it")
    assert sent.status_code == 202, sent.text

    listed = (await chatting.get(listing)).json()["conversations"]
    assert [(row["id"], row["title"]) for row in listed] == [(talked, "Summarize it")]
    assert (await chatting.get(f"/api/conversations/{empty}")).status_code == 200

    # The retry lands on the empty row, which is listed from then on, first.
    retried = await send(chatting, empty, text="Again")
    assert retried.status_code == 202, retried.text
    listed = (await chatting.get(listing)).json()["conversations"]
    assert [row["id"] for row in listed] == [empty, talked]


async def test_a_rename_is_one_clean_line_and_keeps_the_order(chatting) -> None:
    """The reader's title: cleaned like a memory's text, cut at the cap `/api/me` hands the
    field, never empty, and written without touching `updated_at`, so the thread keeps its
    place in the list — a turn queued on it refuses nothing (docs/chat.md § Conversations)."""
    listing = f"/api/library/documents/{chatting.document_id}/conversations"
    ids = []
    for text in ("Explain table 2\nin detail", "Summarize it"):
        made = (await chatting.post(listing, headers=CSRF_HEADERS)).json()["id"]
        sent = await send(chatting, made, text=text)
        assert sent.status_code == 202, sent.text
        ids.append(made)
    first, second = ids
    before = (await chatting.get(listing)).json()["conversations"]
    assert [(row["id"], row["title"]) for row in before] == [
        (second, "Summarize it"),
        (first, "Explain table 2"),
    ]
    url = f"/api/conversations/{first}"

    renamed = await chatting.put(
        url, json={"title": "  Table\u200b 2,\n\tthe \x07baseline  "}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Table 2, the baseline"
    assert renamed.json()["updated_at"] == before[1]["updated_at"]
    after = (await chatting.get(listing)).json()["conversations"]
    assert [(row["id"], row["title"]) for row in after] == [
        (second, "Summarize it"),
        (first, "Table 2, the baseline"),
    ]
    assert after[1]["updated_at"] == before[1]["updated_at"]

    limit = (await chatting.get("/api/me")).json()["limits"]["conversation_title_max_chars"]
    capped = await chatting.put(url, json={"title": "x" * (limit + 20)}, headers=CSRF_HEADERS)
    assert capped.status_code == 200, capped.text
    assert capped.json()["title"] == "x" * limit

    for blank in ("", " \n\t ", "\u200b"):
        refused = await chatting.put(url, json={"title": blank}, headers=CSRF_HEADERS)
        assert refused.status_code == 400, blank
    # Neither field: refused as the request it is (docs/api.md).
    assert (await chatting.put(url, json={}, headers=CSRF_HEADERS)).status_code == 400
    assert (await chatting.put(url, json={"title": "Plain"})).status_code == 403
    detail = (await chatting.get(url)).json()["conversation"]
    assert detail["title"] == "x" * limit
    assert detail["updated_at"] == before[1]["updated_at"]


async def test_a_rename_reaches_only_the_callers_conversations(chatting) -> None:
    async with chatting.app.state.sessionmaker() as db:
        stranger = user_row("stranger@example.com")
        db.add(stranger)
        await db.commit()
        stranger_id = stranger.id
    theirs_document = await seed_document(chatting, stranger_id)
    async with chatting.app.state.sessionmaker() as db:
        theirs = Conversation(
            user_id=stranger_id, document_id=theirs_document, title="Theirs", session="s"
        )
        db.add(theirs)
        await db.commit()
        theirs_id = theirs.id

    refused = await chatting.put(
        f"/api/conversations/{theirs_id}", json={"title": "Mine"}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 404
    async with chatting.app.state.sessionmaker() as db:
        assert (await db.get(Conversation, theirs_id)).title == "Theirs"


async def test_a_title_given_before_the_first_message_is_kept(chatting) -> None:
    """The first message names only a conversation still untitled: read under the row lock
    the send takes, so a rename that came first stands (docs/chat.md § Conversations)."""
    listing = f"/api/library/documents/{chatting.document_id}/conversations"
    made = (await chatting.post(listing, headers=CSRF_HEADERS)).json()["id"]
    renamed = await chatting.put(
        f"/api/conversations/{made}", json={"title": "Reading notes"}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 200, renamed.text
    sent = await send(chatting, made, text="Summarize it")
    assert sent.status_code == 202, sent.text
    listed = (await chatting.get(listing)).json()["conversations"]
    assert [(row["id"], row["title"]) for row in listed] == [(made, "Reading notes")]


async def test_a_first_line_cut_at_a_space_names_the_conversation_trimmed(chatting) -> None:
    """The first line is cut at the title cap and trimmed after the cut, as a rename is: the
    title never ends in the space the cut fell on (docs/chat.md § Conversations)."""
    listing = f"/api/library/documents/{chatting.document_id}/conversations"
    made = (await chatting.post(listing, headers=CSRF_HEADERS)).json()["id"]
    limit = (await chatting.get("/api/me")).json()["limits"]["conversation_title_max_chars"]
    line = "x" * (limit - 1) + " runs past the cap"
    sent = await send(chatting, made, text=f"{line}\nsecond line")
    assert sent.status_code == 202, sent.text
    listed = (await chatting.get(listing)).json()["conversations"]
    assert [(row["id"], row["title"]) for row in listed] == [(made, "x" * (limit - 1))]


async def test_a_usage_row_is_written_through_a_cancel(monkeypatch) -> None:
    """A cancel that lands while a call's usage row is being written waits for the write:
    the paid call reaches usage_log whether or not the turn survives it (docs/chat.md
    § Conversations, turns, events)."""
    started = asyncio.Event()
    release = asyncio.Event()
    added: list = []
    committed: list = []

    class Session:
        """A session whose commit waits until the test releases it."""

        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row) -> None:
            added.append(row)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            started.set()
            await release.wait()
            committed.extend(added)

    async def no_settlement(db, settings, turn_id):
        return None

    monkeypatch.setattr(chat_runner.ledger, "settle_chat", no_settlement)
    ctx = SimpleNamespace(
        sessionmaker=Session(),
        settings=None,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        credits_exhausted=False,
    )
    usage = ChatUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=None,
        cost_source="unknown",
    )
    task = asyncio.create_task(chat_runner._record_usage(ctx, "claude-x", usage))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    # The cancel is in, the write is not: the task waits rather than ending over it.
    assert not task.done() and not committed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [(row.model, row.prompt_tokens) for row in committed] == [("claude-x", 10)]


def test_a_directory_view_is_bounded_like_every_tool_result(tmp_path) -> None:
    """A directory a command filled with many entries is read no further than the result
    can carry, and cut like a long file view (docs/chat.md § Tools)."""
    root = tmp_path / "ws" / "1"
    crowded = root / "files" / "crowded"
    crowded.mkdir(parents=True)
    for index in range(250):
        (crowded / f"entry-{index:03d}.txt").write_text("")
    assert len(safe_io.list_dir(root, crowded, limit=5)) == 5
    full = chat_tools._listing(root, crowded, 20_000)
    assert full.count("\n") == 249 and "entry-249.txt" in full and "more entries" not in full
    # Past the cap: fewer entries read than the directory holds, the listing says so, and
    # the result is cut like a long file view.
    cut = chat_tools._listing(root, crowded, 200)
    assert len(cut) <= 200 and "omitted" in cut and "more entries" in cut
    # Which entries are read first is the filesystem's order; what was read is sorted. The
    # head's last line may be cut mid-name.
    names = {f"entry-{index:03d}.txt" for index in range(250)}
    head = cut.split("\n\n[...", 1)[0].split("\n")[:-1]
    assert head and all(line in names for line in head) and head == sorted(head)


async def test_a_subagents_retry_is_not_made_once_the_allowance_is_used(monkeypatch) -> None:
    """docs/billing.md § A chat turn: a subagent's call waiting out a 429 while another call
    of the turn uses the allowance makes no further attempt; the turn's own call does."""
    spent = False
    ctx = SimpleNamespace(overdraft_spent=lambda: spent)
    attempts = 0

    async def stream_once(ctx, body, parent=None):
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return "answered"
        raise RateLimited(retry_after=1)

    async def backoff(ctx, seconds):
        nonlocal spent
        spent = True

    monkeypatch.setattr(chat_runner, "_stream_once", stream_once)
    monkeypatch.setattr(chat_runner, "_backoff", backoff)
    monkeypatch.setattr(chat_runner, "_emit", _no_events)
    with pytest.raises(chat_runner.TurnOutOfCredits):
        await chat_runner._call_model(ctx, {}, parent="toolu_1")
    assert attempts == 1
    spent, attempts = False, 0
    assert await chat_runner._call_model(ctx, {}) == "answered"
    assert attempts == 2


async def test_the_event_stream_closes_its_listener_when_its_first_read_fails(monkeypatch) -> None:
    """The conversation stream's LISTEN connection lives outside the pool: a first read that
    fails, or a client gone before the first frame, still closes it."""
    from sightread.jobs import chat_events

    closed: list[bool] = []

    class Listener:
        async def close(self) -> None:
            closed.append(True)

    async def open_listener(*args, **kwargs):
        return Listener()

    async def shown_branch(sessionmaker, conversation_id):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(chat_events, "open_listener", open_listener)
    monkeypatch.setattr(chat_events, "_shown_branch", shown_branch)
    stream = chat_events.stream_conversation_events(None, "postgresql://unused", 1, 0)
    with pytest.raises(RuntimeError):
        await anext(stream)
    assert closed == [True]
