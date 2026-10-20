"""The chat agent's instructions, languages and memory, and the sections a message carries
(docs/chat.md § Instructions, § Memory, § What a user message carries; docs/api.md): the
pure helpers, the routes, and turns running them against a stubbed Messages stream. No test
reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.agents import CHAT
from sightread.agents import chat as chat_agent
from sightread.agents import memory as memory_defs
from sightread.db.models import (
    Conversation,
    Document,
    Folder,
    Job,
    Memory,
    Message,
    Result,
    User,
)
from sightread.jobs import chat_events, chat_memory, chat_runner, chat_tools
from sightread.routes.chat import TURN_CONTEXT, turn_note
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, DATABASE_URL
from tests.test_chat import _run_queued_turn
from tests.test_chat_extensions import (
    DOC_PATH,
    chatting_client,
    new_conversation,
    one_call,
    replayed_events,
    send,
    stub_messages,
)

DONE = ([{"type": "text", "text": "Done."}], "end_turn")


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


async def put_chat(client: AsyncClient, **fields):
    return await client.put("/api/agents/chat/settings", json=fields, headers=CSRF_HEADERS)


async def save_memory(client: AsyncClient, **body) -> dict:
    created = await client.post("/api/memories", json=body, headers=CSRF_HEADERS)
    assert created.status_code == 201, created.text
    return created.json()


async def another_document(client: AsyncClient, user_id: int, name: str) -> int:
    """A second document of the reader's, at the library's root."""
    async with client.app.state.sessionmaker() as db:
        job = Job(
            id=uuid.uuid4(),
            user_id=user_id,
            agent="parse",
            kind="pdf",
            filename=name,
            media_type="application/pdf",
            size_bytes=10,
            sha256="c" * 64,
            model="claude-x",
            bbox_format="yxyx_norm1000",
            status="succeeded",
            page_count=2,
        )
        db.add(job)
        await db.flush()
        db.add(
            Result(job_id=job.id, markdown="# Other\n", pages=[], figures=[], errors=[], meta={})
        )
        document = Document(user_id=user_id, folder_id=None, job_id=job.id, name=name)
        db.add(document)
        await db.commit()
        return document.id


async def stranger_rows(client: AsyncClient) -> SimpleNamespace:
    """Another reader with a document, a conversation and a memory of their own."""
    async with client.app.state.sessionmaker() as db:
        stranger = User(google_sub="stranger", email="stranger@example.com")
        db.add(stranger)
        await db.flush()
        folder = Folder(user_id=stranger.id, parent_id=None, name="Theirs")
        job = Job(
            id=uuid.uuid4(),
            user_id=stranger.id,
            agent="parse",
            kind="pdf",
            filename="theirs.pdf",
            media_type="application/pdf",
            size_bytes=1,
            sha256="d" * 64,
            model="m",
            bbox_format="yxyx_norm1000",
            status="succeeded",
            page_count=1,
        )
        db.add_all([folder, job])
        await db.flush()
        document = Document(user_id=stranger.id, folder_id=folder.id, job_id=job.id, name="t.pdf")
        db.add(document)
        await db.flush()
        conversation = Conversation(
            user_id=stranger.id, document_id=document.id, title="Theirs", session="s"
        )
        db.add(conversation)
        await db.flush()
        memory = Memory(user_id=stranger.id, kind="about", text="Not yours.")
        db.add(memory)
        await db.commit()
        return SimpleNamespace(
            document_id=document.id, conversation_id=conversation.id, memory_id=memory.id
        )


async def memory_ids_of(client: AsyncClient, conversation_id: int) -> list | None:
    async with client.app.state.sessionmaker() as db:
        return (await db.get(Conversation, conversation_id)).memory_ids


def turn_starts(events: list[tuple[str, dict]]) -> list[dict]:
    return [data for name, data in events if name == "turn_start"]


# --- the pure helpers -------------------------------------------------------------------


def test_clean_text_drops_invisible_characters_and_keeps_the_joiners() -> None:
    hidden = "a\u200bb\u2060c\ufeffd\u200ee\u200ff\u061cg\u202ah\u202ei\u2066j\u2069k"
    assert memory_defs.clean_text(hidden, single_line=False) == "abcdefghijk"
    # The joiners scripts and emoji sequences need stay.
    family = "\U0001f469\u200d\U0001f467 م\u200cی"
    assert memory_defs.clean_text(family, single_line=True) == family
    assert memory_defs.clean_text("  one\n\n  two\t three \r\n", single_line=True) == (
        "one two three"
    )
    assert memory_defs.clean_text("  # Me\n\n- brief  \n", single_line=False) == "# Me\n\n- brief"


def test_clean_text_drops_control_characters_but_tab_newline_and_return() -> None:
    # NUL first: PostgreSQL refuses it in TEXT, and the write would fail with the text logged.
    assert memory_defs.clean_text("a\x00b\x07c\x1bd\x7fe", single_line=True) == "abcde"
    assert memory_defs.clean_text("# Me\x00\n\n-\tbrief\r\n\x0bend\x0c", single_line=False) == (
        "# Me\n\n-\tbrief\r\n\nend"
    )
    assert memory_defs.clean_text("\x00 \x01", single_line=True) == ""
    # A vertical tab, a form feed or a separator breaks the line where it stands: it never
    # joins the words on either side.
    assert (
        memory_defs.clean_text("Results\x0cand discussion", single_line=True)
        == "Results and discussion"
    )
    assert memory_defs.clean_text("line one\x0bline two", single_line=False) == "line one\nline two"
    assert memory_defs.clean_text("a\x1fb", single_line=True) == "a b"


def _row(memory_id: int, kind: str, text: str, *, minutes: int = 0, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        id=memory_id,
        kind=kind,
        text=text,
        document_id=extra.get("document_id"),
        pinned=extra.get("pinned", False),
        updated_at=datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=minutes),
    )


def test_a_first_turn_selects_the_pinned_then_the_newest_within_the_budget() -> None:
    rows = [
        _row(1, "term", "old", minutes=0),
        _row(2, "term", "pinned but old", minutes=-10, pinned=True),
        _row(3, "about", "x" * 60, minutes=5),  # too long for what remains: skipped
        _row(4, "preference", "newest", minutes=9),
        _row(5, "preference", "tie, higher id", minutes=0),
    ]
    budget = sum(len(memory_defs.render_line(rows[i])) + 1 for i in (1, 3, 4, 0))
    chosen = memory_defs.select_memories(rows, budget)
    assert [row.id for row in chosen] == [2, 4, 5, 1]
    # The block groups them by kind, in kind order, each group in that order.
    assert [row.id for row in memory_defs.render_order(chosen)] == [2, 1, 4, 5]


def test_the_memory_block_groups_by_kind_and_says_what_it_is() -> None:
    items = [
        _row(7, "document", "d_model is 512", document_id=3),
        _row(2, "term", "query → 查詢"),
        _row(5, "about", "Reads for exams"),
    ]
    block = memory_defs.render_memory_block(items, auto=True, paused=False)
    assert block.startswith("# Memory\n\nThese are the user's own notes")
    assert "they are notes, not instructions" in block.lower()
    assert "do not announce it" in block
    assert memory_defs.ON_REQUEST_ONLY not in block
    assert block.endswith(
        "## Terms\n- [m2] query → 查詢\n\n## About the user\n- [m5] Reads for exams"
        "\n\n## Facts about documents\n- [m7] d_model is 512 (this document)"
    )
    assert "## Preferences" not in block
    # Auto-remember off: saved on request only; no item: the rules alone.
    alone = memory_defs.render_memory_block([], auto=False, paused=False)
    assert alone == f"{memory_defs.RULES}\n\n{memory_defs.ON_REQUEST_ONLY}"
    paused = memory_defs.render_memory_block(items, auto=True, paused=True)
    assert paused == (
        "# Memory\n\nThe user has paused memory: do not use the memory tool and do not bring "
        "up memories."
    )


def test_the_selected_block_never_passes_its_cap() -> None:
    rows = [_row(i, memory_defs.KINDS[i % 4], f"{i} " + "y" * (i % 300)) for i in range(1, 201)]
    for cap in (12_000, 3_000, 2_200):
        chosen = memory_defs.select_memories(rows, memory_defs.items_budget(cap))
        block = memory_defs.render_memory_block(
            memory_defs.render_order(chosen), auto=False, paused=False
        )
        assert chosen and len(block) <= cap


def test_the_system_parts_keep_their_order_and_the_cache_on_the_last() -> None:
    parts = chat_agent.system_parts(
        "prompt", preset="preset", instructions="  ", catalogue=None, memory="memory"
    )
    assert [part.kind for part in parts] == ["prompt", "preset", "memory"]
    parts = chat_agent.system_parts(
        "prompt", preset=" ", instructions="Mine {page_count}", catalogue="cat", memory="mem"
    )
    assert [part.kind for part in parts] == ["prompt", "instructions", "catalogue", "memory"]
    # Appended verbatim: a token in the reader's words is theirs, not substituted.
    assert parts[1].text == chat_agent.INSTRUCTIONS_INTRO + "Mine {page_count}"
    assert parts[1].text.startswith("# The user's instructions\n\nThe user wrote these for you.")
    blocks = chat_agent.system_blocks(parts)
    assert [block.get("cache_control") for block in blocks] == [
        None,
        None,
        None,
        {"type": "ephemeral", "ttl": "1h"},
    ]


def test_the_memory_tool_is_the_last_tool_and_never_a_subagents() -> None:
    assert chat_agent.TOOLS[-1] is chat_agent.MEMORY_TOOL
    assert chat_agent.TOOLS_WITHOUT_BASH[-1] is chat_agent.MEMORY_TOOL
    assert "memory" not in chat_agent.SUBAGENT_TOOL_NAMES
    schema = chat_agent.MEMORY_TOOL["input_schema"]
    assert schema["required"] == ["action", "id", "kind", "text", "scope"]
    assert schema["properties"]["kind"]["enum"] == ["term", "preference", "about", "document", ""]
    assert schema["properties"]["scope"]["enum"] == ["all", "document", ""]
    assert CHAT.pipeline_version == 7


def test_the_prompt_says_how_to_link_a_section_and_which_language_to_answer_in() -> None:
    prompt = chat_agent.DEFAULT_SYSTEM_PROMPT
    assert "\n- memory: save, update or forget one of the user's memories" in prompt
    assert "[3.2 Attention](#32-attention)" in prompt and "Link nothing else that way." in prompt
    assert "whole sections of the document they added to it" in prompt
    assert "Answer in the reader's locale unless the block names an answer language." in prompt
    # A turn without the tool would not be told of it; the tokens stay without facts.
    rendered = chat_agent.render_system_prompt(prompt, None, tools=["view_page"])
    assert "- memory:" not in rendered and "{document_path}" in rendered


ANSWER_JA = "- answer language: ja (the reader's choice: answer in it, whatever the locale)"


def translation_line(tag: str) -> str:
    return (
        f"- translation language: {tag} "
        "(the reader's choice: translate into it unless told otherwise)"
    )


def test_the_turn_block_names_the_languages_only_when_chosen() -> None:
    assert turn_note("abc", "en", None, None) == (
        "Context for this message:\n- document revision: abc\n- reader locale: en"
    )
    # Each line carries its own rule: a prompt the reader replaced, or the one a migration
    # moved their preset into, knows nothing of these lines (docs/chat.md § Instructions).
    assert turn_note(None, "zh-TW", "ja", "zh-TW").endswith(
        f"- reader locale: zh-TW\n{ANSWER_JA}\n{translation_line('zh-TW')}"
    )
    assert "unknown" in turn_note(None, "en", None, "fr")
    assert "answer language" not in turn_note("r", "en", None, "fr")


# --- the agent's settings -----------------------------------------------------------------


async def test_the_chat_settings_take_instructions_languages_and_switches(make_client) -> None:
    client = make_client(chat_instructions_max_chars=16)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    agents = (await client.get("/api/agents")).json()["agents"]
    defaults = next(agent for agent in agents if agent["name"] == "chat")["settings"]
    assert {key: defaults[key] for key in memory_setting_keys()} == {
        "instructions": None,
        "answer_language": None,
        "translate_language": None,
        "memory_auto": True,
        "memory_paused": False,
    }

    saved = await put_chat(
        client,
        instructions="  \u200bBe brief.\u202e\n\nSI.  ",
        answer_language="zh-TW",
        translate_language="ja",
        memory_auto=False,
        memory_paused=True,
    )
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["instructions"] == "Be brief.\n\nSI."
    assert (body["answer_language"], body["translate_language"]) == ("zh-TW", "ja")
    assert (body["memory_auto"], body["memory_paused"]) == (False, True)
    # Only the fields present change.
    kept = (await put_chat(client, effort="high")).json()
    assert kept["instructions"] == "Be brief.\n\nSI." and kept["memory_paused"] is True

    # Counted as cleaned: sixteen characters and invisible ones fit; seventeen do not.
    fits = await put_chat(client, instructions="x" * 16 + "\u200b" * 6)
    assert fits.status_code == 200 and fits.json()["instructions"] == "x" * 16
    assert (await put_chat(client, instructions="x" * 17)).status_code == 400
    # Empty is none, and a language may follow the locale again.
    cleared = (await put_chat(client, instructions=" \u200b ", answer_language=None)).json()
    assert cleared["instructions"] is None and cleared["answer_language"] is None
    assert (await put_chat(client, translate_language="")).json()["translate_language"] is None

    for bad in ("zh_TW", "abcdefgh-abcdefgh", "e", "en-", "123"):
        assert (await put_chat(client, answer_language=bad)).status_code == 400, bad
    # A well-formed tag past sixteen characters.
    assert (await put_chat(client, translate_language="zh-Hant-TW-abcdef")).status_code == 400
    assert (await put_chat(client, memory_auto=None)).status_code == 400
    assert (await put_chat(client, memory_paused=None)).status_code == 400
    for field, value in (
        ("instructions", "x"),
        ("answer_language", "en"),
        ("translate_language", None),
        ("memory_auto", True),
        ("memory_paused", False),
    ):
        refused = await client.put(
            "/api/agents/parse/settings", json={field: value}, headers=CSRF_HEADERS
        )
        assert refused.status_code == 400, field
        assert refused.json()["error"]["type"] == "invalid_request"


def memory_setting_keys() -> tuple[str, ...]:
    return ("instructions", "answer_language", "translate_language", "memory_auto", "memory_paused")


async def test_me_carries_the_composer_and_editor_limits(make_client) -> None:
    client = make_client(
        chat_instructions_max_chars=111,
        chat_memory_max_chars=222,
        chat_sections_per_message=3,
        chat_section_heading_max_chars=55,
        chat_selection_max_chars=444,
    )
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    limits = (await client.get("/api/me")).json()["limits"]
    assert limits["instructions_max_chars"] == 111
    assert limits["memory_max_chars"] == 222
    assert limits["sections_per_message"] == 3
    assert limits["section_heading_max_chars"] == 55
    assert limits["selection_max_chars"] == 444


# --- the memories routes --------------------------------------------------------------------


async def test_memories_are_listed_edited_and_deleted(chat_client) -> None:
    client = chat_client
    conversation_id = await new_conversation(client)
    first = await save_memory(
        client,
        kind="term",
        text="  query\n→ 查詢 \u200b",
        document_id=client.document_id,
        conversation_id=conversation_id,
        source_ref="toolu_1",
        pinned=True,
    )
    assert first["text"] == "query → 查詢"
    assert (first["document_id"], first["document_name"]) == (client.document_id, "paper.pdf")
    assert (first["conversation_id"], first["conversation_title"]) == (conversation_id, "")
    assert first["pinned"] is True and first["source_ref"] == "toolu_1"
    second = await save_memory(client, kind="about", text="Reads for exams")
    assert second["document_id"] is None and second["pinned"] is False

    listed = (await client.get("/api/memories")).json()
    assert [memory["id"] for memory in listed["memories"]] == [second["id"], first["id"]]
    assert listed["max"] == client.app.state.settings.chat_memories_max

    widened = await client.put(
        f"/api/memories/{first['id']}",
        json={"document_id": None, "pinned": False, "kind": "preference"},
        headers=CSRF_HEADERS,
    )
    assert widened.status_code == 200, widened.text
    edited = widened.json()
    assert (edited["document_id"], edited["document_name"]) == (None, None)
    assert (edited["kind"], edited["pinned"], edited["text"]) == (
        "preference",
        False,
        first["text"],
    )
    retexted = await client.put(
        f"/api/memories/{first['id']}", json={"text": "a\nb"}, headers=CSRF_HEADERS
    )
    assert retexted.json()["text"] == "a b"
    for body in ({"kind": None}, {"kind": "fact"}, {"text": " \u200b "}, {"pinned": None}):
        refused = await client.put(f"/api/memories/{first['id']}", json=body, headers=CSRF_HEADERS)
        assert refused.status_code == 400, body

    assert (
        await client.delete(f"/api/memories/{first['id']}", headers=CSRF_HEADERS)
    ).status_code == 204
    assert (
        await client.delete(f"/api/memories/{first['id']}", headers=CSRF_HEADERS)
    ).status_code == 404
    # Clearing takes every memory and leaves the switches as they are.
    await put_chat(client, memory_paused=True)
    assert (await client.delete("/api/memories", headers=CSRF_HEADERS)).status_code == 204
    assert (await client.get("/api/memories")).json()["memories"] == []
    agents = (await client.get("/api/agents")).json()["agents"]
    assert (
        next(agent for agent in agents if agent["name"] == "chat")["settings"]["memory_paused"]
        is True
    )


async def test_a_memory_is_checked_capped_and_the_callers_own(make_client, tmp_path) -> None:
    client = await chatting_client(
        make_client(
            workspaces_dir=str(tmp_path / "ws"), chat_memories_max=2, chat_memory_max_chars=10
        )
    )
    for body in (
        {"kind": "fact", "text": "x"},
        {"kind": "term", "text": "  \n\u200b "},
        {"kind": "term", "text": "x" * 11},
    ):
        refused = await client.post("/api/memories", json=body, headers=CSRF_HEADERS)
        assert refused.status_code == 400, body
    # Counted after cleaning: ten characters and an invisible one fit.
    fits = await save_memory(client, kind="term", text="x" * 10 + "\u2066")
    assert fits["text"] == "x" * 10

    theirs = await stranger_rows(client)
    for body in (
        {"kind": "term", "text": "y", "document_id": theirs.document_id},
        {"kind": "term", "text": "y", "conversation_id": theirs.conversation_id},
    ):
        assert (
            await client.post("/api/memories", json=body, headers=CSRF_HEADERS)
        ).status_code == 404
    assert (
        await client.put(
            f"/api/memories/{fits['id']}",
            json={"document_id": theirs.document_id},
            headers=CSRF_HEADERS,
        )
    ).status_code == 404
    assert (
        await client.put(
            f"/api/memories/{theirs.memory_id}", json={"pinned": True}, headers=CSRF_HEADERS
        )
    ).status_code == 404
    assert (
        await client.delete(f"/api/memories/{theirs.memory_id}", headers=CSRF_HEADERS)
    ).status_code == 404
    assert [memory["id"] for memory in (await client.get("/api/memories")).json()["memories"]] == [
        fits["id"]
    ]

    await save_memory(client, kind="about", text="second")
    full = await client.post(
        "/api/memories", json={"kind": "about", "text": "third"}, headers=CSRF_HEADERS
    )
    assert full.status_code == 409
    # Clearing all leaves the stranger's alone.
    await client.delete("/api/memories", headers=CSRF_HEADERS)
    async with client.app.state.sessionmaker() as db:
        left = (await db.execute(select(Memory.id))).scalars().all()
    assert left == [theirs.memory_id]


@pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="row locks only block on PostgreSQL",
)
async def test_the_readers_memory_writes_wait_for_a_call_in_flight(chat_client) -> None:
    """A Pause, an edit, a delete and *Clear all* each take the reader's row before they
    act, as the `memory` tool does before it reads the switch, so none lands under a call
    already deciding (docs/chat.md § Memory)."""
    client = chat_client
    edited = await save_memory(client, kind="term", text="query → 查詢")
    deleted = await save_memory(client, kind="term", text="key → 鍵")
    writes = {
        "pause": lambda: put_chat(client, memory_paused=True),
        "edit": lambda: client.put(
            f"/api/memories/{edited['id']}", json={"pinned": True}, headers=CSRF_HEADERS
        ),
        "delete": lambda: client.delete(f"/api/memories/{deleted['id']}", headers=CSRF_HEADERS),
        "clear": lambda: client.delete("/api/memories", headers=CSRF_HEADERS),
    }
    for name, write in writes.items():
        async with client.app.state.sessionmaker() as call:
            # A `memory` call deciding: it holds the reader's row until it commits.
            await call.execute(select(User.id).where(User.id == client.user_id).with_for_update())
            pending = asyncio.create_task(write())
            await asyncio.sleep(0.3)
            assert not pending.done(), name
            await call.commit()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code in (200, 204), (name, response.text)


# --- what the agent receives ------------------------------------------------------------------


async def test_the_context_route_shows_what_a_turn_would_send(chat_client) -> None:
    client = chat_client
    other = await another_document(client, client.user_id, "other.pdf")
    everywhere = await save_memory(client, kind="term", text="query → 查詢")
    await save_memory(client, kind="document", text="d is 512", document_id=client.document_id)
    await save_memory(client, kind="document", text="elsewhere", document_id=other)

    bare = await client.get("/api/agents/chat/context")
    assert bare.status_code == 200, bare.text
    context = bare.json()
    assert [part["kind"] for part in context["parts"]] == ["prompt", "catalogue", "memory"]
    assert all(part["chars"] == len(part["text"]) for part in context["parts"])
    prompt, catalogue, memory = context["parts"]
    # No document: the template with its tokens, its tool lines as a turn has them.
    assert prompt["own"] is False and "{document_path}" in prompt["text"]
    assert "\n- bash:" not in prompt["text"] and "\n- memory:" in prompt["text"]
    assert (catalogue["skills"], catalogue["subagents"]) == (1, 1)  # fix-math, translator
    assert (memory["count"], memory["paused"]) == (1, False)
    assert f"[m{everywhere['id']}]" in memory["text"] and "d is 512" not in memory["text"]
    assert context["facts"] is None
    assert context["turn"] == {
        "text": "Context for this message:\n- document revision: <revision>\n- reader locale: en",
        "chars": len(
            "Context for this message:\n- document revision: <revision>\n- reader locale: en"
        ),
    }

    # The reader's prompt, a legacy preset, instructions and languages, with a document.
    preset = await client.post(
        "/api/prompts",
        json={"agent": "chat", "name": "Legacy", "text": "Legacy words."},
        headers=CSRF_HEADERS,
    )
    await put_chat(client, prompt_preset_id=preset.json()["id"])
    await put_chat(client, instructions="Be brief.", answer_language="ja", translate_language="en")
    with_document = (
        await client.get(
            "/api/agents/chat/context",
            params={"locale": "zh-TW", "document_id": client.document_id},
        )
    ).json()
    kinds = [part["kind"] for part in with_document["parts"]]
    assert kinds == ["prompt", "preset", "instructions", "catalogue", "memory"]
    parts = {part["kind"]: part for part in with_document["parts"]}
    assert parts["preset"]["text"] == "Legacy words."
    assert parts["instructions"]["text"] == chat_agent.INSTRUCTIONS_INTRO + "Be brief."
    assert parts["memory"]["count"] == 2 and "d is 512 (this document)" in parts["memory"]["text"]
    assert "elsewhere" not in parts["memory"]["text"]
    assert with_document["facts"] == {
        "document_path": DOC_PATH,
        "page_count": "1",
        "bbox_format": "yxyx_norm1000",
        "parse_model": "claude-x",
        "network": chat_agent.network_note("none", ""),
    }
    assert f"Current document: {DOC_PATH}" in parts["prompt"]["text"]
    assert with_document["turn"]["text"].endswith(
        f"- reader locale: zh-TW\n{ANSWER_JA}\n{translation_line('en')}"
    )
    # Reads only: the document was never materialized for it.
    assert not (user_root(client.app.state.settings.workspaces_dir, client.user_id)).exists()

    await put_chat(client, prompt="Mine for {document_path}.")
    await put_chat(client, memory_paused=True)
    own = (await client.get("/api/agents/chat/context")).json()
    assert [part["kind"] for part in own["parts"]] == [
        "prompt",
        "instructions",
        "catalogue",
        "memory",
    ]
    assert own["parts"][0] == {
        "kind": "prompt",
        "text": "Mine for {document_path}.",
        "chars": len("Mine for {document_path}."),
        "own": True,
    }
    paused = own["parts"][-1]
    assert (paused["count"], paused["paused"]) == (0, True)
    assert paused["text"] == memory_defs.PAUSED

    theirs = await stranger_rows(client)
    missing = await client.get(
        "/api/agents/chat/context", params={"document_id": theirs.document_id}
    )
    assert missing.status_code == 404
    assert (
        await client.get("/api/agents/chat/context", params={"locale": "not a tag"})
    ).status_code == 422


# --- sections ---------------------------------------------------------------------------------


@respx.mock
async def test_a_message_carries_whole_sections_after_the_selection(chat_client) -> None:
    client = chat_client
    main, _ = stub_messages([DONE])
    conversation_id = await new_conversation(client)
    sent = await send(
        client,
        conversation_id,
        text="Compare these",
        selection="Greedy",
        sections=[
            {"heading": "3.2\nAttention\u200b", "text": " ## 3.2 Attention\n\nBody. "},
            {"heading": "4 Results", "text": None},
        ],
        quote="Earlier.",
    )
    assert sent.status_code == 202, sent.text
    async with client.app.state.sessionmaker() as db:
        stored = (await db.execute(select(Message))).scalars().one().content
    assert [block.get("context") for block in stored] == [
        None,
        "selection",
        "section",
        "section",
        "quote",
        TURN_CONTEXT,
    ]
    assert stored[2] == {
        "type": "text",
        "text": 'The whole section "3.2 Attention" of the document:\n\n## 3.2 Attention\n\nBody.',
        "context": "section",
        "section": "3.2 Attention",
    }
    assert stored[3]["section"] == "4 Results"
    assert '"4 Results"' in stored[3]["text"] and "read it from the document" in stored[3]["text"]

    shown = (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"][0]
    assert shown["content"][2:4] == [
        {"type": "text", "context": "section", "section": "3.2 Attention"},
        {"type": "text", "context": "section", "section": "4 Results"},
    ]

    await _run_queued_turn(client)
    upstream = main[0]["messages"][0]["content"]
    assert all("section" not in block and "context" not in block for block in upstream)
    assert upstream[2]["text"].endswith("## 3.2 Attention\n\nBody.")


async def test_sections_are_capped_and_never_a_message_alone(make_client, tmp_path) -> None:
    client = await chatting_client(
        make_client(
            workspaces_dir=str(tmp_path / "ws"),
            chat_sections_per_message=2,
            chat_section_heading_max_chars=20,
            chat_selection_max_chars=50,
        )
    )
    conversation_id = await new_conversation(client)
    section = {"heading": "Intro", "text": "Body."}
    for sections in (
        [section] * 3,
        [{"heading": "  \u200b\n ", "text": "x"}],
        [{"heading": "h" * 21, "text": "x"}],
        [{"heading": "Long", "text": "x" * 51}],
    ):
        refused = await send(client, conversation_id, text="Look", sections=sections)
        assert refused.status_code == 400, sections
    alone = await send(client, conversation_id, text="  ", sections=[section])
    assert alone.status_code == 400
    ok = await send(
        client,
        conversation_id,
        text="Look",
        sections=[{"heading": "h" * 20, "text": "x" * 50}, {"heading": "Long", "text": None}],
    )
    assert ok.status_code == 202, ok.text


# --- the turn ---------------------------------------------------------------------------------


async def test_the_turn_block_carries_the_readers_languages(chat_client) -> None:
    client = chat_client
    await put_chat(client, answer_language="ja", translate_language="zh-TW")
    conversation_id = await new_conversation(client)
    assert (await send(client, conversation_id, text="hi", locale="en")).status_code == 202
    async with client.app.state.sessionmaker() as db:
        stored = (await db.execute(select(Message))).scalars().one().content
    note = next(block for block in stored if block.get("context") == TURN_CONTEXT)["text"]
    assert note.endswith(f"- reader locale: en\n{ANSWER_JA}\n{translation_line('zh-TW')}")


@respx.mock
async def test_the_instructions_follow_the_prompt_and_preset_verbatim(chat_client) -> None:
    client = chat_client
    preset = await client.post(
        "/api/prompts",
        json={"agent": "chat", "name": "Legacy", "text": "Legacy words."},
        headers=CSRF_HEADERS,
    )
    await put_chat(client, prompt_preset_id=preset.json()["id"])
    instructions = "I study {page_count} things.\n\n- Answer in tables"
    await put_chat(client, instructions=instructions)
    main, _ = stub_messages([DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello")
    await _run_queued_turn(client)

    system = main[0]["system"]
    assert len(system) == 5
    assert system[1]["text"] == "Legacy words."
    assert system[2]["text"] == chat_agent.INSTRUCTIONS_INTRO + instructions
    assert "# Skills" in system[3]["text"]
    assert system[4]["text"].startswith("# Memory\n")
    assert [block.get("cache_control") for block in system] == [None] * 4 + [
        {"type": "ephemeral", "ttl": "1h"}
    ]
    assert main[0]["tools"][-1]["name"] == "memory"
    start = turn_starts(await replayed_events(client, conversation_id))[0]
    assert start == {
        "model": "claude-code/claude-opus-5-5",
        "instructions": True,
        "memory_ids": [],
        "turn_id": start["turn_id"],
    }


@respx.mock
async def test_a_conversation_renders_the_snapshot_its_first_turn_took(chat_client) -> None:
    client = chat_client
    other = await another_document(client, client.user_id, "other.pdf")
    kept = await save_memory(client, kind="term", text="query → 查詢")
    scoped = await save_memory(
        client, kind="document", text="d is 512", document_id=client.document_id
    )
    narrowed = await save_memory(client, kind="preference", text="Short answers")
    pinned = await save_memory(client, kind="about", text="A student", pinned=True)
    await save_memory(client, kind="document", text="elsewhere", document_id=other)

    main, _ = stub_messages([DONE, DONE, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="one")
    await _run_queued_turn(client)
    first = main[0]["system"][-1]["text"]
    assert f"## Terms\n- [m{kept['id']}] query → 查詢" in first
    assert f"- [m{scoped['id']}] d is 512 (this document)" in first
    assert "elsewhere" not in first
    order = [kept["id"], narrowed["id"], pinned["id"], scoped["id"]]  # kind order
    assert await memory_ids_of(client, conversation_id) == order

    # Between the turns: one saved, one edited, one deleted, one narrowed to another
    # document.
    added = await save_memory(client, kind="term", text="key → 鍵")
    await client.put(
        f"/api/memories/{kept['id']}", json={"text": "query → 查询"}, headers=CSRF_HEADERS
    )
    await client.delete(f"/api/memories/{scoped['id']}", headers=CSRF_HEADERS)
    await client.put(
        f"/api/memories/{narrowed['id']}", json={"document_id": other}, headers=CSRF_HEADERS
    )
    await send(client, conversation_id, text="two")
    await _run_queued_turn(client)
    second = main[1]["system"][-1]["text"]
    assert f"- [m{kept['id']}] query → 查询" in second
    assert f"m{added['id']}" not in second and "d is 512" not in second
    assert "Short answers" not in second and f"- [m{pinned['id']}] A student" in second
    assert await memory_ids_of(client, conversation_id) == order
    starts = turn_starts(await replayed_events(client, conversation_id))
    assert [start["memory_ids"] for start in starts] == [order, [kept["id"], pinned["id"]]]
    assert all(start["instructions"] is False for start in starts)

    # A new conversation reads them as they are now.
    fresh = await new_conversation(client)
    await send(client, fresh, text="three")
    await _run_queued_turn(client)
    assert f"m{added['id']}" in main[2]["system"][-1]["text"]


@respx.mock
async def test_paused_memory_loads_nothing_and_the_tool_refuses(chat_client) -> None:
    client = chat_client
    kept = await save_memory(client, kind="term", text="query → 查詢")
    await put_chat(client, memory_paused=True)
    main, _ = stub_messages(
        [
            one_call(
                "m1",
                "memory",
                {"action": "save", "id": "", "kind": "about", "text": "x", "scope": "all"},
            ),
            DONE,
            DONE,
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="remember I am a student")
    await _run_queued_turn(client)

    assert main[0]["system"][-1]["text"] == memory_defs.PAUSED
    # Still declared: a changed tool list would cost the cached prefix.
    assert main[0]["tools"][-1]["name"] == "memory"
    refused = main[1]["messages"][2]["content"][0]
    assert refused["is_error"] is True and "paused" in refused["content"]
    assert await memory_ids_of(client, conversation_id) is None
    events = await replayed_events(client, conversation_id)
    assert not [name for name, _ in events if name == "memory"]
    assert turn_starts(events)[0]["memory_ids"] == []
    assert [memory["id"] for memory in (await client.get("/api/memories")).json()["memories"]] == [
        kept["id"]
    ]

    # Resumed, with auto-remember off: the next turn takes the snapshot.
    await put_chat(client, memory_paused=False, memory_auto=False)
    await send(client, conversation_id, text="go on")
    await _run_queued_turn(client)
    block = main[2]["system"][-1]["text"]
    assert f"- [m{kept['id']}] query → 查詢" in block and memory_defs.ON_REQUEST_ONLY in block
    assert await memory_ids_of(client, conversation_id) == [kept["id"]]


@respx.mock
async def test_the_memory_tool_saves_updates_and_forgets(chat_client) -> None:
    client = chat_client

    def call(call_id: str, **fields) -> tuple[list[dict], str]:
        tool_input = {"action": "save", "id": "", "kind": "", "text": "", "scope": "", **fields}
        return one_call(call_id, "memory", tool_input)

    main, _ = stub_messages(
        [
            call("toolu_s1", kind="term", text="query\n→ 查詢", scope="all"),
            call("toolu_s2", kind="document", text="d_model is 512", scope="document"),
            call("toolu_bad", kind="term", text="", scope="all"),
            DONE,
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="Remember query is 查詢")
    await _run_queued_turn(client)

    async with client.app.state.sessionmaker() as db:
        rows = (await db.execute(select(Memory).order_by(Memory.id))).scalars().all()
    term, fact = rows
    assert (term.kind, term.text, term.document_id) == ("term", "query → 查詢", None)
    assert (fact.kind, fact.document_id) == ("document", client.document_id)
    assert [(row.conversation_id, row.source_ref) for row in rows] == [
        (conversation_id, "toolu_s1"),
        (conversation_id, "toolu_s2"),
    ]
    results = [main[index]["messages"][2 * index]["content"][0] for index in (1, 2, 3)]
    assert results[0]["content"] == f"Saved as m{term.id}." and "is_error" not in results[0]
    assert results[2]["is_error"] is True  # a save needs text
    # The snapshot was taken before the saves: they wait for the next conversation.
    assert await memory_ids_of(client, conversation_id) == []

    stub_messages(
        [
            call("toolu_u", action="update", id=f"m{term.id}", text="query → 查询"),
            call("toolu_f", action="forget", id=f"m{fact.id}"),
            call("toolu_x", action="forget", id="m999999"),
            DONE,
        ]
    )
    await send(client, conversation_id, text="Actually 查询, and forget d_model")
    await _run_queued_turn(client)

    events = [
        data for name, data in await replayed_events(client, conversation_id) if name == "memory"
    ]
    assert [event["tool_use_id"] for event in events] == [
        "toolu_s1",
        "toolu_s2",
        "toolu_u",
        "toolu_f",
    ]
    assert [event["action"] for event in events] == ["saved", "saved", "updated", "forgotten"]
    term_saved = {
        "id": term.id,
        "kind": "term",
        "text": "query → 查詢",
        "document_id": None,
        "pinned": False,
    }
    assert events[0]["memory"] == term_saved and events[0]["previous"] is None
    assert events[2]["previous"] == term_saved
    assert events[2]["memory"] == {**term_saved, "text": "query → 查询"}
    assert events[3]["memory"]["id"] == fact.id and events[3]["memory"]["text"] == "d_model is 512"
    assert events[3]["previous"] is None
    listed = (await client.get("/api/memories")).json()["memories"]
    assert [(memory["id"], memory["text"]) for memory in listed] == [(term.id, "query → 查询")]
    # Undo of the forget: saved again with the call's id as its source.
    restored = await save_memory(
        client,
        kind="document",
        text="d_model is 512",
        document_id=client.document_id,
        conversation_id=conversation_id,
        source_ref="toolu_f",
    )
    assert restored["source_ref"] == "toolu_f"


async def test_the_tool_refuses_what_it_cannot_do(chat_client) -> None:
    client = chat_client
    settings = client.app.state.settings.model_copy(update={"chat_memories_max": 1})
    conversation_id = await new_conversation(client)
    run = {
        "user_id": client.user_id,
        "conversation_id": conversation_id,
        "document_id": client.document_id,
        "tool_use_id": "t",
    }
    sessionmaker = client.app.state.sessionmaker
    too_long = "x" * (settings.chat_memory_max_chars + 1)

    async def tool(**tool_input) -> str:
        filled = {"action": "save", "id": "", "kind": "", "text": "", "scope": "", **tool_input}
        outcome = await chat_memory.run_memory_tool(
            sessionmaker, settings, **run, tool_input=filled
        )
        assert outcome.is_error == (outcome.memory is None)
        return outcome.content

    assert "action" in await tool(action="remember")
    assert "needs" in await tool(kind="term", text="x")  # no scope
    assert "at most" in await tool(kind="term", text=too_long, scope="all")
    assert "id" in await tool(action="update", id="twelve", text="x")
    assert "none was given" in await tool(action="update", id="m1")
    assert (await tool(kind="term", text="first", scope="all")).startswith("Saved as m")
    assert "full" in await tool(kind="term", text="second", scope="all")
    theirs = await stranger_rows(client)
    assert "No memory" in await tool(action="forget", id=f"m{theirs.memory_id}")
    async with sessionmaker() as db:
        assert (await db.get(Memory, theirs.memory_id)) is not None


async def test_a_save_of_what_is_already_saved_adds_nothing(chat_client) -> None:
    """A save cut off before its result was stored reads as interrupted on the replay, and
    the agent makes it again: the reader keeps one memory, not two (docs/chat.md § Memory)."""
    client = chat_client
    settings = client.app.state.settings.model_copy(update={"chat_memories_max": 2})
    conversation_id = await new_conversation(client)
    sessionmaker = client.app.state.sessionmaker

    async def save(tool_use_id: str, **fields):
        tool_input = {
            "action": "save",
            "id": "",
            "kind": "term",
            "text": "query → 查詢",
            "scope": "all",
            **fields,
        }
        return await chat_memory.run_memory_tool(
            sessionmaker,
            settings,
            user_id=client.user_id,
            conversation_id=conversation_id,
            document_id=client.document_id,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
        )

    first = await save("toolu_1")
    saved_id = first.memory["memory"]["id"]
    # The same memory once cleaned: answered with its id, no row and no event.
    again = await save("toolu_2", text=" query\n→ 查詢​ ")
    assert (again.content, again.is_error, again.memory) == (
        f"Already saved as m{saved_id}.",
        False,
        None,
    )
    # Another scope is another memory; at the cap, the same one still answers its id
    # rather than saying the memory is full.
    scoped = await save("toolu_3", scope="document")
    assert scoped.memory is not None and scoped.memory["memory"]["id"] != saved_id
    assert (await save("toolu_4", scope="document")).content == (
        f"Already saved as m{scoped.memory['memory']['id']}."
    )
    assert "full" in (await save("toolu_5", kind="preference")).content
    async with sessionmaker() as db:
        rows = (await db.execute(select(Memory).order_by(Memory.id))).scalars().all()
    assert [(row.source_ref, row.document_id) for row in rows] == [
        ("toolu_1", None),
        ("toolu_3", client.document_id),
    ]


async def test_control_characters_never_reach_a_stored_text(chat_client) -> None:
    """NUL is refused by PostgreSQL's TEXT: every way a memory, the instructions or a
    section's heading comes in is cleaned of it first, never answered with a 500."""
    client = chat_client
    saved = await save_memory(client, kind="term", text="a\x00b\x07")
    assert saved["text"] == "ab"
    edited = await client.put(
        f"/api/memories/{saved['id']}", json={"text": "c\x00d"}, headers=CSRF_HEADERS
    )
    assert edited.status_code == 200 and edited.json()["text"] == "cd"
    instructions = (await put_chat(client, instructions="Be\x00 brief.\n\tAlways.")).json()
    assert instructions["instructions"] == "Be brief.\n\tAlways."
    conversation_id = await new_conversation(client)
    outcome = await chat_memory.run_memory_tool(
        client.app.state.sessionmaker,
        client.app.state.settings,
        user_id=client.user_id,
        conversation_id=conversation_id,
        document_id=client.document_id,
        tool_use_id="toolu_nul",
        tool_input={"action": "save", "id": "", "kind": "about", "text": "x\x00y", "scope": "all"},
    )
    assert outcome.memory is not None and outcome.memory["memory"]["text"] == "xy"
    sent = await send(
        client,
        conversation_id,
        text="Look",
        sections=[{"heading": "3.2\x00 Attention", "text": "Body."}],
    )
    assert sent.status_code == 202, sent.text
    async with client.app.state.sessionmaker() as db:
        stored = (await db.execute(select(Message))).scalars().one().content
    assert [block.get("section") for block in stored if block.get("context") == "section"] == [
        "3.2 Attention"
    ]


