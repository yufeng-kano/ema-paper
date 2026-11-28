"""The app's database engine and sessions (sightread.db.session), and how long a request
keeps one (docs/api.md § Database connections)."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from sightread.db.models import User
from sightread.db.session import commit_or_conflict, create_sessionmaker
from sightread.errors import ApiError
from sightread.workspace import safe_io
from sightread.workspace.layout import sources_dir
from tests.conftest import CSRF_HEADERS, api_key_client, base_settings
from tests.factories import user_row
from tests.library_helpers import upload


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///sightread.db",
        "postgresql+psycopg://sightread:a-password-never-shown@pg:5432/sightread",
        "postgresql://sightread:a-password-never-shown@pg:5432/sightread",
    ],
)
def test_a_database_other_than_postgresql_through_asyncpg_is_refused_at_start(url: str) -> None:
    """One line at start, not a driver error at the first connection, and never the URL
    itself: it carries the password (docs/deployment.md)."""
    with pytest.raises(ValueError, match="postgresql\\+asyncpg://") as refused:
        base_settings(database_url=url)
    assert "a-password-never-shown" not in str(refused.value)
    assert "sightread.db" not in str(refused.value)


async def test_a_failed_statement_never_carries_the_readers_text(sessionmaker) -> None:
    """The engine hides bound values, so a database error logged with its traceback cannot
    write a memory's text — or instructions, or a message — into the log. The fixture's
    engine is the app's own (`create_engine`)."""
    async with sessionmaker.kw["bind"].connect() as connection:
        with pytest.raises(DBAPIError) as failed:
            await connection.execute(
                sql("insert into no_such_table (text) values (:text)"),
                {"text": "the reader reads with a screen reader"},
            )
    assert "screen reader" not in str(failed.value)


async def test_a_commit_a_constraint_refuses_is_rolled_back_and_answered_409(
    sessionmaker,
) -> None:
    """`commit_or_conflict`: the loser of a race on a unique value rolls back and is answered
    409 with the caller's message, and its session stays usable."""
    async with sessionmaker() as first:
        first.add(user_row("taken@example.com"))
        await first.commit()
    async with sessionmaker() as db:
        db.add(user_row("taken@example.com"))
        with pytest.raises(ApiError) as refused:
            await commit_or_conflict(db, "That account was just made elsewhere")
        assert (refused.value.status_code, refused.value.error_type, refused.value.message) == (
            409,
            "invalid_request",
            "That account was just made elsewhere",
        )
        db.add(user_row("free@example.com"))
        await commit_or_conflict(db, "unused")
    async with sessionmaker() as db:
        emails = (await db.execute(select(User.email).order_by(User.email))).scalars().all()
    assert emails == ["free@example.com", "taken@example.com"]


async def test_a_file_its_reader_stopped_reading_leaves_the_pool_to_the_others(
    writer, sessionmaker
) -> None:
    """A video paused in a tab keeps its response open for as long as the tab stays: with a
    pool of one connection, the next request still answers while that body is stopped
    halfway, rather than wait for the connection and fail."""
    size = safe_io.READ_CHUNK + 1
    video = (await upload(writer, "clip.mp4", b"\0" * size, "video/mp4")).json()
    engine = create_async_engine(
        sessionmaker.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=2
    )
    app = writer.app
    app.state.sessionmaker = create_sessionmaker(engine)
    others = []

    async def paused(scope, receive, send):
        async def sending(message):
            if message["type"] == "http.response.body" and not others:
                # The first chunk is out and the reader stops; someone else asks meanwhile.
                others.append(await writer.get("/api/me"))
            await send(message)

        await app(scope, receive, sending)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=paused),
            base_url="https://testserver",
            cookies=writer.cookies,
        ) as reader:
            played = await reader.get(f"/api/library/documents/{video['id']}/file")
    finally:
        app.state.sessionmaker = sessionmaker
        await engine.dispose()
    assert played.status_code == 200 and len(played.content) == size
    assert [answer.status_code for answer in others] == [200]


