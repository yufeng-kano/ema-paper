"""Re-converting a document: a new parse of its original that the document takes only once
it succeeds, retried or dismissed when it fails (docs/jobs.md § Re-converting a document).
Every upstream call is stubbed (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select

from sightread.db.models import Document, Job, Result
from sightread.parsing.figures import crop_path
from sightread.workspace import documents as workspace_documents
from sightread.workspace import git
from sightread.workspace.documents import original_of
from tests.conftest import CSRF_HEADERS, KANO_CHAT_URL, PARSE_MODEL
from tests.helpers import FALLBACK, Upstream, completion, drain_queue
from tests.library_helpers import (
    fail_next_commit,
    library_row,
    retry_parse,
    upload_file,
)

# What the re-convert's model answers for every page. Its figure takes the whole page, as
# the first parse's does, so the figure check asks no model (docs/parsing.md § Figure check).
RECONVERTED = (
    "## Re-converted page\n\n"
    "![fig](sightread://p9/0,0,1000,1000)\n"
    "Figure 2: the photograph\n\n"
    "Better text."
)


def _upstream(**failing: int) -> Upstream:
    """The first parse on PARSE_MODEL, the re-convert on FALLBACK; `fallback_fails=N` makes
    FALLBACK refuse page N."""
    answers = {(FALLBACK, page): (lambda: completion(RECONVERTED)) for page in (1, 2, 3)}
    if "fallback_fails" in failing:
        answers[(FALLBACK, failing["fallback_fails"])] = lambda: httpx.Response(400)
    return Upstream(answers)


async def _reconvert(client: AsyncClient, document_id: int, **body) -> httpx.Response:
    return await client.post(
        f"/api/library/documents/{document_id}/reconvert", json=body, headers=CSRF_HEADERS
    )


async def _dismiss(client: AsyncClient, document_id: int) -> httpx.Response:
    return await client.delete(
        f"/api/library/documents/{document_id}/reconvert", headers=CSRF_HEADERS
    )


async def _content(client: AsyncClient, document_id: int) -> dict:
    response = await client.get(f"/api/library/documents/{document_id}/content")
    assert response.status_code == 200, response.text
    return response.json()


async def _baseline(sessionmaker, job_id: str) -> str:
    """What materialization writes for a parse: its reading copy when it was checked."""
    async with sessionmaker() as db:
        result = await db.get(Result, uuid.UUID(job_id))
    return result.reading_markdown if result.reading_markdown is not None else result.markdown


async def _parsed(client: AsyncClient, sessionmaker, documents) -> dict:
    """An uploaded PDF whose parse succeeded."""
    uploaded = await upload_file(client, documents["mixed_pdf"])
    await drain_queue(client, sessionmaker)
    assert (await library_row(client, uploaded["id"]))["status"] == "succeeded"
    return uploaded


def _directory(client: AsyncClient) -> Path:
    """The uploaded PDF's directory in the workspace."""
    return client.root / "files/mixed.pdf"


def _history(client: AsyncClient) -> list[git.Revision]:
    """The commits that touched the uploaded PDF's directory, newest first."""
    return git.log(client.root, Path("files/mixed.pdf"))


async def _not_taken(*args, **kwargs) -> bool:
    """The worker stopped between the parse's terminal commit and the swap."""
    return False


