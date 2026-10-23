"""Markdown documents (docs/workspace.md § Markdown documents): a library entry with no
parse behind it — a `.md` uploaded on the Files page, or a document written new there.

No upstream traffic: a markdown document never queues a parse, and the one chat turn here
answers from a stubbed transport (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select

from sightread.db.models import Document, Job
from sightread.workspace.documents import original_of
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS
from tests.test_chat import MESSAGES_URL, _run_queued_turn, assistant_turn


@pytest.fixture
async def writer(make_client, tmp_path) -> AsyncClient:
    """Signed in, with a workspace of its own and no upstream connected: nothing here
    needs one."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _upload_markdown(
    client: AsyncClient,
    data: bytes,
    name: str = "notes.md",
    media_type: str = "application/octet-stream",
    folder_id: int | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/library/documents",
        files={"file": (name, data, media_type)},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )


async def _new(client: AsyncClient, name: str, folder_id: int | None = None) -> httpx.Response:
    return await client.post(
        "/api/library/documents/new",
        json={"name": name, "folder_id": folder_id},
        headers=CSRF_HEADERS,
    )


async def _jobs(client: AsyncClient) -> int:
    async with client.app.state.sessionmaker() as db:
        return (await db.execute(select(func.count()).select_from(Job))).scalar_one()


NO_PARSE = {
    "job_id": None,
    "status": None,
    "kind": "markdown",
    "model": None,
    "page_count": None,
    "pages_done": None,
    "size_bytes": None,
    "error": None,
    "finished_at": None,
}


async def test_an_uploaded_md_is_a_document_with_no_parse(writer) -> None:
    """The upload is filed as it is — no job, no model needed — and its text, a BOM and
    CRLF line ends normalised, is the document; the original stays byte for byte."""
    folder = (
        await writer.post("/api/library/folders", json={"name": "Drafts"}, headers=CSRF_HEADERS)
    ).json()
    raw = "﻿# Plan\r\n\r\nShip it.\r\n".encode()
    uploaded = await _upload_markdown(writer, raw, folder_id=folder["id"])
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()
    assert {key: document[key] for key in NO_PARSE} == NO_PARSE
    assert document["name"] == "notes.md" and document["folder_id"] == folder["id"]
    assert await _jobs(writer) == 0
    listed = (await writer.get("/api/library")).json()["documents"]
    assert [(row["id"], row["kind"], row["job_id"]) for row in listed] == [
        (document["id"], "markdown", None)
    ]

    original = original_of(writer.root, document["id"])
    assert original is not None and original.name == f"{document['id']}.md"
    assert original.read_bytes() == raw
    # The staged upload is gone once it is kept.
    assert list(Path(writer.app.state.settings.upload_dir).iterdir()) == []

    content = await writer.get(f"/api/library/documents/{document['id']}/content")
    assert content.status_code == 200, content.text
    assert content.json()["markdown"] == "# Plan\n\nShip it.\n"
    assert content.json()["notes"] == ""
    # No pages to render: the original is text, not a source.
    assert content.json()["has_source"] is False

    directory = writer.root / "files" / "Drafts" / "notes.md"
    meta = json.loads((directory / ".meta.json").read_text())
    assert meta == {
        "document_id": document["id"],
        "job_id": None,
        "model": None,
        "bbox_format": None,
        "page_count": None,
        "kind": "markdown",
    }
    # The lookup a moved directory is found by needs a key after the id.
    assert f'"document_id": {document["id"]},' in (directory / ".meta.json").read_text()
    assert sorted(path.name for path in directory.iterdir()) == [".meta.json", "doc.md", "notes.md"]
    assert not (writer.root / ".sightread").exists()

    result = await writer.get(f"/api/library/documents/{document['id']}/result")
    assert result.status_code == 404
    figure = await writer.get(f"/api/library/documents/{document['id']}/figures/1/1,2,3,4")
    assert figure.status_code == 404


async def test_a_markdown_type_alone_is_enough(writer) -> None:
    uploaded = await _upload_markdown(writer, b"# Hi\n", name="readme", media_type="text/markdown")
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["kind"] == "markdown"
    assert uploaded.json()["name"] == "readme"


async def test_a_markdown_upload_must_be_utf8_text_under_the_content_cap(
    make_client, tmp_path
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), content_max_bytes=64)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    uploads = Path(client.app.state.settings.upload_dir)

    latin1 = await _upload_markdown(client, "café\n".encode("latin-1"))
    assert latin1.status_code == 400, latin1.text
    too_large = await _upload_markdown(client, b"x" * 65)
    assert too_large.status_code == 413, too_large.text
    assert "CONTENT_MAX_BYTES" in too_large.json()["error"]["message"]

    assert (await client.get("/api/library")).json()["documents"] == []
    assert list(uploads.iterdir()) == []
    # A character split across the reader's slices is still UTF-8.
    assert (await _upload_markdown(client, "é" * 32)).status_code == 201


async def test_an_upload_the_quota_refuses_files_nothing(writer) -> None:
    first = await _upload_markdown(writer, b"# One\n")
    assert first.status_code == 201
    writer.app.state.settings.workspace_quota_bytes = 1
    try:
        refused = await _upload_markdown(writer, b"# Two\n")
    finally:
        writer.app.state.settings.workspace_quota_bytes = 10 * 1024 * 1024 * 1024
    assert refused.status_code == 409, refused.text
    assert [row["name"] for row in (await writer.get("/api/library")).json()["documents"]] == [
        "notes.md"
    ]
    assert list(Path(writer.app.state.settings.upload_dir).iterdir()) == []
    # Nothing kept under the id of the row that is gone.
    sources = writer.root.parent / ".repos" / f"{writer.user_id}.sources"
    assert sorted(path.name for path in sources.iterdir()) == [f"{first.json()['id']}.md"]


