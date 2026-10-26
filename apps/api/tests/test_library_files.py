"""Files kept as they are (docs/workspace.md § Files): the Files page takes a file of any
kind, keeps the one copy in the tree, serves it back, converts a PDF or an image when the
reader asks, and follows what the shell does to it.

No upstream traffic: a conversion is enqueued and left queued (docs/testing.md § Cost
safety).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import Document, Folder, Job, User
from sightread.jobs import chat_tools, sweeper
from sightread.workspace import documents as workspace_documents
from sightread.workspace import git, safe_io
from sightread.workspace.documents import original_of
from sightread.workspace.layout import trash_root, user_root
from sightread.workspace.reconcile import reconcile_user
from tests.conftest import CSRF_HEADERS
from tests.test_library import _upload_markdown

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OCTET = "application/octet-stream"


@pytest.fixture
async def writer(make_client, tmp_path) -> AsyncClient:
    """Signed in, with a workspace of its own; the parse agent can enqueue."""
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


async def _upload(
    client: AsyncClient,
    name: str,
    data: bytes,
    media_type: str,
    folder_id: int | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/library/documents",
        files={"file": (name, data, media_type)},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )


async def _kept(client: AsyncClient, name: str, data: bytes, media_type: str) -> dict:
    response = await _upload(client, name, data, media_type)
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "file"
    return response.json()


async def _read(client: AsyncClient) -> dict:
    response = await client.get("/api/library")
    assert response.status_code == 200, response.text
    return response.json()


async def _switch(client: AsyncClient, on: bool) -> None:
    saved = await client.put(
        "/api/agents/parse/settings", json={"auto_convert": on}, headers=CSRF_HEADERS
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["auto_convert"] is on


async def _convert(client: AsyncClient, document_id: int, **body) -> httpx.Response:
    return await client.post(
        f"/api/library/documents/{document_id}/convert", json=body, headers=CSRF_HEADERS
    )


def _multipart(name: str, media_type: str, chunks: list[bytes]) -> tuple[dict, object]:
    """A body sent chunked, with no `Content-Length` to judge up front."""
    boundary = "emapapertestboundary"

    async def body():
        yield (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode()
        for chunk in chunks:
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    return {**CSRF_HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"}, body()


# --- the upload ---------------------------------------------------------------------------


async def test_a_file_of_any_kind_is_kept_once_in_the_tree(writer) -> None:
    work = await _folder(writer, "Work")
    data = b"PK\x03\x04" + b"\0" * 64 + b"report"
    created = await _upload(writer, "report.docx", data, DOCX, work["id"])
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["kind"], body["media_type"], body["size_bytes"], body["storage_bytes"]) == (
        "file",
        DOCX,
        len(data),
        len(data),
    )
    assert (body["job_id"], body["status"], body["folder_id"]) == (None, None, work["id"])
    path = writer.root / "files" / "Work" / "report.docx"
    assert path.is_file() and path.read_bytes() == data
    # The one copy: no original outside the tree, no metadata beside it, nothing in flight
    # left behind.
    assert [entry.name for entry in (writer.root / "files" / "Work").iterdir()] == ["report.docx"]
    assert [entry.name for entry in (writer.root / "files").iterdir()] == ["Work"]
    assert original_of(writer.root, body["id"]) is None
    listed = (await _read(writer))["documents"]
    assert [(row["id"], row["kind"], row["media_type"], row["size_bytes"]) for row in listed] == [
        (body["id"], "file", DOCX, len(data))
    ]
    # A document's routes are not a file's.
    base = f"/api/library/documents/{body['id']}"
    refused = [
        await writer.get(f"{base}/content"),
        await writer.get(f"{base}/revisions"),
        await writer.get(f"{base}/result"),
        await writer.get(f"{base}/conversations"),
        await writer.post(f"{base}/conversations", headers=CSRF_HEADERS),
    ]
    assert [response.status_code for response in refused] == [404] * len(refused)
    assert path.read_bytes() == data


async def test_a_text_file_is_a_revision_and_a_binary_one_is_not(writer) -> None:
    await _kept(writer, "notes.txt", b"alpha\nbeta\n", "text/plain")
    top = git.log(writer.root)[0]
    assert top.message == "upload files/notes.txt"
    assert top.files == ("files/notes.txt",)
    await _kept(writer, "blob.bin", b"\0\1\2\3", OCTET)
    # Nothing to record: history ends where it was.
    assert git.log(writer.root)[0].commit == top.commit
    assert (writer.root / "files" / "blob.bin").read_bytes() == b"\0\1\2\3"


async def test_the_type_comes_from_the_client_or_the_name(writer) -> None:
    declared = await _kept(writer, "data.bin", b"abc", "text/csv")
    assert declared["media_type"] == "text/csv"
    guessed = await _kept(writer, "table.csv", b"a,b\n", OCTET)
    assert guessed["media_type"] == "text/csv"
    unknown = await _kept(writer, "mystery", b"?", "")
    assert unknown["media_type"] == OCTET


# --- the file route -----------------------------------------------------------------------


async def test_a_file_is_served_under_its_own_policy_and_by_range(writer) -> None:
    data = b"0123456789abcdef"
    created = await _kept(writer, "筆記.txt", data, "text/plain")
    url = f"/api/library/documents/{created['id']}/file"
    whole = await writer.get(url)
    assert whole.status_code == 200 and whole.content == data
    assert whole.headers["content-type"].startswith("text/plain")
    assert whole.headers["content-disposition"] == (
        "inline; filename=\"__.txt\"; filename*=UTF-8''%E7%AD%86%E8%A8%98.txt"
    )
    assert whole.headers["content-security-policy"] == "sandbox"
    assert whole.headers["x-frame-options"] == "DENY"
    assert whole.headers["x-content-type-options"] == "nosniff"
    assert whole.headers["cache-control"] == "private, no-store"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-length"] == str(len(data))

    download = await writer.get(url, params={"download": "1"})
    assert download.status_code == 200 and download.content == data
    assert download.headers["content-disposition"].startswith("attachment;")

    part = await writer.get(url, headers={"Range": "bytes=2-5"})
    assert (part.status_code, part.content) == (206, b"2345")
    assert part.headers["content-range"] == f"bytes 2-5/{len(data)}"
    assert part.headers["content-length"] == "4"
    tail = await writer.get(url, headers={"Range": "bytes=-3"})
    assert (tail.status_code, tail.content) == (206, b"def")
    open_ended = await writer.get(url, headers={"Range": "bytes=12-"})
    assert (open_ended.status_code, open_ended.content) == (206, b"cdef")
    past = await writer.get(url, headers={"Range": f"bytes={len(data)}-"})
    assert past.status_code == 416
    assert past.headers["content-range"] == f"bytes */{len(data)}"
    # A range the route does not serve is answered whole, as the specification allows.
    several = await writer.get(url, headers={"Range": "bytes=0-1, 4-5"})
    assert (several.status_code, several.content) == (200, data)

    # What a browser would run, or cannot show on its own, goes as an attachment.
    svg = await _kept(
        writer, "drawing.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"
    )
    shown = await writer.get(f"/api/library/documents/{svg['id']}/file")
    assert shown.headers["content-type"].startswith("image/svg+xml")
    assert shown.headers["content-disposition"].startswith("attachment;")
    zipped = await _kept(writer, "archive.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip")
    packed = await writer.get(f"/api/library/documents/{zipped['id']}/file")
    assert packed.headers["content-disposition"].startswith("attachment;")
    assert packed.headers["content-security-policy"] == "sandbox"


async def test_the_file_route_is_the_owners_and_a_documents_is_not_a_file(
    writer, make_client
) -> None:
    created = await _kept(writer, "notes.txt", b"mine", "text/plain")
    markdown = (await _upload_markdown(writer)).json()
    assert (await writer.get(f"/api/library/documents/{markdown['id']}/file")).status_code == 404
    stranger = make_client(workspaces_dir=writer.app.state.settings.workspaces_dir)
    async with writer.app.state.sessionmaker() as db:
        other = User(google_sub="stranger", email="stranger@example.com")
        db.add(other)
        await db.flush()
        token = await create_session(db, other)
        await db.commit()
    stranger.cookies.set(SESSION_COOKIE, token)
    assert (await stranger.get(f"/api/library/documents/{created['id']}/file")).status_code == 404
    signed_out = make_client(workspaces_dir=writer.app.state.settings.workspaces_dir)
    assert (await signed_out.get(f"/api/library/documents/{created['id']}/file")).status_code == 401


# --- the convert switch -------------------------------------------------------------------


async def test_the_switch_keeps_a_pdf_as_a_file_and_this_origin_may_frame_it(
    writer, documents
) -> None:
    # On until the reader turns it off, and `GET /api/me` says which, so the Files page
    # applies the cap the server will (docs/web.md § Files).
    assert (await writer.get("/api/me")).json()["auto_convert"] is True
    await _switch(writer, False)
    assert (await writer.get("/api/me")).json()["auto_convert"] is False
    pdf = documents["text_pdf"].read_bytes()
    kept = await _kept(writer, "paper.pdf", pdf, "application/pdf")
    assert (kept["media_type"], kept["job_id"], kept["size_bytes"]) == (
        "application/pdf",
        None,
        len(pdf),
    )
    assert (writer.root / "files" / "paper.pdf").read_bytes() == pdf
    async with writer.app.state.sessionmaker() as db:
        assert (await db.execute(select(Job))).scalars().all() == []
    url = f"/api/library/documents/{kept['id']}/file"
    shown = await writer.get(url)
    assert shown.status_code == 200 and shown.content == pdf
    assert shown.headers["content-type"] == "application/pdf"
    assert shown.headers["content-disposition"].startswith("inline;")
    # A browser's PDF viewer refuses a sandboxed frame: that one answer may be framed by
    # this origin, and by no other.
    assert shown.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert shown.headers["x-frame-options"] == "SAMEORIGIN"
    saved = await writer.get(url, params={"download": "1"})
    assert saved.headers["content-disposition"].startswith("attachment;")
    assert (saved.headers["content-security-policy"], saved.headers["x-frame-options"]) == (
        "sandbox",
        "DENY",
    )

    # The switch is the parse agent's alone, and `GET /api/agents` reports it.
    agents = {agent["name"]: agent for agent in (await writer.get("/api/agents")).json()["agents"]}
    assert agents["parse"]["settings"]["auto_convert"] is False
    for agent in ("chat", "check"):
        refused = await writer.put(
            f"/api/agents/{agent}/settings", json={"auto_convert": False}, headers=CSRF_HEADERS
        )
        assert refused.status_code == 400, agent

    # Back on, the same upload is a parse — under the next free name, since the file
    # holds this one (docs/workspace.md § Names).
    await _switch(writer, True)
    assert (await writer.get("/api/me")).json()["auto_convert"] is True
    converted = await _upload(writer, "paper.pdf", pdf, "application/pdf")
    assert converted.status_code == 201, converted.text
    assert (converted.json()["kind"], converted.json()["name"]) == ("pdf", "paper (2).pdf")
    assert converted.json()["status"] == "queued"


async def test_a_kept_image_is_converted_into_a_document_on_request(writer, documents) -> None:
    await _switch(writer, False)
    png = documents["png"].read_bytes()
    kept = await _kept(writer, "tiny.png", png, "image/png")
    converted = await _convert(writer, kept["id"])
    assert converted.status_code == 200, converted.text
    body = converted.json()
    assert (body["id"], body["name"], body["kind"], body["status"]) == (
        kept["id"],
        "tiny.png",
        "image",
        "queued",
    )
    assert body["job_id"] is not None and body["media_type"] is None
    # The file left the tree as the job's source; the row is the parse's document now,
    # with the id and the place it had.
    assert not (writer.root / "files" / "tiny.png").exists()
    assert [entry.name for entry in (writer.root / "files").iterdir()] == []
    upload = Path(writer.app.state.settings.upload_dir) / f"{body['job_id']}.png"
    assert upload.read_bytes() == png
    async with writer.app.state.sessionmaker() as db:
        row = await db.get(Document, kept["id"])
        job = await db.get(Job, uuid.UUID(body["job_id"]))
    assert (row.kind, row.job_id, row.media_type, row.size_bytes) == ("parse", job.id, None, None)
    assert (job.status, job.filename, job.retain_source) == ("queued", "tiny.png", True)
    assert job.check_model is not None  # a Files upload's check comes with the conversion
    assert (await writer.get(f"/api/library/documents/{kept['id']}/file")).status_code == 404
    assert (await _convert(writer, kept["id"])).status_code == 404
    listed = (await _read(writer))["documents"]
    assert [(row["id"], row["kind"], row["status"]) for row in listed] == [
        (kept["id"], "image", "queued")
    ]


async def test_only_a_pdf_or_an_image_converts(writer) -> None:
    zipped = await _kept(writer, "archive.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip")
    refused = await _convert(writer, zipped["id"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["type"] == "not_convertible"
    assert (writer.root / "files" / "archive.zip").is_file()
    markdown = (await _upload_markdown(writer)).json()
    assert (await _convert(writer, markdown["id"])).status_code == 404
    # A file in the Trash is not there to convert.
    trashed = await writer.post(
        f"/api/library/documents/{zipped['id']}/trash", headers=CSRF_HEADERS
    )
    assert trashed.status_code == 200, trashed.text
    assert (await _convert(writer, zipped["id"])).status_code == 404


async def test_a_conversion_names_only_the_parse_agents_models(writer, documents) -> None:
    await _switch(writer, False)
    kept = await _kept(writer, "tiny.png", documents["png"].read_bytes(), "image/png")
    refused = await _convert(writer, kept["id"], model="vendor/chat-model")
    assert refused.status_code == 400, refused.text
    # Refused whole: the file is where it was, the row still a file.
    assert (writer.root / "files" / "tiny.png").is_file()
    assert (await writer.get(f"/api/library/documents/{kept['id']}/file")).status_code == 200
    async with writer.app.state.sessionmaker() as db:
        assert (await db.get(Document, kept["id"])).kind == "file"
        assert (await db.execute(select(Job))).scalars().all() == []


# --- the caps -----------------------------------------------------------------------------


async def test_a_file_the_plan_cannot_hold_is_refused_before_or_after_its_bytes(
    make_client, tmp_path
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), free_storage_bytes=64)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    root = user_root(client.app.state.settings.workspaces_dir, user_id)
    # Declared up front: refused before a byte is written.
    refused = await _upload(client, "big.bin", b"x" * 100, OCTET)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["type"] == "storage_full"
    assert not (root / "files").exists() or list((root / "files").iterdir()) == []
    # Declared nowhere: the bytes land in the tree, are counted with it, and go again.
    headers, body = _multipart("big.bin", OCTET, [b"x" * 100])
    streamed = await client.post("/api/library/documents", content=body, headers=headers)
    assert streamed.status_code == 409, streamed.text
    assert streamed.json()["error"]["type"] == "storage_full"
    assert list((root / "files").iterdir()) == []
    async with client.app.state.sessionmaker() as db:
        assert (await db.execute(select(Document))).scalars().all() == []


async def test_a_file_past_the_cap_is_refused_while_it_streams(
    make_client, tmp_path, documents
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), file_max_bytes=1024)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    root = user_root(client.app.state.settings.workspaces_dir, user_id)
    limits = (await client.get("/api/me")).json()["limits"]
    assert limits["file_max_bytes"] == 1024

    declared = await _upload(client, "big.bin", b"x" * 4096, OCTET)
    assert declared.status_code == 413, declared.text
    assert "FILE_MAX_BYTES" in declared.json()["error"]["message"]
    headers, body = _multipart("big.bin", OCTET, [b"x" * 512] * 8)
    streamed = await client.post("/api/library/documents", content=body, headers=headers)
    assert streamed.status_code == 413, streamed.text
    assert "FILE_MAX_BYTES" in streamed.json()["error"]["message"]
    # Nothing in flight stays: no temporary file, no row.
    assert list((root / "files").iterdir()) == []
    async with client.app.state.sessionmaker() as db:
        assert (await db.execute(select(Document))).scalars().all() == []
    # Under the cap, the same route keeps the file.
    kept = await _upload(client, "small.bin", b"x" * 1024, OCTET)
    assert kept.status_code == 201, kept.text
    assert (root / "files" / "small.bin").stat().st_size == 1024
    # The caps are independent: a PDF converted on arrival is judged by its own, larger
    # cap, never by the file's, however a deployment orders them.
    pdf = documents["text_pdf"].read_bytes()
    assert len(pdf) > 1024
    parsed = await _upload(client, "paper.pdf", pdf, "application/pdf")
    assert parsed.status_code == 201, parsed.text
    assert parsed.json()["kind"] == "pdf"


async def test_a_body_cut_off_or_with_headers_too_long_is_refused(writer) -> None:
    boundary = "emapapertestboundary"
    headers = {**CSRF_HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"}

    async def cut_off():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="cut.txt"\r\n'
            "Content-Type: text/plain\r\n\r\nhello, wor"
        ).encode()

    # The stream ends inside the file part, no closing boundary: a part of the file is
    # not the library's one copy of it.
    refused = await writer.post("/api/library/documents", content=cut_off(), headers=headers)
    assert refused.status_code == 400, refused.text
    assert list((writer.root / "files").iterdir()) == []
    async with writer.app.state.sessionmaker() as db:
        assert (await db.execute(select(Document))).scalars().all() == []

    def padded(extra: str):
        async def body():
            yield (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="padded.txt"\r\n'
                f"{extra}\r\n"
                "Content-Type: text/plain\r\n\r\nhello\r\n"
                f"--{boundary}--\r\n"
            ).encode()

        return body()

    # A part's headers are bounded before any is kept whole: one line past the parser's
    # own limit, and six under it that together pass the part's budget.
    one_long = "X-Pad: " + "p" * 8192
    many = "\r\n".join(f"X-Pad-{n}: " + "p" * 4000 for n in range(6))
    for extra in (one_long, many):
        refused = await writer.post(
            "/api/library/documents", content=padded(extra), headers=headers
        )
        assert refused.status_code == 400, refused.text
    assert list((writer.root / "files").iterdir()) == []


async def test_an_upload_without_a_file_part_or_with_two_is_refused(writer) -> None:
    no_file = await writer.post(
        "/api/library/documents",
        data={"folder_id": ""},
        files={"other": ("a.txt", b"x")},
        headers=CSRF_HEADERS,
    )
    assert no_file.status_code == 400, no_file.text
    two = await writer.post(
        "/api/library/documents",
        files=[("file", ("a.txt", b"x", "text/plain")), ("file", ("b.txt", b"y", "text/plain"))],
        headers=CSRF_HEADERS,
    )
    assert two.status_code == 400, two.text
    assert list((writer.root / "files").iterdir()) == []


# --- the library around a file ------------------------------------------------------------


async def test_a_file_is_renamed_moved_copied_trashed_and_put_back(writer) -> None:
    drafts = await _folder(writer, "Drafts")
    archive = await _folder(writer, "Archive")
    data = b"alpha\nbeta\n"
    created = (await _upload(writer, "notes.txt", data, "text/plain", drafts["id"])).json()
    base = f"/api/library/documents/{created['id']}"

    renamed = await writer.put(base, json={"name": "todo.txt"}, headers=CSRF_HEADERS)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "todo.txt"
    assert (writer.root / "files" / "Drafts" / "todo.txt").read_bytes() == data
    assert not (writer.root / "files" / "Drafts" / "notes.txt").exists()
    moved = await writer.put(base, json={"folder_id": archive["id"]}, headers=CSRF_HEADERS)
    assert moved.status_code == 200, moved.text
    assert (writer.root / "files" / "Archive" / "todo.txt").read_bytes() == data
    assert (await writer.get(f"{base}/file")).content == data

    copied = await writer.post(
        f"{base}/copy", json={"folder_id": archive["id"]}, headers=CSRF_HEADERS
    )
    assert copied.status_code == 201, copied.text
    made = copied.json()
    assert (made["name"], made["kind"], made["media_type"], made["size_bytes"]) == (
        "todo (2).txt",
        "file",
        "text/plain",
        len(data),
    )
    assert (writer.root / "files" / "Archive" / "todo (2).txt").read_bytes() == data
    assert git.log(writer.root)[0].message == (
        "copy files/Archive/todo.txt -> files/Archive/todo (2).txt"
    )
    assert (await writer.get(f"/api/library/documents/{made['id']}/file")).content == data

    trashed = await writer.post(f"{base}/trash", headers=CSRF_HEADERS)
    assert trashed.status_code == 200, trashed.text
    entry = trash_root(writer.root) / f"document-{created['id']}"
    assert entry.is_file() and entry.read_bytes() == data
    assert not (writer.root / "files" / "Archive" / "todo.txt").exists()
    assert (await writer.get(f"{base}/file")).status_code == 404
    library = await _read(writer)
    assert [row["id"] for row in library["trash"]["documents"]] == [created["id"]]
    assert [row["id"] for row in library["documents"]] == [made["id"]]

    restored = await writer.post(f"{base}/restore", headers=CSRF_HEADERS)
    assert restored.status_code == 200, restored.text
    assert (writer.root / "files" / "Archive" / "todo.txt").read_bytes() == data
    assert not entry.exists()
    assert (await writer.get(f"{base}/file")).content == data

    # Deleted for good from the Trash: the one copy is gone.
    assert (await writer.post(f"{base}/trash", headers=CSRF_HEADERS)).status_code == 200
    deleted = await writer.delete(base, headers=CSRF_HEADERS)
    assert deleted.status_code == 204, deleted.text
    assert not entry.exists()
    assert [row["id"] for row in (await _read(writer))["documents"]] == [made["id"]]


async def test_reconciliation_follows_a_file_the_shell_made_moved_and_removed(writer) -> None:
    settings = writer.app.state.settings
    git.ensure_repo(writer.root)
    shell = writer.root / "files" / "Shell"
    shell.mkdir(parents=True)
    (shell / "data.csv").write_bytes(b"a,b\n1,2\n")
    (writer.root / "files" / ".env").write_text("SECRET=x\n")
    (writer.root / "files" / f"{safe_io.TEMP_PREFIX}deadbeef").write_bytes(b"half")
    git.commit_all(writer.root, "agent:1", "shell writes")

    async def rows() -> dict[str, Document]:
        async with writer.app.state.sessionmaker() as db:
            await reconcile_user(db, settings, writer.user_id)
            found = (
                await db.execute(select(Document).where(Document.user_id == writer.user_id))
            ).scalars()
            return {row.name: row for row in found}

    found = await rows()
    async with writer.app.state.sessionmaker() as db:
        folders = {
            row.name: row
            for row in (
                await db.execute(select(Folder).where(Folder.user_id == writer.user_id))
            ).scalars()
        }
    assert set(found) == {"data.csv", ".env"}
    csv = found["data.csv"]
    assert (csv.kind, csv.media_type, csv.size_bytes, csv.folder_id) == (
        "file",
        "text/csv",
        8,
        folders["Shell"].id,
    )
    assert (found[".env"].kind, found[".env"].folder_id) == ("file", None)
    # The shell's name is made canonical on disk, as a directory's is; a write in
    # progress is left alone and gets no row.
    assert (writer.root / "files" / "_.env").is_file()
    assert not (writer.root / "files" / ".env").exists()
    assert (writer.root / "files" / f"{safe_io.TEMP_PREFIX}deadbeef").is_file()
    listed = {row["name"]: row for row in (await _read(writer))["documents"]}
    assert (listed["data.csv"]["kind"], listed["data.csv"]["size_bytes"]) == ("file", 8)
    served = await writer.get(f"/api/library/documents/{csv.id}/file")
    assert served.status_code == 200 and served.content == b"a,b\n1,2\n"

    # Moved by the shell: a file carries no id, so it is a new entry and the old one is
    # gone; grown: its size follows.
    (shell / "data.csv").rename(writer.root / "files" / "moved.csv")
    (writer.root / "files" / "_.env").write_text("SECRET=xy\n")
    found = await rows()
    assert set(found) == {"moved.csv", ".env"}
    assert found[".env"].size_bytes == 10
    assert (await writer.get(f"/api/library/documents/{csv.id}/file")).status_code == 404

    # Removed by the shell: the row goes with it — the one deletion reconciliation makes.
    (writer.root / "files" / "moved.csv").unlink()
    assert set(await rows()) == {".env"}


async def test_reconciliation_puts_back_a_file_a_crashed_delete_left_aside(writer) -> None:
    kept = await _kept(writer, "notes.txt", b"keep me\n", "text/plain")
    settings = writer.app.state.settings
    # The delete's first half alone — the file aside with its origin, the row untouched —
    # is what a process that dies before the rows' commit leaves.
    removal = await workspace_documents.remove_path(
        settings, writer.user_id, Path("files/notes.txt")
    )
    assert removal is not None
    assert not (writer.root / "files" / "notes.txt").exists()
    async with writer.app.state.sessionmaker() as db:
        await reconcile_user(db, settings, writer.user_id)
    # Back where its row says, and the row kept: the tree is read again after a restore.
    assert (writer.root / "files" / "notes.txt").read_bytes() == b"keep me\n"
    assert [name for name in os.listdir(writer.root) if name.startswith(safe_io.TEMP_PREFIX)] == []
    rows = (await _read(writer))["documents"]
    assert [(row["id"], row["kind"], row["size_bytes"]) for row in rows] == [
        (kept["id"], "file", 8)
    ]


async def test_reconciliation_sweeps_the_file_a_committed_conversion_left_aside(
    writer, documents
) -> None:
    await _switch(writer, False)
    png = documents["png"].read_bytes()
    kept = await _kept(writer, "tiny.png", png, "image/png")
    assert (await _convert(writer, kept["id"])).status_code == 200
    settings = writer.app.state.settings
    # The conversion's aside as a process that died after its commit leaves it: the file
    # set aside with its origin, and a parse row standing at that path now.
    (writer.root / "files" / "tiny.png").write_bytes(png)
    removal = await workspace_documents.remove_path(
        settings, writer.user_id, Path("files/tiny.png"), message="convert files/tiny.png"
    )
    assert removal is not None
    async with writer.app.state.sessionmaker() as db:
        await reconcile_user(db, settings, writer.user_id)
    # Swept, not put back over the document's place.
    assert not (writer.root / "files" / "tiny.png").exists()
    assert [name for name in os.listdir(writer.root) if name.startswith(safe_io.TEMP_PREFIX)] == []
    rows = (await _read(writer))["documents"]
    assert [(row["id"], row["kind"]) for row in rows] == [(kept["id"], "image")]


async def test_reconciliation_leaves_a_trashed_file_in_the_trash(writer) -> None:
    created = await _kept(writer, "notes.txt", b"kept", "text/plain")
    base = f"/api/library/documents/{created['id']}"
    assert (await writer.post(f"{base}/trash", headers=CSRF_HEADERS)).status_code == 200
    async with writer.app.state.sessionmaker() as db:
        await reconcile_user(db, writer.app.state.settings, writer.user_id)
        row = await db.get(Document, created["id"])
    assert row.trashed_at is not None and row.kind == "file"
    assert (trash_root(writer.root) / f"document-{created['id']}").read_bytes() == b"kept"
    assert (await writer.post(f"{base}/restore", headers=CSRF_HEADERS)).status_code == 200
    assert (writer.root / "files" / "notes.txt").read_bytes() == b"kept"


async def test_a_file_the_trash_holds_keeps_its_row(writer) -> None:
    """An interrupted Put Back whose place was taken meanwhile: the rows are live, the
    bytes wait in the Trash, and the repair rule cannot return them. The rows are not
    deleted as missing — the rule would then remove the bytes as nobody's."""
    settings = writer.app.state.settings
    shell = await _folder(writer, "Shell")
    kept = await _kept(writer, "notes.txt", b"kept", "text/plain")
    inside = await _upload(writer, "data.csv", b"a,b\n", "text/csv", shell["id"])
    assert inside.status_code == 201, inside.text
    trash = trash_root(writer.root)
    trash.mkdir(parents=True, exist_ok=True)
    (writer.root / "files" / "notes.txt").rename(trash / f"document-{kept['id']}")
    (writer.root / "files" / "notes.txt").mkdir()
    (writer.root / "files" / "Shell").rename(trash / f"folder-{shell['id']}")
    (writer.root / "files" / "Shell").mkdir()

    async def reconciled() -> list[tuple[int, str, int]]:
        async with writer.app.state.sessionmaker() as db:
            await reconcile_user(db, settings, writer.user_id)
        rows = (await _read(writer))["documents"]
        return sorted((row["id"], row["name"], row["size_bytes"]) for row in rows)

    expected = sorted([(kept["id"], "notes.txt", 4), (inside.json()["id"], "data.csv", 4)])
    assert await reconciled() == expected
    assert (trash / f"document-{kept['id']}").read_bytes() == b"kept"
    assert (trash / f"folder-{shell['id']}" / "data.csv").read_bytes() == b"a,b\n"
    # The places freed: the rule brings both back, to the rows they kept.
    (writer.root / "files" / "notes.txt").rmdir()
    (writer.root / "files" / "Shell").rmdir()
    assert await reconciled() == expected
    assert (writer.root / "files" / "notes.txt").read_bytes() == b"kept"
    assert (writer.root / "files" / "Shell" / "data.csv").read_bytes() == b"a,b\n"
    assert list(trash.iterdir()) == []


