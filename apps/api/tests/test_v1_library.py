"""The library on the data plane and the connector's library tools (docs/api.md § The library
on the data plane, docs/mcp.md § The library tools, docs/auth.md § 4 and § 7).

No upstream traffic: every document is seeded in the database and its workspace, so nothing
here parses (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread.auth import document_tickets
from sightread.db.models import (
    Document,
    DocumentTicket,
    Folder,
    Job,
    OAuthGrant,
    Result,
    User,
    utcnow,
)
from sightread.parsing.figures import crop_path
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, mcp_running
from tests.test_mcp import _mcp_session, _payload
from tests.test_oauth_as import CHALLENGE, REDIRECT_URI, VERIFIER, _authorize, _register, _token
from tests.test_parse_end_to_end import _authorize as _with_api_key

FIGURE = "![fig1](sightread://p1/100,200,300,400)"
MARKDOWN = f"<!-- page: 1 -->\n# Attention\n\nBody text.\n\n{FIGURE}\nFigure 1: the model.\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"crop"


@pytest.fixture
async def reader(make_client, sessionmaker, tmp_path) -> AsyncClient:
    """A signed-in account holding an API key, with a workspace of its own."""
    client = await _with_api_key(make_client(workspaces_dir=str(tmp_path / "ws")), sessionmaker)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _seed_parse(client: AsyncClient, user_id: int, name: str = "paper.pdf") -> int:
    """A finished parse filed under Papers/2024, its figure's crop in the crop store."""
    async with client.app.state.sessionmaker() as db:
        papers = Folder(user_id=user_id, parent_id=None, name="Papers")
        db.add(papers)
        await db.flush()
        year = Folder(user_id=user_id, parent_id=papers.id, name="2024")
        db.add(year)
        await db.flush()
        job = Job(
            id=uuid.uuid4(),
            user_id=user_id,
            agent="parse",
            kind="pdf",
            filename=name,
            media_type="application/pdf",
            size_bytes=10,
            sha256="c" * 64,
            model="vendor/vision-model",
            bbox_format="yxyx_norm1000",
            status="succeeded",
            page_count=1,
            finished_at=utcnow(),
        )
        db.add(job)
        await db.flush()
        db.add(Result(job_id=job.id, markdown=MARKDOWN, pages=[], figures=[], errors=[], meta={}))
        document = Document(user_id=user_id, folder_id=year.id, job_id=job.id, name=name)
        db.add(document)
        await db.commit()
        crop = crop_path(
            Path(client.app.state.settings.figures_dir), job.id, 1, (100, 200, 300, 400)
        )
        crop.parent.mkdir(parents=True, exist_ok=True)
        crop.write_bytes(PNG)
        return document.id