async def test_a_new_document_starts_empty_and_takes_a_free_name(writer) -> None:
    created = await _new(writer, "Draft")
    assert created.status_code == 201, created.text
    document = created.json()
    assert {key: document[key] for key in NO_PARSE} == NO_PARSE
    assert (await _new(writer, "Draft")).json()["name"] == "Draft (2)"
    assert (await _new(writer, "Draft", folder_id=999_999)).status_code == 404
    assert original_of(writer.root, document["id"]) is None

    content = (await writer.get(f"/api/library/documents/{document['id']}/content")).json()
    assert content["markdown"] == "" and content["has_source"] is False
    saved = await writer.put(
        f"/api/library/documents/{document['id']}/content",
        json={"markdown": "# Written here\n", "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text
    again = (await writer.get(f"/api/library/documents/{document['id']}/content")).json()
    assert again["markdown"] == "# Written here\n"
    revisions = (await writer.get(f"/api/library/documents/{document['id']}/revisions")).json()
    assert [entry["message"] for entry in revisions["revisions"]] == [
        "edit files/Draft/doc.md",
        "materialize files/Draft",
    ]


async def test_a_new_document_needs_a_name_and_the_csrf_header(writer) -> None:
    assert (await _new(writer, "")).status_code == 422
    refused = await writer.post("/api/library/documents/new", json={"name": "Draft"})
    assert refused.status_code == 403


async def test_a_removed_directory_comes_back_from_the_original(writer) -> None:
    """The baseline is the upload, as a parsed document's is its parse — so a directory
    the shell removed reads again as the text the document started with."""
    document = (await _upload_markdown(writer, b"# Start\n")).json()
    content = (await writer.get(f"/api/library/documents/{document['id']}/content")).json()
    await writer.put(
        f"/api/library/documents/{document['id']}/content",
        json={"markdown": "# Changed\n", "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    directory = writer.root / "files" / "notes.md"
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()
    back = (await writer.get(f"/api/library/documents/{document['id']}/content")).json()
    assert back["markdown"] == "# Start\n"


async def test_a_markdown_document_is_renamed_moved_and_deleted(writer) -> None:
    document = (await _upload_markdown(writer, b"# Doc\n")).json()
    folder = (
        await writer.post("/api/library/folders", json={"name": "Kept"}, headers=CSRF_HEADERS)
    ).json()
    await writer.get(f"/api/library/documents/{document['id']}/content")
    moved = await writer.put(
        f"/api/library/documents/{document['id']}",
        json={"name": "plan.md", "folder_id": folder["id"]},
        headers=CSRF_HEADERS,
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["name"] == "plan.md" and moved.json()["job_id"] is None
    assert (writer.root / "files" / "Kept" / "plan.md" / "doc.md").read_text() == "# Doc\n"

    deleted = await writer.delete(f"/api/library/documents/{document['id']}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204
    assert original_of(writer.root, document["id"]) is None
    assert not (writer.root / "files" / "Kept" / "plan.md").exists()


async def test_me_lists_markdown_with_its_own_cap(writer) -> None:
    limits = (await writer.get("/api/me")).json()["limits"]
    assert "text/markdown" in limits["accepted_media_types"]
    assert {".md", ".markdown"} <= set(limits["accepted_extensions"])
    assert limits["markdown_media_types"] == ["text/markdown"]
    assert limits["markdown_extensions"] == [".md", ".markdown"]
    assert limits["markdown_max_bytes"] == writer.app.state.settings.content_max_bytes


async def test_the_agent_page_renders_a_markdown_documents_facts(writer) -> None:
    document = (await _new(writer, "Draft")).json()
    context = await writer.get("/api/agents/chat/context", params={"document_id": document["id"]})
    assert context.status_code == 200, context.text
    facts = context.json()["facts"]
    assert facts["page_count"] == "none" and facts["bbox_format"] == "none"
    assert facts["parse_model"] == "not parsed (a markdown document)"
    assert facts["document_path"] == "/workspace/files/Draft"


@respx.mock
async def test_a_turn_on_a_markdown_document_edits_it_and_has_no_pages(writer) -> None:
    chosen = await writer.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    assert chosen.status_code == 200, chosen.text
    document = (await _upload_markdown(writer, b"# Plan\n\nShip it.\n")).json()
    created = await writer.post(
        f"/api/library/documents/{document['id']}/conversations", headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text

    turns = iter(
        [
            assistant_turn(
                [{"type": "tool_use", "id": "t1", "name": "view_page", "input": {"page": 1}}],
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
                            "path": "/workspace/files/notes.md/doc.md",
                            "old_str": "Ship it.",
                            "new_str": "Ship it today.",
                        },
                    }
                ],
                "tool_use",
            ),
            assistant_turn([{"type": "text", "text": "Done."}], "end_turn"),
        ]
    )
    requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(turns))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    sent = await writer.post(
        f"/api/conversations/{created.json()['id']}/messages",
        json={"text": "Say when"},
        headers=CSRF_HEADERS,
    )
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(writer)

    system = requests[0]["system"][0]["text"]
    assert "Pages: none   Bbox format: none   Parsed with: not parsed (a markdown document)" in (
        system
    )
    result = requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True
    assert "markdown document" in json.dumps(result["content"])
    async with writer.app.state.sessionmaker() as db:
        row = await db.get(Document, document["id"])
        assert row is not None and row.job_id is None
    content = (await writer.get(f"/api/library/documents/{document['id']}/content")).json()
    assert content["markdown"] == "# Plan\n\nShip it today.\n"
