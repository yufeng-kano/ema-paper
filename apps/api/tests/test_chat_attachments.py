"""The chat composer's attachments (docs/chat.md § What a user message carries; docs/api.md):
the upload route preparing images, PDFs and text files, the send embedding them, the thread
showing them without their bytes. No test reaches the network (docs/testing.md)."""

from __future__ import annotations

import base64
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import respx
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import select

from sightread.db.models import ChatAttachment, User, utcnow
from sightread.parsing import attachments as attachment_prep
from tests.conftest import CSRF_HEADERS
from tests.test_chat import _run_queued_turn
from tests.test_chat_extensions import chatting_client, new_conversation, send, stub_messages


async def attach(
    client: AsyncClient, name: str, data: bytes, media_type: str = "application/octet-stream"
) -> httpx.Response:
    return await client.post(
        "/api/attachments", files={"file": (name, data, media_type)}, headers=CSRF_HEADERS
    )


def waiting(client: AsyncClient) -> list[Path]:
    """The attachment files in the upload directory: prepared, or still being staged."""
    return sorted(Path(client.app.state.settings.upload_dir).glob("chat-attachment-*"))


@respx.mock
async def test_attachments_are_prepared_embedded_and_shown_without_their_bytes(
    make_client, tmp_path, documents
) -> None:
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_attachment_cache_seconds=600)
    )
    image = await attach(client, "wide.png", documents["wide_png"].read_bytes(), "image/png")
    assert image.status_code == 201, image.text
    image = image.json()
    assert {key: image[key] for key in ("name", "kind", "media_type", "chars")} == {
        "name": "wide.png",
        "kind": "image",
        "media_type": "image/png",
        "chars": None,
    }
    # Downscaled the way Claude would resize it, PNG kept as PNG.
    with Image.open(waiting(client)[0]) as prepared:
        assert prepared.size == (1568, 523)
    # A browser reports no type for most of what a reader drags in: the extension decides.
    notes = await attach(client, "notes.md", b"# Notes\n\nGreedy should be 3.45.\n")
    assert notes.status_code == 201, notes.text
    notes = notes.json()
    assert notes["kind"] == "text" and notes["media_type"] == "text/plain"
    assert notes["chars"] == len("# Notes\n\nGreedy should be 3.45.\n")
    pdf = await attach(client, "paper.pdf", documents["text_pdf"].read_bytes(), "application/pdf")
    assert pdf.status_code == 201, pdf.text
    pdf = pdf.json()
    assert pdf["kind"] == "text" and pdf["chars"] > 0
    assert len(waiting(client)) == 3

    main, _ = stub_messages([([{"type": "text", "text": "Seen."}], "end_turn")])
    conversation_id = await new_conversation(client)
    # Images first, as the Messages API advises, whatever order they were sent in.
    sent = await send(client, conversation_id, attachments=[notes["id"], image["id"], pdf["id"]])
    assert sent.status_code == 202, sent.text
    message_id = sent.json()["message_id"]
    # Embedded, so what waited for the send is gone: the files and the rows.
    assert waiting(client) == []
    async with client.app.state.sessionmaker() as db:
        assert (await db.execute(select(ChatAttachment))).scalars().all() == []
    await _run_queued_turn(client)
    again = await send(client, conversation_id, text="Again", attachments=[image["id"]])
    assert again.status_code == 409

    content = main[0]["messages"][0]["content"]
    assert content[0]["type"] == "image" and set(content[0]) == {"type", "source"}
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1] == {
        "type": "text",
        "text": "Attached file: notes.md\n\n# Notes\n\nGreedy should be 3.45.\n",
    }
    assert content[2]["text"].startswith("Attached file: paper.pdf\n\n")
    assert set(content[2]) == {"type", "text"}

    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    assert thread["messages"][0]["content"] == [
        {"type": "image", "attachment": {"name": "wide.png"}},
        {
            "type": "text",
            "context": "attachment",
            "attachment": {"name": "notes.md", "kind": "text", "chars": notes["chars"]},
        },
        {
            "type": "text",
            "context": "attachment",
            "attachment": {"name": "paper.pdf", "kind": "text", "chars": pdf["chars"]},
        },
    ]
    # Sent without text, the thread is named after its first attachment.
    assert thread["conversation"]["title"] == "wide.png"
    # The panel asks for the image's bytes by its block's index: the stored copy is the one.
    base = f"/api/conversations/{conversation_id}/messages/{message_id}/attachments"
    served = await client.get(f"{base}/0")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    # As long as the deployment says, and only in the reader's browser.
    assert served.headers["cache-control"] == "private, max-age=600, immutable"
    assert served.content == base64.b64decode(content[0]["source"]["data"])
    for index in (1, 3, 99):
        assert (await client.get(f"{base}/{index}")).status_code == 404, index
    answer = thread["messages"][1]["id"]
    other = f"/api/conversations/{conversation_id}/messages/{answer}/attachments/0"
    assert (await client.get(other)).status_code == 404