@respx.mock
async def test_the_document_keeps_its_version_until_the_new_parse_succeeds(
    parse_library, sessionmaker, documents
) -> None:
    upstream = _upstream()
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    first = await _content(parse_library, uploaded["id"])
    edited = await parse_library.put(
        f"/api/library/documents/{uploaded['id']}/content",
        json={"markdown": first["markdown"] + "\n\nMy edit.", "base_revision": first["revision"]},
        headers=CSRF_HEADERS,
    )
    assert edited.status_code == 200, edited.text
    notes = await parse_library.put(
        f"/api/library/documents/{uploaded['id']}/notes",
        json={"text": "My notes.", "base_revision": first["notes_revision"]},
        headers=CSRF_HEADERS,
    )
    assert notes.status_code == 200, notes.text

    upstream.calls.clear()
    started = await _reconvert(parse_library, uploaded["id"], model=FALLBACK)
    assert started.status_code == 202, started.text
    new_job = started.json()["job_id"]
    assert new_job != uploaded["job_id"]
    assert started.json()["status"] == "queued"

    # While it runs the document is as it was: its own parse, its edit, and the new parse
    # beside it on the row.
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["status"]) == (uploaded["job_id"], "succeeded")
    assert {key: row["reconvert"][key] for key in ("status", "model")} == {
        "status": "queued",
        "model": FALLBACK,
    }
    async with sessionmaker() as db:
        assert str((await db.get(Document, uploaded["id"])).reconvert_job_id) == new_job
    assert (await _content(parse_library, uploaded["id"]))["markdown"].endswith("My edit.")
    # One at a time.
    assert (await _reconvert(parse_library, uploaded["id"])).status_code == 409
    assert (await _dismiss(parse_library, uploaded["id"])).status_code == 409

    await drain_queue(parse_library, sessionmaker)

    # Every page ran again on the model named, none answered from the cache.
    assert sorted(upstream.calls) == [(FALLBACK, 1), (FALLBACK, 2), (FALLBACK, 3)]
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["status"], row["model"], row["reconvert"]) == (
        new_job,
        "succeeded",
        FALLBACK,
        None,
    )
    content = await _content(parse_library, uploaded["id"])
    assert content["markdown"] == await _baseline(sessionmaker, new_job)
    assert "Better text." in content["markdown"]
    assert content["notes"] == "My notes."
    meta = json.loads((_directory(parse_library) / ".meta.json").read_text())
    assert (meta["document_id"], meta["job_id"], meta["model"]) == (
        uploaded["id"],
        new_job,
        FALLBACK,
    )
    # The swap is one revision, and the edit it replaced stays in the history before it.
    revisions = _history(parse_library)
    assert revisions[0].message == "reconvert files/mixed.pdf"
    assert sorted(revisions[0].files) == [
        "files/mixed.pdf/.meta.json",
        "files/mixed.pdf/doc.md",
    ]
    assert any(entry.message == "edit files/mixed.pdf/doc.md" for entry in revisions[1:])
    # The new crops are in the shell's view, the earlier parse is still kept — the
    # retention's to delete (docs/jobs.md § Retention) — and the copy of the original the
    # run took is gone with it.
    assert (_directory(parse_library) / "figures" / "p1_0_0_1000_1000.png").is_file()
    async with sessionmaker() as db:
        assert await db.get(Job, uuid.UUID(uploaded["job_id"])) is not None
        job = await db.get(Job, uuid.UUID(new_job))
    assert job.source_deleted_at is not None
    assert not Path(job.source_path).exists()


@respx.mock
async def test_a_failed_reconvert_leaves_the_document_and_is_retried_or_dismissed(
    parse_library, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream(fallback_fails=2))
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    before = await _content(parse_library, uploaded["id"])

    started = await _reconvert(parse_library, uploaded["id"], model=FALLBACK)
    assert started.status_code == 202, started.text
    new_job = started.json()["job_id"]
    await drain_queue(parse_library, sessionmaker)

    # The document keeps its version, and the row says the re-convert failed.
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["status"]) == (uploaded["job_id"], "succeeded")
    assert {key: row["reconvert"][key] for key in ("status", "failed_pages")} == {
        "status": "failed",
        "failed_pages": 1,
    }
    async with sessionmaker() as db:
        assert str((await db.get(Document, uploaded["id"])).reconvert_job_id) == new_job
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == before["markdown"]
    # The pages a retry would send are the re-convert's.
    pages = (await parse_library.get(f"/api/library/documents/{uploaded['id']}/pages")).json()
    assert [(page["status"], page["model"]) for page in pages["pages"]] == [
        ("succeeded", FALLBACK),
        ("failed", FALLBACK),
        ("succeeded", FALLBACK),
    ]

    # Retried on a model that answers: only the failed page runs, and the document takes it.
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    retried = await retry_parse(parse_library, uploaded["id"], model=PARSE_MODEL)
    assert retried.status_code == 202, retried.text
    assert retried.json()["job_id"] == new_job
    await drain_queue(parse_library, sessionmaker)
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["status"], row["reconvert"]) == (new_job, "succeeded", None)
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == await _baseline(
        sessionmaker, new_job
    )

    # A second one that fails is dismissed: the pointer clears, the parse row stays.
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream(fallback_fails=1))
    failed = (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).json()["job_id"]
    await drain_queue(parse_library, sessionmaker)
    assert (await library_row(parse_library, uploaded["id"]))["reconvert"]["status"] == "failed"
    assert (await _dismiss(parse_library, uploaded["id"])).status_code == 204
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["reconvert"]) == (new_job, None)
    assert (await _dismiss(parse_library, uploaded["id"])).status_code == 404
    async with sessionmaker() as db:
        assert (await db.get(Job, uuid.UUID(failed))).status == "failed"


@respx.mock
async def test_a_failed_reconvert_gives_way_to_a_new_one(
    parse_library, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream(fallback_fails=3))
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    failed = (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).json()["job_id"]
    await drain_queue(parse_library, sessionmaker)

    again = await _reconvert(parse_library, uploaded["id"], model=PARSE_MODEL)
    assert again.status_code == 202, again.text
    assert (await library_row(parse_library, uploaded["id"]))["reconvert"] is not None
    async with sessionmaker() as db:
        row = await db.get(Document, uploaded["id"])
    assert str(row.reconvert_job_id) == again.json()["job_id"] != failed