# --- the tool in a round ------------------------------------------------------------------


def round_context() -> SimpleNamespace:
    """What `_run_tools` reads of a turn, for a round of reads with no database behind it."""
    return SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=1000,
            chat_image_turn_max_bytes=1000,
            chat_page_image_max_pixels=1,
            upload_max_bytes=1,
            chat_bash_timeout_seconds=1,
            sandbox_start_timeout_seconds=1,
            sandbox_exec_grace_seconds=1,
            chat_writer_grace_seconds=1,
        ),
        sessionmaker=None,
        conversation=SimpleNamespace(id=1, session="s"),
        paths=None,
        document=SimpleNamespace(id=1),
        decode_slot=asyncio.Semaphore(1),
        job=SimpleNamespace(page_count=12, kind="pdf"),
        render_slots=None,
        sandbox=None,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        image_spent=0,
        writers={},
        write_gate=asyncio.Lock(),
    )


SAVED = chat_tools.ToolOutcome(
    "Saved as m1.",
    memory={
        "action": "saved",
        "memory": {"id": 1, "kind": "about", "text": "x", "document_id": None, "pinned": False},
        "previous": None,
    },
)
MEMORY_CALL = {
    "type": "tool_use",
    "id": "toolu_m",
    "name": "memory",
    "input": {"action": "save", "id": "", "kind": "about", "text": "x", "scope": "all"},
}