async def test_an_attachment_is_refused_by_size_type_and_content(make_client, documents) -> None:
    client = make_client(chat_attachment_max_bytes=1024 * 1024)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    big = await attach(client, "big.txt", b"x" * (1024 * 1024 + 1), "text/plain")
    assert big.status_code == 413
    assert "CHAT_ATTACHMENT_MAX_BYTES" in big.json()["error"]["message"]
    assert (
        await attach(client, "archive.zip", b"PK\x03\x04", "application/zip")
    ).status_code == 415
    for name, data in (
        ("latin.txt", "café".encode("latin-1")),
        ("empty.txt", b""),
        ("blank.md", b" \n\n "),
        ("broken.png", b"not a png at all"),
        ("scan.pdf", documents["scanned_pdf"].read_bytes()),
        ("broken.pdf", b"%PDF-1.4 nothing else"),
    ):
        refused = await attach(client, name, data)
        assert refused.status_code == 400, (name, refused.text)
    plain = await client.post(
        "/api/attachments", content=b"x", headers={**CSRF_HEADERS, "content-type": "text/plain"}
    )
    assert plain.status_code == 400
    unnamed = await client.post(
        "/api/attachments", files={"other": ("a.txt", b"x", "text/plain")}, headers=CSRF_HEADERS
    )
    assert unnamed.status_code == 400
    # Nothing a refusal staged is left behind, and no row was written.
    assert waiting(client) == []
    async with client.app.state.sessionmaker() as db:
        assert (await db.execute(select(ChatAttachment))).scalars().all() == []


async def test_a_long_text_keeps_its_head_and_tail(make_client, tmp_path) -> None:
    client = make_client(chat_attachment_text_max_chars=200)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    text = "HEAD " + "x" * 5000 + " TAIL"
    created = await attach(client, "long.txt", text.encode(), "text/plain")
    assert created.status_code == 201, created.text
    assert created.json()["chars"] <= 200
    prepared = waiting(client)[0].read_text(encoding="utf-8")
    assert prepared.startswith("HEAD ") and prepared.endswith(" TAIL")
    assert "characters omitted" in prepared
    # The bound holds at a chunk's edge too, and NULs never reach a JSONB column.
    source = tmp_path / "chunked.txt"
    source.write_bytes(b"\x00" + "é".encode() * attachment_prep.READ_CHUNK_BYTES)
    bounded = attachment_prep._read_bounded(source, 100, strict=True)
    assert "\x00" not in bounded and len(bounded) <= 100 and bounded.startswith("é")


async def test_the_attachment_route_streams_past_the_json_body_cap(make_client) -> None:
    """The general body cap is for JSON; an attachment streams under its own (docs/api.md)."""
    client = make_client(content_max_bytes=1024)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    created = await attach(client, "large.txt", b"line\n" * 100_000, "text/plain")
    assert created.status_code == 201, created.text