@respx.mock
async def test_only_a_parsed_document_with_its_original_is_reconverted(
    parse_library, sessionmaker, documents
) -> None:
    upstream = Upstream({(PARSE_MODEL, 2): lambda: httpx.Response(400)})
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    # A markdown document has no parse.
    markdown = await parse_library.post(
        "/api/library/documents/new", json={"name": "plan.md"}, headers=CSRF_HEADERS
    )
    assert (await _reconvert(parse_library, markdown.json()["id"])).status_code == 404

    # A parse on its way is waited for, a failed one is retried instead.
    uploaded = await upload_file(parse_library, documents["mixed_pdf"])
    assert (await _reconvert(parse_library, uploaded["id"])).status_code == 409
    await drain_queue(parse_library, sessionmaker)
    assert (await library_row(parse_library, uploaded["id"]))["status"] == "failed"
    assert (await _reconvert(parse_library, uploaded["id"])).status_code == 409

    # A document that kept no original is uploaded again.
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    assert (await retry_parse(parse_library, uploaded["id"])).status_code == 202
    await drain_queue(parse_library, sessionmaker)
    original_of(parse_library.root, uploaded["id"]).unlink()
    refused = await _reconvert(parse_library, uploaded["id"])
    assert refused.status_code == 409
    assert "kept no original" in refused.json()["error"]["message"]
    async with sessionmaker() as db:
        jobs = (await db.execute(select(Job))).scalars().all()
    assert len(jobs) == 1  # nothing was queued


