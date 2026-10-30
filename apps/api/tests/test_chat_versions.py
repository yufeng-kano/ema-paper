"""Versions of a sent message (docs/chat.md § History is append-only, *An edited message
starts a version*; docs/api.md): the edit route, the version switch, what each branch
replays and shows, and the summary row each branch keeps — against a stubbed Messages
stream. No test reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import update

from sightread.agents import chat as chat_agent
from sightread.db.models import IDLE_COMPACT_TURN, MESSAGE_TURN, Conversation, Message
from sightread.jobs import chat_compact, chat_versions
from sightread.jobs.queue import history_bytes
from sightread.routes import chat as chat_routes
from sightread.routes.chat import QUOTE_PREFIX, SECTION_CONTEXT, SELECTION_PREFIX, TURN_CONTEXT
from tests.conftest import CSRF_HEADERS
from tests.test_chat import _run_queued_turn
from tests.test_chat_attachments import attach
from tests.test_chat_compact import DONE, SUMMARY, conversation_row, detail, set_clock, texts
from tests.test_chat_extensions import (
    chatting_client,
    new_conversation,
    replayed_events,
    send,
    stub_messages,
)


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


async def edit(
    client: AsyncClient, conversation_id: int, message_id: int, **body
) -> httpx.Response:
    return await client.post(
        f"/api/conversations/{conversation_id}/messages/{message_id}/edit",
        json=body,
        headers=CSRF_HEADERS,
    )


async def switch(client: AsyncClient, conversation_id: int, version: str) -> httpx.Response:
    return await client.put(
        f"/api/conversations/{conversation_id}", json={"version": version}, headers=CSRF_HEADERS
    )


def user_message_of(shown: dict, turn_id: str) -> int:
    return next(
        message["id"]
        for message in shown["messages"]
        if message["turn_id"] == turn_id and message["role"] == "user"
    )


def first_texts(replay: list[dict]) -> list[str]:
    """Each replayed message's first text: the user's own line, or the answer."""
    return [texts(message["content"])[0] for message in replay]


# --- the edit and the switch -----------------------------------------------------------------


@respx.mock
async def test_an_edit_is_a_new_version_and_the_old_one_can_be_shown_again(chat_client) -> None:
    """An edit stores a new message with a turn of its own, the edited turn's sibling, and
    the conversation shows that branch: its replay, its thread and its events hold the
    prefix and the new message, nothing of the old version. The switch brings the old
    branch back — with the turn that continued it last — and nothing is deleted."""
    client = chat_client
    main, _ = stub_messages([DONE, DONE, DONE, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    await send(client, conversation_id, text="second")
    await _run_queued_turn(client)
    before = await detail(client, conversation_id)
    first, second = (turn["id"] for turn in before["turns"])
    assert [turn["versions"] for turn in before["turns"]] == [None, None]

    edited = await edit(
        client, conversation_id, user_message_of(before, second), text="second, revised"
    )
    assert edited.status_code == 202, edited.text
    third = edited.json()["turn_id"]
    assert edited.json()["message_id"] != user_message_of(before, second)

    shown = await detail(client, conversation_id)
    assert [turn["id"] for turn in shown["turns"]] == [first, third]
    assert [turn["versions"] for turn in shown["turns"]] == [None, [second, third]]
    assert [(message["role"], message["turn_id"]) for message in shown["messages"]] == [
        ("user", first),
        ("assistant", first),
        ("user", third),
    ]
    assert shown["messages"][-1]["content"] == [{"type": "text", "text": "second, revised"}]
    # An edit continues the thread: the list moves it up, as a send does.
    assert shown["conversation"]["updated_at"] > before["conversation"]["updated_at"]

    await _run_queued_turn(client)
    assert first_texts(main[2]["messages"]) == ["hello there", "Done.", "second, revised"]
    events = await replayed_events(client, conversation_id)
    assert {data["turn_id"] for _, data in events} == {first, third}
    # The old rows stay, unsent: the whole conversation is larger than its shown branch.
    async with client.app.state.sessionmaker() as db:
        whole = await history_bytes(db, conversation_id)
        branch = await history_bytes(db, conversation_id, path=[uuid.UUID(first), uuid.UUID(third)])
    assert 0 < branch < whole

    # Back to the old version: its branch and its answer, the thread not moved up.
    latest = await detail(client, conversation_id)
    switched = await switch(client, conversation_id, second)
    assert switched.status_code == 200, switched.text
    assert switched.json()["updated_at"] == latest["conversation"]["updated_at"]
    old = await detail(client, conversation_id)
    assert [turn["id"] for turn in old["turns"]] == [first, second]
    assert old["turns"][1]["versions"] == [second, third]
    assert [message["turn_id"] for message in old["messages"]] == [first, first, second, second]
    assert texts(old["messages"][2]["content"]) == ["second"]
    events = await replayed_events(client, conversation_id)
    assert {data["turn_id"] for _, data in events} == {first, second}

    # The old branch continues; a switch to the edit shows it alone, and the switch back
    # shows the old version with what continued it.
    sent = await send(client, conversation_id, text="third")
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)
    assert first_texts(main[3]["messages"]) == ["hello there", "Done.", "second", "Done.", "third"]
    fourth = (await detail(client, conversation_id))["turns"][2]["id"]
    assert (await switch(client, conversation_id, third)).status_code == 200
    assert [turn["id"] for turn in (await detail(client, conversation_id))["turns"]] == [
        first,
        third,
    ]
    assert (await switch(client, conversation_id, second)).status_code == 200
    assert [turn["id"] for turn in (await detail(client, conversation_id))["turns"]] == [
        first,
        second,
        fourth,
    ]


@respx.mock
async def test_an_edit_takes_the_sends_refusals_and_only_a_messages_own_row(chat_client) -> None:
    """An edit is refused while a turn is unfinished, past the message cap and for a message
    left empty, as a send is; an answer, a compaction's rows and a row that is not the
    caller's are no message to edit (**404**); and a switch names a `message` turn of the
    conversation or is refused (**400**)."""
    client = chat_client
    settings = client.app.state.settings
    stub_messages([DONE, SUMMARY])
    conversation_id = await new_conversation(client)
    sent = await send(client, conversation_id, text="hello there")
    message_id = sent.json()["message_id"]
    busy = await edit(client, conversation_id, message_id, text="hi")
    assert busy.status_code == 409, busy.text
    await _run_queued_turn(client)

    shown = await detail(client, conversation_id)
    answer = next(message["id"] for message in shown["messages"] if message["role"] == "assistant")
    assert (await edit(client, conversation_id, answer, text="hi")).status_code == 404
    assert (await edit(client, conversation_id, 999_999, text="hi")).status_code == 404
    too_long = "x" * (settings.chat_message_max_chars + 1)
    assert (await edit(client, conversation_id, message_id, text=too_long)).status_code == 400
    assert (await edit(client, conversation_id, message_id, text="   ")).status_code == 400

    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    shown = await detail(client, conversation_id)
    compaction = shown["turns"][1]["id"]
    for message in shown["messages"]:
        if message["turn_id"] == compaction and message["role"] == "user":
            refused = await edit(client, conversation_id, message["id"], text="hi")
            assert refused.status_code == 404, refused.text
    assert (await switch(client, conversation_id, compaction)).status_code == 400
    assert (await switch(client, conversation_id, str(uuid.uuid4()))).status_code == 400
    empty = await client.put(f"/api/conversations/{conversation_id}", json={}, headers=CSRF_HEADERS)
    assert empty.status_code == 400
    assert "version" in empty.json()["error"]["message"]
    # Nothing of the above changed what the conversation shows.
    assert [turn["id"] for turn in (await detail(client, conversation_id))["turns"]] == [
        shown["turns"][0]["id"],
        compaction,
    ]


@respx.mock
async def test_an_edit_carries_the_messages_context_and_renders_its_command_again(
    chat_client,
) -> None:
    """The new message carries the attachments, the selection, the sections and the quote
    as stored, the new text in place of the old, a command's skill rendered again from the
    skill as it is now — refused when the skill is gone — and this send's turn block."""
    client = chat_client
    main, _ = stub_messages([DONE, DONE, DONE, DONE])
    conversation_id = await new_conversation(client)
    attached = await attach(client, "notes.txt", b"alpha beta", "text/plain")
    assert attached.status_code == 201, attached.text
    sent = await send(
        client,
        conversation_id,
        text="look",
        attachments=[attached.json()["id"]],
        selection="the passage",
        sections=[{"heading": "Methods", "text": "m1"}],
        quote="an answer",
    )
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)

    edited = await edit(
        client, conversation_id, sent.json()["message_id"], text="look again", locale="zh-TW"
    )
    assert edited.status_code == 202, edited.text
    shown = (await detail(client, conversation_id))["messages"][-1]["content"]
    assert shown == [
        {
            "type": "text",
            "context": "attachment",
            "attachment": {"name": "notes.txt", "kind": "text", "chars": 10},
        },
        {"type": "text", "text": "look again"},
        {"type": "text", "text": "the passage", "context": "selection"},
        {"type": "text", "context": SECTION_CONTEXT, "section": "Methods"},
        {"type": "text", "text": "an answer", "context": "quote"},
    ]
    async with client.app.state.sessionmaker() as db:
        stored = (await db.get(Message, edited.json()["message_id"])).content
    assert stored[-1]["context"] == TURN_CONTEXT
    assert "- reader locale: zh-TW" in stored[-1]["text"]
    await _run_queued_turn(client)
    request = main[1]["messages"][-1]["content"]
    assert request[0]["text"].startswith("Attached file: notes.txt")
    assert request[1]["text"] == "look again"
    assert request[2]["text"] == SELECTION_PREFIX + "the passage"
    assert '"Methods"' in request[3]["text"]
    assert request[4]["text"] == QUOTE_PREFIX + "an answer"
    assert request[5]["text"] == stored[-1]["text"]
    assert all("context" not in block for block in request)

    # A command: the body rendered again with the new arguments, kept to the selection.
    created = await client.post(
        "/api/skills",
        json={"name": "outline", "body": "Outline $ARGUMENTS for the reader."},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    sent = await send(
        client, conversation_id, text="the abstract", command="outline", selection="passage two"
    )
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)
    edited = await edit(
        client, conversation_id, sent.json()["message_id"], text="the abstract, briefly"
    )
    assert edited.status_code == 202, edited.text
    shown = (await detail(client, conversation_id))["messages"][-1]["content"]
    assert shown[0] == {"type": "text", "text": "/outline the abstract, briefly"}
    assert shown[-1] == {"type": "text", "context": "skill", "skill": "outline"}
    await _run_queued_turn(client)
    request = main[3]["messages"][-1]["content"]
    assert request[0]["text"] == "/outline the abstract, briefly"
    assert request[1]["text"] == SELECTION_PREFIX + "passage two"
    assert request[2]["text"] == (
        "The user ran the skill /outline. Follow its instructions:\n\n"
        "Outline the abstract, briefly for the reader."
        "\n\nRun it on the selection above only: the rest of the document is outside its range."
    )

    # The skill gone, the command cannot be sent again.
    deleted = await client.delete("/api/skills/outline", headers=CSRF_HEADERS)
    assert deleted.status_code == 204, deleted.text
    gone = await edit(client, conversation_id, sent.json()["message_id"], text="once more")
    assert gone.status_code == 400, gone.text
    assert "/outline" in gone.json()["error"]["message"]