async def test_the_staging_file_of_an_upload_the_process_died_in_is_swept(
    make_client, tmp_path
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), free_storage_bytes=1_000_000)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    settings = client.app.state.settings
    root = user_root(settings.workspaces_dir, user_id)
    (root / "files").mkdir(parents=True)
    prefix = workspace_documents.UPLOAD_STAGING_PREFIX
    # What a process that died mid-stream leaves: a staging file nobody holds, counted
    # against the plan — here, more than all of it.
    dead = root / "files" / f"{prefix}{'0' * 16}"
    dead.write_bytes(b"x" * 2_000_000)
    # One an upload still streams into holds its lock, as the route holds it.
    fd, live = workspace_documents.stage_upload(root)
    assert live.parent == root / "files" and live.name.startswith(prefix)
    os.write(fd, b"half")
    try:
        # The next upload sweeps the dead one first and fits; the live one stays.
        kept = await _upload(client, "small.bin", b"x" * 8, OCTET)
        assert kept.status_code == 201, kept.text
        assert not dead.exists() and live.is_file()
        dead.write_bytes(b"x" * 16)
        async with client.app.state.sessionmaker() as db:
            await reconcile_user(db, settings, user_id)
        assert not dead.exists() and live.is_file()
        dead.write_bytes(b"x" * 16)
        assert await sweeper.sweep_staged_uploads(Path(settings.workspaces_dir)) == 1
        assert not dead.exists() and live.is_file()
    finally:
        os.close(fd)
    # Its holder gone, the lock went with it: the next sweep takes the file.
    assert await sweeper.sweep_staged_uploads(Path(settings.workspaces_dir)) == 1
    assert sorted(path.name for path in (root / "files").iterdir()) == ["small.bin"]


