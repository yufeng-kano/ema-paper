"""Images inserted into a document (docs/workspace.md § Inserted images): uploaded on their
own route, kept beside the originals under their hash, copied into the document
directory's `figures/`, and served only by a name the store makes.

No upstream traffic: nothing here parses or asks a model (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from PIL import Image

from sightread.workspace.documents import sweep_orphaned_images
from sightread.workspace.layout import images_dir, user_root
from tests.conftest import CSRF_HEADERS
from tests.test_chat import _seed_document

IMAGE_PATH = re.compile(r"figures/img-[0-9a-f]{16}\.(png|jpg)")


@pytest.fixture
async def writer(make_client, tmp_path) -> AsyncClient:
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


def _image(image_format: str, size: tuple[int, int] = (40, 30), mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, (200, 40, 40) if mode == "RGB" else (200, 40, 40, 128)).save(
        buffer, format=image_format
    )
    return buffer.getvalue()


async def _document(client: AsyncClient, name: str = "Draft") -> int:
    created = await client.post(
        "/api/library/documents/new", json={"name": name}, headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def _insert(
    client: AsyncClient,
    document_id: int | str,
    data: bytes,
    name: str = "shot.png",
    media_type: str = "image/png",
) -> httpx.Response:
    return await client.post(
        "/api/library/images",
        files={"file": (name, data, media_type)},
        data={"document_id": str(document_id)},
        headers=CSRF_HEADERS,
    )


async def test_an_inserted_image_is_kept_copied_and_served(writer) -> None:
    document_id = await _document(writer)
    data = _image("PNG")
    inserted = await _insert(writer, document_id, data)
    assert inserted.status_code == 201, inserted.text
    path = inserted.json()["path"]
    assert IMAGE_PATH.fullmatch(path) and path.endswith(".png")
    name = path.removeprefix("figures/")

    kept = images_dir(writer.root) / str(document_id) / name
    assert kept.is_file()
    # The shell sees what the text will name, in the directory history excludes.
    assert (writer.root / "files" / "Draft" / "figures" / name).read_bytes() == kept.read_bytes()
    # Nothing staged is left behind.
    assert list(Path(writer.app.state.settings.upload_dir).iterdir()) == []
    # The route writes no text: the line is the caller's.
    content = (await writer.get(f"/api/library/documents/{document_id}/content")).json()
    assert content["markdown"] == ""

    served = await writer.get(f"/api/library/documents/{document_id}/images/{name}")
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.headers["cache-control"] == "private, no-store"
    assert served.content == kept.read_bytes()
    with Image.open(io.BytesIO(served.content)) as image:
        assert image.size == (40, 30)

    # The same picture again is the same file.
    again = await _insert(writer, document_id, data, name="again.png")
    assert again.json()["path"] == path
    assert sorted(entry.name for entry in kept.parent.iterdir()) == [name]


async def test_an_image_is_kept_as_png_or_jpeg_within_the_long_edge(make_client, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), document_image_long_edge=50)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    document_id = await _document(client)
    jpeg = await _insert(client, document_id, _image("JPEG", (200, 100)), "photo.jpg", "image/jpeg")
    assert jpeg.json()["path"].endswith(".jpg")
    gif = await _insert(client, document_id, _image("GIF"), "anim.gif", "image/gif")
    assert gif.json()["path"].endswith(".jpg")
    # Transparency is flattened, and a PNG stays a PNG.
    png = await _insert(client, document_id, _image("PNG", mode="RGBA"), "alpha.png")
    assert png.json()["path"].endswith(".png")
    name = jpeg.json()["path"].removeprefix("figures/")
    served = await client.get(f"/api/library/documents/{document_id}/images/{name}")
    assert served.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(served.content)) as image:
        assert image.size == (50, 25)


async def test_only_the_accepted_formats_are_decoded(writer) -> None:
    """A format Pillow could read but the route does not accept is refused before it is
    decoded, whatever the request calls it."""
    document_id = await _document(writer)
    bmp = await _insert(writer, document_id, _image("BMP"), "shot.png", "image/png")
    assert bmp.status_code == 400
    assert "PNG, JPEG, WebP, GIF or HEIC" in bmp.json()["error"]["message"]
    text = await _insert(writer, document_id, b"not an image", "shot.png", "image/png")
    assert text.status_code == 400
    assert not (images_dir(writer.root) / str(document_id)).exists()
    assert list(Path(writer.app.state.settings.upload_dir).iterdir()) == []


async def test_an_image_past_its_caps_is_refused(make_client, tmp_path) -> None:
    client = make_client(
        workspaces_dir=str(tmp_path / "ws"),
        document_image_max_bytes=2048,
        chat_page_image_max_pixels=1000,
    )
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    document_id = await _document(client)
    large = await _insert(client, document_id, b"\x89PNG" + b"\0" * 4096)
    assert large.status_code == 413
    assert "DOCUMENT_IMAGE_MAX_BYTES" in large.json()["error"]["message"]
    wide = await _insert(client, document_id, _image("PNG", (100, 20)))
    assert wide.status_code == 400
    assert wide.json()["error"]["message"] == "The image has too many pixels"
    assert list(Path(client.app.state.settings.upload_dir).iterdir()) == []


async def test_an_image_needs_the_callers_document_and_the_csrf_header(writer, make_client) -> None:
    document_id = await _document(writer)
    unknown = await _insert(writer, document_id + 1, _image("PNG"))
    assert unknown.status_code == 404
    missing = await writer.post(
        "/api/library/images",
        files={"file": ("a.png", _image("PNG"), "image/png")},
        headers=CSRF_HEADERS,
    )
    assert missing.status_code == 400
    forged = await writer.post(
        "/api/library/images",
        files={"file": ("a.png", _image("PNG"), "image/png")},
        data={"document_id": str(document_id)},
    )
    assert forged.status_code == 403

    name = (
        (await _insert(writer, document_id, _image("PNG"))).json()["path"].removeprefix("figures/")
    )
    # Only a name the store makes is ever looked up.
    for bad in ("img-0123456789abcdef.gif", "../1.sources", "source.pdf", name.upper()):
        response = await writer.get(f"/api/library/documents/{document_id}/images/{bad}")
        assert response.status_code == 404, bad
    absent = await writer.get(f"/api/library/documents/{document_id}/images/img-{'0' * 16}.png")
    assert absent.status_code == 404


async def test_an_image_goes_into_a_parsed_document_and_comes_back_when_removed(
    writer,
) -> None:
    document_id = await _seed_document(writer, writer.user_id)
    path = (await _insert(writer, document_id, _image("PNG"))).json()["path"]
    copy = writer.root / "files" / "Papers" / "paper.pdf" / path
    assert copy.is_file()
    copy.unlink()
    # The next materialization copies each missing image again, as it does the crops.
    assert (await writer.get(f"/api/library/documents/{document_id}/content")).status_code == 200
    assert copy.is_file()


async def test_the_images_go_with_the_document(writer) -> None:
    document_id = await _document(writer)
    await _insert(writer, document_id, _image("PNG"))
    store = images_dir(writer.root) / str(document_id)
    assert store.is_dir()
    deleted = await writer.delete(f"/api/library/documents/{document_id}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204, deleted.text
    assert not store.exists()


async def test_the_images_of_rows_that_are_gone_are_swept(writer) -> None:
    live = await _document(writer)
    await _insert(writer, live, _image("PNG"))
    orphan = images_dir(writer.root) / "999"
    orphan.mkdir()
    (orphan / f"img-{'1' * 16}.png").write_bytes(b"left behind")
    assert sweep_orphaned_images(writer.root, {live}) == 1
    assert not orphan.exists()
    assert (images_dir(writer.root) / str(live)).is_dir()


async def test_an_image_the_quota_has_no_room_for_is_refused(writer) -> None:
    document_id = await _document(writer)
    writer.app.state.settings.workspace_quota_bytes = 1
    try:
        refused = await _insert(writer, document_id, _image("PNG"))
    finally:
        writer.app.state.settings.workspace_quota_bytes = 10 * 1024 * 1024 * 1024
    assert refused.status_code == 409
    assert not (images_dir(writer.root) / str(document_id)).exists()


async def test_an_image_counts_towards_the_workspace_once(writer) -> None:
    document_id = await _document(writer)
    # Its directory written first, so the image is all the insert adds.
    assert (await writer.get(f"/api/library/documents/{document_id}/content")).status_code == 200

    async def measured() -> tuple[int, int]:
        workspace = (await writer.get("/api/sandbox")).json()["workspace"]
        (row,) = (await writer.get("/api/library")).json()["documents"]
        return workspace["files_bytes"], row["storage_bytes"]

    files, share = await measured()
    inserted = await _insert(writer, document_id, _image("PNG"))
    assert inserted.status_code == 201, inserted.text
    name = inserted.json()["path"].removeprefix("figures/")
    stored = (images_dir(writer.root) / str(document_id) / name).stat().st_size
    # The stored image counts, and its copy in the document's figures/ is the service's.
    assert (writer.root / "files" / "Draft" / "figures" / name).is_file()
    assert await measured() == (files + stored, share + stored)


async def test_me_lists_what_an_image_may_be(writer) -> None:
    limits = (await writer.get("/api/me")).json()["limits"]
    assert limits["image_media_types"] == [
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/heic",
        "image/heif",
    ]
    assert limits["image_max_bytes"] == writer.app.state.settings.document_image_max_bytes