# --- branches and compaction -------------------------------------------------------------------


@respx.mock
async def test_an_edit_joins_its_branch_with_that_branchs_summary(chat_client) -> None:
    """The summary row is the branch's: an edit after a compaction keeps it and replays the
    summary, an edit of the first message joins the root with none, and the switch back
    finds it again; `compactable` reads the shown branch (docs/chat.md § Compaction)."""
    client = chat_client
    main, _ = stub_messages([DONE, SUMMARY, DONE, DONE, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    boundary = (await conversation_row(client, conversation_id)).compacted_message_id
    assert boundary is not None
    await send(client, conversation_id, text="after")
    await _run_queued_turn(client)
    shown = await detail(client, conversation_id)
    first, compaction, third = (turn["id"] for turn in shown["turns"])

    # After the compaction: the branch joined holds the summary, which stands.
    edited = await edit(
        client, conversation_id, user_message_of(shown, third), text="after, revised"
    )
    assert edited.status_code == 202, edited.text
    revised = edited.json()["turn_id"]
    assert (await conversation_row(client, conversation_id)).compacted_message_id == boundary
    # The one answer since the summary is on the other branch: nothing to compact yet.
    assert not (await detail(client, conversation_id))["conversation"]["compactable"]
    await _run_queued_turn(client)
    replay = main[3]["messages"]
    assert [message["role"] for message in replay] == ["user", "user"]
    assert texts(replay[0]["content"]) == [chat_agent.SUMMARY_PREFIX + SUMMARY[0][0]["text"]]
    assert first_texts(replay[1:]) == ["after, revised"]
    assert (await detail(client, conversation_id))["conversation"]["compactable"]

    # The first message: the root, where no summary stands for what the branch does not hold.
    edited = await edit(client, conversation_id, user_message_of(shown, first), text="hi there")
    assert edited.status_code == 202, edited.text
    fresh = edited.json()["turn_id"]
    row = await conversation_row(client, conversation_id)
    assert row.compacted_message_id is None
    assert row.context_tokens is None
    assert row.cache_warm_until is None
    shown = await detail(client, conversation_id)
    assert [turn["id"] for turn in shown["turns"]] == [fresh]
    assert shown["turns"][0]["versions"] == [first, fresh]
    assert [message["turn_id"] for message in shown["messages"]] == [fresh]
    # No answer on this branch yet: nothing to compact, whatever the other branch holds.
    assert not shown["conversation"]["compactable"]
    await _run_queued_turn(client)
    assert first_texts(main[4]["messages"]) == ["hi there"]
    assert (await detail(client, conversation_id))["conversation"]["compactable"]

    # Back to the first version: its branch down to the turn that continued it last — the
    # compaction, then the revised message — with its summary row again.
    switched = await switch(client, conversation_id, first)
    assert switched.status_code == 200, switched.text
    assert switched.json()["compacted_message_id"] == boundary
    assert [turn["id"] for turn in (await detail(client, conversation_id))["turns"]] == [
        first,
        compaction,
        revised,
    ]


@respx.mock
async def test_an_edit_writes_its_branchs_summary_row_over_a_switch_that_landed_first(
    chat_client, monkeypatch
) -> None:
    """The branch fields are written outright, not through the row read before the lock:
    a switch from another tab that lands between that read and the lock puts its branch's
    summary row in the column, and an edit onto a branch without one — the value the stale
    row still holds — would otherwise leave it there, and the new branch would replay from
    another branch's summary (docs/chat.md § History is append-only, *An edited message
    starts a version*)."""
    client = chat_client
    main, _ = stub_messages([DONE, SUMMARY, DONE, DONE, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    await send(client, conversation_id, text="", command="compact")
    await _run_queued_turn(client)
    boundary = (await conversation_row(client, conversation_id)).compacted_message_id
    await send(client, conversation_id, text="after")
    await _run_queued_turn(client)
    shown = await detail(client, conversation_id)
    first, _, third = (turn["id"] for turn in shown["turns"])
    # A version of the first message: the root branch, which has no summary row.
    edited = await edit(client, conversation_id, user_message_of(shown, first), text="hi there")
    assert edited.status_code == 202, edited.text
    fresh = edited.json()["turn_id"]
    await _run_queued_turn(client)
    assert (await conversation_row(client, conversation_id)).compacted_message_id is None
    shown = await detail(client, conversation_id)

    # The other tab goes back to the first version — its summary row with it — after this
    # edit has read the conversation's row and before it takes the lock.
    admitted = chat_routes._admitted
    seen: list = []

    async def admitted_after_the_switch(db, settings, user, row):
        if not seen:
            seen.append(row.compacted_message_id)
            async with client.app.state.sessionmaker() as other:
                await other.execute(
                    update(Conversation)
                    .where(Conversation.id == conversation_id)
                    .values(head_id=uuid.UUID(third), compacted_message_id=boundary)
                )
                await other.commit()
        return await admitted(db, settings, user, row)

    monkeypatch.setattr(chat_routes, "_admitted", admitted_after_the_switch)
    edited = await edit(client, conversation_id, user_message_of(shown, fresh), text="hi again")
    assert edited.status_code == 202, edited.text
    assert seen == [None]
    row = await conversation_row(client, conversation_id)
    assert str(row.head_id) == edited.json()["turn_id"]
    assert row.compacted_message_id is None
    await _run_queued_turn(client)
    assert first_texts(main[4]["messages"]) == ["hi again"]


@respx.mock
async def test_the_stream_shows_the_turn_that_starts_as_it_opens(chat_client, monkeypatch) -> None:
    """A turn sent and answered while the stream reads its snapshot is replayed whole: the
    newest seq is read before the head, so the turn's events are past it. In the other
    order the head read misses the turn and the seq read counts its events, which are then
    on no branch the stream knows and not past the seq either, and are left out
    (docs/chat.md § Conversations, turns, events)."""
    client = chat_client
    stub_messages([DONE, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)

    turns_read = chat_versions.conversation_turns
    landed: list[str] = []

    async def a_send_lands_first(db, conversation_id_read):
        if not landed:
            landed.append("")  # the send and its turn read the turns too: once only
            response = await send(client, conversation_id, text="and again")
            assert response.status_code == 202, response.text
            landed[0] = response.json()["turn_id"]
            await _run_queued_turn(client)
        return await turns_read(db, conversation_id_read)

    monkeypatch.setattr(chat_versions, "conversation_turns", a_send_lands_first)
    events = await replayed_events(client, conversation_id)
    done = [data["turn_id"] for name, data in events if name == "turn_done"]
    assert len(done) == 2 and landed[0] == done[1]


@respx.mock
async def test_the_history_cap_measures_the_branch_a_message_joins(make_client, tmp_path) -> None:
    """A branch edited away counts for nothing: a message on the shown branch is accepted
    while the conversation's rows together are past the cap (docs/chat.md § History is
    append-only)."""
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_history_max_bytes=2500)
    )
    long_answer = ([{"type": "text", "text": "y" * 3000}], "end_turn")
    stub_messages([DONE, long_answer, DONE, DONE, DONE])
    conversation_id = await new_conversation(client)
    first = await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    second = await send(client, conversation_id, text="second")
    assert second.status_code == 202, second.text
    await _run_queued_turn(client)
    async with client.app.state.sessionmaker() as db:
        assert await history_bytes(db, conversation_id) > 2500
    # On the branch as shown, the next message is past the cap.
    refused = await send(client, conversation_id, text="go on")
    assert refused.status_code == 409, refused.text
    # Edited away, the long answer counts for nothing: the edit and the next message pass.
    edited = await edit(client, conversation_id, second.json()["message_id"], text="second, short")
    assert edited.status_code == 202, edited.text
    await _run_queued_turn(client)
    accepted = await send(client, conversation_id, text="go on")
    assert accepted.status_code == 202, accepted.text
    await _run_queued_turn(client)
    # Back on the long branch the cap holds again; the first message's edit measures the
    # root alone and passes.
    versions = (await detail(client, conversation_id))["turns"][1]["versions"]
    assert (await switch(client, conversation_id, versions[0])).status_code == 200
    assert (await send(client, conversation_id, text="go on")).status_code == 409
    rooted = await edit(client, conversation_id, first.json()["message_id"], text="hi")
    assert rooted.status_code == 202, rooted.text


@respx.mock
async def test_the_idle_compaction_continues_the_shown_branch(chat_client) -> None:
    """The worker's compaction is a turn of the shown branch: its parent is the head, and
    the head follows it, with `updated_at` kept (docs/chat.md § Compaction, *Idle
    compaction*)."""
    client = chat_client
    settings = client.app.state.settings
    stub_messages([DONE, DONE])
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hello there")
    await _run_queued_turn(client)
    shown = await detail(client, conversation_id)
    first = shown["turns"][0]["id"]
    edited = await edit(client, conversation_id, user_message_of(shown, first), text="hi there")
    assert edited.status_code == 202, edited.text
    second = edited.json()["turn_id"]
    await _run_queued_turn(client)
    before = await conversation_row(client, conversation_id)
    await set_clock(
        client,
        conversation_id,
        warm_for=settings.chat_idle_compact_lead_seconds - 10,
        tokens=settings.chat_idle_compact_min_tokens,
    )
    assert await chat_compact.idle_compact_once(client.app.state.sessionmaker, settings) == 1
    after = await conversation_row(client, conversation_id)
    shown = await detail(client, conversation_id)
    assert [turn["kind"] for turn in shown["turns"]] == [MESSAGE_TURN, IDLE_COMPACT_TURN]
    assert shown["turns"][0]["id"] == second
    assert str(after.head_id) == shown["turns"][1]["id"]
    assert after.updated_at == before.updated_at
    async with client.app.state.sessionmaker() as db:
        turns = await chat_versions.conversation_turns(db, conversation_id)
    compaction = next(turn for turn in turns if turn.kind == IDLE_COMPACT_TURN)
    assert str(compaction.parent_id) == second


# --- the tree -----------------------------------------------------------------------------------


def test_the_branch_helpers_walk_the_tree() -> None:
    """The ancestry of a turn, the newest leaf under one, and a message's versions, read
    from the turns of one conversation; a conversation without a head is one branch."""

    def turn(turn_id: uuid.UUID, parent_id: uuid.UUID | None, kind: str = MESSAGE_TURN):
        return SimpleNamespace(id=turn_id, parent_id=parent_id, kind=kind, status="succeeded")

    a, b, c, d, e = (uuid.uuid4() for _ in range(5))
    turns = [turn(a, None), turn(b, a), turn(c, a), turn(d, c, IDLE_COMPACT_TURN), turn(e, d)]
    assert [step.id for step in chat_versions.branch(turns, e)] == [a, c, d, e]
    assert [step.id for step in chat_versions.branch(turns, b)] == [a, b]
    assert [step.id for step in chat_versions.branch(turns, None)] == [a, b, c, d, e]
    assert [step.id for step in chat_versions.branch(turns, uuid.uuid4())] == [a, b, c, d, e]
    assert chat_versions.branch([], None) == []
    assert chat_versions.newest_leaf(turns, a) == e
    assert chat_versions.newest_leaf(turns, b) == b
    assert chat_versions.newest_leaf(turns, c) == e
    assert chat_versions.versions_of(turns, turns[1]) == [b, c]
    assert chat_versions.versions_of(turns, turns[2]) == [b, c]
    assert chat_versions.versions_of(turns, turns[0]) is None
    assert chat_versions.versions_of(turns, turns[3]) is None
    assert chat_versions.versions_of(turns, turns[4]) is None