async def test_an_attachment_is_removed_expires_or_is_another_readers(
    make_client, tmp_path
) -> None:
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_attachments_per_message=2)
    )
    removed = (await attach(client, "a.txt", b"alpha", "text/plain")).json()
    deleted = await client.delete(f"/api/attachments/{removed['id']}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204
    assert waiting(client) == []
    again = await client.delete(f"/api/attachments/{removed['id']}", headers=CSRF_HEADERS)
    assert again.status_code == 404
    assert (await client.delete("/api/attachments/x", headers=CSRF_HEADERS)).status_code == 404

    conversation_id = await new_conversation(client)
    swept = (await attach(client, "b.txt", b"beta", "text/plain")).json()
    for path in waiting(client):
        path.unlink()  # the sweeper took the file after a day
    assert (
        await send(client, conversation_id, text="See", attachments=[swept["id"]])
    ).status_code == 409
    gone = await send(client, conversation_id, text="See", attachments=[removed["id"]])
    assert gone.status_code == 409
    ids = [(await attach(client, f"{n}.txt", b"x", "text/plain")).json()["id"] for n in "cde"]
    assert (await send(client, conversation_id, text="See", attachments=ids)).status_code == 400

    # Another reader's attachment is not the caller's to send or to remove.
    stranger_id = uuid.uuid4()
    async with client.app.state.sessionmaker() as db:
        stranger = User(google_sub="stranger", email="stranger@example.com")
        db.add(stranger)
        await db.flush()
        db.add(
            ChatAttachment(
                id=stranger_id,
                user_id=stranger.id,
                name="theirs.txt",
                kind="text",
                media_type="text/plain",
                size_bytes=1,
                chars=1,
            )
        )
        await db.commit()
    taken = await send(client, conversation_id, text="See", attachments=[str(stranger_id)])
    assert taken.status_code == 409
    theirs = await client.delete(f"/api/attachments/{stranger_id}", headers=CSRF_HEADERS)
    assert theirs.status_code == 404
    # None of the refused sends stored anything.
    assert (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"] == []


async def test_a_new_upload_drops_the_rows_the_sweeper_has_taken(make_client) -> None:
    client = make_client()
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    stale = (await attach(client, "old.txt", b"old", "text/plain")).json()
    async with client.app.state.sessionmaker() as db:
        row = await db.get(ChatAttachment, uuid.UUID(stale["id"]))
        row.created_at = utcnow() - timedelta(days=2)
        await db.commit()
    fresh = (await attach(client, "new.txt", b"new", "text/plain")).json()
    async with client.app.state.sessionmaker() as db:
        ids = {str(row.id) for row in (await db.execute(select(ChatAttachment))).scalars()}
    assert ids == {fresh["id"]}


async def test_the_attachments_waiting_for_a_message_are_capped(make_client) -> None:
    """Each upload is bounded and so is their number: uploads never sent would otherwise fill
    the upload directory for a day (docs/api.md)."""
    client = make_client(chat_attachments_pending_max=2)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    first = await attach(client, "a.txt", b"one", "text/plain")
    second = await attach(client, "b.txt", b"two", "text/plain")
    assert (first.status_code, second.status_code) == (201, 201)
    third = await attach(client, "c.txt", b"three", "text/plain")
    assert third.status_code == 429
    assert "waiting to be sent" in third.json()["error"]["message"]
    # Refused before a byte was stored.
    assert len(waiting(client)) == 2
    # Removing one frees its place.
    removed = await client.delete(f"/api/attachments/{first.json()['id']}", headers=CSRF_HEADERS)
    assert removed.status_code == 204
    assert (await attach(client, "c.txt", b"three", "text/plain")).status_code == 201


@respx.mock
async def test_attachments_that_would_fill_the_history_stay_for_another_conversation(
    make_client, tmp_path, documents
) -> None:
    """The cap counts the message being sent: an image that would carry the history past it
    is refused before it is spent, rather than embedded into a turn that then cannot run
    (docs/chat.md § History is append-only)."""
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), chat_history_max_bytes=2000)
    )
    image = (
        await attach(client, "wide.png", documents["wide_png"].read_bytes(), "image/png")
    ).json()
    conversation_id = await new_conversation(client)
    refused = await send(client, conversation_id, text="Look", attachments=[image["id"]])
    assert refused.status_code == 409
    assert "start a new conversation" in refused.json()["error"]["message"]
    # Rolled back with the send: still waiting, file and row, for the next conversation.
    assert len(waiting(client)) == 1
    async with client.app.state.sessionmaker() as db:
        assert [str(row.id) for row in (await db.execute(select(ChatAttachment))).scalars()] == [
            image["id"]
        ]
    # The same history takes a message that fits.
    assert (await send(client, conversation_id, text="Look")).status_code == 202


def test_an_attachment_is_classified_by_extension_then_by_media_type() -> None:
    assert attachment_prep.classify("photo.HEIC", "") == "image"
    assert attachment_prep.classify("paper.pdf", "application/octet-stream") == "pdf"
    assert attachment_prep.classify("data.csv", "application/vnd.ms-excel") == "text"
    assert attachment_prep.classify("clipboard", "image/png") == "image"
    assert attachment_prep.classify("notes", "text/plain; charset=utf-8") == "text"
    assert attachment_prep.classify("archive.zip", "application/zip") is None
    assert ".heic" in attachment_prep.ATTACHMENT_EXTENSIONS


async def test_the_composer_learns_the_attachment_limits(make_client) -> None:
    client = make_client(chat_attachment_max_bytes=5_000_000, chat_attachments_per_message=3)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    limits = (await client.get("/api/me")).json()["limits"]
    assert limits["attachment_max_bytes"] == 5_000_000
    assert limits["attachments_per_message"] == 3
    assert limits["attachment_extensions"] == list(attachment_prep.ATTACHMENT_EXTENSIONS)
    assert {".png", ".pdf", ".md", ".txt"} <= set(limits["attachment_extensions"])
