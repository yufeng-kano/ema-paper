"""Requests and reads several test modules make the same way: a second account signed in,
the Files library, an upload ticket, `POST /v1/parse` and the worker cycle that runs it
against a stubbed vision upstream, the billing period, the upload directory, and a Kano
Proxy connection."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
from httpx import AsyncClient

from sightread.auth import upload_tickets
from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import Job, User
from sightread.jobs.queue import claim_next_job
from sightread.jobs.runner import run_job
from sightread.upstream.client import Connection
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, KANO_BASE

# The model a deployment names as its parse fallback, where a test needs one.
FALLBACK = "vendor/fallback-model"

# Its figure takes the whole page: no side of its crop can cut anything, so the figure check
# (docs/parsing.md § Figure check) asks no model, whatever the fixture page holds.
VISION_MARKDOWN = (
    "## Scanned page\n\n"
    "![fig](sightread://p9/0,0,1000,1000)\n"
    "Figure 2: the photograph\n\n"
    "Body text."
)


async def account_client(
    make_client, email: str = "stranger@example.com", name: str | None = None, **settings
) -> AsyncClient:
    """Another account than the dev user's, signed in on a client of its own:
    `client.user_id`, and `client.root`, its workspace."""
    client = make_client(**settings)
    async with client.app.state.sessionmaker() as db:
        user = User(google_sub=f"sub-{uuid.uuid4().hex}", email=email, name=name)
        db.add(user)
        await db.flush()
        token = await create_session(db, user)
        await db.commit()
        client.user_id = user.id
    # No domain: `http.cookiejar` rewrites a dotless host to "testserver.local", and a
    # domain-scoped cookie then never matches the request it was set for.
    client.cookies.set(SESSION_COOKIE, token)
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


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


async def mint_upload_ticket(
    client: AsyncClient, *, ttl_seconds: int | None = None, **options
) -> str:
    """An upload ticket for the client's user, minted straight through the shared module,
    with the `upload_tickets.mint` options given (`into_library`, `folder_id`)."""
    settings = client.app.state.settings
    if ttl_seconds is not None:
        settings = settings.model_copy(update={"upload_ticket_ttl_seconds": ttl_seconds})
    async with client.app.state.sessionmaker() as db:
        user = await db.get(User, client.user_id)
        _, token = await upload_tickets.mint(db, settings, user, **options)
    return token


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


async def drain_queue(client: AsyncClient, sessionmaker) -> uuid.UUID | None:
    """One worker cycle in-process: claim a job and run it to a terminal state."""
    settings = client.app.state.settings
    async with sessionmaker() as db:
        job_id = await claim_next_job(db, settings.max_jobs_per_user)
    if job_id is not None:
        await run_job(sessionmaker, settings, job_id, asyncio.Semaphore(2))
    return job_id


def completion(content: str = VISION_MARKDOWN, finish_reason: str = "stop") -> httpx.Response:
    """A chat completion that answers `content`, billed at a fixed usage."""
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "cost": 0.0015},
        },
    )


def vision_stub(request: httpx.Request) -> httpx.Response:
    """Every page is a vision transcription (docs/parsing.md § Vision-only conversion)."""
    return completion()


class Upstream:
    """The stubbed upstream: an answer per (model, page), a transcription for the rest,
    and every call it took, in order. The page is read from the prompt's placeholder."""

    def __init__(self, answers: dict[tuple[str, int], Callable[[], httpx.Response]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"][0]["text"]
        page = int(re.search(r"sightread://p(\d+)/", prompt).group(1))
        self.calls.append((body["model"], page))
        answer = self.answers.get((body["model"], page))
        return answer() if answer else completion()


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