async def test_no_answer_holds_a_connection_while_its_body_goes_out(make_client, documents) -> None:
    """On either plane, streamed from disk or written whole: by the first byte of the body
    the request's session has ended, its authentication's reads included."""
    client = await api_key_client(make_client())
    text = (await upload(client, "notes.txt", b"mine", "text/plain")).json()
    png = documents["png"].read_bytes()
    image = (await upload(client, "tiny.png", png, "image/png")).json()
    converted = await client.post(
        f"/api/library/documents/{image['id']}/convert", json={}, headers=CSRF_HEADERS
    )
    assert converted.status_code == 200, converted.text
    # Filed by hand, as the worker files it at the parse's end (docs/workspace.md § Layout).
    sources_dir(client.root).mkdir(parents=True, exist_ok=True)
    (sources_dir(client.root) / f"{image['id']}.png").write_bytes(png)
    engine = client.app.state.sessionmaker.kw["bind"]
    held: list[int] = []

    async def probed(scope, receive, send):
        async def sending(message):
            if message["type"] == "http.response.body":
                held.append(engine.pool.checkedout())
            await send(message)

        await client.app(scope, receive, sending)

    async with AsyncClient(
        transport=ASGITransport(app=probed),
        base_url="https://testserver",
        cookies=client.cookies,
        headers=client.headers,
    ) as probe:
        for url, wanted in (
            (f"/api/library/documents/{text['id']}/file", b"mine"),
            (f"/v1/library/documents/{text['id']}/file", b"mine"),
            (f"/api/library/documents/{image['id']}/original", png),
        ):
            held.clear()
            answer = await probe.get(url)
            assert (answer.status_code, answer.content) == (200, wanted), url
            assert held and set(held) == {0}, url
        held.clear()
        listed = await probe.get("/api/library")
        assert listed.status_code == 200 and len(listed.json()["documents"]) == 2
        assert held and set(held) == {0}


def _in_two(method: str, url: str, **request) -> tuple[str, str, dict, bytes, bytes]:
    """A request as httpx would send it, its body cut in two halves."""
    built = httpx.Request(method, f"https://testserver{url}", **request)
    body = built.read()
    headers = dict(request.get("headers", {}))
    if "content-type" in built.headers:
        headers["content-type"] = built.headers["content-type"]
    half = len(body) // 2
    return method, url, headers, body[:half], body[half:]


async def _halves(head: bytes, tail: bytes, held: list[int], engine) -> AsyncIterator[bytes]:
    yield head
    # The route is reading its body now.
    held.append(engine.pool.checkedout())
    yield tail


async def test_no_pooled_connection_waits_for_a_body_still_arriving(make_client, documents) -> None:
    """Every route that reads its own body has ended its transaction by then: halfway
    through the body, nothing of the pool is checked out (docs/api.md § Database
    connections)."""
    client = await api_key_client(make_client())
    png = documents["png"].read_bytes()
    markdown = (await upload(client, "notes.md", b"# Notes\n", "text/markdown")).json()
    doc_url = f"/v1/library/documents/{markdown['id']}/doc.md"
    notes_url = f"/v1/library/documents/{markdown['id']}/notes.md"
    etag = (await client.get(doc_url)).headers["etag"]
    notes_etag = (await client.get(notes_url)).headers["etag"]
    source = base64.b64encode(documents["jpg"].read_bytes()).decode()
    engine = client.app.state.sessionmaker.kw["bind"]
    requests = [
        _in_two(
            "POST",
            "/api/library/documents",
            files={"file": ("kept.txt", b"kept " * 1000, "text/plain")},
            headers=CSRF_HEADERS,
        ),
        _in_two(
            "POST",
            "/api/library/images",
            files={"file": ("shot.png", png, "image/png")},
            data={"document_id": str(markdown["id"])},
            headers=CSRF_HEADERS,
        ),
        _in_two(
            "POST",
            "/api/attachments",
            files={"file": ("a.txt", b"attached " * 1000, "text/plain")},
            headers=CSRF_HEADERS,
        ),
        _in_two("POST", "/v1/parse", files={"file": ("tiny.png", png, "image/png")}),
        _in_two(
            "POST",
            "/v1/parse",
            json={"source": source, "filename": "tiny.jpg", "media_type": "image/jpeg"},
        ),
        _in_two("PUT", doc_url, content=b"# Notes, saved\n" * 100, headers={"If-Match": etag}),
        _in_two("PUT", notes_url, content=b"A note.\n" * 100, headers={"If-Match": notes_etag}),
        _in_two(
            "POST",
            "/oauth/register",
            json={"client_name": "Claude", "redirect_uris": ["https://claude.ai/cb"]},
        ),
    ]
    for method, url, headers, head, tail in requests:
        held: list[int] = []
        # A settings read that misses the cache runs on the request's session too.
        client.app.state.runtime_settings.invalidate()
        answer = await client.request(
            method, url, content=_halves(head, tail, held, engine), headers=headers
        )
        assert answer.status_code in (200, 201, 202), (url, answer.text)
        assert held == [0], url
