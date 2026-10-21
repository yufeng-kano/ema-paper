"""The Trash and copies on the Files page (docs/workspace.md § The Trash, § Copies): an
item in the Trash has left `files/` and comes back whole; a copy is an entry with files of
its own.

No upstream traffic: the documents here are markdown or seeded parses, and the one chat
message accepted is left queued (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from sightread.db.models import Document, Folder, Job, utcnow
from sightread.jobs import runner
from sightread.workspace import documents as workspace_documents
from sightread.workspace import git
from sightread.workspace.documents import original_of, trash_home
from sightread.workspace.layout import images_dir, sources_dir, trash_root, user_root
from sightread.workspace.reconcile import reconcile_user
from tests.conftest import CSRF_HEADERS
from tests.test_document_images import _image, _insert
from tests.test_markdown_documents import _new, _upload_markdown
from tests.test_parse_end_to_end import _connect_kano

RESULT = {"markdown": "# Paper\n", "pages": [], "figures": [], "errors": [], "meta": {}}


@pytest.fixture
async def writer(make_client, tmp_path) -> AsyncClient:
    """Signed in, with a workspace of its own and no upstream connected."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _folder(client: AsyncClient, name: str, parent_id: int | None = None) -> dict:
    response = await client.post(
        "/api/library/folders", json={"name": name, "parent_id": parent_id}, headers=CSRF_HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _document(client: AsyncClient, name: str, folder_id: int | None, text: str) -> dict:
    """A markdown document written with `text`, so its directory exists."""
    created = await _new(client, name, folder_id)
    assert created.status_code == 201, created.text
    await _write(client, created.json()["id"], text)
    return created.json()


async def _write(client: AsyncClient, document_id: int, markdown: str) -> None:
    content = (await client.get(f"/api/library/documents/{document_id}/content")).json()
    saved = await client.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": markdown, "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text


async def _read(client: AsyncClient) -> dict:
    response = await client.get("/api/library")
    assert response.status_code == 200, response.text
    return response.json()


async def _trash(client: AsyncClient, kind: str, row_id: int) -> httpx.Response:
    return await client.post(f"/api/library/{kind}/{row_id}/trash", headers=CSRF_HEADERS)


async def _restore(client: AsyncClient, kind: str, row_id: int) -> httpx.Response:
    return await client.post(f"/api/library/{kind}/{row_id}/restore", headers=CSRF_HEADERS)


async def _copy(client: AsyncClient, kind: str, row_id: int, place: int | None) -> httpx.Response:
    key = "folder_id" if kind == "documents" else "parent_id"
    return await client.post(
        f"/api/library/{kind}/{row_id}/copy", json={key: place}, headers=CSRF_HEADERS
    )


async def _running_parse(client: AsyncClient, upload: Path) -> tuple[int, uuid.UUID]:
    """A library document whose parse is still running, its upload waiting for retention."""
    upload.write_bytes(b"%PDF-1.4")
    async with client.app.state.sessionmaker() as db:
        job = Job(
            id=uuid.uuid4(),
            user_id=client.user_id,
            agent="parse",
            kind="pdf",
            filename="paper.pdf",
            media_type="application/pdf",
            size_bytes=8,
            sha256="c" * 64,
            model="vendor/vision-model",
            bbox_format="yxyx_norm1000",
            status="running",
            page_count=1,
            source_path=str(upload),
            retain_source=True,
        )
        db.add(job)
        await db.flush()
        document = Document(user_id=client.user_id, folder_id=None, job_id=job.id, name="paper.pdf")
        db.add(document)
        await db.commit()
        return document.id, job.id


def _fail_next_commit(monkeypatch, *, landed: bool) -> None:
    """The next commit raises: after it went through (`landed`, the acknowledgement lost
    with the connection) or instead of it (rolled back)."""
    original = AsyncSession.commit
    failing = {"on": True}

    async def commit(self):
        if failing["on"]:
            failing["on"] = False
            if landed:
                await original(self)
            else:
                await self.rollback()
            raise OperationalError("COMMIT", {}, Exception("the connection dropped"))
        await original(self)

    monkeypatch.setattr(AsyncSession, "commit", commit)


# --- the Trash ------------------------------------------------------------------------------


async def test_a_trashed_document_leaves_the_tree_and_comes_back_whole(writer) -> None:
    drafts = await _folder(writer, "Drafts")
    document = await _document(writer, "plan.md", drafts["id"], "# Plan\n")
    directory = writer.root / "files" / "Drafts" / "plan.md"

    trashed = await _trash(writer, "documents", document["id"])
    assert trashed.status_code == 200, trashed.text
    assert trashed.json()["trashed_at"] is not None
    # Out of `files/`, whole, into the Trash, as one revision.
    entry = trash_root(writer.root) / f"document-{document['id']}"
    assert not directory.exists()
    assert (entry / "doc.md").read_text() == "# Plan\n"
    assert git.log(writer.root)[0].message == "trash files/Drafts/plan.md"
    library = await _read(writer)
    assert library["documents"] == []
    [listed] = library["trash"]["documents"]
    assert (listed["id"], listed["origin"]) == (document["id"], ["Drafts"])
    assert listed["trashed_at"] is not None

    # Nothing opens, edits, moves, copies or talks about it until it is put back.
    base = f"/api/library/documents/{document['id']}"
    refused = [
        await writer.get(f"{base}/content"),
        await writer.get(f"{base}/revisions"),
        await writer.get(f"{base}/conversations"),
        await writer.post(f"{base}/conversations", headers=CSRF_HEADERS),
        await writer.put(base, json={"name": "other.md"}, headers=CSRF_HEADERS),
        await _copy(writer, "documents", document["id"], None),
        await _trash(writer, "documents", document["id"]),
        await _insert(writer, document["id"], _image("PNG")),
    ]
    assert [response.status_code for response in refused] == [404] * len(refused)
    assert not directory.exists()

    # Its name is free meanwhile, and Put Back takes the next one.
    assert (await _new(writer, "plan.md", drafts["id"])).json()["name"] == "plan.md"
    restored = await _restore(writer, "documents", document["id"])
    assert restored.status_code == 200, restored.text
    assert (restored.json()["folder_id"], restored.json()["name"]) == (drafts["id"], "plan (2).md")
    assert (writer.root / "files" / "Drafts" / "plan (2).md" / "doc.md").read_text() == "# Plan\n"
    assert git.log(writer.root)[0].message == "put back files/Drafts/plan (2).md"
    assert not entry.exists()
    content = await writer.get(f"{base}/content")
    assert content.status_code == 200 and content.json()["markdown"] == "# Plan\n"
    assert (await _read(writer))["trash"] == {"folders": [], "documents": []}
    # Only what was trashed itself can be put back.
    assert (await _restore(writer, "documents", document["id"])).status_code == 404


async def test_a_trashed_folder_goes_with_everything_in_it(writer) -> None:
    papers = await _folder(writer, "Papers")
    year = await _folder(writer, "2026", papers["id"])
    deep = await _document(writer, "deep.md", year["id"], "# Deep\n")
    alone = await _document(writer, "alone.md", papers["id"], "# Alone\n")
    # Trashed on its own first: an entry of its own, which the folder's move leaves be.
    assert (await _trash(writer, "documents", alone["id"])).status_code == 200
    assert (await _trash(writer, "folders", papers["id"])).status_code == 200

    entry = trash_root(writer.root) / f"folder-{papers['id']}"
    assert not (writer.root / "files" / "Papers").exists()
    assert (entry / "2026" / "deep.md" / "doc.md").read_text() == "# Deep\n"
    library = await _read(writer)
    assert library["folders"] == [] and library["documents"] == []
    [folder] = library["trash"]["folders"]
    assert (folder["id"], folder["origin"], folder["items"]) == (papers["id"], [], 1)
    # Its origin is where Put Back returns it: its folder is in the Trash too.
    [document] = library["trash"]["documents"]
    assert (document["id"], document["origin"]) == (alone["id"], [])

    # What is inside a trashed folder is the folder's: not addressable on its own, and
    # nothing is made in it.
    refused = [
        await writer.get(f"/api/library/documents/{deep['id']}/content"),
        await writer.delete(f"/api/library/documents/{deep['id']}", headers=CSRF_HEADERS),
        await _restore(writer, "documents", deep["id"]),
        await _trash(writer, "folders", year["id"]),
        await _new(writer, "more.md", year["id"]),
        await writer.post(
            "/api/library/folders",
            json={"name": "More", "parent_id": papers["id"]},
            headers=CSRF_HEADERS,
        ),
        await _copy(writer, "documents", alone["id"], papers["id"]),
    ]
    assert [response.status_code for response in refused] == [404] * len(refused)

    # The file trashed on its own goes back to the nearest live place, the top level; the
    # folder comes back with everything it took.
    back = await _restore(writer, "documents", alone["id"])
    assert back.json()["folder_id"] is None
    assert (writer.root / "files" / "alone.md" / "doc.md").read_text() == "# Alone\n"
    assert (await _restore(writer, "folders", papers["id"])).status_code == 200
    assert (writer.root / "files" / "Papers" / "2026" / "deep.md" / "doc.md").exists()
    content = await writer.get(f"/api/library/documents/{deep['id']}/content")
    assert content.status_code == 200 and content.json()["markdown"] == "# Deep\n"
    assert list(trash_root(writer.root).iterdir()) == []


async def test_put_back_finds_the_nearest_live_folder(writer) -> None:
    top = await _folder(writer, "Top")
    middle = await _folder(writer, "Middle", top["id"])
    note = await _document(writer, "note.md", middle["id"], "# Note\n")
    assert (await _trash(writer, "documents", note["id"])).status_code == 200
    assert (await _trash(writer, "folders", middle["id"])).status_code == 200

    back = await _restore(writer, "documents", note["id"])
    assert back.status_code == 200, back.text
    assert back.json()["folder_id"] == top["id"]
    assert (writer.root / "files" / "Top" / "note.md" / "doc.md").read_text() == "# Note\n"


def test_an_entry_inside_a_trashed_folder_belongs_in_that_folders_entry() -> None:
    top = Folder(id=1, user_id=1, parent_id=None, name="Top")
    gone = Folder(id=2, user_id=1, parent_id=1, name="Gone", trashed_at=utcnow())
    inner = Folder(id=3, user_id=1, parent_id=2, name="Inner")
    by_id = {row.id: row for row in (top, gone, inner)}
    inside = Document(id=9, user_id=1, folder_id=3, name="note.md")
    assert trash_home(inside, by_id) == Path(".sightread/trash/folder-2/Inner/note.md")
    live = Document(id=10, user_id=1, folder_id=1, name="a.md")
    assert trash_home(live, by_id) == Path("files/Top/a.md")


async def test_a_trashed_file_is_deleted_for_good_with_its_entry(writer) -> None:
    document = (await _upload_markdown(writer, b"# Old\n", name="old.md")).json()
    await _write(writer, document["id"], "# Old, edited\n")
    assert (await _trash(writer, "documents", document["id"])).status_code == 200
    entry = trash_root(writer.root) / f"document-{document['id']}"
    assert entry.is_dir()

    deleted = await writer.delete(f"/api/library/documents/{document['id']}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204, deleted.text
    assert not entry.exists()
    assert original_of(writer.root, document["id"]) is None
    assert (await _read(writer))["trash"]["documents"] == []


async def test_deleting_a_trashed_folder_leaves_what_was_trashed_on_its_own(writer) -> None:
    archive = await _folder(writer, "Archive")
    kept = await _folder(writer, "Kept", archive["id"])
    await _document(writer, "inside.md", kept["id"], "# Inside\n")
    gone = (
        await _upload_markdown(writer, b"# Gone\n", name="gone.md", folder_id=archive["id"])
    ).json()
    assert original_of(writer.root, gone["id"]) is not None
    assert (await _trash(writer, "folders", kept["id"])).status_code == 200
    assert (await _trash(writer, "folders", archive["id"])).status_code == 200

    deleted = await writer.delete(f"/api/library/folders/{archive['id']}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204, deleted.text
    assert not (trash_root(writer.root) / f"folder-{archive['id']}").exists()
    assert original_of(writer.root, gone["id"]) is None
    async with writer.app.state.sessionmaker() as db:
        assert await db.get(Document, gone["id"]) is None
        assert await db.get(Folder, archive["id"]) is None
    # Kept went to the Trash on its own: it stays there, now under the top level.
    [folder] = (await _read(writer))["trash"]["folders"]
    assert (folder["id"], folder["parent_id"], folder["origin"]) == (kept["id"], None, [])
    assert (await _restore(writer, "folders", kept["id"])).status_code == 200
    assert (writer.root / "files" / "Kept" / "inside.md" / "doc.md").read_text() == "# Inside\n"


async def test_empty_trash_deletes_everything_in_it_for_good(writer) -> None:
    loose = (await _upload_markdown(writer, b"# Loose\n", name="loose.md")).json()
    box = await _folder(writer, "Box")
    boxed = (
        await _upload_markdown(writer, b"# Boxed\n", name="boxed.md", folder_id=box["id"])
    ).json()
    stays = await _document(writer, "stays.md", None, "# Stays\n")
    assert (await _trash(writer, "documents", loose["id"])).status_code == 200
    assert (await _trash(writer, "folders", box["id"])).status_code == 200

    emptied = await writer.delete("/api/library/trash", headers=CSRF_HEADERS)
    assert emptied.status_code == 204, emptied.text
    library = await _read(writer)
    assert library["trash"] == {"folders": [], "documents": []}
    assert [row["id"] for row in library["documents"]] == [stays["id"]]
    assert list(trash_root(writer.root).iterdir()) == []
    for row in (loose, boxed):
        assert original_of(writer.root, row["id"]) is None
    async with writer.app.state.sessionmaker() as db:
        assert await db.get(Document, boxed["id"]) is None
        assert await db.get(Folder, box["id"]) is None
    assert (writer.root / "files" / "stays.md" / "doc.md").read_text() == "# Stays\n"
    # An empty Trash empties to nothing.
    assert (await writer.delete("/api/library/trash", headers=CSRF_HEADERS)).status_code == 204


async def test_an_entry_an_interrupted_move_left_goes_back_before_anything_is_written(
    writer,
) -> None:
    """The process died between the move into the Trash, recorded, and the row's commit:
    the row is live and its directory waits in the Trash. The next read brings it back
    rather than writing a fresh copy beside the edits (docs/workspace.md § The Trash)."""
    document = await _document(writer, "draft.md", None, "# Edited\n")
    entry = trash_root(writer.root) / f"document-{document['id']}"
    entry.parent.mkdir(parents=True)
    (writer.root / "files" / "draft.md").rename(entry)
    git.commit_paths(writer.root, git.USER_AUTHOR, "trash files/draft.md", [Path("files/draft.md")])

    content = await writer.get(f"/api/library/documents/{document['id']}/content")
    assert content.status_code == 200, content.text
    assert content.json()["markdown"] == "# Edited\n"
    assert not entry.exists()
    assert git.log(writer.root)[0].message == "restore files/draft.md"


async def test_no_fresh_copy_stands_while_the_trash_keeps_the_files(writer, monkeypatch) -> None:
    document = await _document(writer, "draft.md", None, "# Edited\n")
    directory = writer.root / "files" / "draft.md"
    entry = trash_root(writer.root) / f"document-{document['id']}"
    entry.parent.mkdir(parents=True)
    directory.rename(entry)
    # The place cannot take it back, for whatever reason: the edits stay in the Trash, and
    # no baseline copy is written where they belong.
    monkeypatch.setattr(workspace_documents, "_return_entry", lambda root, name, home: False)

    content = await writer.get(f"/api/library/documents/{document['id']}/content")
    assert content.status_code == 409
    assert not directory.exists()
    assert (entry / "doc.md").read_text() == "# Edited\n"


async def test_an_entry_whose_row_is_gone_is_removed(writer) -> None:
    orphan = trash_root(writer.root) / "folder-424242"
    (orphan / "notes").mkdir(parents=True)
    (orphan / "notes" / "a.txt").write_text("left by a delete that died")
    unrelated = trash_root(writer.root) / "not-an-entry"
    unrelated.mkdir()

    # Every Trash operation runs the repair rule first.
    assert (await writer.delete("/api/library/trash", headers=CSRF_HEADERS)).status_code == 204
    assert not orphan.exists()
    assert unrelated.is_dir()


async def test_reconciliation_leaves_trashed_rows_in_the_trash(writer) -> None:
    document = await _document(writer, "kept.md", None, "# Kept\n")
    old = await _folder(writer, "Old")
    assert (await _trash(writer, "documents", document["id"])).status_code == 200
    assert (await _trash(writer, "folders", old["id"])).status_code == 200
    # The shell copies the trashed directory out, `.meta.json` and all, and makes a
    # directory under the trashed folder's name.
    shutil.copytree(
        trash_root(writer.root) / f"document-{document['id']}", writer.root / "files" / "copied"
    )
    (writer.root / "files" / "Old").mkdir()

    async with writer.app.state.sessionmaker() as db:
        await reconcile_user(db, writer.app.state.settings, writer.user_id)
    library = await _read(writer)
    [trashed] = library["trash"]["documents"]
    assert (trashed["id"], trashed["name"], trashed["folder_id"]) == (
        document["id"],
        "kept.md",
        None,
    )
    assert [row["id"] for row in library["trash"]["folders"]] == [old["id"]]
    # The shell's directory is a folder of its own.
    [made] = library["folders"]
    assert made["name"] == "Old" and made["id"] != old["id"]


async def test_a_move_to_the_trash_whose_commit_rolled_back_comes_back(writer, monkeypatch) -> None:
    document = await _document(writer, "draft.md", None, "# Draft\n")
    _fail_next_commit(monkeypatch, landed=False)
    with contextlib.suppress(Exception):
        await _trash(writer, "documents", document["id"])

    assert (writer.root / "files" / "draft.md" / "doc.md").read_text() == "# Draft\n"
    assert git.log(writer.root)[0].message == "restore files/draft.md"
    assert list(trash_root(writer.root).iterdir()) == []
    library = await _read(writer)
    assert [row["id"] for row in library["documents"]] == [document["id"]]
    assert library["trash"]["documents"] == []


async def test_a_move_to_the_trash_whose_commit_landed_stays_there(writer, monkeypatch) -> None:
    document = await _document(writer, "draft.md", None, "# Draft\n")
    _fail_next_commit(monkeypatch, landed=True)
    with contextlib.suppress(Exception):
        await _trash(writer, "documents", document["id"])

    assert not (writer.root / "files" / "draft.md").exists()
    entry = trash_root(writer.root) / f"document-{document['id']}"
    assert (entry / "doc.md").read_text() == "# Draft\n"
    assert [row["id"] for row in (await _read(writer))["trash"]["documents"]] == [document["id"]]


async def test_a_put_back_whose_commit_rolled_back_changes_nothing(writer, monkeypatch) -> None:
    document = await _document(writer, "draft.md", None, "# Draft\n")
    assert (await _trash(writer, "documents", document["id"])).status_code == 200
    _fail_next_commit(monkeypatch, landed=False)

    refused = await _restore(writer, "documents", document["id"])
    assert refused.status_code == 409
    assert "nothing was changed" in refused.json()["error"]["message"]
    entry = trash_root(writer.root) / f"document-{document['id']}"
    assert (entry / "doc.md").read_text() == "# Draft\n"
    assert not (writer.root / "files" / "draft.md").exists()
    assert [row["id"] for row in (await _read(writer))["trash"]["documents"]] == [document["id"]]


async def test_a_put_back_whose_commit_landed_brings_the_files(writer, monkeypatch) -> None:
    document = await _document(writer, "draft.md", None, "# Draft\n")
    assert (await _trash(writer, "documents", document["id"])).status_code == 200
    _fail_next_commit(monkeypatch, landed=True)

    restored = await _restore(writer, "documents", document["id"])
    assert restored.status_code == 200, restored.text
    assert restored.json()["name"] == "draft.md"
    assert (writer.root / "files" / "draft.md" / "doc.md").read_text() == "# Draft\n"


async def test_a_parse_that_ends_in_the_trash_keeps_its_original_outside_files(
    writer, tmp_path
) -> None:
    document_id, job_id = await _running_parse(writer, tmp_path / "upload.pdf")
    # No directory yet: the move to the Trash moves nothing.
    assert (await _trash(writer, "documents", document_id)).status_code == 200

    await runner._finish(
        writer.app.state.sessionmaker,
        job_id,
        status="succeeded",
        result=RESULT,
        settings=writer.app.state.settings,
    )
    assert original_of(writer.root, document_id).read_bytes() == b"%PDF-1.4"
    assert not (writer.root / "files" / "paper.pdf").exists()

    assert (await _restore(writer, "documents", document_id)).status_code == 200
    content = await writer.get(f"/api/library/documents/{document_id}/content")
    assert content.status_code == 200 and content.json()["markdown"] == "# Paper\n"
    assert (writer.root / "files" / "paper.pdf" / "source.pdf").read_bytes() == b"%PDF-1.4"


async def test_a_trashed_files_conversation_takes_no_message(writer) -> None:
    await _connect_kano(writer.app.state.sessionmaker, writer.user_id)
    chosen = await writer.put(
        "/api/agents/chat/settings", json={"model": "vendor/chat-model"}, headers=CSRF_HEADERS
    )
    assert chosen.status_code == 200, chosen.text
    document = await _document(writer, "draft.md", None, "# Draft\n")
    conversation = await writer.post(
        f"/api/library/documents/{document['id']}/conversations", headers=CSRF_HEADERS
    )
    assert conversation.status_code == 201, conversation.text
    url = f"/api/conversations/{conversation.json()['id']}/messages"
    assert (await _trash(writer, "documents", document["id"])).status_code == 200

    refused = await writer.post(url, json={"text": "Summarize it"}, headers=CSRF_HEADERS)
    assert refused.status_code == 409
    assert "Trash" in refused.json()["error"]["message"]
    assert (await _restore(writer, "documents", document["id"])).status_code == 200
    accepted = await writer.post(url, json={"text": "Summarize it"}, headers=CSRF_HEADERS)
    assert accepted.status_code == 202, accepted.text


# --- copies ---------------------------------------------------------------------------------


async def test_a_copied_document_has_files_of_its_own(writer) -> None:
    papers = await _folder(writer, "Papers")
    other = await _folder(writer, "Other")
    uploaded = await _upload_markdown(writer, b"# Draft\n", name="draft.md", folder_id=papers["id"])
    source = uploaded.json()
    await _write(writer, source["id"], "# Draft, edited\n")
    content = (await writer.get(f"/api/library/documents/{source['id']}/content")).json()
    noted = await writer.put(
        f"/api/library/documents/{source['id']}/notes",
        json={"text": "a note", "base_revision": content["notes_revision"]},
        headers=CSRF_HEADERS,
    )
    assert noted.status_code == 200, noted.text
    inserted = await _insert(writer, source["id"], _image("PNG"))
    assert inserted.status_code == 201, inserted.text
    image = inserted.json()["path"].removeprefix("figures/")
    talked = await writer.post(
        f"/api/library/documents/{source['id']}/conversations", headers=CSRF_HEADERS
    )
    assert talked.status_code == 201

    duplicate = await _copy(writer, "documents", source["id"], papers["id"])
    assert duplicate.status_code == 201, duplicate.text
    made = duplicate.json()
    assert (made["name"], made["folder_id"], made["kind"]) == (
        "draft (2).md",
        papers["id"],
        "markdown",
    )
    directory = writer.root / "files" / "Papers" / "draft (2).md"
    assert (directory / "doc.md").read_text() == "# Draft, edited\n"
    assert (directory / "notes.md").read_text() == "a note"
    assert json.loads((directory / ".meta.json").read_text())["document_id"] == made["id"]
    assert original_of(writer.root, made["id"]).read_bytes() == b"# Draft\n"
    assert (images_dir(writer.root) / str(made["id"]) / image).is_file()
    assert git.log(writer.root)[0].message == (
        "copy files/Papers/draft.md -> files/Papers/draft (2).md"
    )

    # Its own from here: an edit to the copy leaves the source as it was, the images come
    # into its `figures/` at its first read, and the conversations stay with the source.
    await _write(writer, made["id"], "# Copy\n")
    assert (directory / "figures" / image).is_file()
    source_content = await writer.get(f"/api/library/documents/{source['id']}/content")
    assert source_content.json()["markdown"] == "# Draft, edited\n"
    listed = await writer.get(f"/api/library/documents/{made['id']}/conversations")
    assert listed.json() == {"conversations": []}

    # Into another folder, the name is free.
    elsewhere = (await _copy(writer, "documents", source["id"], other["id"])).json()
    assert (elsewhere["name"], elsewhere["folder_id"]) == ("draft.md", other["id"])
    assert (writer.root / "files" / "Other" / "draft.md" / "doc.md").read_text() == (
        "# Draft, edited\n"
    )


async def test_a_copied_folder_mirrors_its_live_tree(writer) -> None:
    papers = await _folder(writer, "Papers")
    year = await _folder(writer, "2026", papers["id"])
    gone = await _folder(writer, "Gone", papers["id"])
    await _document(writer, "top.md", papers["id"], "# Top\n")
    await _document(writer, "deep.md", year["id"], "# Deep\n")
    await _document(writer, "hidden.md", gone["id"], "# Hidden\n")
    trashed = await _document(writer, "trashed.md", papers["id"], "# Trashed\n")
    (writer.root / "files" / "Papers" / "2026" / "stray.txt").write_text("from the shell")
    assert (await _trash(writer, "folders", gone["id"])).status_code == 200
    assert (await _trash(writer, "documents", trashed["id"])).status_code == 200

    copied = await _copy(writer, "folders", papers["id"], None)
    assert copied.status_code == 201, copied.text
    made = copied.json()
    assert (made["name"], made["parent_id"]) == ("Papers (2)", None)
    library = await _read(writer)
    children = {
        row["name"]: row["id"] for row in library["folders"] if row["parent_id"] == made["id"]
    }
    assert list(children) == ["2026"]
    placed = {
        row["name"]: row
        for row in library["documents"]
        if row["folder_id"] in (made["id"], children["2026"])
    }
    assert sorted(placed) == ["deep.md", "top.md"]
    tree = writer.root / "files" / "Papers (2)"
    assert (tree / "top.md" / "doc.md").read_text() == "# Top\n"
    assert (tree / "2026" / "deep.md" / "doc.md").read_text() == "# Deep\n"
    assert (tree / "2026" / "stray.txt").read_text() == "from the shell"
    meta = json.loads((tree / "2026" / "deep.md" / ".meta.json").read_text())
    assert meta["document_id"] == placed["deep.md"]["id"]
    assert not (tree / "Gone").exists() and not (tree / "trashed.md").exists()
    assert git.log(writer.root)[0].message == "copy files/Papers -> files/Papers (2)"

    # Never into itself, at any depth.
    for place in (papers["id"], year["id"]):
        assert (await _copy(writer, "folders", papers["id"], place)).status_code == 400


async def test_a_copy_of_a_running_parse_gets_its_files_when_the_parse_ends(
    writer, tmp_path
) -> None:
    upload = tmp_path / "upload.pdf"
    source_id, job_id = await _running_parse(writer, upload)

    copied = await _copy(writer, "documents", source_id, None)
    assert copied.status_code == 201, copied.text
    made = copied.json()
    # The parse is shared, not repeated.
    assert (made["name"], made["job_id"], made["status"]) == (
        "paper (2).pdf",
        str(job_id),
        "running",
    )
    assert not (writer.root / "files" / "paper (2).pdf").exists()

    await runner._finish(
        writer.app.state.sessionmaker,
        job_id,
        status="succeeded",
        result=RESULT,
        settings=writer.app.state.settings,
    )
    for name, document_id in (("paper.pdf", source_id), ("paper (2).pdf", made["id"])):
        assert original_of(writer.root, document_id).read_bytes() == b"%PDF-1.4"
        directory = writer.root / "files" / name
        assert (directory / "doc.md").read_text() == "# Paper\n"
        assert json.loads((directory / ".meta.json").read_text())["document_id"] == document_id
    assert not upload.exists()


async def test_a_copy_whose_commit_rolled_back_leaves_nothing_behind(writer, monkeypatch) -> None:
    source = (await _upload_markdown(writer, b"# Draft\n", name="draft.md")).json()
    await _write(writer, source["id"], "# Draft, edited\n")
    _fail_next_commit(monkeypatch, landed=False)

    refused = await _copy(writer, "documents", source["id"], None)
    assert refused.status_code == 409
    assert "nothing was copied" in refused.json()["error"]["message"]
    assert not (writer.root / "files" / "draft (2).md").exists()
    assert [path.name for path in sources_dir(writer.root).iterdir()] == [f"{source['id']}.md"]
    assert git.log(writer.root)[0].message == "remove files/draft (2).md"
    assert [row["name"] for row in (await _read(writer))["documents"]] == ["draft.md"]
    # The retry lands under the name the lost copy would have had.
    again = await _copy(writer, "documents", source["id"], None)
    assert again.status_code == 201 and again.json()["name"] == "draft (2).md"


async def test_a_copy_whose_commit_landed_despite_the_error_is_answered_as_made(
    writer, monkeypatch
) -> None:
    source = await _document(writer, "draft.md", None, "# Draft\n")
    _fail_next_commit(monkeypatch, landed=True)

    copied = await _copy(writer, "documents", source["id"], None)
    assert copied.status_code == 201, copied.text
    assert copied.json()["name"] == "draft (2).md"
    assert (writer.root / "files" / "draft (2).md" / "doc.md").read_text() == "# Draft\n"


async def test_a_copy_brings_back_files_an_interrupted_move_left_in_the_trash(writer) -> None:
    """The process died between the move into the Trash and the row's commit: the row is
    live and its edits wait in the Trash. A copy runs the repair rule first, so it copies
    the edits, not a baseline (docs/workspace.md § Copies)."""
    document = await _document(writer, "draft.md", None, "# Edited\n")
    entry = trash_root(writer.root) / f"document-{document['id']}"
    entry.parent.mkdir(parents=True)
    (writer.root / "files" / "draft.md").rename(entry)

    copied = await _copy(writer, "documents", document["id"], None)
    assert copied.status_code == 201, copied.text
    assert not entry.exists()
    assert (writer.root / "files" / "draft.md" / "doc.md").read_text() == "# Edited\n"
    assert (writer.root / "files" / "draft (2).md" / "doc.md").read_text() == "# Edited\n"


async def test_no_copy_is_made_while_the_trash_keeps_the_files(writer, monkeypatch) -> None:
    """The repair rule cannot bring the files back: the copy is refused, never made from
    the baseline as if the item had never been read — a document's own entry, a folder's
    above it, or an entry of anything inside a copied folder."""
    papers = await _folder(writer, "Papers")
    year = await _folder(writer, "2026", papers["id"])
    deep = await _document(writer, "deep.md", year["id"], "# Deep\n")
    monkeypatch.setattr(workspace_documents, "_return_entry", lambda root, name, home: False)
    entry = trash_root(writer.root) / f"document-{deep['id']}"
    entry.parent.mkdir(parents=True)
    (writer.root / "files" / "Papers" / "2026" / "deep.md").rename(entry)

    for kind, row_id in (("documents", deep["id"]), ("folders", papers["id"])):
        refused = await _copy(writer, kind, row_id, None)
        assert refused.status_code == 409, refused.text
        assert "could not be brought back from the Trash" in refused.json()["error"]["message"]

    entry.rename(writer.root / "files" / "Papers" / "2026" / "deep.md")
    stranded = trash_root(writer.root) / f"folder-{year['id']}"
    (writer.root / "files" / "Papers" / "2026").rename(stranded)
    refused = await _copy(writer, "documents", deep["id"], None)
    assert refused.status_code == 409, refused.text

    library = await _read(writer)
    assert [row["name"] for row in library["documents"]] == ["deep.md"]
    assert sorted(row["name"] for row in library["folders"]) == ["2026", "Papers"]
    assert (stranded / "deep.md" / "doc.md").read_text() == "# Deep\n"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/library/documents/new", {"name": "draft"}),
        ("/api/library/folders", {"name": "child"}),
    ],
)
async def test_a_destination_trashed_after_the_check_takes_no_new_entry(
    writer, monkeypatch, path, body
) -> None:
    """Another tab moves the destination to the Trash between a create's check and its
    insert. The insert decides again under the user's row lock, from a fresh read — the
    session still holds the folder as the check saw it — and refuses, rather than hiding
    the new item in the Trash at once (docs/database.md § Rules)."""
    from sqlalchemy import select, update

    from sightread.routes import library as routes

    inbox = await _folder(writer, "Inbox")
    checked = routes.live_folder

    async def check_then_trash(db, user_id, folder_id):
        row = await checked(db, user_id, folder_id)
        async with writer.app.state.sessionmaker() as other:
            await other.execute(
                update(Folder).where(Folder.id == folder_id).values(trashed_at=utcnow())
            )
            await other.commit()
        return row

    monkeypatch.setattr(routes, "live_folder", check_then_trash)
    key = "folder_id" if "documents" in path else "parent_id"
    response = await writer.post(path, json={**body, key: inbox["id"]}, headers=CSRF_HEADERS)
    assert response.status_code == 404, response.text

    async with writer.app.state.sessionmaker() as db:
        documents = (await db.execute(select(Document.id))).all()
        folders = (await db.execute(select(Folder.name))).scalars().all()
    assert (documents, folders) == ([], ["Inbox"])


async def test_the_status_route_counts_the_trash_on_its_own_line(writer) -> None:
    from sightread.routes.chat import _workspace_usage

    document = await _document(writer, "draft.md", None, "# Draft\n")
    assert (await _trash(writer, "documents", document["id"])).status_code == 200
    usage = _workspace_usage(writer.root, writer.app.state.settings.workspace_quota_bytes)
    lines = {line["path"]: line["bytes"] for line in usage["breakdown"]}
    assert lines["trash/"] > 0
