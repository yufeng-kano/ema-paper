"""AGENTS.md (docs/chat.md § AGENTS.md): the files on a document's path read into the system
prompt as a snapshot, the Agent page's drawer, `/init`, and an upload so named kept as a
file. Turns run against a stubbed Messages stream; no test reaches the network
(docs/testing.md § Cost safety)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import respx
from httpx import AsyncClient

from sightread.agents import chat as chat_agent
from sightread.workspace.agents_file import UNREADABLE, agents_file_chain, render_chain
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS
from tests.test_chat import _run_queued_turn
from tests.test_chat_compact import DONE, SUMMARY, conversation_row
from tests.test_chat_extensions import chatting_client, new_conversation, send, stub_messages
from tests.test_chat_versions import edit


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


def _root(client: AsyncClient) -> Path:
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _agents_part(request: dict) -> str | None:
    return next(
        (
            part["text"]
            for part in request["system"]
            if part["text"].startswith(chat_agent.AGENTS_FILE_INTRO)
        ),
        None,
    )


# --- the chain -----------------------------------------------------------------------------


def test_the_chain_reads_the_files_on_the_path_outermost_first(tmp_path) -> None:
    """`files/AGENTS.md`, then one per folder down to the document's own; never one inside
    the document's directory, in a folder beside the path, or a directory of that name; a
    file that is not text stands as one line (docs/chat.md § AGENTS.md)."""
    root = tmp_path / "ws"
    _write(root, "files/AGENTS.md", "# Everything\n\n- Notes in English\n")
    _write(root, "files/Papers/AGENTS.md", "- Cite with the DOI\n")
    _write(root, "files/Papers/2017/paper/AGENTS.md", "inside the document: not read")
    _write(root, "files/Other/AGENTS.md", "beside the path: not read")
    (root / "files/Papers/2017/AGENTS.md").mkdir()
    chain = agents_file_chain(root, Path("files/Papers/2017"), 1_000_000)
    assert chain == [
        (Path("files/AGENTS.md"), "# Everything\n\n- Notes in English\n"),
        (Path("files/Papers/AGENTS.md"), "- Cite with the DOI\n"),
    ]
    assert render_chain(chain) == (
        "## /workspace/files/AGENTS.md\n\n# Everything\n\n- Notes in English\n\n"
        "## /workspace/files/Papers/AGENTS.md\n\n- Cite with the DOI"
    )
    # The root file alone, for a document at the top of the library and for the drawer.
    assert agents_file_chain(root, Path("files"), 1_000_000) == chain[:1]
    # Not text, or past the cap: said, not sent.
    (root / "files/Papers/AGENTS.md").write_bytes(b"\xff\xfe\x00binary")
    assert agents_file_chain(root, Path("files/Papers"), 1_000_000)[1][1] == UNREADABLE
    assert agents_file_chain(root, Path("files/Papers"), 10)[0][1] == UNREADABLE
    assert agents_file_chain(root, Path("other"), 1_000_000) == []
    assert render_chain([]) == ""


def test_the_agents_block_is_cut_at_its_cap() -> None:
    assert chat_agent.render_agents_file(None, 100) is None
    assert chat_agent.render_agents_file("  \n", 100) is None
    assert (
        chat_agent.render_agents_file("  ## /workspace/files/AGENTS.md\n\n- x\n", 100)
        == chat_agent.AGENTS_FILE_INTRO + "## /workspace/files/AGENTS.md\n\n- x"
    )
    cut = chat_agent.render_agents_file("abcdefghij", 4)
    assert cut == chat_agent.AGENTS_FILE_INTRO + "abcd" + chat_agent.AGENTS_FILE_CUT


# --- a conversation ------------------------------------------------------------------------


@respx.mock
async def test_a_conversation_keeps_the_agents_files_it_started_with(chat_client) -> None:
    """The files on the document's path are snapshotted at the first turn and sent after the
    catalogue, before the memory file; a change made meanwhile reaches the conversation
    only after a compaction takes the snapshot again (docs/chat.md § AGENTS.md)."""
    client = chat_client
    root = _root(client)
    _write(root, "files/AGENTS.md", "- Notes in English\n")
    _write(root, "files/Papers/AGENTS.md", "- Cite with the DOI\n")
    _write(root, "files/Other/AGENTS.md", "- Not this one\n")
    await client.put(
        "/api/agents/chat/memory-file", json={"text": "My notes."}, headers=CSRF_HEADERS
    )
    main, _ = stub_messages([DONE, DONE, SUMMARY, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="one")
    await _run_queued_turn(client)

    expected = chat_agent.AGENTS_FILE_INTRO + (
        "## /workspace/files/AGENTS.md\n\n- Notes in English\n\n"
        "## /workspace/files/Papers/AGENTS.md\n\n- Cite with the DOI"
    )
    assert _agents_part(main[0]) == expected
    system = main[0]["system"]
    kinds = [block["text"].split("\n", 1)[0] for block in system]
    assert kinds.index("# AGENTS.md") < kinds.index("# Memory file") < kinds.index("# Memory")
    assert "Skills" in system[kinds.index("# AGENTS.md") - 1]["text"]
    assert [block.get("cache_control") for block in system] == [None] * (len(system) - 1) + [
        {"type": "ephemeral", "ttl": "1h"}
    ]
    row = await conversation_row(client, conversation_id)
    assert row.agents_file == (
        "## /workspace/files/AGENTS.md\n\n- Notes in English\n\n"
        "## /workspace/files/Papers/AGENTS.md\n\n- Cite with the DOI"
    )

    _write(root, "files/Papers/AGENTS.md", "- Cite with the DOI\n- Tables, never lists\n")
    await send(client, conversation_id, text="two")
    await _run_queued_turn(client)
    assert _agents_part(main[1]) == expected

    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    assert _agents_part(main[2]) == expected
    assert "Tables, never lists" in (await conversation_row(client, conversation_id)).agents_file
    await send(client, conversation_id, text="three")
    await _run_queued_turn(client)
    assert _agents_part(main[3]).endswith("- Cite with the DOI\n- Tables, never lists")


@respx.mock
async def test_a_conversation_without_agents_files_sends_no_block(chat_client) -> None:
    client = chat_client
    main, _ = stub_messages([DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="one")
    await _run_queued_turn(client)
    assert _agents_part(main[0]) is None
    assert (await conversation_row(client, conversation_id)).agents_file == ""


async def test_the_drawer_shows_the_files_a_new_conversation_would_read(chat_client) -> None:
    """`GET /api/agents/chat/context`: the document's path with `document_id`, the root file
    alone without one, no part when there is none (docs/api.md)."""
    client = chat_client
    bare = (await client.get("/api/agents/chat/context")).json()
    assert "agents_file" not in [part["kind"] for part in bare["parts"]]
    root = _root(client)
    _write(root, "files/AGENTS.md", "- Notes in English\n")
    _write(root, "files/Papers/AGENTS.md", "- Cite with the DOI\n")
    without = (await client.get("/api/agents/chat/context")).json()
    kinds = [part["kind"] for part in without["parts"]]
    assert kinds.index("catalogue") < kinds.index("agents_file") < kinds.index("memory")
    text = without["parts"][kinds.index("agents_file")]["text"]
    assert "Notes in English" in text and "Cite with the DOI" not in text
    with_document = (
        await client.get("/api/agents/chat/context", params={"document_id": client.document_id})
    ).json()
    text = next(part["text"] for part in with_document["parts"] if part["kind"] == "agents_file")
    assert text.endswith("## /workspace/files/Papers/AGENTS.md\n\n- Cite with the DOI")


# --- /init ---------------------------------------------------------------------------------


@respx.mock
async def test_init_carries_the_instruction_and_nothing_of_the_passage(chat_client) -> None:
    """`/init` is the composer's own: the reader's line, the instruction (`context: init`,
    which the thread does not show) and the turn block; no selection, sections or quote;
    no attachments (400); a `message` turn (docs/chat.md § AGENTS.md)."""
    client = chat_client
    main, _ = stub_messages([DONE, DONE])
    conversation_id = await new_conversation(client)
    refused = await send(
        client, conversation_id, text="", command="init", attachments=[str(uuid.uuid4())]
    )
    assert refused.status_code == 400 and "attachments" in refused.text
    sent = await send(
        client,
        conversation_id,
        text="my thesis papers",
        command="init",
        selection="a passage",
        quote="an answer",
        sections=[{"heading": "1 Intro", "text": "..."}],
    )
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)
    content = main[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "/init my thesis papers"}
    assert content[1]["text"] == chat_agent.INIT_INSTRUCTION + chat_agent.INIT_NOTE.format(
        note="my thesis papers"
    )
    assert "context" not in content[1]
    assert len(content) == 3 and content[2]["text"].startswith("Context for this message")
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert [turn["kind"] for turn in detail["turns"]] == ["message"]
    shown = detail["messages"][0]["content"]
    assert shown == [{"type": "text", "text": "/init my thesis papers"}]
    assert detail["conversation"]["title"] == "/init my thesis papers"

    # Edited, it is an `/init` again, its instruction rendered with the new note.
    edited = await edit(client, conversation_id, detail["messages"][0]["id"], text="")
    assert edited.status_code == 202, edited.text
    await _run_queued_turn(client)
    content = main[1]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "/init"}
    assert content[1]["text"] == chat_agent.INIT_INSTRUCTION


async def test_init_is_no_skill_name(chat_client) -> None:
    refused = await chat_client.post(
        "/api/skills", json={"name": "init", "description": "x", "body": "x"}, headers=CSRF_HEADERS
    )
    assert refused.status_code == 400, refused.text


# --- an upload named AGENTS.md -------------------------------------------------------------


async def test_an_upload_named_agents_md_is_kept_as_a_file(chat_client) -> None:
    """A `.md` becomes a markdown document, except one named `AGENTS.md`, which is kept as a
    file where the agent reads it (docs/workspace.md § Markdown documents)."""
    client = chat_client
    kept = await client.post(
        "/api/library/documents",
        files={"file": ("AGENTS.md", b"- Notes in English\n", "text/markdown")},
        headers=CSRF_HEADERS,
    )
    assert kept.status_code == 201, kept.text
    assert (kept.json()["kind"], kept.json()["name"]) == ("file", "AGENTS.md")
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "files/AGENTS.md").read_text() == "- Notes in English\n"
    document = await client.post(
        "/api/library/documents",
        files={"file": ("agents.md", b"# Notes\n", "text/markdown")},
        headers=CSRF_HEADERS,
    )
    assert document.status_code == 201, document.text
    assert document.json()["kind"] == "markdown"
    # What the drawer now reads.
    context = (await client.get("/api/agents/chat/context")).json()
    text = next(part["text"] for part in context["parts"] if part["kind"] == "agents_file")
    assert text.endswith("## /workspace/files/AGENTS.md\n\n- Notes in English")
