"""Requests the Files library tests make the same way: an account whose uploads are parsed,
an upload, a markdown document written through the editor's route, and a commit that fails
once."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import CSRF_HEADERS, convert_on_upload, sign_in


async def ready_to_parse(client: AsyncClient) -> None:
    """Sign `client` in (`sign_in`) with the parse agent's `auto_convert` on, so a PDF or an
    image uploaded on the Files page is parsed."""
    await sign_in(client)
    await convert_on_upload(client)


async def upload(
    client: AsyncClient,
    name: str,
    data: bytes,
    media_type: str = "application/octet-stream",
    folder_id: int | None = None,
) -> httpx.Response:
    """`POST /api/library/documents` of one file, into `folder_id` or the top level."""
    return await client.post(
        "/api/library/documents",
        files={"file": (name, data, media_type)},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )


async def upload_file(client: AsyncClient, path: Path, media_type: str = "application/pdf") -> dict:
    """The file at `path` uploaded to the top level; its payload."""
    response = await upload(client, path.name, path.read_bytes(), media_type)
    assert response.status_code == 201, response.text
    return response.json()


async def upload_png(
    client: AsyncClient,
    documents: dict[str, Path],
    name: str = "tiny.png",
    folder_id: int | None = None,
) -> httpx.Response:
    """The fixture PNG uploaded as `name`."""
    return await upload(client, name, documents["png"].read_bytes(), "image/png", folder_id)


async def new_document(
    client: AsyncClient, name: str, folder_id: int | None = None
) -> httpx.Response:
    """A markdown document written new on the Files page."""
    return await client.post(
        "/api/library/documents/new",
        json={"name": name, "folder_id": folder_id},
        headers=CSRF_HEADERS,
    )


async def write_content(client: AsyncClient, document_id: int, markdown: str) -> None:
    """Save `markdown` as the document's text, on the revision it reads at."""
    content = (await client.get(f"/api/library/documents/{document_id}/content")).json()
    saved = await client.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": markdown, "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text


async def written_document(
    client: AsyncClient, name: str, folder_id: int | None, text: str
) -> dict:
    """A markdown document written with `text`, so its directory exists; its payload."""
    created = await new_document(client, name, folder_id)
    assert created.status_code == 201, created.text
    await write_content(client, created.json()["id"], text)
    return created.json()


def fail_next_commit(monkeypatch: pytest.MonkeyPatch, *, landed: bool) -> dict[str, bool]:
    """The next commit raises: after it went through (`landed`, the acknowledgement lost
    with the connection) or instead of it (rolled back). Every later commit passes. The
    flag it returns reads `{"on": False}` once the failure has fired."""
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
    return failing
