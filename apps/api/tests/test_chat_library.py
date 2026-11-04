"""Library conversations (docs/chat.md § Library conversations): the Files page's threads
about the whole library, the Files page a message carries, the library prompt and tools a
turn runs on, and the library tools' changes with the undo records the Files page replays.
Turns run against a stubbed Messages stream; no test reaches the network (docs/testing.md
§ Cost safety)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.agents import chat as chat_agent
from sightread.db.models import ChatTurn, Conversation, Document, Folder, Message, User
from sightread.errors import ApiError
from sightread.jobs import chat_compact, chat_library, chat_memory
from sightread.jobs.chat_tools import ToolOutcome
from sightread.workspace import documents as workspace_documents
from sightread.workspace import filing, git
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, DATABASE_URL
from tests.test_chat import _run_queued_turn
from tests.test_chat_agents_file import _agents_part, _write
from tests.test_chat_compact import DONE, SUMMARY, detail, set_clock, turns_of
from tests.test_chat_extensions import (
    chatting_client,
    new_conversation,
    one_call,
    send,
    stub_messages,
)
from tests.test_chat_versions import edit
from tests.test_chat_web import tool_results

LIBRARY_PROMPT_START = "You are the library agent inside Ema Paper"


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


async def new_library_conversation(client: AsyncClient) -> int:
    created = await client.post("/api/library/conversations", headers=CSRF_HEADERS)
    assert created.status_code == 201, created.text
    assert created.json()["document_id"] is None
    return created.json()["id"]


async def _papers(client: AsyncClient) -> int:
    library = (await client.get("/api/library")).json()
    return next(folder["id"] for folder in library["folders"] if folder["name"] == "Papers")


async def _rows(client: AsyncClient) -> tuple[dict[int, Folder], dict[int, Document]]:
    async with client.app.state.sessionmaker() as db:
        folders = (await db.execute(select(Folder))).scalars().all()
        documents = (await db.execute(select(Document))).scalars().all()
        for row in (*folders, *documents):
            db.expunge(row)
    return {row.id: row for row in folders}, {row.id: row for row in documents}


async def _tool(client: AsyncClient, name: str, tool_input: dict) -> ToolOutcome:
    """One call of a library tool that changes the library, as a turn makes it."""
    return await chat_library.run_tool(
        client.app.state.sessionmaker,
        client.app.state.settings,
        user_id=client.user_id,
        author="agent:1",
        name=name,
        tool_input=tool_input,
    )


def _authors(client: AsyncClient) -> set[str]:
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return set(git._git(root, "log", "--format=%an").stdout.split())


# --- the routes ------------------------------------------------------------------------------


@respx.mock
async def test_the_files_page_lists_its_own_threads_and_no_one_elses(chat_client) -> None:
    """The list holds the caller's library conversations that hold a message, the one
    continued last first: never a document's, never another reader's, never an empty one
    (docs/api.md § Chat)."""
    client = chat_client
    stub_messages([DONE, DONE, DONE])
    assert (await client.get("/api/library/conversations")).json() == {"conversations": []}
    first = await new_library_conversation(client)
    second = await new_library_conversation(client)
    empty = await new_library_conversation(client)
    about_document = await new_conversation(client)
    for conversation_id in (first, second, about_document):
        await send(client, conversation_id, text="hello")
        await _run_queued_turn(client)
    async with client.app.state.sessionmaker() as db:
        other = User(google_sub="other", email="other@example.test")
        db.add(other)
        await db.flush()
        theirs = Conversation(user_id=other.id, document_id=None, title="", session="o")
        db.add(theirs)
        await db.flush()
        db.add(Message(conversation_id=theirs.id, role="user", content=[]))
        await db.commit()
        theirs_id = theirs.id

    listed = (await client.get("/api/library/conversations")).json()["conversations"]
    assert [row["id"] for row in listed] == [second, first]
    assert all(row["document_id"] is None for row in listed)
    assert empty not in [row["id"] for row in listed]
    # A document's list never holds one either way.
    on_document = await client.get(f"/api/library/documents/{client.document_id}/conversations")
    assert [row["id"] for row in on_document.json()["conversations"]] == [about_document]
    assert (await client.get(f"/api/conversations/{theirs_id}")).status_code == 404
    assert (await send(client, theirs_id, text="hello")).status_code == 404


@respx.mock
async def test_a_message_carries_the_files_page_and_no_passage(chat_client) -> None:
    """The open folder and the selected rows go with the message as their Files paths, for
    the model as text and for the thread as the paths alone; what is gone or in the Trash
    is left out. A library conversation takes no passage of a document, a document's
    conversation no Files page, and a skill that works on a document is refused
    (docs/chat.md § Library conversations)."""
    client = chat_client
    main, _ = stub_messages([DONE])
    papers = await _papers(client)
    conversation_id = await new_library_conversation(client)
    for refused in (
        {"text": "x", "selection": "Greedy is 3.45."},
        {"text": "x", "sections": [{"heading": "1 Intro", "text": "Body."}]},
        {"command": "translate"},
        {"command": "check-figures"},
        {"text": "x", "library": {"folder_id": None, "items": [{"kind": "folder", "id": 1}] * 201}},
    ):
        response = await send(client, conversation_id, **refused)
        assert response.status_code == 400, refused
    on_document = await send(
        client, await new_conversation(client), text="x", library={"folder_id": None}
    )
    assert on_document.status_code == 400
    assert "Files page" in on_document.json()["error"]["message"]

    sent = await send(
        client,
        conversation_id,
        text="Tidy this up",
        library={
            "folder_id": papers,
            "items": [
                {"kind": "document", "id": client.document_id},
                {"kind": "document", "id": client.document_id},
                {"kind": "folder", "id": 999_999},
                {"kind": "document", "id": 999_999},
            ],
        },
    )
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)

    content = main[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Tidy this up"}
    assert content[1] == {
        "type": "text",
        "text": (
            "The Files page as the user sent this message:\n"
            f"- open folder: Papers (folder {papers})\n"
            "- selected (1):\n"
            f"  - Papers/paper.pdf (document {client.document_id})"
        ),
    }
    # The turn's note has no document revision to name.
    note = content[-1]["text"]
    assert note.startswith("Context for this message:") and "revision" not in note
    thread = await detail(client, conversation_id)
    assert thread["conversation"]["document_id"] is None
    assert thread["messages"][0]["content"][:2] == [
        {"type": "text", "text": "Tidy this up"},
        {
            "type": "text",
            "context": "library",
            "library": {"folder": "Papers", "items": ["Papers/paper.pdf"]},
        },
    ]


@respx.mock
async def test_an_edit_keeps_the_files_page_it_was_sent_with_unless_it_sends_its_own(
    chat_client,
) -> None:
    client = chat_client
    main, _ = stub_messages([DONE, DONE, DONE])
    papers = await _papers(client)
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, text="one", library={"folder_id": papers})
    await _run_queued_turn(client)
    message_id = (await detail(client, conversation_id))["messages"][0]["id"]

    kept = await edit(client, conversation_id, message_id, text="two")
    assert kept.status_code == 202, kept.text
    await _run_queued_turn(client)
    assert main[1]["messages"][0]["content"][1]["text"] == (
        f"The Files page as the user sent this message:\n- open folder: Papers (folder {papers})"
    )
    message_id = (await detail(client, conversation_id))["messages"][0]["id"]
    replaced = await edit(
        client, conversation_id, message_id, text="three", library={"folder_id": None}
    )
    assert replaced.status_code == 202, replaced.text
    await _run_queued_turn(client)
    assert main[2]["messages"][0]["content"][1]["text"] == (
        "The Files page as the user sent this message:\n- open folder: the top of the library"
    )


# --- the turn --------------------------------------------------------------------------------


@respx.mock
async def test_a_library_turn_runs_on_the_library_prompt_and_tools(chat_client) -> None:
    """The library prompt, the library tools in place of the page tools, the root AGENTS.md
    alone, and no skill or subagent that works on a document: a call naming one, or a page
    tool, answers an error without running (docs/chat.md § Library conversations)."""
    client = chat_client
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    _write(root, "files/AGENTS.md", "- Notes in English\n")
    _write(root, "files/Papers/AGENTS.md", "- Cite with the DOI\n")
    main, _ = stub_messages(
        [
            one_call("v", "view_page", {"page": 1}),
            one_call("s", "skill", {"name": "translate", "arguments": ""}),
            DONE,
            DONE,
        ]
    )
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, text="What is here?")
    await _run_queued_turn(client)

    request = main[0]
    assert request["system"][0]["text"].startswith(LIBRARY_PROMPT_START)
    names = [tool["name"] for tool in request["tools"]]
    assert set(chat_agent.LIBRARY_TOOL_NAMES) <= set(names)
    assert not set(chat_agent.DOCUMENT_TOOL_NAMES) & set(names)
    assert names.index("list_files") > names.index("insert_image")
    # The root's file alone, under rules of use that speak of no document.
    assert (
        chat_agent.LIBRARY_AGENTS_FILE_INTRO + "## /workspace/files/AGENTS.md\n\n- Notes in English"
    ) in [part["text"] for part in request["system"]]
    assert _agents_part(request) is None
    catalogue = "\n".join(part["text"] for part in request["system"])
    assert "translate" not in catalogue and "fix-math" not in catalogue
    assert "check-figures" not in catalogue and "translator" not in catalogue
    results = {result["id"]: result for result in await tool_results(client, conversation_id)}
    assert results["v"] == {
        **results["v"],
        "is_error": True,
        "output": "'view_page' is not one of this turn's tools",
    }
    assert results["s"]["is_error"] is True

    # A document's conversation declares none of the library tools.
    await send(client, await new_conversation(client), text="hello")
    await _run_queued_turn(client)
    declared = {tool["name"] for tool in main[3]["tools"]}
    assert set(chat_agent.DOCUMENT_TOOL_NAMES) <= declared
    assert not set(chat_agent.LIBRARY_TOOL_NAMES) & declared


@respx.mock
async def test_the_library_tools_change_the_library_and_say_how_to_undo_it(chat_client) -> None:
    """Each tool runs the Files page's own operation, says what it did item by item, and a
    call that changed something carries its undo record in the Files page's step shapes —
    on the event and on the stored result, never upstream; a batch does what it can, and
    every tree commit is the conversation's (docs/chat.md § Library conversations, *The
    library tools*)."""
    client = chat_client
    papers = await _papers(client)
    document = client.document_id
    main, _ = stub_messages(
        [
            one_call("l1", "list_files", {}),
            one_call("l2", "list_files", {"path": "/workspace/files/Papers"}),
            one_call("f1", "new_folder", {"path": "Reading/2017"}),
            one_call("f2", "new_folder", {"path": "Reading"}),
            one_call(
                "m1", "move", {"paths": ["Papers/paper.pdf", "Nowhere/x.pdf"], "to": "Reading/2017"}
            ),
            one_call("m2", "move", {"paths": ["Reading"], "to": "Reading/2017"}),
            one_call("r1", "rename", {"path": "Reading/2017/paper.pdf", "name": "attention.pdf"}),
            one_call(
                "c1", "copy", {"paths": ["Reading/2017/attention.pdf/doc.md"], "to": "Reading/2017"}
            ),
            one_call("t1", "trash", {"paths": ["Reading/2017/attention.pdf"]}),
            one_call("l3", "list_files", {"trash": True}),
            one_call("p1", "put_back", {"paths": ["Reading/2017/attention.pdf"]}),
            one_call("n1", "new_document", {"path": "Notes/Plan", "text": "# Plan\n\nRead it."}),
            one_call("l4", "list_files", {"path": "Nowhere"}),
            DONE,
        ]
    )
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, text="Organize my papers")
    await _run_queued_turn(client)

    folders, documents = await _rows(client)
    reading = next(row.id for row in folders.values() if row.name == "Reading")
    year = next(row.id for row in folders.values() if row.name == "2017")
    notes = next(row.id for row in folders.values() if row.name == "Notes")
    copy = next(row for row in documents.values() if row.name == "attention (2).pdf")
    plan = next(row for row in documents.values() if row.name == "Plan.md")
    results = {result["id"]: result for result in await tool_results(client, conversation_id)}

    assert results["l1"]["output"] == (
        f"The top of the library holds 1 item:\nfolder Papers (folder {papers}, 1 item)"
    )
    assert results["l2"]["output"] == (
        f"Papers (folder {papers}) holds 1 item:\n"
        f"document Papers/paper.pdf (document {document}, PDF document, ready) "
        "text: /workspace/files/Papers/paper.pdf/doc.md"
    )
    assert "library" not in results["l1"] and "library" not in results["l2"]
    assert results["f1"]["output"] == (
        f"Made the folders Reading (folder {reading}), Reading/2017 (folder {year})."
    )
    assert results["f1"]["library"] == {
        "type": "appear",
        "items": [{"kind": "folder", "id": reading}],
    }
    # A folder already there is reported, not suffixed.
    assert results["f2"] == {
        **results["f2"],
        "is_error": False,
        "output": f"The folder Reading (folder {reading}) already exists.",
    }
    assert "library" not in results["f2"]
    # A batch does what it can: one moved, one named nothing — found out before anything
    # changed.
    assert results["m1"]["is_error"] is False
    assert results["m1"]["output"] == (
        "Could not move Nowhere/x.pdf: nothing is there.\n"
        f"Moved Papers/paper.pdf (document {document}) to Reading/2017/paper.pdf."
    )
    assert results["m1"]["library"] == {
        "type": "move",
        "moves": [
            {
                "item": {"kind": "file", "id": document},
                "from": papers,
                "to": year,
                "fromName": "paper.pdf",
                "toName": "paper.pdf",
            }
        ],
    }
    # Into its own subtree: refused, nothing done, no record.
    assert results["m2"]["is_error"] is True and "library" not in results["m2"]
    assert results["m2"]["output"].startswith("Could not move Reading: ")
    assert folders[reading].parent_id is None
    assert results["r1"]["library"] == {
        "type": "rename",
        "item": {"kind": "file", "id": document},
        "from": "paper.pdf",
        "to": "attention.pdf",
    }
    # Named by its doc.md, copied beside itself under a suffixed name.
    assert results["c1"]["output"] == (
        f"Copied Reading/2017/attention.pdf (document {document}) to "
        f"Reading/2017/attention (2).pdf (document {copy.id})."
    )
    assert results["c1"]["library"] == {
        "type": "appear",
        "items": [{"kind": "file", "id": copy.id}],
    }
    assert results["t1"]["library"] == {
        "type": "trash",
        "items": [{"kind": "file", "id": document}],
    }
    assert results["l3"]["output"].startswith(
        "The Trash holds 1 item, each named by where Put Back returns it:\n"
        f"document Reading/2017/attention.pdf (document {document}, PDF document, trashed "
    )
    assert results["p1"]["output"] == (
        f"Put back attention.pdf (document {document}): it is at Reading/2017/attention.pdf."
    )
    assert results["p1"]["library"] == {
        "type": "appear",
        "items": [{"kind": "file", "id": document}],
    }
    assert results["n1"]["output"] == (
        f"Made the document Notes/Plan.md (document {plan.id}), with the new folder Notes; "
        "its text is at /workspace/files/Notes/Plan.md/doc.md."
    )
    assert results["n1"]["library"] == {
        "type": "appear",
        "items": [{"kind": "file", "id": plan.id}],
    }
    assert results["l4"] == {
        **results["l4"],
        "is_error": True,
        "output": "Nowhere names no folder of the library.",
    }

    # The rows and the tree as the Files page's own operations leave them.
    original = documents[document]
    assert (original.folder_id, original.name, original.trashed_at) == (
        year,
        "attention.pdf",
        None,
    )
    assert folders[year].parent_id == reading and plan.folder_id == notes
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    assert (root / "files/Reading/2017/attention.pdf/doc.md").is_file()
    assert (root / "files/Reading/2017/attention (2).pdf/doc.md").is_file()
    assert (root / "files/Notes/Plan.md/doc.md").read_text() == "# Plan\n\nRead it."
    assert not (root / "files/Papers/paper.pdf").exists()
    assert _authors(client) == {f"agent:{conversation_id}"}

    # The record stays on the stored result for a reloaded thread, never sent upstream.
    stored = [
        block
        for message in (await detail(client, conversation_id))["messages"]
        for block in message["content"]
        if block.get("type") == "tool_result" and block["tool_use_id"] == "m1"
    ]
    assert stored[0]["library"] == results["m1"]["library"]
    replayed = [
        block
        for message in main[-1]["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert len(replayed) == 13 and not any("library" in block for block in replayed)


async def test_a_document_that_is_answering_stays_where_it_is(chat_client) -> None:
    """A document whose own conversation has an unfinished turn is refused as the Files page
    refuses it, as an error result that says why (docs/chat.md § Library conversations)."""
    client = chat_client
    settings = client.app.state.settings
    sessionmaker = client.app.state.sessionmaker
    answering = await new_conversation(client)
    async with sessionmaker() as db:
        db.add(
            ChatTurn(
                user_id=client.user_id,
                conversation_id=answering,
                message_id=0,
                status="running",
                started_at=datetime.now(UTC),
            )
        )
        await db.commit()

    for name, tool_input, action in (
        ("trash", {"paths": ["Papers/paper.pdf"]}, "move Papers/paper.pdf to the Trash"),
        ("move", {"paths": ["Papers/paper.pdf"], "to": ""}, "move Papers/paper.pdf"),
        ("rename", {"path": "Papers/paper.pdf", "name": "b.pdf"}, "rename Papers/paper.pdf"),
        ("trash", {"paths": ["Papers"]}, "move Papers to the Trash"),
    ):
        outcome = await chat_library.run_tool(
            sessionmaker,
            settings,
            user_id=client.user_id,
            author="agent:1",
            name=name,
            tool_input=tool_input,
        )
        assert outcome.is_error is True and outcome.library is None, name
        assert outcome.content == (
            f"Could not {action}: a conversation about this file is still answering."
        )
    _, documents = await _rows(client)
    row = documents[client.document_id]
    assert (row.name, row.trashed_at) == ("paper.pdf", None)

    # Past the cap of paths, nothing is done at all.
    capped = await chat_library.run_tool(
        sessionmaker,
        settings,
        user_id=client.user_id,
        author="agent:1",
        name="trash",
        tool_input={"paths": ["x"] * (settings.chat_library_max_paths + 1)},
    )
    assert capped.is_error is True and capped.content.startswith("At most 100 paths")


async def test_a_batch_goes_on_past_an_item_the_operation_refuses(chat_client) -> None:
    """An item the operation itself refuses in the middle of a batch is rolled back and
    said, and the items after it still go; a name taken where an item lands is suffixed,
    the record keeping the name before and after (docs/chat.md § Library conversations,
    *A batch does what it can*)."""
    client = chat_client
    for path in ("A", "B", "C", "A/Plan", "Plan"):
        tool = "new_folder" if "Plan" not in path else "new_document"
        assert (await _tool(client, tool, {"path": path})).is_error is False
    folders, documents = await _rows(client)
    a, b, c = (next(row.id for row in folders.values() if row.name == name) for name in "ABC")
    plan = next(row.id for row in documents.values() if row.folder_id is None)

    moved = await _tool(client, "move", {"paths": ["B", "A", "C", "Plan.md"], "to": "A"})
    assert moved.is_error is False
    assert moved.content == (
        f"Moved B (folder {b}) to A/B.\n"
        "Could not move A: a folder cannot be moved inside itself.\n"
        f"Moved C (folder {c}) to A/C.\n"
        f"Moved Plan.md (document {plan}) to A/Plan (2).md."
    )
    # The refused one is in no move of the record; the suffixed one keeps both names.
    assert moved.library == {
        "type": "move",
        "moves": [
            {
                "item": {"kind": "folder", "id": b},
                "from": None,
                "to": a,
                "fromName": "B",
                "toName": "B",
            },
            {
                "item": {"kind": "folder", "id": c},
                "from": None,
                "to": a,
                "fromName": "C",
                "toName": "C",
            },
            {
                "item": {"kind": "file", "id": plan},
                "from": None,
                "to": a,
                "fromName": "Plan.md",
                "toName": "Plan (2).md",
            },
        ],
    }
    folders, documents = await _rows(client)
    assert (folders[a].parent_id, folders[b].parent_id, folders[c].parent_id) == (None, a, a)
    assert (documents[plan].folder_id, documents[plan].name) == (a, "Plan (2).md")


async def test_put_back_finds_its_items_in_the_trash_as_it_was_listed(chat_client) -> None:
    """Every path is looked up in the Trash as the call found it, before anything goes
    back: a folder and an item trashed out of it go back together under the names
    `list_files` gave them, though the first changes where the second returns; and one name
    given twice takes the two trashed under it, newest first, the second suffixed (docs/chat.md
    § Library conversations, *The library tools*)."""
    client = chat_client
    settings = client.app.state.settings
    await _tool(client, "new_folder", {"path": "A/C"})
    await _tool(client, "trash", {"paths": ["A/C"]})
    await _tool(client, "trash", {"paths": ["A"]})
    folders, _ = await _rows(client)
    a, c = (next(row.id for row in folders.values() if row.name == name) for name in "AC")
    listed = await chat_library.list_files(
        client.app.state.sessionmaker,
        settings,
        user_id=client.user_id,
        author="agent:1",
        tool_input={"trash": True},
    )
    lines = listed.content.splitlines()[1:]
    assert lines[0].startswith(f"folder A (folder {a}, trashed ")
    assert lines[1].startswith(f"folder C (folder {c}, trashed ")

    back = await _tool(client, "put_back", {"paths": ["A", "C"]})
    assert (back.is_error, back.content) == (
        False,
        f"Put back A (folder {a}): it is at A.\nPut back C (folder {c}): it is at A/C.",
    )
    assert back.library == {
        "type": "appear",
        "items": [{"kind": "folder", "id": a}, {"kind": "folder", "id": c}],
    }
    folders, _ = await _rows(client)
    assert (folders[a].parent_id, folders[a].trashed_at) == (None, None)
    assert (folders[c].parent_id, folders[c].trashed_at) == (a, None)
    root = user_root(settings.workspaces_dir, client.user_id)
    assert (root / "files/A/C").is_dir()

    for _ in range(2):
        await _tool(client, "new_document", {"path": "Notes/Plan"})
        await _tool(client, "trash", {"paths": ["Notes/Plan.md"]})
    _, documents = await _rows(client)
    older, newer = sorted(row.id for row in documents.values() if row.name == "Plan.md")
    back = await _tool(client, "put_back", {"paths": ["Notes/Plan.md"] * 3})
    assert back.content == (
        "Could not put back Notes/Plan.md: the Trash holds nothing there.\n"
        f"Put back Plan.md (document {newer}): it is at Notes/Plan.md.\n"
        f"Put back Plan (2).md (document {older}): it is at Notes/Plan (2).md."
    )
    assert back.library == {
        "type": "appear",
        "items": [{"kind": "file", "id": newer}, {"kind": "file", "id": older}],
    }


async def test_a_folder_named_by_its_directory_is_found_not_made_again(chat_client) -> None:
    """A folder whose directory is named apart from it (`_inbox` is `__inbox` in the VM) is
    found by the path `list_files` prints, as every other tool finds it, and never made a
    second time beside itself (docs/chat.md § Library conversations, *The library tools*)."""
    client = chat_client
    await _tool(client, "new_folder", {"path": "_inbox"})
    folders, _ = await _rows(client)
    inbox = next(row.id for row in folders.values() if row.name == "_inbox")

    again = await _tool(client, "new_folder", {"path": "/workspace/files/__inbox"})
    assert (again.content, again.library) == (
        f"The folder _inbox (folder {inbox}) already exists.",
        None,
    )
    made = await _tool(client, "new_document", {"path": "/workspace/files/__inbox/Plan"})
    folders, documents = await _rows(client)
    plan = next(row for row in documents.values() if row.name == "Plan.md")
    assert plan.folder_id == inbox
    assert made.content == (
        f"Made the document _inbox/Plan.md (document {plan.id}); its text is at "
        "/workspace/files/__inbox/Plan.md/doc.md."
    )
    assert sorted(row.name for row in folders.values()) == ["Papers", "_inbox"]


async def test_a_name_typed_decomposed_names_the_folder_and_a_path_too_deep_is_refused(
    chat_client,
) -> None:
    """Names are kept NFC: an é typed decomposed, as a macOS file name gives it, still names
    the folder. A folder path deeper than a filing's is refused with nothing made
    (docs/chat.md § Library conversations, *The library tools*)."""
    client = chat_client
    await _tool(client, "new_folder", {"path": "R\u00e9sum\u00e9s"})
    folders, _ = await _rows(client)
    resumes = next(row.id for row in folders.values() if row.name == "R\u00e9sum\u00e9s")

    again = await _tool(client, "new_folder", {"path": "Re\u0301sume\u0301s"})
    assert again.content == f"The folder R\u00e9sum\u00e9s (folder {resumes}) already exists."
    deep = await _tool(client, "new_folder", {"path": "/".join(["d"] * 65)})
    assert deep.is_error
    assert deep.content == "No folder was made: a folder path is at most 64 folders deep."
    folders, _ = await _rows(client)
    assert sorted(row.name for row in folders.values()) == ["Papers", "R\u00e9sum\u00e9s"]


@respx.mock
async def test_a_listing_sees_what_the_calls_before_it_in_the_round_changed(chat_client) -> None:
    """`list_files` takes its turn with the writes: sent after a change in the same round,
    it lists the library as that change left it (docs/chat.md § Library conversations)."""
    client = chat_client
    stub_messages(
        [
            (
                [
                    {
                        "type": "tool_use",
                        "id": "f",
                        "name": "new_folder",
                        "input": {"path": "Later"},
                    },
                    {"type": "tool_use", "id": "l", "name": "list_files", "input": {}},
                ],
                "tool_use",
            ),
            DONE,
        ]
    )
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, text="Make Later and show me")
    await _run_queued_turn(client)

    results = {result["id"]: result for result in await tool_results(client, conversation_id)}
    folders, _ = await _rows(client)
    later = next(row.id for row in folders.values() if row.name == "Later")
    assert f"folder Later (folder {later}, 0 items)" in results["l"]["output"]


async def test_what_was_made_before_a_refusal_is_said_with_its_undo(
    chat_client, monkeypatch
) -> None:
    """A name refused is refused before any folder is made; a folder or a document's
    directory refused after something was made leaves what was made, said in the answer
    with its undo record rather than as nothing done (docs/chat.md § Library
    conversations, *A batch does what it can*)."""
    client = chat_client
    for tool in ("new_folder", "new_document"):
        refused = await _tool(client, tool, {"path": "Alpha/\u0001"})
        assert (refused.is_error, refused.content, refused.library) == (
            True,
            "Nothing was done: a name is required.",
            None,
        )
    folders, _ = await _rows(client)
    assert sorted(row.name for row in folders.values()) == ["Papers"]

    make_folder = filing.make_folder

    async def refused_inside(db, settings, user_id, parent_id, name):
        if parent_id is not None:
            raise ApiError(409, "invalid_request", "The tree refused it")
        return await make_folder(db, settings, user_id, parent_id, name)

    with monkeypatch.context() as patch:
        patch.setattr(filing, "make_folder", refused_inside)
        folder = await _tool(client, "new_folder", {"path": "Alpha/Beta"})
        document = await _tool(client, "new_document", {"path": "Gamma/Delta/Plan"})
    folders, _ = await _rows(client)
    alpha, gamma = (
        next(row.id for row in folders.values() if row.name == name) for name in ("Alpha", "Gamma")
    )
    assert folder.is_error is False
    assert folder.content == (
        f"Made the folder Alpha (folder {alpha}), but the folder Alpha/Beta could not be made "
        "(the tree refused it)."
    )
    assert folder.library == {"type": "appear", "items": [{"kind": "folder", "id": alpha}]}
    assert document.is_error is False
    assert document.content == (
        "No document was made: the folder Gamma/Delta could not be made (the tree refused "
        "it). Made on the way: the folder Gamma."
    )
    assert document.library == {"type": "appear", "items": [{"kind": "folder", "id": gamma}]}
    assert sorted(row.name for row in folders.values()) == ["Alpha", "Gamma", "Papers"]

    async def unwritable(*args, **kwargs):
        raise ApiError(409, "invalid_request", "The tree refused it")

    with monkeypatch.context() as patch:
        patch.setattr(workspace_documents, "materialize_document", unwritable)
        made = await _tool(client, "new_document", {"path": "Notes/Plan"})
    _, documents = await _rows(client)
    plan = next(row.id for row in documents.values() if row.name == "Plan.md")
    assert made.is_error is False
    assert made.content == (
        f"Made the document Notes/Plan.md (document {plan}), with the new folder Notes; its "
        "text is at /workspace/files/Notes/Plan.md/doc.md, though its directory could not be "
        "written yet (the tree refused it)."
    )
    assert made.library == {"type": "appear", "items": [{"kind": "file", "id": plan}]}


@respx.mock
async def test_a_memory_for_this_document_has_no_document_to_go_with(chat_client) -> None:
    client = chat_client

    def call(call_id: str, scope: str) -> tuple[list[dict], str]:
        tool_input = {"action": "save", "id": "", "kind": "term", "text": "query → 查詢"}
        return one_call(call_id, "memory", {**tool_input, "scope": scope})

    stub_messages([call("d", "document"), call("a", "all"), DONE])
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, text="Remember query is 查詢")
    await _run_queued_turn(client)

    results = {result["id"]: result for result in await tool_results(client, conversation_id)}
    assert results["d"]["is_error"] is True
    assert chat_memory.NO_DOCUMENT in results["d"]["output"]
    assert results["a"]["is_error"] is False


@respx.mock
async def test_init_in_a_library_conversation_names_the_open_folders_agents_md(
    chat_client,
) -> None:
    client = chat_client
    main, _ = stub_messages([DONE, DONE, DONE])
    papers = await _papers(client)
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, command="init", library={"folder_id": papers})
    await _run_queued_turn(client)
    await send(client, conversation_id, command="init", text="short")
    await _run_queued_turn(client)

    first = main[0]["messages"][0]["content"]
    assert first[-2]["text"] == chat_agent.library_init_instruction(
        "Papers", "/workspace/files/Papers/AGENTS.md", ""
    )
    second = main[1]["messages"][-1]["content"]
    assert second[-2]["text"] == chat_agent.library_init_instruction(
        "", "/workspace/files/AGENTS.md", "short"
    )

    # Edited after its folder was renamed, the first finds the folder where it is now.
    await _tool(client, "rename", {"path": "Papers", "name": "Reading"})
    first_id = (await detail(client, conversation_id))["messages"][0]["id"]
    edited = await edit(client, conversation_id, first_id, text="")
    assert edited.status_code == 202, edited.text
    await _run_queued_turn(client)
    assert main[2]["messages"][0]["content"][-2]["text"] == chat_agent.library_init_instruction(
        "Reading", "/workspace/files/Reading/AGENTS.md", ""
    )


@respx.mock
async def test_the_idle_pass_compacts_a_library_conversation(chat_client) -> None:
    client = chat_client
    settings = client.app.state.settings
    main, _ = stub_messages([DONE, SUMMARY])
    conversation_id = await new_library_conversation(client)
    await send(client, conversation_id, text="hello")
    await _run_queued_turn(client)
    await set_clock(
        client,
        conversation_id,
        warm_for=settings.chat_idle_compact_lead_seconds - 10,
        tokens=settings.chat_idle_compact_min_tokens,
    )

    assert await chat_compact.idle_compact_once(client.app.state.sessionmaker, settings) == 1
    await _run_queued_turn(client)
    turns = await turns_of(client, conversation_id)
    assert [turn.status for turn in turns] == ["succeeded", "succeeded"]
    instruction = main[1]["messages"][-1]["content"]
    assert instruction[0]["text"] == chat_agent.compact_instruction("")
    assert "revision" not in instruction[-1]["text"]
    assert (await detail(client, conversation_id))["conversation"]["compacted_message_id"]


@pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="the foreign key cascades only on PostgreSQL",
)
@respx.mock
async def test_deleting_a_document_leaves_the_library_conversations(chat_client) -> None:
    """The document's conversations go with it; a library conversation, about none, stays
    (docs/database.md)."""
    client = chat_client
    stub_messages([DONE, DONE])
    library = await new_library_conversation(client)
    about_document = await new_conversation(client)
    for conversation_id in (library, about_document):
        await send(client, conversation_id, text="hello")
        await _run_queued_turn(client)

    trashed = await client.post(
        f"/api/library/documents/{client.document_id}/trash", headers=CSRF_HEADERS
    )
    assert trashed.status_code == 200, trashed.text
    gone = await client.delete(f"/api/library/documents/{client.document_id}", headers=CSRF_HEADERS)
    assert gone.status_code == 204, gone.text
    assert (await client.get(f"/api/conversations/{about_document}")).status_code == 404
    listed = (await client.get("/api/library/conversations")).json()["conversations"]
    assert [row["id"] for row in listed] == [library]