@respx.mock
async def test_a_document_in_the_trash_keeps_its_version(
    parse_library, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    before = await _content(parse_library, uploaded["id"])
    new_job = (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).json()["job_id"]
    trashed = await parse_library.post(
        f"/api/library/documents/{uploaded['id']}/trash", headers=CSRF_HEADERS
    )
    assert trashed.status_code == 200, trashed.text

    await drain_queue(parse_library, sessionmaker)

    async with sessionmaker() as db:
        row = await db.get(Document, uploaded["id"])
    assert (str(row.job_id), row.reconvert_job_id) == (uploaded["job_id"], None)
    restored = await parse_library.post(
        f"/api/library/documents/{uploaded['id']}/restore", headers=CSRF_HEADERS
    )
    assert restored.status_code == 200, restored.text
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == before["markdown"]
    # The new parse is finished, and stays for the retention to delete.
    async with sessionmaker() as db:
        assert (await db.get(Job, uuid.UUID(new_job))).status == "succeeded"


@respx.mock
async def test_a_reconvert_the_worker_did_not_take_is_taken_by_the_next_read(
    parse_library, sessionmaker, documents, monkeypatch
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    await _content(parse_library, uploaded["id"])
    new_job = (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).json()["job_id"]

    monkeypatch.setattr("sightread.jobs.runner.take_reconvert", _not_taken)
    await drain_queue(parse_library, sessionmaker)
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["reconvert"]["status"]) == (uploaded["job_id"], "succeeded")

    result = await parse_library.get(f"/api/library/documents/{uploaded['id']}/result")
    assert result.status_code == 200, result.text
    assert result.json()["meta"]["job_id"] == new_job
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == await _baseline(
        sessionmaker, new_job
    )
    assert (await library_row(parse_library, uploaded["id"]))["reconvert"] is None


@respx.mock
async def test_a_swap_whose_commit_fails_leaves_the_files_ahead_for_the_next_taker(
    parse_library, sessionmaker, documents, monkeypatch
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    await _content(parse_library, uploaded["id"])
    new_job = (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).json()["job_id"]
    monkeypatch.setattr("sightread.jobs.runner.take_reconvert", _not_taken)
    await drain_queue(parse_library, sessionmaker)
    monkeypatch.undo()

    settings = parse_library.app.state.settings
    fail_next_commit(monkeypatch, landed=False)
    async with sessionmaker() as db:
        row = await db.get(Document, uploaded["id"])
        with pytest.raises(Exception, match="the connection dropped"):
            await workspace_documents.take_reconvert(db, settings, row)
    monkeypatch.undo()

    # The files took the new parse as their revision; the row did not, so it still points.
    doc = _directory(parse_library) / "doc.md"
    assert doc.read_text() == await _baseline(sessionmaker, new_job)
    async with sessionmaker() as db:
        row = await db.get(Document, uploaded["id"])
    assert (str(row.job_id), str(row.reconvert_job_id)) == (uploaded["job_id"], new_job)

    # The next read finishes it, with no second revision.
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == doc.read_text()
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["reconvert"]) == (new_job, None)
    revisions = _history(parse_library)
    assert [entry.message for entry in revisions].count("reconvert files/mixed.pdf") == 1


@respx.mock
async def test_a_swap_that_cannot_be_written_leaves_the_document_as_it_was_and_readable(
    parse_library, sessionmaker, documents, monkeypatch
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    before = await _content(parse_library, uploaded["id"])
    new_job = (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).json()["job_id"]
    monkeypatch.setattr("sightread.jobs.runner.take_reconvert", _not_taken)
    await drain_queue(parse_library, sessionmaker)
    monkeypatch.undo()

    # The disk fills between the two files the swap writes.
    write_text = workspace_documents.safe_io.write_text

    def full_at_meta(root, path, text, **kwargs):
        if path.name == ".meta.json":
            raise OSError(28, "No space left on device")
        write_text(root, path, text, **kwargs)

    monkeypatch.setattr(workspace_documents.safe_io, "write_text", full_at_meta)
    directory = _directory(parse_library)
    meta = (directory / ".meta.json").read_text()

    # Each read tries the swap, which rolls back, and answers with the version it has.
    result = await parse_library.get(f"/api/library/documents/{uploaded['id']}/result")
    assert result.status_code == 200, result.text
    assert result.json()["meta"]["job_id"] == uploaded["job_id"]
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == before["markdown"]
    assert (directory / "doc.md").read_text() == before["markdown"]
    assert (directory / ".meta.json").read_text() == meta
    revisions = _history(parse_library)
    assert "reconvert files/mixed.pdf" not in [entry.message for entry in revisions]
    row = await library_row(parse_library, uploaded["id"])
    assert (row["job_id"], row["reconvert"]["status"]) == (uploaded["job_id"], "succeeded")
    monkeypatch.undo()

    # With room again, the next read takes it.
    assert (await _content(parse_library, uploaded["id"]))["markdown"] == await _baseline(
        sessionmaker, new_job
    )
    assert (await library_row(parse_library, uploaded["id"]))["job_id"] == new_job


@respx.mock
async def test_a_figure_from_before_a_reconvert_keeps_its_crop(
    parse_library, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    # A crop only the first parse made: a placeholder in a reverted text would name it.
    figures_dir = Path(parse_library.app.state.settings.figures_dir)
    only_before = crop_path(figures_dir, uuid.UUID(uploaded["job_id"]), 2, (100, 100, 900, 900))
    only_before.write_bytes(
        crop_path(figures_dir, uuid.UUID(uploaded["job_id"]), 1, (0, 0, 1000, 1000)).read_bytes()
    )
    (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).raise_for_status()
    await drain_queue(parse_library, sessionmaker)
    assert (await library_row(parse_library, uploaded["id"]))["job_id"] != uploaded["job_id"]

    base = f"/api/library/documents/{uploaded['id']}/figures"
    # The document's own parse first, then the earlier parse of the same original.
    assert (await parse_library.get(f"{base}/1/0,0,1000,1000")).status_code == 200
    kept = await parse_library.get(f"{base}/2/100,100,900,900")
    assert kept.status_code == 200
    assert kept.content == only_before.read_bytes()
    assert (await parse_library.get(f"{base}/2/200,200,800,800")).status_code == 404


@respx.mock
async def test_a_reconvert_takes_the_earlier_parse_copies_out_of_the_directory(
    parse_library, sessionmaker, documents
) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=_upstream())
    uploaded = await _parsed(parse_library, sessionmaker, documents)
    figures_dir = Path(parse_library.app.state.settings.figures_dir)
    earlier = uuid.UUID(uploaded["job_id"])
    kept = crop_path(figures_dir, earlier, 1, (0, 0, 1000, 1000))
    # Two crops only the first parse made, copied in as materialization copies them; a
    # command then wrote over the second copy.
    dropped = crop_path(figures_dir, earlier, 2, (100, 100, 900, 900))
    rewritten = crop_path(figures_dir, earlier, 3, (100, 100, 900, 900))
    view = _directory(parse_library) / "figures"
    for crop in (dropped, rewritten):
        crop.write_bytes(kept.read_bytes())
        (view / crop.name).write_bytes(kept.read_bytes())
    (view / rewritten.name).write_bytes(b"the agent's own picture")
    (await _reconvert(parse_library, uploaded["id"], model=FALLBACK)).raise_for_status()
    await drain_queue(parse_library, sessionmaker)
    assert (await library_row(parse_library, uploaded["id"]))["job_id"] != uploaded["job_id"]
    # The new parse's crops are in the directory, the earlier parse's copy is not — the
    # quota would count it, no longer the document's parse — and what a command wrote
    # stays. The crops themselves stay with their parse (docs/jobs.md § Re-converting).
    assert (view / kept.name).is_file()
    assert not (view / dropped.name).exists()
    assert (view / rewritten.name).read_bytes() == b"the agent's own picture"
    assert dropped.is_file() and rewritten.is_file()