# --- revisions and the editor -------------------------------------------------------------


def test_history_records_text_files_within_the_cap_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(git, "revision_max_file_bytes", lambda: 64)
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    git.ensure_repo(root)
    (root / "files" / "small.txt").write_text("small\n")
    (root / "files" / "blob.bin").write_bytes(b"\0" * 8)
    (root / "files" / "large.txt").write_text("x" * 65)
    (root / "files" / "d" / "doc.md").write_text("x" * 65)
    assert git.commit_all(root, "user", "shell") is not None
    # A document's own file is recorded whatever it holds; the rest only when text and
    # within the cap.
    assert sorted(git.log(root)[0].files) == ["files/d/doc.md", "files/small.txt"]
    # A change to a file history does not record is no revision.
    (root / "files" / "blob.bin").write_bytes(b"\0" * 9)
    (root / "files" / "large.txt").write_text("y" * 65)
    assert git.commit_all(root, "user", "shell again") is None
    (root / "files" / "small.txt").write_text("edited\n")
    assert git.log(root, limit=1)[0].message == "shell"
    assert git.commit_all(root, "user", "edit") is not None
    assert git.log(root)[0].files == ("files/small.txt",)


async def test_the_editor_writes_nothing_a_revision_could_not_record(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(git, "revision_max_file_bytes", lambda: 64)
    root = tmp_path / "ws" / "1"
    (root / "files" / "d").mkdir(parents=True)
    git.ensure_repo(root)

    async def run(command: str, path: str, **fields):
        return await chat_tools.run_editor(
            root,
            {"command": command, "path": f"/workspace/{path}", **fields},
            "agent:1",
            max_chars=1000,
            max_bytes=1_000_000,
        )

    refused = await run("create", "files/big.txt", file_text="x" * 65)
    assert refused.is_error and "revision" in refused.content
    assert not (root / "files" / "big.txt").exists()
    allowed = await run("create", "files/small.txt", file_text="x" * 10)
    assert not allowed.is_error, allowed.content
    assert allowed.edit is not None and allowed.edit["commit"]
    # A document's own file is recorded whatever its size.
    own = await run("create", "files/d/doc.md", file_text="x" * 65)
    assert not own.is_error, own.content
    assert (root / "files" / "d" / "doc.md").read_text() == "x" * 65
    # A file history never recorded — too large when it arrived — is not edited here
    # either, even into a size a revision would take: that revision would read as its
    # creation, and Revert would delete it.
    (root / "files" / "huge.txt").write_text("x" * 100)
    shrunk = await run("str_replace", "files/huge.txt", old_str="x" * 50, new_str="")
    assert shrunk.is_error and "revision" in shrunk.content
    assert (root / "files" / "huge.txt").read_text() == "x" * 100
    # Binary by git's test, text to the decoder — a NUL byte: neither the file nor a
    # result that holds one is written, for the same reason.
    mixed = root / "files" / "mixed.txt"
    mixed.write_bytes(b"hello\0world")
    cleaned = await run("str_replace", "files/mixed.txt", old_str="\0", new_str="")
    assert cleaned.is_error and "revision" in cleaned.content
    assert mixed.read_bytes() == b"hello\0world"
    nul = await run("create", "files/nul.txt", file_text="a\0b")
    assert nul.is_error and "revision" in nul.content
    assert not (root / "files" / "nul.txt").exists()
    soiled = await run("insert", "files/small.txt", insert_line=0, insert_text="\0")
    assert soiled.is_error and "revision" in soiled.content
    assert (root / "files" / "small.txt").read_text() == "x" * 10
