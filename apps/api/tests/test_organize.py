"""Organizing the Files library through the connector and the data plane (docs/mcp.md § The
organizing tools, docs/api.md § The library on the data plane): the Files page's own
operations, named by path, many per call, each item its own transaction, answered in
sentences with the undo record the `undo` tool replays. No upstream traffic: every item is a
kept file, a markdown document or a folder (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from sightread.auth import document_tickets
from sightread.db.models import Document, Folder, User
from sightread.workspace import git
from tests.conftest import CSRF_HEADERS, api_key_client, mcp_running
from tests.connector_helpers import mcp_session, oauth_connected, tool_payload
from tests.factories import user_row
from tests.helpers import bearer
from tests.library_helpers import new_document, upload


@pytest.fixture
async def reader(make_client) -> AsyncClient:
    """A signed-in account holding an API key, with a workspace of its own."""
    return await api_key_client(make_client())


async def _folder(client: AsyncClient, name: str, parent_id: int | None = None) -> int:
    made = await client.post(
        "/api/library/folders", json={"name": name, "parent_id": parent_id}, headers=CSRF_HEADERS
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def _file(
    client: AsyncClient, name: str, folder_id: int | None = None, media_type: str = "text/plain"
) -> int:
    kept = await upload(client, name, b"kept as it is\n", media_type, folder_id)
    assert kept.status_code == 201, kept.text
    return kept.json()["id"]


async def _rows(client: AsyncClient) -> tuple[dict[int, Folder], dict[int, Document]]:
    async with client.app.state.sessionmaker() as db:
        folders = (await db.execute(select(Folder))).scalars().all()
        documents = (await db.execute(select(Document))).scalars().all()
        for row in (*folders, *documents):
            db.expunge(row)
    return {row.id: row for row in folders}, {row.id: row for row in documents}


async def test_the_tools_organize_the_library_as_the_files_page_does(reader: AsyncClient) -> None:
    dl = await _folder(reader, "DL")
    paper = await _file(reader, "Attention.pdf", dl, "application/pdf")
    twin = await _file(reader, "Attention.pdf", None, "application/pdf")
    clip = await _file(reader, "OP2_Act02.m4v", None, "video/mp4")
    draft = (await new_document(reader, "Notes", dl)).json()["id"]
    before = len(git.log(reader.root))

    async with mcp_running(reader), mcp_session(reader) as session:
        made = tool_payload(await session.call_tool("new_folder", {"path": "DL/Transformer"}))
        again = tool_payload(await session.call_tool("new_folder", {"path": "DL/Transformer"}))
        moved = tool_payload(
            await session.call_tool(
                "move", {"paths": ["DL/Attention.pdf", "Attention.pdf"], "to": "DL/Transformer"}
            )
        )
        renamed = tool_payload(
            await session.call_tool("rename", {"path": "DL/Notes.md", "name": "Reading list"})
        )
        clash = await session.call_tool(
            "rename",
            {"path": "DL/Transformer/Attention (2).pdf", "name": "Attention.pdf"},
        )
        copied = tool_payload(
            await session.call_tool("copy", {"paths": ["DL/Reading list.md"], "to": "/"})
        )
        trashed = tool_payload(await session.call_tool("trash", {"paths": ["OP2_Act02.m4v"]}))
        listed = tool_payload(await session.call_tool("list_library", {"trash": True}))
        put = tool_payload(await session.call_tool("put_back", {"paths": ["OP2_Act02.m4v"]}))

    folders, documents = await _rows(reader)
    transformer = next(row for row in folders.values() if row.name == "Transformer")
    assert made["result"] == f"Made the folder DL/Transformer (folder {transformer.id})."
    assert made["undo"] == {"type": "appear", "items": [{"kind": "folder", "id": transformer.id}]}
    assert again["undo"] is None and "already exists" in again["result"]

    # A taken name is suffixed, as a drop on the page suffixes it, and the record says so.
    assert (documents[paper].folder_id, documents[paper].name) == (transformer.id, "Attention.pdf")
    assert (documents[twin].folder_id, documents[twin].name) == (
        transformer.id,
        "Attention (2).pdf",
    )
    assert moved["done"] == 2 and moved["failed"] == 0
    assert moved["undo"]["moves"][1] == {
        "item": {"kind": "file", "id": twin},
        "from": None,
        "to": transformer.id,
        "fromName": "Attention.pdf",
        "toName": "Attention (2).pdf",
    }

    # A markdown document keeps `.md`; a rename that collides is refused, never suffixed.
    assert documents[draft].name == "Reading list.md"
    assert renamed["undo"] == {
        "type": "rename",
        "item": {"kind": "file", "id": draft},
        "from": "Notes.md",
        "to": "Reading list.md",
    }
    assert clash.is_error is True
    assert "already exists" in clash.content[0].text
    assert documents[twin].name == "Attention (2).pdf"

    copy_id = copied["undo"]["items"][0]["id"]
    assert (documents[copy_id].folder_id, documents[copy_id].name) == (None, "Reading list.md")

    # The Trash lists the video by where Put Back returns it, the path put_back takes.
    assert trashed["undo"] == {"type": "trash", "items": [{"kind": "file", "id": clip}]}
    assert [(row["id"], row["kind"], row["path"]) for row in listed["trash"]] == [
        (clip, "file", "OP2_Act02.m4v")
    ]
    assert listed["total"] == 1 and listed["truncated"] is False
    assert put["result"] == f"Put back OP2_Act02.m4v (document {clip}): it is at OP2_Act02.m4v."
    assert documents[clip].trashed_at is None

    # Every tree commit the connector made is authored `api`.
    revisions = git.log(reader.root)
    assert len(revisions) > before
    assert {revision.author for revision in revisions[: len(revisions) - before]} == {
        git.API_AUTHOR
    }


async def test_a_move_goes_only_into_a_folder_that_exists(reader: AsyncClient) -> None:
    await _file(reader, "a.txt")
    async with mcp_running(reader), mcp_session(reader) as session:
        refused = await session.call_tool("move", {"paths": ["a.txt"], "to": "Nowhere"})
    assert refused.is_error is True
    assert "make it with new_folder first" in refused.content[0].text
    folders, documents = await _rows(reader)
    assert folders == {}
    assert [row.folder_id for row in documents.values()] == [None]


async def test_a_batch_does_what_it_can(reader: AsyncClient) -> None:
    dl = await _folder(reader, "DL")
    kept = await _file(reader, "a.txt")
    async with mcp_running(reader), mcp_session(reader) as session:
        some = tool_payload(
            await session.call_tool("move", {"paths": ["a.txt", "missing.txt"], "to": "DL"})
        )
        none = await session.call_tool("trash", {"paths": ["missing.txt", "/"]})

    assert some["done"] == 1 and some["failed"] == 1
    # Every path is looked up first, so a path that names nothing is said first.
    assert some["result"] == (
        f"Could not move missing.txt: nothing is there.\nMoved a.txt (document {kept}) to DL/a.txt."
    )
    assert none.is_error is True
    assert "the top of the library is no item" in none.content[0].text
    _, documents = await _rows(reader)
    assert documents[kept].folder_id == dl and documents[kept].trashed_at is None


async def test_undo_takes_a_step_back_by_its_ids_and_redo_does_it_again(
    reader: AsyncClient,
) -> None:
    dl = await _folder(reader, "DL")
    first = await _file(reader, "a.txt")
    await _file(reader, "a.txt", dl)

    async with mcp_running(reader), mcp_session(reader) as session:
        moved = tool_payload(await session.call_tool("move", {"paths": ["a.txt"], "to": "DL"}))
        undone = tool_payload(await session.call_tool("undo", {"step": moved["undo"]}))
        redone = tool_payload(
            await session.call_tool("undo", {"step": moved["undo"], "redo": True})
        )
        made = tool_payload(await session.call_tool("new_folder", {"path": "Old/Deeper"}))
        unmade = tool_payload(await session.call_tool("undo", {"step": made["undo"]}))
        trashed = tool_payload(await session.call_tool("trash", {"paths": ["DL/a (2).txt"]}))
        restored = tool_payload(await session.call_tool("undo", {"step": trashed["undo"]}))
        nonsense = await session.call_tool("undo", {"step": {"type": "delete", "items": []}})

    folders, documents = await _rows(reader)
    assert moved["undo"]["moves"][0]["toName"] == "a (2).txt"
    # Back under its old name, where it came from; then the move again, suffixed again.
    assert undone["done"] == 1
    assert undone["result"] == f"Moved DL/a (2).txt (document {first}) to a.txt."
    assert redone["result"] == f"Moved a.txt (document {first}) to DL/a (2).txt."
    assert (documents[first].folder_id, documents[first].name) == (dl, "a (2).txt")

    # A new folder taken back goes to the Trash with what it made, never deleted for good.
    old = next(row for row in folders.values() if row.name == "Old")
    assert unmade["result"] == f"Moved Old (folder {old.id}) to the Trash."
    assert old.trashed_at is not None

    assert restored["result"].startswith(f"Put back a (2).txt (document {first})")
    assert documents[first].trashed_at is None
    assert nonsense.is_error is True
    assert "`step` is not an undo record" in nonsense.content[0].text


async def test_undo_refuses_an_item_changed_since_its_record(reader: AsyncClient) -> None:
    dl = await _folder(reader, "DL")
    paper = await _file(reader, "paper.txt")
    notes = await _file(reader, "notes.txt")

    async with mcp_running(reader), mcp_session(reader) as session:
        moved = tool_payload(await session.call_tool("move", {"paths": ["paper.txt"], "to": "DL"}))
        renamed = tool_payload(
            await session.call_tool("rename", {"path": "notes.txt", "name": "draft.txt"})
        )
        # The reader changes both again before the agent takes its records back.
        await session.call_tool("rename", {"path": "DL/paper.txt", "name": "kept.txt"})
        await session.call_tool("rename", {"path": "draft.txt", "name": "final.txt"})
        stale_move = await session.call_tool("undo", {"step": moved["undo"]})
        stale_rename = await session.call_tool("undo", {"step": renamed["undo"]})

    assert stale_move.is_error is True
    assert stale_move.content[0].text.endswith(
        f"Could not move document {paper}: it was moved or renamed since."
    )
    assert stale_rename.is_error is True
    assert "it was moved or renamed since" in stale_rename.content[0].text
    _, documents = await _rows(reader)
    assert (documents[paper].folder_id, documents[paper].name) == (dl, "kept.txt")
    assert (documents[notes].folder_id, documents[notes].name) == (None, "final.txt")


async def test_another_accounts_items_are_out_of_reach(reader: AsyncClient) -> None:
    async with reader.app.state.sessionmaker() as db:
        other = user_row("someone-else@example.com")
        db.add(other)
        await db.flush()
        theirs = Document(user_id=other.id, folder_id=None, name="theirs.txt", kind="file")
        db.add(theirs)
        await db.commit()
        their_id = theirs.id

    async with mcp_running(reader), mcp_session(reader) as session:
        by_path = await session.call_tool("trash", {"paths": ["theirs.txt"]})
        by_id = await session.call_tool(
            "undo", {"step": {"type": "appear", "items": [{"kind": "file", "id": their_id}]}}
        )

    assert by_path.is_error is True and by_id.is_error is True
    async with reader.app.state.sessionmaker() as db:
        assert (await db.get(Document, their_id)).trashed_at is None


async def test_the_routes_answer_as_the_tools_do(reader: AsyncClient) -> None:
    dl = await _folder(reader, "DL")
    kept = await _file(reader, "a.txt")
    await _file(reader, "b.txt", dl)

    made = await reader.post("/v1/library/new_folder", json={"path": "DL/Old"})
    moved = await reader.post("/v1/library/move", json={"paths": ["a.txt"], "to": "DL"})
    missing = await reader.post("/v1/library/move", json={"paths": ["DL/a.txt"], "to": "Nowhere"})
    clash = await reader.post("/v1/library/rename", json={"path": "DL/a.txt", "name": "b.txt"})
    trashed = await reader.post("/v1/library/trash", json={"paths": ["DL/Old"]})
    listed = await reader.get("/v1/library/trash")
    put = await reader.post("/v1/library/put_back", json={"paths": ["DL/Old"]})
    undone = await reader.post("/v1/library/undo", json={"step": moved.json()["undo"]})
    too_many = await reader.post("/v1/library/trash", json={"paths": ["a.txt"] * 101})
    malformed = await reader.post("/v1/library/move", json={"paths": "a.txt"})

    assert made.status_code == 200, made.text
    assert moved.status_code == 200
    assert moved.json() == {
        "result": f"Moved a.txt (document {kept}) to DL/a.txt.",
        "done": 1,
        "failed": 0,
        "undo": {
            "type": "move",
            "moves": [
                {
                    "item": {"kind": "file", "id": kept},
                    "from": None,
                    "to": dl,
                    "fromName": "a.txt",
                    "toName": "a.txt",
                }
            ],
        },
    }
    # Nothing done: the first refusal's status, the sentences as the message.
    assert missing.status_code == 404
    assert "names no folder" in missing.json()["error"]["message"]
    assert clash.status_code == 409
    assert clash.json()["error"]["message"] == (
        "Could not rename DL/a.txt: a file with this name already exists here."
    )
    assert trashed.status_code == 200
    old = trashed.json()["undo"]["items"][0]["id"]
    assert [
        (row["id"], row["kind"], row["name"], row["path"]) for row in listed.json()["trash"]
    ] == [(old, "folder", "Old", "DL/Old")]
    assert put.status_code == 200 and put.json()["done"] == 1
    assert undone.status_code == 200
    _, documents = await _rows(reader)
    assert documents[kept].folder_id is None
    assert too_many.status_code == 400
    assert malformed.status_code == 422


async def test_a_document_ticket_neither_lists_nor_organizes(reader: AsyncClient) -> None:
    draft = (await new_document(reader, "Draft")).json()["id"]
    async with reader.app.state.sessionmaker() as db:
        user = await db.get(User, reader.user_id)
        _, token = await document_tickets.mint(
            db, reader.app.state.settings, user, draft, document_tickets.WRITE
        )
    listed = await reader.get("/v1/library/trash", headers=bearer(token))
    moved = await reader.post(
        "/v1/library/trash", json={"paths": ["Draft.md"]}, headers=bearer(token)
    )
    assert listed.status_code == 401 and moved.status_code == 401
    assert "organize it with an API key" in moved.json()["error"]["message"]


async def test_a_connector_approved_now_can_organize(make_client) -> None:
    client = make_client()
    _, tokens = await oauth_connected(client)
    made = await client.post(
        "/v1/library/new_folder",
        json={"path": "Papers"},
        headers=bearer(tokens["access_token"]),
    )
    assert made.status_code == 200, made.text
    async with mcp_running(client), mcp_session(client, tokens["access_token"]) as session:
        listed = tool_payload(await session.call_tool("list_library", {}))
    assert [folder["path"] for folder in listed["folders"]] == ["Papers"]