async def _new_markdown(client: AsyncClient, name: str, folder_id: int | None = None) -> int:
    made = await client.post(
        "/api/library/documents/new",
        json={"name": name, "folder_id": folder_id},
        headers=CSRF_HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _mint(client: AsyncClient, document_id: int, scope: str) -> str:
    async with client.app.state.sessionmaker() as db:
        user = (await db.execute(select(User).where(User.id == client.user_id))).scalar_one()
        _, token = await document_tickets.mint(
            db, client.app.state.settings, user, document_id, scope
        )
    return token


# --- the index ------------------------------------------------------------------------------


async def test_the_library_lists_live_rows_with_their_paths(reader: AsyncClient) -> None:
    paper = await _seed_parse(reader, reader.user_id)
    draft = await _new_markdown(reader, "Draft")
    gone = await _new_markdown(reader, "Old draft")
    assert (
        await reader.post(f"/api/library/documents/{gone}/trash", headers=CSRF_HEADERS)
    ).status_code == 200
    # A document that is not trashed itself but sits in a trashed folder is not live either.
    attic = await reader.post("/api/library/folders", json={"name": "Attic"}, headers=CSRF_HEADERS)
    hidden = await _new_markdown(reader, "Inside", attic.json()["id"])
    assert (
        await reader.post(f"/api/library/folders/{attic.json()['id']}/trash", headers=CSRF_HEADERS)
    ).status_code == 200

    listed = await reader.get("/v1/library")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [folder["path"] for folder in body["folders"]] == ["Papers", "Papers/2024"]
    by_id = {entry["id"]: entry for entry in body["documents"]}
    assert set(by_id) == {paper, draft}
    assert hidden not in by_id and gone not in by_id
    assert by_id[paper]["path"] == "Papers/2024/paper.pdf"
    assert by_id[paper]["kind"] == "pdf"
    assert by_id[paper]["status"] == "succeeded"
    assert by_id[draft] | {"created_at": None} == {
        "id": draft,
        "folder_id": None,
        "name": "Draft",
        "path": "Draft",
        "kind": "markdown",
        "status": None,
        "media_type": None,
        "page_count": None,
        "size_bytes": None,
        "created_at": None,
        "finished_at": None,
    }


async def test_another_accounts_document_is_not_found(reader: AsyncClient) -> None:
    async with reader.app.state.sessionmaker() as db:
        other = User(google_sub="other", email="other@example.com")
        db.add(other)
        await db.commit()
        other_id = other.id
    theirs = await _seed_parse(reader, other_id)

    assert theirs not in {
        entry["id"] for entry in (await reader.get("/v1/library")).json()["documents"]
    }
    for path in ("", "/doc.md", "/notes.md", "/figures/1/100,200,300,400"):
        answer = await reader.get(f"/v1/library/documents/{theirs}{path}")
        assert answer.status_code == 404, (path, answer.text)


# --- reading and saving -----------------------------------------------------------------------


async def test_a_document_reads_its_text_and_notes_with_an_etag(reader: AsyncClient) -> None:
    paper = await _seed_parse(reader, reader.user_id)

    row = (await reader.get(f"/v1/library/documents/{paper}")).json()
    assert row["path"] == "Papers/2024/paper.pdf"
    assert row["original"] is False

    text = await reader.get(f"/v1/library/documents/{paper}/doc.md")
    assert text.status_code == 200, text.text
    assert text.text == MARKDOWN
    assert text.headers["content-type"] == "text/markdown; charset=utf-8"
    content = (await reader.get(f"/api/library/documents/{paper}/content")).json()
    assert text.headers["etag"] == f'"{content["revision"]}"'

    notes = await reader.get(f"/v1/library/documents/{paper}/notes.md")
    assert notes.text == ""
    assert notes.headers["etag"] == f'"{content["notes_revision"]}"'

    crop = await reader.get(f"/v1/library/documents/{paper}/figures/1/100,200,300,400")
    assert crop.status_code == 200
    assert crop.content == PNG


async def test_a_save_needs_the_etag_it_was_edited_from(reader: AsyncClient) -> None:
    draft = await _new_markdown(reader, "Draft")
    url = f"/v1/library/documents/{draft}/doc.md"
    etag = (await reader.get(url)).headers["etag"]

    blind = await reader.put(url, content=b"# Mine\n")
    assert blind.status_code == 428, blind.text

    saved = await reader.put(url, content="# Mine — 改好了\n".encode(), headers={"If-Match": etag})
    assert saved.status_code == 200, saved.text
    assert saved.headers["etag"] == f'"{saved.json()["revision"]}"'
    assert (await reader.get(url)).text == "# Mine — 改好了\n"

    # The copy the first save was made from is stale now: refused, the file as it was.
    stale = await reader.put(url, content=b"# Theirs\n", headers={"If-Match": etag})
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "revision_conflict"
    assert (await reader.get(url)).text == "# Mine — 改好了\n"

    # A save follows a save on the ETag the last one answered with, weak form included.
    again = await reader.put(
        url, content=b"# Mine, twice\n", headers={"If-Match": f"W/{saved.headers['etag']}"}
    )
    assert again.status_code == 200, again.text

    history = (await reader.get(f"/api/library/documents/{draft}/revisions")).json()["revisions"]
    assert [entry["author"] for entry in history[:2]] == ["api", "api"]
    assert history[0]["message"] == "edit files/Draft/doc.md"


async def test_notes_save_the_same_way(reader: AsyncClient) -> None:
    paper = await _seed_parse(reader, reader.user_id)
    url = f"/v1/library/documents/{paper}/notes.md"
    etag = (await reader.get(url)).headers["etag"]
    saved = await reader.put(url, content=b"- read it\n", headers={"If-Match": etag})
    assert saved.status_code == 200, saved.text
    content = (await reader.get(f"/api/library/documents/{paper}/content")).json()
    assert content["notes"] == "- read it\n"
    assert content["notes_revision"] == saved.json()["revision"]


async def test_a_save_past_the_cap_or_not_utf8_is_refused(
    make_client, sessionmaker, tmp_path
) -> None:
    client = await _with_api_key(
        make_client(workspaces_dir=str(tmp_path / "ws"), content_max_bytes=64), sessionmaker
    )
    draft = await _new_markdown(client, "Draft")
    url = f"/v1/library/documents/{draft}/doc.md"
    etag = (await client.get(url)).headers["etag"]

    large = await client.put(url, content=b"x" * 65, headers={"If-Match": etag})
    assert large.status_code == 413, large.text
    binary = await client.put(url, content=b"\xff\xfe", headers={"If-Match": etag})
    assert binary.status_code == 400, binary.text
    assert (await client.get(url)).text == ""


async def test_a_file_has_no_text_but_serves_its_bytes(reader: AsyncClient) -> None:
    kept = await reader.post(
        "/api/library/documents",
        files={"file": ("table.csv", b"a,b\n1,2\n", "text/csv")},
        headers=CSRF_HEADERS,
    )
    assert kept.status_code == 201, kept.text
    file_id = kept.json()["id"]
    assert kept.json()["kind"] == "file"

    listed = {entry["id"]: entry for entry in (await reader.get("/v1/library")).json()["documents"]}
    assert listed[file_id]["kind"] == "file"
    assert listed[file_id]["media_type"] == "text/csv"
    assert (await reader.get(f"/v1/library/documents/{file_id}/doc.md")).status_code == 404
    served = await reader.get(f"/v1/library/documents/{file_id}/file", params={"download": "true"})
    assert served.status_code == 200
    assert served.content == b"a,b\n1,2\n"
    assert served.headers["content-disposition"].startswith("attachment")


# --- document tickets ---------------------------------------------------------------------------


async def test_a_document_ticket_reaches_its_own_document_and_nothing_else(
    reader: AsyncClient,
) -> None:
    paper = await _seed_parse(reader, reader.user_id)
    draft = await _new_markdown(reader, "Draft")
    token = await _mint(reader, paper, document_tickets.READ)
    assert token.startswith("srd_")

    for path in ("", "/doc.md", "/notes.md", "/figures/1/100,200,300,400"):
        answer = await reader.get(f"/v1/library/documents/{paper}{path}", headers=_bearer(token))
        assert answer.status_code == 200, (path, answer.text)

    other = await reader.get(f"/v1/library/documents/{draft}/doc.md", headers=_bearer(token))
    assert other.status_code == 401
    assert other.json()["error"]["message"] == document_tickets.REJECTION_MESSAGE
    assert (await reader.get("/v1/library", headers=_bearer(token))).status_code == 401
    assert (await reader.get(f"/v1/jobs/{uuid.uuid4()}", headers=_bearer(token))).status_code == 401
    assert (await reader.get("/v1/models", headers=_bearer(token))).status_code == 401
    assert (
        await reader.post("/v1/parse", files={"file": ("a.pdf", b"%PDF")}, headers=_bearer(token))
    ).status_code == 401
    async with mcp_running(reader):
        mcp = await reader.post(
            "/mcp",
            headers={
                **_bearer(token),
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
    assert mcp.status_code == 401


async def test_only_a_write_ticket_saves(reader: AsyncClient) -> None:
    draft = await _new_markdown(reader, "Draft")
    url = f"/v1/library/documents/{draft}/doc.md"
    etag = (await reader.get(url)).headers["etag"]

    read = await _mint(reader, draft, document_tickets.READ)
    refused = await reader.put(url, content=b"# no\n", headers={**_bearer(read), "If-Match": etag})
    assert refused.status_code == 401
    write = await _mint(reader, draft, document_tickets.WRITE)
    saved = await reader.put(url, content=b"# yes\n", headers={**_bearer(write), "If-Match": etag})
    assert saved.status_code == 200, saved.text
    # Not spent by use: the same ticket saves again on the new ETag.
    again = await reader.put(
        url, content=b"# yes again\n", headers={**_bearer(write), "If-Match": saved.headers["etag"]}
    )
    assert again.status_code == 200, again.text


async def test_an_expired_ticket_and_a_trashed_document_are_refused(reader: AsyncClient) -> None:
    draft = await _new_markdown(reader, "Draft")
    token = await _mint(reader, draft, document_tickets.READ)
    assert (
        await reader.post(f"/api/library/documents/{draft}/trash", headers=CSRF_HEADERS)
    ).status_code == 200
    gone = await reader.get(f"/v1/library/documents/{draft}/doc.md", headers=_bearer(token))
    assert gone.status_code == 404

    async with reader.app.state.sessionmaker() as db:
        await db.execute(update(DocumentTicket).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    expired = await reader.get(f"/v1/library/documents/{draft}", headers=_bearer(token))
    assert expired.status_code == 401


# --- OAuth scopes ---------------------------------------------------------------------------


async def _connected(client: AsyncClient) -> tuple[str, dict]:
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    client_id = (await _register(client)).json()["client_id"]
    code = await _authorize(client, client_id)
    tokens = await _token(
        client,
        grant_type="authorization_code",
        code=code,
        code_verifier=VERIFIER,
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
    )
    assert tokens.status_code == 200, tokens.text
    return client_id, tokens.json()


async def test_a_new_grant_reaches_the_library(make_client, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    _, tokens = await _connected(client)
    assert tokens["scope"] == "parse library"
    listed = await client.get("/v1/library", headers=_bearer(tokens["access_token"]))
    assert listed.status_code == 200, listed.text

    consent = await client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": (await _register(client)).json()["client_id"],
            "redirect_uri": REDIRECT_URI,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
        },
    )
    assert "read your Files library" in consent.text
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["scopes_supported"] == ["parse", "library"]


async def test_a_grant_from_before_the_library_scope_is_told_to_reconnect(
    make_client, tmp_path
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    client_id, tokens = await _connected(client)
    async with client.app.state.sessionmaker() as db:
        await db.execute(update(OAuthGrant).values(scope="parse"))
        await db.commit()

    refused = await client.get("/v1/library", headers=_bearer(tokens["access_token"]))
    assert refused.status_code == 403
    assert "remove it in Claude's settings and add it again" in refused.json()["error"]["message"]
    # Its parse rights stand: an unknown job is still a 404 to it, not a 401.
    job = await client.get(f"/v1/jobs/{uuid.uuid4()}", headers=_bearer(tokens["access_token"]))
    assert job.status_code == 404

    async with mcp_running(client), _mcp_session(client, tokens["access_token"]) as session:
        result = await session.call_tool("list_library", {})
        parse = await session.call_tool("parse", {})
    assert result.is_error is True
    assert "add it again" in result.content[0].text
    assert parse.is_error is False

    # A refresh keeps the scope of the grant it rotates: never widened without consent.
    refreshed = await _token(
        client,
        grant_type="refresh_token",
        refresh_token=tokens["refresh_token"],
        client_id=client_id,
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["scope"] == "parse"
    still = await client.get("/v1/library", headers=_bearer(refreshed.json()["access_token"]))
    assert still.status_code == 403


# --- the connector's tools --------------------------------------------------------------------


async def test_list_library_filters_by_folder_and_name(make_client, sessionmaker, tmp_path) -> None:
    client = await _with_api_key(
        make_client(workspaces_dir=str(tmp_path / "ws"), mcp_library_list_max_items=1),
        sessionmaker,
    )
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    paper = await _seed_parse(client, client.user_id)
    await _new_markdown(client, "Draft")
    await _new_markdown(client, "Another draft")

    async with mcp_running(client), _mcp_session(client) as session:
        everything = _payload(await session.call_tool("list_library", {}))
        papers = _payload(await session.call_tool("list_library", {"folder": "Papers"}))
        drafts = await session.call_tool("list_library", {"name": "DRAFT"})
        missing = await session.call_tool("list_library", {"folder": "Nowhere"})

    assert everything["total"] == 3 and everything["truncated"] is True
    assert len(everything["documents"]) == 1
    assert papers["documents"] == [
        {
            "id": paper,
            "name": "paper.pdf",
            "path": "Papers/2024/paper.pdf",
            "kind": "pdf",
            "status": "succeeded",
            "page_count": 1,
        }
    ]
    assert [entry["path"] for entry in papers["folders"]] == ["Papers", "Papers/2024"]
    assert _payload(drafts)["total"] == 2
    assert missing.is_error is True
    assert "call list_library without `folder`" in missing.content[0].text


async def test_read_and_write_document_mint_tickets_for_their_commands(reader: AsyncClient) -> None:
    paper = await _seed_parse(reader, reader.user_id)
    kept = await reader.post(
        "/api/library/documents",
        files={"file": ("it's a table.csv", b"a\n", "text/csv")},
        headers=CSRF_HEADERS,
    )
    file_id = kept.json()["id"]

    async with mcp_running(reader), _mcp_session(reader) as session:
        read = _payload(await session.call_tool("read_document", {"document_id": paper}))
        write = _payload(await session.call_tool("write_document", {"document_id": paper}))
        file_read = _payload(await session.call_tool("read_document", {"document_id": file_id}))
        file_write = await session.call_tool("write_document", {"document_id": file_id})
        unknown = await session.call_tool("read_document", {"document_id": 999_999})

    base = f"http://localhost:8000/v1/library/documents/{paper}"
    assert read["document"]["path"] == "Papers/2024/paper.pdf"
    assert read["token"].startswith("srd_") and read["token"] in read["download"]
    assert read["download"].startswith("d=paper.pdf; ")
    assert f"{base}/doc.md" in read["download"] and f"{base}/notes.md" in read["download"]
    assert "original" not in read
    assert f'"{base}/$f"' in write["save"]
    assert write["token"] != read["token"]

    async with reader.app.state.sessionmaker() as db:
        scopes = {
            row.token_hash: row.scope
            for row in (await db.execute(select(DocumentTicket))).scalars()
        }
    assert sorted(scopes.values()) == ["read", "read", "write"]

    # A name the reader gave is one shell word, quote and all.
    assert file_read["download"].startswith("n='it'\"'\"'s a table.csv'; ")
    assert "/file?download=true" in file_read["download"]
    assert file_write.is_error is True
    assert unknown.is_error is True


async def test_the_document_ticket_mint_rate_is_its_own(
    make_client, sessionmaker, tmp_path
) -> None:
    client = await _with_api_key(
        make_client(workspaces_dir=str(tmp_path / "ws"), document_ticket_rate_per_hour=1),
        sessionmaker,
    )
    draft = await _new_markdown(client, "Draft")
    async with mcp_running(client), _mcp_session(client) as session:
        first = await session.call_tool("read_document", {"document_id": draft})
        second = await session.call_tool("read_document", {"document_id": draft})
        parse = await session.call_tool("parse", {})
    assert first.is_error is False
    assert second.is_error is True and "retry later" in second.content[0].text
    assert parse.is_error is False