async def test_a_spent_image_budget_never_refuses_the_memory_tool(monkeypatch) -> None:
    async def fake_memory_tool(sessionmaker, settings, **kwargs):
        return SAVED

    async def no_events(ctx, event, data):
        return None

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_memory, "run_memory_tool", fake_memory_tool)
    monkeypatch.setattr(chat_runner, "_emit", no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = round_context()
    ctx.image_spent = ctx.settings.chat_image_turn_max_bytes
    page = {"type": "tool_use", "id": "p1", "name": "view_page", "input": {"page": 1}}
    viewed, remembered = await chat_runner._run_tools(ctx, [page, MEMORY_CALL], set())
    # The page is refused; the memory, which returns no image, is saved all the same.
    assert viewed["content"] == chat_runner.IMAGE_TURN_BUDGET_SPENT
    assert remembered["content"] == "Saved as m1." and "is_error" not in remembered


async def test_a_cancel_never_parts_a_memorys_commit_from_its_events(monkeypatch) -> None:
    """Cancelled between the row's commit and what reports it, the call runs on, and the
    end of the turn waits for its tool_result and `memory` event, as it does for a write:
    otherwise the reader keeps a memory the thread draws no line for."""
    in_flight = asyncio.Event()
    release = asyncio.Event()
    emitted: list[tuple[str, dict]] = []

    async def slow_memory_tool(sessionmaker, settings, **kwargs):
        in_flight.set()
        await release.wait()
        return SAVED

    async def record(ctx, event, data):
        emitted.append((event, data))

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_memory, "run_memory_tool", slow_memory_tool)
    monkeypatch.setattr(chat_runner, "_emit", record)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = round_context()
    running = asyncio.create_task(chat_runner._run_tools(ctx, [MEMORY_CALL], set()))
    await asyncio.wait_for(in_flight.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert list(ctx.writers.values()) == ["memory"] and emitted == []

    settling = asyncio.create_task(chat_runner._settle_writers(ctx))
    await asyncio.sleep(0.05)
    assert not settling.done()
    release.set()
    await asyncio.wait_for(settling, 5)
    assert [event for event, _ in emitted] == [chat_events.TOOL_RESULT, chat_events.MEMORY]
    assert emitted[1][1]["tool_use_id"] == "toolu_m" and ctx.writers == {}


async def test_a_memory_call_that_fails_after_a_cancel_is_logged_and_reported(
    monkeypatch, caplog
) -> None:
    """Nobody awaits the call once the round is cancelled, so its own failure is logged and
    answered as an error tool_result there, as `_run_tool` would have done."""
    in_flight = asyncio.Event()
    release = asyncio.Event()
    emitted: list[tuple[str, dict]] = []

    async def failing_memory_tool(sessionmaker, settings, **kwargs):
        in_flight.set()
        await release.wait()
        raise RuntimeError("the connection dropped")

    async def record(ctx, event, data):
        emitted.append((event, data))

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_memory, "run_memory_tool", failing_memory_tool)
    monkeypatch.setattr(chat_runner, "_emit", record)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    ctx = round_context()
    running = asyncio.create_task(chat_runner._run_tools(ctx, [MEMORY_CALL], set()))
    await asyncio.wait_for(in_flight.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    release.set()
    with caplog.at_level(logging.ERROR, logger=chat_runner.logger.name):
        await asyncio.wait_for(chat_runner._settle_writers(ctx), 5)
    assert [record.getMessage() for record in caplog.records] == [
        f"tool memory failed in turn {ctx.turn.id}"
    ]
    assert [event for event, _ in emitted] == [chat_events.TOOL_RESULT]
    assert emitted[0][1]["is_error"] is True and ctx.writers == {}


async def test_a_failed_statement_never_carries_the_readers_text(tmp_path) -> None:
    """The engine hides bound values, so a database error logged with its traceback cannot
    write a memory's text — or instructions, or a message — into the log."""
    from sqlalchemy import text as sql
    from sqlalchemy.exc import DBAPIError

    from sightread.db.session import create_engine

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'hidden.db'}")
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as failed:
                await connection.execute(
                    sql("insert into no_such_table (text) values (:text)"),
                    {"text": "the reader reads with a screen reader"},
                )
    finally:
        await engine.dispose()
    assert "screen reader" not in str(failed.value)
