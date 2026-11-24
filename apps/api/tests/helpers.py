"""Requests and reads several test modules make the same way: the Files library, `POST
/v1/parse`, the billing period, the upload directory, and a Kano Proxy connection."""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
from httpx import AsyncClient

from sightread.db.models import Job
from sightread.upstream.client import Connection
from tests.conftest import CSRF_HEADERS, KANO_BASE, PARSE_MODEL


async def post_folder(client: AsyncClient, name: str, parent_id: int | None = None) -> dict:
    """A Files folder made through the API; its payload."""
    response = await client.post(
        "/api/library/folders", json={"name": name, "parent_id": parent_id}, headers=CSRF_HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


async def read_library(client: AsyncClient) -> dict:
    response = await client.get("/api/library")
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def v1_parse(
    client: AsyncClient, path: Path, token: str | None = None, **fields
) -> httpx.Response:
    """`POST /v1/parse` with the client's API key, or with an upload ticket."""
    return await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data=fields,
        headers=bearer(token) if token is not None else {},
    )


async def v1_upload(client: AsyncClient, path: Path, **fields) -> httpx.Response:
    """`POST /v1/parse` with the client's API key, naming the parse model."""
    return await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"model": PARSE_MODEL, **fields},
    )


async def credits_spent(client: AsyncClient) -> int:
    """What the signed-in user's billing period has spent."""
    return (await client.get("/api/billing")).json()["period"]["spent"]


def staged_uploads(client: AsyncClient) -> list[str]:
    """The files left in the app's upload directory, by name."""
    upload_dir = Path(client.app.state.settings.upload_dir)
    return sorted(path.name for path in upload_dir.iterdir()) if upload_dir.exists() else []


async def job_by_id(sessionmaker, job_id: str) -> Job:
    async with sessionmaker() as db:
        return await db.get(Job, uuid.UUID(job_id))


def stub_connection(path: str = "openai", api_key: str = "sk-kano-proxy-test") -> Connection:
    """The Kano Proxy at `KANO_BASE`, on its `path` API (`openai` or `anthropic`)."""
    return Connection(api_key=api_key, base_url=f"{KANO_BASE}/{path}/v1")
