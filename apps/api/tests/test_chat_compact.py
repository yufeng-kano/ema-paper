"""Compaction, the cache clock, the memory file and a conversation's move to another model
(docs/chat.md § Compaction, § Memory *The memory file*; docs/api.md): the routes, the
worker's idle pass, and turns running them against a stubbed Messages stream. No test
reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread.agents import chat as chat_agent
from sightread.billing.credits import parse_model_prices
from sightread.db.models import (
    COMPACT_TURN,
    IDLE_COMPACT_TURN,
    MESSAGE_TURN,
    ChatTurn,
    Conversation,
    CreditEntry,
    CreditPeriod,
    Message,
)
from sightread.jobs import chat_compact, chat_runner
from sightread.upstream.anthropic import ChatUsage
from sightread.workspace import git
from sightread.workspace.layout import user_root
from sightread.workspace.locks import mark_writing, workspace_lock
from sightread.workspace.memory_file import memory_file_text
from tests.conftest import CSRF_HEADERS
from tests.test_chat import MESSAGES_URL, _run_queued_turn, sse
from tests.test_chat_extensions import (
    chatting_client,
    new_conversation,
    replayed_events,
    send,
    stub_messages,
)

DONE = ([{"type": "text", "text": "Done."}], "end_turn")
SUMMARY = ([{"type": "text", "text": "## Requests\n\nThe reader asked for tables."}], "end_turn")
MODEL = "claude-code/claude-opus-5-5"
OTHER_MODEL = "codex/gpt-5.5"


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


async def conversation_row(client: AsyncClient, conversation_id: int) -> Conversation:
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Conversation, conversation_id)
        db.expunge(row)
        return row


async def detail(client: AsyncClient, conversation_id: int) -> dict:
    response = await client.get(f"/api/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def turns_of(client: AsyncClient, conversation_id: int) -> list[ChatTurn]:
    async with client.app.state.sessionmaker() as db:
        rows = (
            (
                await db.execute(
                    select(ChatTurn)
                    .where(ChatTurn.conversation_id == conversation_id)
                    .order_by(ChatTurn.created_at)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            db.expunge(row)
        return rows


async def set_clock(client: AsyncClient, conversation_id: int, *, warm_for: float, tokens: int):
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                cache_warm_until=datetime.now(UTC) + timedelta(seconds=warm_for),
                context_tokens=tokens,
                # As a turn's own writes leave it: the test reads it back unmoved.
                updated_at=Conversation.updated_at,
            )
        )
        await db.commit()


def texts(content: list[dict]) -> list[str]:
    return [block["text"] for block in content if block.get("type") == "text"]


# --- /compact ------------------------------------------------------------------------------


@respx.mock
async def test_compact_has_nothing_to_do_before_an_answer(chat_client) -> None:
    """A conversation with no answer since its last summary refuses `/compact` with its own
    error, so the composer can say so (docs/api.md)."""
    client = chat_client
    conversation_id = await new_conversation(client)
    refused = await send(client, conversation_id, text="", command="compact")
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["type"] == "nothing_to_compact"
    assert (await detail(client, conversation_id))["messages"] == []
    assert not (await detail(client, conversation_id))["conversation"]["compactable"]


@respx.mock
async def test_a_compaction_replaces_the_history_with_its_summary(chat_client) -> None:
    """`/compact focus` sends the instruction with the focus, runs as a turn of its own
    kind, and leaves a summary row the next turn replays in place of everything before it
    (docs/chat.md § Compaction)."""
    client = chat_client
    main, _ = stub_messages([DONE, SUMMARY, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    before = (await detail(client, conversation_id))["conversation"]
    assert before["compactable"]
    assert before["compacted_message_id"] is None

    sent = await send(client, conversation_id, text="keep the table", command="compact")
    assert sent.status_code == 202, sent.text
    turns = (await detail(client, conversation_id))["turns"]
    assert [turn["kind"] for turn in turns] == [MESSAGE_TURN, COMPACT_TURN]
    shown = (await detail(client, conversation_id))["messages"][-1]["content"]
    assert shown == [{"type": "text", "text": "/compact keep the table"}]
    await _run_queued_turn(client)

    request = main[1]["messages"][-1]
    assert request["role"] == "user"
    instruction = request["content"][1]["text"]
    assert instruction.startswith(chat_agent.COMPACT_INSTRUCTION)
    assert instruction.endswith(chat_agent.COMPACT_FOCUS.format(focus="keep the table"))
    assert request["content"][0]["text"] == "/compact keep the table"
    assert "context" not in request["content"][1]
    events = await replayed_events(client, conversation_id)
    starts = [data for name, data in events if name == "turn_start"]
    assert [start["kind"] for start in starts] == [MESSAGE_TURN, COMPACT_TURN]

    row = await conversation_row(client, conversation_id)
    async with client.app.state.sessionmaker() as db:
        summary = await db.get(Message, row.compacted_message_id)
        assert summary.role == "user"
        assert summary.content[0]["context"] == chat_compact.SUMMARY_CONTEXT
        assert summary.content[0]["text"] == chat_agent.SUMMARY_PREFIX + SUMMARY[0][0]["text"]
        assert str(summary.turn_id) == turns[1]["id"]
    assert row.memory_ids is None
    assert row.context_tokens is None
    assert row.cache_warm_until is None
    after = await detail(client, conversation_id)
    assert after["conversation"]["compacted_message_id"] == row.compacted_message_id
    assert not after["conversation"]["compactable"]
    assert after["turns"][1]["status"] == "succeeded"
    assert after["messages"][-1]["content"] == [
        {
            "type": "text",
            "text": chat_agent.SUMMARY_PREFIX + SUMMARY[0][0]["text"],
            "context": chat_compact.SUMMARY_CONTEXT,
        }
    ]

    await send(client, conversation_id, text="and now?")
    await _run_queued_turn(client)
    replay = main[2]["messages"]
    assert [message["role"] for message in replay] == ["user", "user"]
    assert texts(replay[0]["content"]) == [chat_agent.SUMMARY_PREFIX + SUMMARY[0][0]["text"]]
    assert "context" not in replay[0]["content"][0]
    assert texts(replay[1]["content"])[0] == "and now?"
    assert (await detail(client, conversation_id))["conversation"]["compactable"]


@respx.mock
async def test_a_compaction_that_answers_nothing_fails_and_is_not_replayed(chat_client) -> None:
    """A summary of no text is no summary: the turn fails with `compact_empty`, the boundary
    stays where it was, and the attempt's rows never reach a later call (docs/chat.md
    § Compaction, *What it leaves*)."""
    client = chat_client
    main, _ = stub_messages([DONE, ([], "end_turn"), DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    turns = (await detail(client, conversation_id))["turns"]
    assert (turns[1]["kind"], turns[1]["status"], turns[1]["error"]) == (
        COMPACT_TURN,
        "failed",
        "compact_empty",
    )
    row = await conversation_row(client, conversation_id)
    assert row.compacted_message_id is None
    assert (await detail(client, conversation_id))["conversation"]["compactable"]

    await send(client, conversation_id, text="still here")
    await _run_queued_turn(client)
    replay = main[2]["messages"]
    assert [message["role"] for message in replay] == ["user", "assistant", "user"]
    assert texts(replay[0]["content"])[0] == "hello there"
    assert texts(replay[2]["content"])[0] == "still here"


@respx.mock
async def test_a_compaction_updates_the_memory_file_within_its_own_allowance(
    make_client, tmp_path
) -> None:
    """The compaction may call tools up to `CHAT_COMPACT_MAX_TOOL_CALLS`, counted across
    its rounds: a round that asks for more runs the first of them and answers the rest
    with an error, and the call after the cap takes no tool — its text is the summary. A
    file the agent wrote to `/workspace/memory.md` is the snapshot the compacted
    conversation goes on with, and the next turn's system prompt carries it (docs/chat.md
    § Compaction, § Memory)."""
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_compact_max_tool_calls=1)
    )
    main, _ = stub_messages(
        [
            DONE,
            (
                [
                    {
                        "type": "tool_use",
                        "id": "e1",
                        "name": "str_replace_based_edit_tool",
                        "input": {
                            "command": "create",
                            "path": "/workspace/memory.md",
                            "file_text": "## Conventions\n\n- Tables, never lists\n",
                        },
                    },
                    # Past the allowance in the same round: answered, never run.
                    {
                        "type": "tool_use",
                        "id": "e2",
                        "name": "str_replace_based_edit_tool",
                        "input": {
                            "command": "create",
                            "path": "/workspace/notes.md",
                            "file_text": "never written\n",
                        },
                    },
                ],
                "tool_use",
            ),
            SUMMARY,
            DONE,
        ]
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="use tables")
    await _run_queued_turn(client)
    assert all(part["text"] != chat_agent.MEMORY_FILE_INTRO for part in main[0]["system"])

    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    assert "tool_choice" not in main[1]
    assert main[2]["tool_choice"] == {"type": "none"}
    results = main[2]["messages"][-1]["content"]
    assert [(block["tool_use_id"], block.get("is_error", False)) for block in results] == [
        ("e1", False),
        ("e2", True),
    ]
    assert results[1]["content"] == chat_runner.NOT_RUN_COMPACT_ALLOWANCE
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "memory.md").read_text() == "## Conventions\n\n- Tables, never lists\n"
    assert not (root / "notes.md").exists()
    assert [revision.message for revision in git.log(root, Path("memory.md"))] == [
        "create /workspace/memory.md"
    ]
    row = await conversation_row(client, conversation_id)
    assert row.compacted_message_id is not None
    assert row.memory_file == "## Conventions\n\n- Tables, never lists\n"
    turns = (await detail(client, conversation_id))["turns"]
    assert [(turn["kind"], turn["status"]) for turn in turns] == [
        (MESSAGE_TURN, "succeeded"),
        (COMPACT_TURN, "succeeded"),
    ]

    await send(client, conversation_id, text="go on")
    await _run_queued_turn(client)
    kinds = [part["text"].split("\n", 1)[0] for part in main[3]["system"]]
    memory_file = next(
        part["text"] for part in main[3]["system"] if part["text"].startswith("# Memory file")
    )
    assert memory_file == chat_agent.MEMORY_FILE_INTRO + "## Conventions\n\n- Tables, never lists"
    assert kinds.index("# Memory file") == kinds.index("# Memory") - 1


@respx.mock
async def test_a_cancel_after_the_summary_is_stored_sets_no_boundary(
    chat_client, monkeypatch
) -> None:
    """The boundary is set with the terminal state: a cancel that lands once the summary
    row is stored but before the turn's finish ends a cancelled turn that set nothing, and
    the row it left is replayed by no later turn (docs/chat.md § Compaction)."""
    main, _ = stub_messages([DONE, SUMMARY, DONE])
    conversation_id = await new_conversation(chat_client)
    await send(chat_client, conversation_id, text="use tables")
    await _run_queued_turn(chat_client)

    store = chat_runner._store_summary

    async def store_then_cancel(ctx, content) -> None:
        await store(ctx, content)
        async with chat_client.app.state.sessionmaker() as db:
            turn = await db.get(ChatTurn, ctx.turn.id)
            turn.status = "cancelled"
            await db.commit()

    monkeypatch.setattr(chat_runner, "_store_summary", store_then_cancel)
    await send(chat_client, conversation_id, text="", command="compact")
    await _run_queued_turn(chat_client)
    row = await conversation_row(chat_client, conversation_id)
    assert row.compacted_message_id is None
    turns = (await detail(chat_client, conversation_id))["turns"]
    assert [(turn["kind"], turn["status"]) for turn in turns] == [
        (MESSAGE_TURN, "succeeded"),
        (COMPACT_TURN, "cancelled"),
    ]
    async with chat_client.app.state.sessionmaker() as db:
        stored = (
            (
                await db.execute(
                    select(Message.content)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.id)
                )
            )
            .scalars()
            .all()
        )
    assert chat_compact.is_summary(stored[-1])

    monkeypatch.setattr(chat_runner, "_store_summary", store)
    await send(chat_client, conversation_id, text="go on")
    await _run_queued_turn(chat_client)
    # The first turn's exchange and the new message: nothing of the cancelled compaction.
    assert [message["role"] for message in main[2]["messages"]] == ["user", "assistant", "user"]
    assert "## Requests" not in json.dumps(main[2]["messages"])


async def test_the_memory_file_is_read_under_the_workspace_lock(chat_client) -> None:
    """A snapshot waits for the workspace lock a shell command of another conversation
    holds for its whole run, so the file is never read half-written; a dead worker's
    writing marker is no writer in flight and refuses no read (docs/chat.md § Memory,
    *The memory file*)."""
    settings = chat_client.app.state.settings
    root = user_root(settings.workspaces_dir, chat_client.user_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.md").write_text("notes\n")
    async with workspace_lock(root):
        reading = asyncio.create_task(memory_file_text(settings, chat_client.user_id))
        await asyncio.sleep(0.05)
        assert not reading.done()
    assert await reading == "notes\n"
    mark_writing(root, 1, "dead")
    assert await memory_file_text(settings, chat_client.user_id) == "notes\n"


# --- the cache clock ------------------------------------------------------------------------


def cached_stream(blocks: list[dict], stop_reason: str, usage: dict) -> bytes:
    """A stream whose `message_start` reports the usage given, as the Anthropic API does."""
    events: list[dict] = [
        {"type": "message_start", "message": {"model": MODEL, "usage": usage}},
    ]
    for index, block in enumerate(blocks):
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
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


def stub_usages(usages: list[dict]) -> list[dict]:
    """Answer each call with `Done.` and the next usage; the requests are kept."""
    answers = iter(usages)
    requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=cached_stream(DONE[0], "end_turn", next(answers)))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    return requests


@respx.mock
async def test_the_cache_clock_runs_on_the_native_usage_alone(chat_client) -> None:
    """A call whose usage carries the API's `cache_creation` object and a cache figure sets
    the clock an hour out and the context to the tokens it replayed; a call through a
    conversion path, reporting no such object, leaves no clock, whatever its figures say
    (docs/chat.md § Compaction, *The cache clock and the context*)."""
    client = chat_client
    requests = stub_usages(
        [
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 90,
                "cache_creation_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            },
            {"input_tokens": 10, "cache_read_input_tokens": 90, "cache_creation_input_tokens": 20},
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            },
        ]
    )
    conversation_id = await new_conversation(client)
    started = datetime.now(UTC)
    await send(client, conversation_id, text="one")
    await _run_queued_turn(client)
    row = await conversation_row(client, conversation_id)
    assert row.context_tokens == 10 + 90 + 0 + 5
    assert row.cache_warm_until is not None
    assert timedelta(minutes=59) < row.cache_warm_until - started < timedelta(minutes=61)
    payload = (await detail(client, conversation_id))["conversation"]
    assert payload["context_tokens"] == 105
    assert payload["context_window"] == client.app.state.settings.chat_context_window_tokens
    assert payload["cache_warm_until"] is not None
    # 105 tokens at US$1 per million re-cached, in credits of US$0.002: one, the floor.
    assert payload["recache_credits"] == 1
    listed = (await client.get(f"/api/library/documents/{client.document_id}/conversations")).json()
    assert listed["conversations"][0]["context_tokens"] == 105
    assert listed["conversations"][0]["compactable"]

    await send(client, conversation_id, text="two")
    await _run_queued_turn(client)
    row = await conversation_row(client, conversation_id)
    assert row.context_tokens == 10 + 90 + 20 + 5
    assert row.cache_warm_until is None

    await send(client, conversation_id, text="three")
    await _run_queued_turn(client)
    row = await conversation_row(client, conversation_id)
    assert row.context_tokens == 105
    assert row.cache_warm_until is None
    assert len(requests) == 3


def test_the_usage_knows_whether_the_api_cached() -> None:
    def usage(*, native: bool, read: int = 0, write: int = 0) -> ChatUsage:
        return ChatUsage(
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=read,
            cache_write_tokens=write,
            cost=None,
            cost_source="unknown",
            cache_native=native,
        )

    assert usage(native=True, read=5).cached
    assert usage(native=True, write=5).cached
    assert not usage(native=True).cached
    assert not usage(native=False, read=5, write=5).cached


def test_a_price_may_carry_the_models_context_window() -> None:
    prices = parse_model_prices(
        json.dumps(
            {
                "m": {
                    "input": 1,
                    "cache_read": 1,
                    "cache_write": 1,
                    "output": 1,
                    "context": 1000000,
                },
                "n": {"input": 1, "cache_read": 1, "cache_write": 1, "output": 1},
            }
        )
    )
    assert prices["m"].context == 1_000_000
    assert prices["n"].context is None
    for context in (0, -1, "big", 1.5, True):
        with pytest.raises(ValueError, match="context"):
            parse_model_prices(
                json.dumps(
                    {
                        "m": {
                            "input": 1,
                            "cache_read": 1,
                            "cache_write": 1,
                            "output": 1,
                            "context": context,
                        }
                    }
                )
            )


# --- the model between turns ----------------------------------------------------------------


@respx.mock
async def test_a_conversation_moves_to_another_model_between_turns(chat_client) -> None:
    """The reader picks a model for the conversation: offered models only, never under a
    turn in flight, the cache clock cleared on a change; the next turn runs on it
    (docs/chat.md § Conversations, docs/api.md)."""
    client = chat_client
    main, _ = stub_messages([DONE, DONE])
    conversation_id = await new_conversation(client)
    url = f"/api/conversations/{conversation_id}"
    assert (await client.put(url, json={}, headers=CSRF_HEADERS)).status_code == 400
    unknown = await client.put(url, json={"model": "vendor/unknown"}, headers=CSRF_HEADERS)
    assert unknown.status_code == 400, unknown.text

    await send(client, conversation_id, text="one")
    queued = await client.put(url, json={"model": OTHER_MODEL}, headers=CSRF_HEADERS)
    assert queued.status_code == 409, queued.text
    renamed = await client.put(url, json={"title": "Kept"}, headers=CSRF_HEADERS)
    assert renamed.status_code == 200, renamed.text
    await _run_queued_turn(client)
    assert main[0]["model"] == MODEL
    await set_clock(client, conversation_id, warm_for=3000, tokens=50)

    moved = await client.put(url, json={"model": OTHER_MODEL}, headers=CSRF_HEADERS)
    assert moved.status_code == 200, moved.text
    assert moved.json()["model"] == OTHER_MODEL
    assert moved.json()["title"] == "Kept"
    assert moved.json()["cache_warm_until"] is None
    assert moved.json()["context_tokens"] == 50
    assert moved.json()["compactable"]
    await set_clock(client, conversation_id, warm_for=3000, tokens=50)
    same = await client.put(url, json={"model": OTHER_MODEL}, headers=CSRF_HEADERS)
    assert same.status_code == 200
    assert same.json()["cache_warm_until"] is not None

    await send(client, conversation_id, text="two")
    await _run_queued_turn(client)
    assert main[1]["model"] == OTHER_MODEL
    starts = [
        data
        for name, data in await replayed_events(client, conversation_id)
        if name == "turn_start"
    ]
    assert [start["model"] for start in starts] == [MODEL, OTHER_MODEL]


# --- the idle pass --------------------------------------------------------------------------


@respx.mock
async def test_the_idle_pass_compacts_a_long_conversation_before_its_cache_expires(
    chat_client,
) -> None:
    """A conversation whose cache expires within the lead and whose context has reached the
    minimum gets an idle compaction: a turn of its own kind, in the last turn's locale,
    with a credit entry, and the clock cleared so the next pass leaves it alone. Run, it
    leaves a summary and does not move the conversation up the list (docs/chat.md
    § Compaction, *Idle compaction*)."""
    client = chat_client
    settings = client.app.state.settings
    main, _ = stub_messages([DONE, SUMMARY])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello", locale="zh-TW")
    await _run_queued_turn(client)
    listed_before = (await detail(client, conversation_id))["conversation"]["updated_at"]
    await set_clock(
        client,
        conversation_id,
        warm_for=settings.chat_idle_compact_lead_seconds - 10,
        tokens=settings.chat_idle_compact_min_tokens,
    )

    assert await chat_compact.idle_compact_once(client.app.state.sessionmaker, settings) == 1
    turns = await turns_of(client, conversation_id)
    assert [(turn.kind, turn.status, turn.locale) for turn in turns] == [
        (MESSAGE_TURN, "succeeded", "zh-TW"),
        (IDLE_COMPACT_TURN, "queued", "zh-TW"),
    ]
    row = await conversation_row(client, conversation_id)
    assert row.cache_warm_until is None
    async with client.app.state.sessionmaker() as db:
        message = await db.get(Message, turns[1].message_id)
        assert message.role == "user"
        assert [block.get("context") for block in message.content] == [
            chat_compact.COMPACT_CONTEXT,
            chat_agent.TURN_CONTEXT,
        ]
        assert message.content[0]["text"] == chat_agent.compact_instruction("")
        assert message.turn_id == turns[1].id
        entry = (
            await db.execute(select(CreditEntry).where(CreditEntry.turn_id == turns[1].id))
        ).scalar_one()
        assert entry.kind == "chat"
    # The next pass finds nothing: the clock is cleared, and a turn is queued anyway.
    assert await chat_compact.idle_compact_once(client.app.state.sessionmaker, settings) == 0

    await _run_queued_turn(client)
    assert main[1]["messages"][-1]["content"][0]["text"] == chat_agent.compact_instruction("")
    after = await detail(client, conversation_id)
    assert after["turns"][1] == {
        **after["turns"][1],
        "kind": IDLE_COMPACT_TURN,
        "status": "succeeded",
        "error": None,
    }
    assert after["conversation"]["compacted_message_id"] is not None
    assert after["conversation"]["updated_at"] == listed_before
    assert after["messages"][-1]["content"][0]["context"] == chat_compact.SUMMARY_CONTEXT


@respx.mock
async def test_the_idle_pass_leaves_what_is_not_due_and_what_it_may_not_touch(
    chat_client,
) -> None:
    """Not due: a cache with more than the lead left, a context under the minimum. Not
    touched, though due: the reader's switch off, a turn still unfinished, a balance under
    the send's minimum — each with the clock cleared and nothing enqueued, not even the
    message of a refused reservation (docs/chat.md § Compaction, *Idle compaction*)."""
    client = chat_client
    settings = client.app.state.settings
    sessionmaker = client.app.state.sessionmaker
    stub_messages([DONE, DONE, DONE])
    lead = settings.chat_idle_compact_lead_seconds
    minimum = settings.chat_idle_compact_min_tokens

    async def message_count(conversation_id: int) -> int:
        return len((await detail(client, conversation_id))["messages"])

    later = await new_conversation(client)
    await send(client, later, text="hello")
    await _run_queued_turn(client)
    await set_clock(client, later, warm_for=lead + 600, tokens=minimum)
    short = await new_conversation(client)
    await send(client, short, text="hello")
    await _run_queued_turn(client)
    await set_clock(client, short, warm_for=lead - 10, tokens=minimum - 1)
    assert await chat_compact.idle_compact_once(sessionmaker, settings) == 0
    assert (await conversation_row(client, later)).cache_warm_until is not None
    assert (await conversation_row(client, short)).cache_warm_until is not None

    due = await new_conversation(client)
    await send(client, due, text="hello")
    await _run_queued_turn(client)
    count = await message_count(due)

    switched_off = await client.put(
        "/api/agents/chat/settings", json={"idle_compact": False}, headers=CSRF_HEADERS
    )
    assert switched_off.status_code == 200, switched_off.text
    assert not switched_off.json()["idle_compact"]
    await set_clock(client, due, warm_for=lead - 10, tokens=minimum)
    assert await chat_compact.idle_compact_once(sessionmaker, settings) == 0
    assert (await conversation_row(client, due)).cache_warm_until is None
    assert len(await turns_of(client, due)) == 1
    await client.put("/api/agents/chat/settings", json={"idle_compact": True}, headers=CSRF_HEADERS)

    await send(client, due, text="still thinking")  # queued, never run
    await set_clock(client, due, warm_for=lead - 10, tokens=minimum)
    assert await chat_compact.idle_compact_once(sessionmaker, settings) == 0
    assert (await conversation_row(client, due)).cache_warm_until is None
    assert [turn.kind for turn in await turns_of(client, due)] == [MESSAGE_TURN, MESSAGE_TURN]
    await _run_queued_turn(client)
    count = await message_count(due)

    async with sessionmaker() as db:
        await db.execute(
            update(CreditPeriod)
            .where(CreditPeriod.user_id == client.user_id)
            .values(spent=CreditPeriod.granted)
        )
        await db.commit()
    await set_clock(client, due, warm_for=lead - 10, tokens=minimum)
    assert await chat_compact.idle_compact_once(sessionmaker, settings) == 0
    assert (await conversation_row(client, due)).cache_warm_until is None
    assert len(await turns_of(client, due)) == 2
    assert await message_count(due) == count


# --- the memory file ------------------------------------------------------------------------


@respx.mock
async def test_the_memory_file_is_read_and_written_on_the_agent_page(make_client, tmp_path) -> None:
    """`memory.md` at the workspace root: read as is, saved as a revision of the reader's
    own with one trailing newline, capped, and refused while a turn of theirs is unfinished
    (docs/api.md, docs/chat.md § Memory *The memory file*)."""
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_memory_file_max_chars=40)
    )
    url = "/api/agents/chat/memory-file"
    empty = await client.get(url)
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"text": "", "max_chars": 40}
    assert (await client.get("/api/me")).json()["limits"]["memory_file_max_chars"] == 40

    saved = await client.put(url, json={"text": "## Style\n\n- Tables​ "}, headers=CSRF_HEADERS)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"text": "## Style\n\n- Tables\n"}
    assert (await client.get(url)).json()["text"] == "## Style\n\n- Tables\n"
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "memory.md").read_text() == "## Style\n\n- Tables\n"
    revisions = git.log(root, Path("memory.md"))
    assert [revision.message for revision in revisions] == ["memory memory.md"]
    assert revisions[0].author == git.USER_AUTHOR
    # Saved again as it is: no second revision.
    await client.put(url, json={"text": "## Style\n\n- Tables"}, headers=CSRF_HEADERS)
    assert len(git.log(root, Path("memory.md"))) == 1
    cleared = await client.put(url, json={"text": ""}, headers=CSRF_HEADERS)
    assert cleared.status_code == 200
    assert cleared.json() == {"text": ""}
    assert (await client.get(url)).json()["text"] == ""
    assert len(git.log(root, Path("memory.md"))) == 2

    over = await client.put(url, json={"text": "x" * 41}, headers=CSRF_HEADERS)
    assert over.status_code == 400, over.text
    # The context the Agent page shows carries the file as a new conversation would.
    await client.put(url, json={"text": "Short."}, headers=CSRF_HEADERS)
    context = (await client.get("/api/agents/chat/context")).json()
    kinds = [part["kind"] for part in context["parts"]]
    assert kinds.index("memory_file") < kinds.index("memory")
    assert context["parts"][kinds.index("memory_file")]["text"].endswith("Short.")

    stub_messages([DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello")
    busy = await client.put(url, json={"text": "Later."}, headers=CSRF_HEADERS)
    assert busy.status_code == 409, busy.text
    assert (await client.get(url)).json()["text"] == "Short.\n"
    await _run_queued_turn(client)
    assert (await client.put(url, json={"text": "Later."}, headers=CSRF_HEADERS)).status_code == 200


@respx.mock
async def test_a_conversation_keeps_the_memory_file_it_started_with(chat_client) -> None:
    """The file is snapshotted at the first turn and sent before the memory block; a save
    made meanwhile reaches the conversation only after a compaction takes the snapshot
    again — the cache stays whole in between (docs/chat.md § Memory, § Prompt caching)."""
    client = chat_client
    url = "/api/agents/chat/memory-file"
    await client.put(url, json={"text": "First notes."}, headers=CSRF_HEADERS)
    main, _ = stub_messages([DONE, DONE, SUMMARY, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="one")
    await _run_queued_turn(client)

    def memory_file_part(request: dict) -> str | None:
        return next(
            (
                part["text"]
                for part in request["system"]
                if part["text"].startswith(chat_agent.MEMORY_FILE_INTRO)
            ),
            None,
        )

    assert memory_file_part(main[0]) == chat_agent.MEMORY_FILE_INTRO + "First notes."
    system = main[0]["system"]
    assert system[-1]["text"].startswith("# Memory\n")
    assert system[-2]["text"] == chat_agent.MEMORY_FILE_INTRO + "First notes."
    assert [block.get("cache_control") for block in system] == [None] * (len(system) - 1) + [
        {"type": "ephemeral", "ttl": "1h"}
    ]
    assert (await conversation_row(client, conversation_id)).memory_file == "First notes.\n"

    await client.put(url, json={"text": "Second notes."}, headers=CSRF_HEADERS)
    await send(client, conversation_id, text="two")
    await _run_queued_turn(client)
    assert memory_file_part(main[1]) == chat_agent.MEMORY_FILE_INTRO + "First notes."

    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    assert memory_file_part(main[2]) == chat_agent.MEMORY_FILE_INTRO + "First notes."
    assert (await conversation_row(client, conversation_id)).memory_file == "Second notes.\n"
    await send(client, conversation_id, text="three")
    await _run_queued_turn(client)
    assert memory_file_part(main[3]) == chat_agent.MEMORY_FILE_INTRO + "Second notes."


def test_the_memory_file_block_is_cut_at_its_cap() -> None:
    assert chat_agent.render_memory_file(None, 100) is None
    assert chat_agent.render_memory_file("  \n", 100) is None
    assert (
        chat_agent.render_memory_file("  Notes.\n", 100) == chat_agent.MEMORY_FILE_INTRO + "Notes."
    )
    cut = chat_agent.render_memory_file("abcdefghij", 4)
    assert cut == chat_agent.MEMORY_FILE_INTRO + "abcd" + chat_agent.MEMORY_FILE_CUT


def test_the_compaction_message_is_the_instruction_and_the_note() -> None:
    content = chat_compact.compact_message_content(visible="/compact x", focus="x", note="note")
    assert [block.get("context") for block in content] == [
        None,
        chat_compact.COMPACT_CONTEXT,
        chat_agent.TURN_CONTEXT,
    ]
    assert content[0]["text"] == "/compact x"
    assert content[1]["text"] == chat_agent.compact_instruction("x")
    assert chat_agent.compact_instruction("x").endswith(chat_agent.COMPACT_FOCUS.format(focus="x"))
    assert chat_agent.compact_instruction("  ") == chat_agent.COMPACT_INSTRUCTION
    bare = chat_compact.compact_message_content(visible=None, focus=None, note="note")
    assert [block.get("context") for block in bare] == [
        chat_compact.COMPACT_CONTEXT,
        chat_agent.TURN_CONTEXT,
    ]
    assert chat_compact.is_summary([{"type": "text", "text": "s", "context": "summary"}])
    assert not chat_compact.is_summary([{"type": "text", "text": "s"}])
    assert not chat_compact.is_summary("text")
