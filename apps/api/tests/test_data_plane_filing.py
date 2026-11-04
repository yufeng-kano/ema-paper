"""A data-plane parse filed into Files (docs/mcp.md § Filing into Files, docs/api.md § POST
/v1/parse): the parse tool's `folder` found as listed or made, refused without the library
or by the mint rate before anything is made; the ticket filing there as a Files upload
does, the original kept, spent only by the filing; a file parsed without a folder filed
from that parse with nothing charged; no twin; every page; a filed parse that failed
retried where it is, on a ticket that files nothing. The vision upstream is stubbed
(docs/testing.md § Cost safety).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread.auth import upload_tickets
from sightread.db.models import Document, Job, OAuthGrant, UploadTicket, User
from sightread.errors import ApiError
from sightread.jobs.intake import retry_filed_parse
from sightread.workspace import documents as workspace_documents
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, KANO_CHAT_URL, mcp_running
from tests.test_mcp import _mcp_session, _payload
from tests.test_parse_end_to_end import _authorize, _drain_queue, _vision_stub
from tests.test_retry_failed_pages import FALLBACK, Upstream
from tests.test_upload_tickets import _bearer
from tests.test_v1_library import _connected

MODEL = "vendor/vision-model"


@pytest.fixture
async def api(make_client, sessionmaker, tmp_path) -> AsyncClient:
    """An API key with a workspace, the format check off so a parse makes one upstream call
    per page; its session cookie makes folders and reads billing."""
    client = await _authorize(
        make_client(workspaces_dir=str(tmp_path / "ws"), parse_fallback_model=FALLBACK),
        sessionmaker,
    )
    off = await client.put(
        "/api/agents/check/settings", json={"auto_check": False}, headers=CSRF_HEADERS
    )
    assert off.status_code == 200, off.text
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _folder(client: AsyncClient, name: str, parent_id: int | None = None) -> int:
    made = await client.post(
        "/api/library/folders",
        json={"name": name, "parent_id": parent_id},
        headers=CSRF_HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()["id"]


async def _mint(
    client: AsyncClient,
    sessionmaker,
    *,
    library_access: bool = True,
    into_library: bool = False,
    folder_id: int | None = None,
) -> str:
    async with sessionmaker() as db:
        user = (await db.execute(select(User))).scalars().one()
        _, token = await upload_tickets.mint(
            db,
            client.app.state.settings,
            user,
            library_access=library_access,
            into_library=into_library,
            folder_id=folder_id,
        )
    return token


async def _parse(
    client: AsyncClient, path: Path, token: str | None = None, **fields
) -> httpx.Response:
    return await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data=fields,
        headers=_bearer(token) if token is not None else {},
    )


async def _documents(sessionmaker) -> list[Document]:
    async with sessionmaker() as db:
        return list((await db.execute(select(Document).order_by(Document.id))).scalars())


async def _ticket(sessionmaker, token: str) -> UploadTicket:
    async with sessionmaker() as db:
        return (
            await db.execute(
                select(UploadTicket).where(
                    UploadTicket.prefix == upload_tickets.display_prefix(token)
                )
            )
        ).scalar_one()


async def _spent(client: AsyncClient) -> int:
    return (await client.get("/api/billing")).json()["period"]["spent"]


def _uploads(client: AsyncClient) -> list[str]:
    upload_dir = Path(client.app.state.settings.upload_dir)
    return sorted(path.name for path in upload_dir.iterdir()) if upload_dir.exists() else []


# --- the parse tool's folder -----------------------------------------------------------------


async def test_the_parse_tool_files_into_a_folder_as_listed_or_made(api, sessionmaker) -> None:
    dl = await _folder(api, "DL")
    dino = await _folder(api, "DINO", dl)
    made = await api.post(
        "/api/library/documents/new", json={"name": "Reading list"}, headers=CSRF_HEADERS
    )
    assert made.status_code == 201, made.text

    async with mcp_running(api), _mcp_session(api) as session:
        tool = next(tool for tool in (await session.list_tools()).tools if tool.name == "parse")
        found = _payload(await session.call_tool("parse", {"folder": "DL/DINO"}))
        deeper = _payload(await session.call_tool("parse", {"folder": "/DL/DINO/SSL/2026/"}))
        top = _payload(await session.call_tool("parse", {"folder": "/"}))
        unfiled = _payload(await session.call_tool("parse", {}))
        document = await session.call_tool("parse", {"folder": "Reading list.md/Papers"})
        dots = await session.call_tool("parse", {"folder": "DL/../Papers"})
        empty = await session.call_tool("parse", {"folder": "  "})
        deep = await session.call_tool("parse", {"folder": "/".join(["n"] * 65)})

    # The model is told when to file and where the path comes from.
    assert "folder" in tool.input_schema["properties"]
    assert "list_library exactly as listed" in tool.description
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.read_only_hint is False

    assert found["folder"] == {"id": dino, "path": "DL/DINO", "created": []}
    assert "Files › DL/DINO" in found["notes"]
    assert "created the folder" not in found["notes"]
    ticket = await _ticket(sessionmaker, found["token"])
    assert (ticket.library_access, ticket.into_library, ticket.folder_id) == (True, True, dino)

    # The missing names are made, in order, and the notes ask the agent to say so.
    assert deeper["folder"]["path"] == "DL/DINO/SSL/2026"
    assert deeper["folder"]["created"] == ["DL/DINO/SSL", "DL/DINO/SSL/2026"]
    assert "created the folder(s) DL/DINO/SSL, DL/DINO/SSL/2026" in deeper["notes"]
    listed = (await api.get("/v1/library")).json()["folders"]
    assert [entry["path"] for entry in listed] == [
        "DL",
        "DL/DINO",
        "DL/DINO/SSL",
        "DL/DINO/SSL/2026",
    ]
    assert (api.root / "files" / "DL" / "DINO" / "SSL" / "2026").is_dir()

    assert top["folder"] == {"id": None, "path": "/", "created": []}
    assert "the top level of Files" in top["notes"]
    top_ticket = await _ticket(sessionmaker, top["token"])
    assert (top_ticket.into_library, top_ticket.folder_id) == (True, None)

    # Without a folder nothing is filed, and the notes say how to file it later.
    assert "folder" not in unfiled
    assert "call `parse` with `folder` and run `upload` again" in unfiled["notes"]
    plain = await _ticket(sessionmaker, unfiled["token"])
    assert (plain.library_access, plain.into_library) == (True, False)

    # A document's name is no folder to make a suffixed twin of; nothing was made for it.
    assert document.is_error is True
    assert "is a document here, not a folder" in document.content[0].text
    assert dots.is_error is True and empty.is_error is True
    # Every name is checked before the first is made: a path too deep makes nothing.
    assert deep.is_error is True
    assert "at most 64 folders deep" in deep.content[0].text
    assert len((await api.get("/v1/library")).json()["folders"]) == 4


async def test_a_parse_the_mint_rate_refuses_makes_no_folder(
    make_client, sessionmaker, tmp_path
) -> None:
    client = await _authorize(
        make_client(workspaces_dir=str(tmp_path / "ws"), upload_ticket_rate_per_hour=1),
        sessionmaker,
    )
    async with mcp_running(client), _mcp_session(client) as session:
        first = await session.call_tool("parse", {})
        refused = await session.call_tool("parse", {"folder": "New/Sub"})
    assert first.is_error is False
    assert refused.is_error is True
    assert "retry later" in refused.content[0].text
    # Refused before the folders were made, so there is none the reader was not told of.
    assert (await client.get("/v1/library")).json()["folders"] == []


async def test_a_grant_without_the_library_files_nothing(make_client, documents, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    _, tokens = await _connected(client)
    async with client.app.state.sessionmaker() as db:
        await db.execute(update(OAuthGrant).values(scope="parse"))
        await db.commit()

    async with mcp_running(client), _mcp_session(client, tokens["access_token"]) as session:
        refused = await session.call_tool("parse", {"folder": "/"})
        plain = await session.call_tool("parse", {})
    assert refused.is_error is True
    assert "add it again" in refused.content[0].text
    # Its own parse stands, and is not pointed at a filing it cannot make.
    assert plain.is_error is False
    assert "with `folder`" not in _payload(plain)["notes"]

    rest = await client.post(
        "/v1/parse",
        files={"file": ("a.pdf", documents["text_pdf"].read_bytes(), "application/pdf")},
        data={"folder": "/"},
        headers=_bearer(tokens["access_token"]),
    )
    assert rest.status_code == 403, rest.text
    assert "add it again" in rest.json()["error"]["message"]


# --- a filing ticket's upload ------------------------------------------------------------------


@respx.mock
async def test_a_filing_ticket_files_the_parse_as_a_files_upload(
    api, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    dino = await _folder(api, "DINO")
    token = await _mint(api, sessionmaker, into_library=True, folder_id=dino)

    accepted = await _parse(api, documents["text_pdf"], token)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    document_id = accepted.json()["document_id"]
    (row,) = await _documents(sessionmaker)
    assert (row.id, row.folder_id, row.name, str(row.job_id), row.kind) == (
        document_id,
        dino,
        "text.pdf",
        job_id,
        "parse",
    )
    ticket = await _ticket(sessionmaker, token)
    assert (str(ticket.job_id), ticket.spent_at is not None) == (job_id, True)

    await _drain_queue(api, sessionmaker)
    # The worker filed the upload as the document's original, as for a Files upload.
    assert workspace_documents.original_of(api.root, document_id) is not None
    assert _uploads(api) == []
    landed = await api.get("/v1/jobs/last/result.md", headers=_bearer(token))
    assert landed.status_code == 200, landed.text
    listed = (await api.get("/v1/library")).json()["documents"]
    assert [(entry["path"], entry["status"]) for entry in listed] == [
        ("DINO/text.pdf", "succeeded")
    ]


@respx.mock
async def test_a_file_parsed_without_a_folder_is_filed_from_that_parse_once(
    api, sessionmaker, documents
) -> None:
    route = respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    plain = await _mint(api, sessionmaker)
    job_id = (await _parse(api, documents["mixed_pdf"], plain)).json()["job_id"]
    await _drain_queue(api, sessionmaker)
    assert await _documents(sessionmaker) == []
    calls, spent = len(route.calls), await _spent(api)

    papers = await _folder(api, "Papers")
    filing = await _mint(api, sessionmaker, into_library=True, folder_id=papers)
    cached = await _parse(api, documents["mixed_pdf"], filing)
    assert cached.status_code == 200, cached.text
    assert cached.json()["meta"]["cached"] is True
    document_id = cached.json()["document_id"]
    (row,) = await _documents(sessionmaker)
    assert (row.id, str(row.job_id), row.folder_id) == (document_id, job_id, papers)
    # Nothing parsed or charged again; the fresh upload is the original the parse never kept.
    assert (len(route.calls), await _spent(api)) == (calls, spent)
    assert workspace_documents.original_of(api.root, document_id) is not None
    assert _uploads(api) == []
    landed = await api.get("/v1/jobs/last/result.md", headers=_bearer(filing))
    assert landed.status_code == 200, landed.text

    # The command run again files no twin: the same document answers, its copy discarded.
    again = await _parse(
        api,
        documents["mixed_pdf"],
        await _mint(api, sessionmaker, into_library=True, folder_id=papers),
    )
    assert again.status_code == 200, again.text
    assert again.json()["document_id"] == document_id
    assert [entry.id for entry in await _documents(sessionmaker)] == [document_id]
    assert _uploads(api) == []
    # Another folder is another place to keep it: a second entry of the same parse.
    other = await _parse(
        api, documents["mixed_pdf"], await _mint(api, sessionmaker, into_library=True)
    )
    assert other.status_code == 200, other.text
    assert other.json()["document_id"] != document_id
    assert len(await _documents(sessionmaker)) == 2


@respx.mock
async def test_a_filing_refused_after_a_cache_hit_leaves_the_ticket_unspent(
    api, sessionmaker, documents, monkeypatch
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    await _parse(api, documents["text_pdf"], await _mint(api, sessionmaker))
    await _drain_queue(api, sessionmaker)

    async def full(*args, **kwargs) -> None:
        raise ApiError(409, "storage_full", "The workspace is at its quota; free some space first")

    # The quota the filing checks when it keeps the original, after the cache answered.
    monkeypatch.setattr(workspace_documents, "ensure_within_quota", full)
    filing = await _mint(api, sessionmaker, into_library=True)
    refused = await _parse(api, documents["text_pdf"], filing)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["type"] == "storage_full"
    assert await _documents(sessionmaker) == []
    # Nothing filed, so the ticket is unspent and the read after the upload fails: the
    # connector's command exits non-zero instead of landing a parse it did not file.
    ticket = await _ticket(sessionmaker, filing)
    assert (ticket.spent_at, ticket.job_id) == (None, None)
    read = await api.get("/v1/jobs/last/result.md", headers=_bearer(filing))
    assert read.status_code == 409, read.text


async def test_a_filing_refuses_pages_a_folder_field_and_a_trashed_destination(
    api, sessionmaker, documents
) -> None:
    papers = await _folder(api, "Papers")
    token = await _mint(api, sessionmaker, into_library=True, folder_id=papers)

    pages = await _parse(api, documents["mixed_pdf"], token, pages="1-2")
    assert pages.status_code == 400, pages.text
    assert "covers every page" in pages.json()["error"]["message"]
    field = await _parse(api, documents["mixed_pdf"], token, folder="Papers")
    assert field.status_code == 400, field.text
    assert "call parse with `folder`" in field.json()["error"]["message"]
    # A retry resumes a parse where it is and files nothing: its ticket has no folder.
    retry = await _parse(api, documents["mixed_pdf"], token, retry="true")
    assert retry.status_code == 400, retry.text
    assert "parse without `folder`" in retry.json()["error"]["message"]

    trashed = await api.post(f"/api/library/folders/{papers}/trash", headers=CSRF_HEADERS)
    assert trashed.status_code == 200, trashed.text
    gone = await _parse(api, documents["mixed_pdf"], token)
    assert gone.status_code == 404, gone.text
    assert "in the Trash" in gone.json()["error"]["message"]

    # Refused before a job existed: the ticket can still upload, nothing was kept.
    ticket = await _ticket(sessionmaker, token)
    assert (ticket.spent_at, ticket.job_id) == (None, None)
    assert await _documents(sessionmaker) == []
    assert _uploads(api) == []


@respx.mock
async def test_an_api_key_files_with_the_folder_field(api, sessionmaker, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    accepted = await _parse(api, documents["text_pdf"], folder="Papers/2026")
    assert accepted.status_code == 202, accepted.text
    (row,) = await _documents(sessionmaker)
    assert accepted.json()["document_id"] == row.id
    listed = (await api.get("/v1/library")).json()
    assert [entry["path"] for entry in listed["folders"]] == ["Papers", "Papers/2026"]
    assert [entry["path"] for entry in listed["documents"]] == ["Papers/2026/text.pdf"]

    # A retry resumes a parse where it is; it is never a filing.
    retry = await _parse(api, documents["text_pdf"], folder="Papers", retry="true")
    assert retry.status_code == 400, retry.text
    assert len(await _documents(sessionmaker)) == 1


# --- a filed parse that failed --------------------------------------------------------------


@respx.mock
async def test_a_filed_parse_that_failed_is_retried_where_it_is(
    api, sessionmaker, documents
) -> None:
    upstream = Upstream({(MODEL, 2): lambda: httpx.Response(400)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    filing = await _mint(api, sessionmaker, into_library=True)
    accepted = await _parse(api, documents["mixed_pdf"], filing)
    job_id, document_id = accepted.json()["job_id"], accepted.json()["document_id"]
    await _drain_queue(api, sessionmaker)
    async with sessionmaker() as db:
        assert (await db.get(Job, uuid.UUID(job_id))).status == "failed"
    # A failed parse's row keeps its original, which the retry runs on.
    assert workspace_documents.original_of(api.root, document_id) is not None

    upstream.calls.clear()
    token = await _mint(api, sessionmaker)
    retried = await _parse(api, documents["mixed_pdf"], token, retry="true", model=FALLBACK)
    assert retried.status_code == 202, retried.text
    assert retried.json() == {"job_id": job_id, "status": "queued"}
    async with sessionmaker() as db:
        job = await db.get(Job, uuid.UUID(job_id))
    assert (job.status, job.pages_done, job.retry_model) == ("queued", 2, FALLBACK)
    # The library's retry runs on a copy of the original; the upload sent is not kept.
    assert [name.split(".retry-")[0] for name in _uploads(api)] == [job_id]
    ticket = await _ticket(sessionmaker, token)
    assert (str(ticket.job_id), ticket.spent_at is not None) == (job_id, True)

    # A retry that read the parse as failed before this one put it back is answered with
    # it, as it stands, its ticket spent on it; nothing is queued twice.
    second = await _mint(api, sessionmaker)
    second_id = (await _ticket(sessionmaker, second)).id
    async with sessionmaker() as db:
        answered = await retry_filed_parse(
            db,
            api.app.state.settings,
            user_id=api.user_id,
            ticket_id=second_id,
            job_id=uuid.UUID(job_id),
            model=None,
        )
    assert str(answered) == job_id
    late = await _ticket(sessionmaker, second)
    assert (str(late.job_id), late.spent_at is not None) == (job_id, True)
    assert [name.split(".retry-")[0] for name in _uploads(api)] == [job_id]

    await _drain_queue(api, sessionmaker)
    assert upstream.calls == [(FALLBACK, 2)]
    landed = await api.get("/v1/jobs/last/result.md", headers=_bearer(token))
    assert landed.status_code == 200, landed.text
    assert [entry.id for entry in await _documents(sessionmaker)] == [document_id]
