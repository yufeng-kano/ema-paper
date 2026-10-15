"""The web file library (docs/web.md § Files): folders, files, and the one upload path.

No upstream traffic here. A file uploaded in these tests is enqueued and left queued —
what is under test is the library around a parse, not the parse (docs/testing.md § Cost
safety).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import Document, Job, JobPage, Result, User
from sightread.routes.library import DOCUMENT_NAME_MAX, free_name
from tests.conftest import CSRF_HEADERS

MODEL = "vendor/vision-model"


@pytest.fixture
async def library(client: AsyncClient) -> AsyncClient:
    """A signed-in client whose account can enqueue a parse (a default model is set)."""
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    settings = await client.put(
        "/api/settings",
        json={"default_model": MODEL, "default_profile": None},
        headers=CSRF_HEADERS,
    )
    assert settings.status_code == 200
    return client


async def _folder(client: AsyncClient, name: str, parent_id: int | None = None) -> dict:
    response = await client.post(
        "/api/library/folders",
        json={"name": name, "parent_id": parent_id},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload(
    client: AsyncClient,
    documents,
    name: str = "tiny.png",
    folder_id: int | None = None,
) -> httpx.Response:
    return await client.post(
        "/api/library/documents",
        files={"file": (name, documents["png"].read_bytes(), "image/png")},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )


async def _read(client: AsyncClient) -> dict:
    response = await client.get("/api/library")
    assert response.status_code == 200, response.text
    return response.json()


# --- folders --------------------------------------------------------------------------


async def test_folders_nest_and_the_whole_tree_comes_back_in_one_read(library) -> None:
    parent = await _folder(library, "Invoices")
    child = await _folder(library, "2026", parent_id=parent["id"])

    body = await _read(library)
    by_id = {row["id"]: row for row in body["folders"]}
    assert by_id[parent["id"]]["parent_id"] is None
    assert by_id[child["id"]]["parent_id"] == parent["id"]
    assert body["documents"] == []


async def test_a_repeated_folder_name_is_suffixed_rather_than_refused(library) -> None:
    first = await _folder(library, "Invoices")
    second = await _folder(library, "Invoices")
    assert first["name"] == "Invoices"
    assert second["name"] == "Invoices (2)"

    # The suffix is per place, so the same name is free again one level down.
    nested = await _folder(library, "Invoices", parent_id=first["id"])
    assert nested["name"] == "Invoices"


async def test_a_rename_onto_a_taken_name_is_refused(library) -> None:
    await _folder(library, "Invoices")
    other = await _folder(library, "Receipts")

    clash = await library.put(
        f"/api/library/folders/{other['id']}", json={"name": "Invoices"}, headers=CSRF_HEADERS
    )
    assert clash.status_code == 409

    renamed = await library.put(
        f"/api/library/folders/{other['id']}", json={"name": "Statements"}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Statements"


async def test_a_move_that_collides_is_suffixed(library) -> None:
    destination = await _folder(library, "Archive")
    await _folder(library, "2026", parent_id=destination["id"])
    moving = await _folder(library, "2026")

    moved = await library.put(
        f"/api/library/folders/{moving['id']}",
        json={"parent_id": destination["id"]},
        headers=CSRF_HEADERS,
    )
    assert moved.status_code == 200
    assert moved.json()["parent_id"] == destination["id"]
    assert moved.json()["name"] == "2026 (2)"


async def test_a_folder_cannot_be_moved_into_its_own_subtree(library) -> None:
    root = await _folder(library, "Invoices")
    child = await _folder(library, "2026", parent_id=root["id"])
    grandchild = await _folder(library, "Q1", parent_id=child["id"])

    for target in (root["id"], grandchild["id"]):
        response = await library.put(
            f"/api/library/folders/{root['id']}",
            json={"parent_id": target},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 400, target
        assert "inside itself" in response.json()["error"]["message"]


async def test_deleting_a_folder_takes_its_subtree_and_the_files_in_it(
    library, sessionmaker, documents
) -> None:
    root = await _folder(library, "Invoices")
    child = await _folder(library, "2026", parent_id=root["id"])
    inside = await _upload(library, documents, folder_id=child["id"])
    assert inside.status_code == 201, inside.text
    outside = await _upload(library, documents, name="kept.png")
    assert outside.status_code == 201

    deleted = await library.delete(f"/api/library/folders/{root['id']}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204

    body = await _read(library)
    assert body["folders"] == []
    assert [row["name"] for row in body["documents"]] == ["kept.png"]
    # The parse itself is history, and history is not deleted with the folder.
    async with sessionmaker() as db:
        assert await db.get(Job, uuid.UUID(inside.json()["job_id"])) is not None


# --- documents ------------------------------------------------------------------------


async def test_an_upload_becomes_a_queued_job_and_a_file(library, sessionmaker, documents) -> None:
    created = await _upload(library, documents)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "tiny.png"
    assert body["folder_id"] is None
    assert body["status"] == "queued"
    assert body["kind"] == "image"
    assert body["model"] == MODEL
    assert body["page_count"] == 1
    assert body["size_bytes"] > 0

    listed = (await _read(library))["documents"]
    assert [row["id"] for row in listed] == [body["id"]]

    async with sessionmaker() as db:
        document = (await db.execute(select(Document))).scalars().one()
        job = await db.get(Job, document.job_id)
    assert job.status == "queued"
    assert job.filename == "tiny.png"


def test_a_collision_suffix_never_pushes_a_name_past_its_column() -> None:
    # A name already at the limit has to give up room for the suffix: PostgreSQL answers a
    # name one character too long with a truncation error, and for an upload that lands
    # after the job is committed and already parsing.
    at_limit = f"{'x' * (DOCUMENT_NAME_MAX - 4)}.pdf"
    suffixed = free_name({at_limit}, at_limit, DOCUMENT_NAME_MAX)

    assert suffixed != at_limit
    assert len(suffixed) <= DOCUMENT_NAME_MAX
    assert suffixed.endswith(" (2).pdf")


async def test_an_upload_at_the_name_limit_still_lands(library, documents) -> None:
    long_name = f"{'x' * (DOCUMENT_NAME_MAX - 4)}.png"
    first = await _upload(library, documents, name=long_name)
    second = await _upload(library, documents, name=long_name)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert len(second.json()["name"]) <= DOCUMENT_NAME_MAX
    assert second.json()["name"] != first.json()["name"]


async def test_the_same_filename_twice_is_suffixed_before_the_extension(library, documents) -> None:
    first = await _upload(library, documents)
    second = await _upload(library, documents)
    assert first.json()["name"] == "tiny.png"
    assert second.json()["name"] == "tiny (2).png"


async def test_a_retried_upload_does_not_buy_a_second_parse(
    library, sessionmaker, documents
) -> None:
    # The browser cannot tell a lost response from a lost request, so it retries an upload
    # that may already have landed. The same bytes, still queued, must not become a second
    # parse — that is the user's own key being spent twice for one document.
    first = (await _upload(library, documents)).json()
    second = (await _upload(library, documents)).json()

    assert second["job_id"] == first["job_id"]
    assert second["id"] != first["id"]
    async with sessionmaker() as db:
        assert len((await db.execute(select(Job))).scalars().all()) == 1


async def test_a_body_past_the_cap_is_refused_while_it_streams(make_client, documents) -> None:
    # No `Content-Length` to reject up front: the parser spools file parts to disk as they
    # arrive, so the cap has to bite on the stream itself.
    client = make_client(upload_max_bytes=1024)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    await client.put(
        "/api/settings",
        json={"default_model": MODEL, "default_profile": None},
        headers=CSRF_HEADERS,
    )

    boundary = "sightreadtestboundary"

    async def chunked():
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        for _ in range(16):
            yield b"x" * 16384
        yield f"\r\n--{boundary}--\r\n".encode()

    response = await client.post(
        "/api/library/documents",
        content=chunked(),
        headers={**CSRF_HEADERS, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 413
    upload_dir = Path(client.app.state.settings.upload_dir)
    assert not upload_dir.exists() or list(upload_dir.iterdir()) == []


async def test_moving_a_file_onto_a_taken_name_is_suffixed(library, documents) -> None:
    folder = await _folder(library, "Invoices")
    await _upload(library, documents, folder_id=folder["id"])
    moving = (await _upload(library, documents)).json()

    moved = await library.put(
        f"/api/library/documents/{moving['id']}",
        json={"folder_id": folder["id"]},
        headers=CSRF_HEADERS,
    )

    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder["id"]
    assert moved.json()["name"] == "tiny (2).png"


async def test_an_upload_without_a_configured_model_is_refused(client, documents) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    response = await _upload(client, documents)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request"


async def test_an_upload_into_a_folder_that_is_not_yours_is_a_404(library, documents) -> None:
    response = await _upload(library, documents, folder_id=9999)
    assert response.status_code == 404
    assert (await _read(library))["documents"] == []


async def test_a_file_is_renamed_moved_and_deleted_without_touching_its_parse(
    library, sessionmaker, documents
) -> None:
    folder = await _folder(library, "Invoices")
    created = (await _upload(library, documents)).json()
    job_id = created["job_id"]

    renamed = await library.put(
        f"/api/library/documents/{created['id']}",
        json={"name": "March invoice.png"},
        headers=CSRF_HEADERS,
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "March invoice.png"

    moved = await library.put(
        f"/api/library/documents/{created['id']}",
        json={"folder_id": folder["id"]},
        headers=CSRF_HEADERS,
    )
    assert moved.status_code == 200
    assert moved.json()["folder_id"] == folder["id"]
    assert moved.json()["name"] == "March invoice.png"

    deleted = await library.delete(f"/api/library/documents/{created['id']}", headers=CSRF_HEADERS)
    assert deleted.status_code == 204
    assert (await _read(library))["documents"] == []

    # Deleting the file deletes the entry; the job stays in the parse history.
    history = (await library.get("/api/jobs")).json()["jobs"]
    assert [row["job_id"] for row in history] == [job_id]


async def test_a_document_serves_the_result_of_its_own_job(
    library, sessionmaker, documents
) -> None:
    created = (await _upload(library, documents)).json()

    # Queued with no result: a partial answer, empty so far (docs/api.md § Partial results).
    pending = await library.get(f"/api/library/documents/{created['id']}/result")
    assert pending.status_code == 200
    assert pending.json()["meta"]["partial"] is True
    assert pending.json()["markdown"] == ""

    async with sessionmaker() as db:
        db.add(
            Result(
                job_id=uuid.UUID(created["job_id"]),
                markdown="<!-- page: 1 -->\n# Title",
                pages=[{"page": 1, "width_pt": 612, "height_pt": 792, "method": "vision"}],
                figures=[],
                errors=[],
                meta={"model": MODEL, "bbox_format": "yxyx_norm1000"},
            )
        )
        await db.commit()

    ready = await library.get(f"/api/library/documents/{created['id']}/result")
    assert ready.status_code == 200
    assert ready.json()["markdown"].endswith("# Title")


async def test_a_running_document_serves_its_finished_pages(
    library, sessionmaker, documents
) -> None:
    """Partial results: the finished pages, assembled, while the job still runs
    (docs/api.md § Partial results)."""
    created = (await _upload(library, documents)).json()
    job_id = uuid.UUID(created["job_id"])

    async with sessionmaker() as db:
        await db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status="running", page_count=3, pages_done=2)
        )
        db.add(
            JobPage(
                job_id=job_id,
                page_no=1,
                method="vision",
                status="succeeded",
                # The model's own page claim ("p9") must be renumbered to ours, exactly as
                # the final assembly does.
                markdown="# One\n\n![fig](sightread://p9/10,20,30,40)\nFigure 1: chart",
            )
        )
        db.add(JobPage(job_id=job_id, page_no=2, status="failed", error="render failed"))
        await db.commit()

    response = await library.get(f"/api/library/documents/{created['id']}/result")
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["partial"] is True
    assert (body["meta"]["page_count"], body["meta"]["pages_done"]) == (3, 2)
    assert "<!-- page: 1 -->" in body["markdown"]
    assert "![fig1](sightread://p1/10,20,30,40)" in body["markdown"]
    assert body["figures"] == [
        {"id": "fig1", "page": 1, "bbox": [10, 20, 30, 40], "caption": "Figure 1: chart"}
    ]
    assert body["errors"] == [{"page": 2, "reason": "render failed"}]
    assert [page["page"] for page in body["pages"]] == [1, 2]


async def test_a_documents_stored_figure_crop_is_served(library, tmp_path, documents) -> None:
    created = (await _upload(library, documents)).json()

    crop = tmp_path / "figures" / created["job_id"] / "p1_10_20_30_40.png"
    crop.parent.mkdir(parents=True)
    crop.write_bytes(documents["png"].read_bytes())

    ok = await library.get(f"/api/library/documents/{created['id']}/figures/1/10,20,30,40")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"
    assert ok.content == documents["png"].read_bytes()

    # No crop stored for this box, and a bbox that is not one — both are plain 404s.
    missing = await library.get(f"/api/library/documents/{created['id']}/figures/1/10,20,30,41")
    assert missing.status_code == 404
    invalid = await library.get(f"/api/library/documents/{created['id']}/figures/1/evil")
    assert invalid.status_code == 404


# --- filing a parse from the history ----------------------------------------------------


async def _file_job(client: AsyncClient, job_id: str, folder_id: int | None = None):
    return await client.post(
        "/api/library/documents/from-job",
        json={"job_id": job_id, "folder_id": folder_id},
        headers=CSRF_HEADERS,
    )


async def test_a_parse_in_the_history_can_be_filed_without_a_second_parse(
    library, sessionmaker, documents
) -> None:
    uploaded = (await _upload(library, documents)).json()
    folder = await _folder(library, "Papers")

    filed = await _file_job(library, uploaded["job_id"], folder["id"])
    assert filed.status_code == 201, filed.text
    body = filed.json()
    assert body["job_id"] == uploaded["job_id"]
    assert body["folder_id"] == folder["id"]
    assert body["name"] == "tiny.png"
    assert body["status"] == "queued"

    async with sessionmaker() as db:
        assert len((await db.execute(select(Job))).scalars().all()) == 1


async def test_filing_a_parse_twice_into_one_folder_suffixes_the_name(library, documents) -> None:
    uploaded = (await _upload(library, documents)).json()
    filed = await _file_job(library, uploaded["job_id"])
    assert filed.status_code == 201, filed.text
    assert filed.json()["name"] == "tiny (2).png"


async def test_a_failed_parse_cannot_be_filed(library, sessionmaker, documents) -> None:
    uploaded = (await _upload(library, documents)).json()
    async with sessionmaker() as db:
        await db.execute(
            update(Job).where(Job.id == uuid.UUID(uploaded["job_id"])).values(status="failed")
        )
        await db.commit()

    refused = await _file_job(library, uploaded["job_id"])
    assert refused.status_code == 400
    assert len((await _read(library))["documents"]) == 1


async def test_a_parse_whose_filename_cleans_to_nothing_is_still_filed(
    library, sessionmaker, documents
) -> None:
    uploaded = (await _upload(library, documents)).json()
    async with sessionmaker() as db:
        await db.execute(
            update(Job).where(Job.id == uuid.UUID(uploaded["job_id"])).values(filename="//")
        )
        await db.commit()

    filed = await _file_job(library, uploaded["job_id"])
    assert filed.status_code == 201, filed.text
    assert filed.json()["name"] == "upload"


async def test_filing_needs_a_known_job_and_a_known_folder(library, documents) -> None:
    assert (await _file_job(library, str(uuid.uuid4()))).status_code == 404
    uploaded = (await _upload(library, documents)).json()
    assert (await _file_job(library, uploaded["job_id"], 999_999)).status_code == 404


# --- ownership and CSRF ---------------------------------------------------------------


async def test_another_accounts_library_is_invisible(
    library, make_client, sessionmaker, documents
) -> None:
    folder = await _folder(library, "Invoices")
    document = (await _upload(library, documents)).json()

    stranger = make_client()
    async with sessionmaker() as db:
        other = User(google_sub="stranger", email="stranger@example.com")
        db.add(other)
        await db.flush()
        token = await create_session(db, other)
        await db.commit()
    # No domain: `http.cookiejar` rewrites a dotless host to "testserver.local", and a
    # domain-scoped cookie then never matches the request it was set for.
    stranger.cookies.set(SESSION_COOKIE, token)

    assert (await stranger.get("/api/library")).json() == {"folders": [], "documents": []}
    assert (
        await stranger.put(
            f"/api/library/folders/{folder['id']}", json={"name": "Mine"}, headers=CSRF_HEADERS
        )
    ).status_code == 404
    assert (
        await stranger.delete(f"/api/library/folders/{folder['id']}", headers=CSRF_HEADERS)
    ).status_code == 404
    assert (
        await stranger.get(f"/api/library/documents/{document['id']}/result")
    ).status_code == 404
    assert (
        await stranger.delete(f"/api/library/documents/{document['id']}", headers=CSRF_HEADERS)
    ).status_code == 404
    assert (await _file_job(stranger, document["job_id"])).status_code == 404
    # Nothing the stranger tried removed anything.
    assert len((await _read(library))["folders"]) == 1


async def test_library_mutations_need_the_csrf_header(library, documents) -> None:
    assert (
        await library.post("/api/library/folders", json={"name": "Invoices"})
    ).status_code == 403
    assert (
        await library.post(
            "/api/library/documents",
            files={"file": ("tiny.png", documents["png"].read_bytes(), "image/png")},
        )
    ).status_code == 403
    assert (
        await library.post(
            "/api/library/documents/from-job", json={"job_id": str(uuid.uuid4())}
        )
    ).status_code == 403


async def test_the_library_is_signed_in_only(client: AsyncClient) -> None:
    assert (await client.get("/api/library")).status_code == 401


async def test_me_carries_the_upload_limits(library) -> None:
    limits = (await library.get("/api/me")).json()["limits"]
    assert limits["upload_max_bytes"] > 0
    assert limits["page_cap"] > 0
    assert "application/pdf" in limits["accepted_media_types"]
    assert "image/png" in limits["accepted_media_types"]
