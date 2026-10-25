"""What the worker's sweep deletes by age (docs/jobs.md § Retention, docs/workspace.md § The
Trash): the Trash's items past `TRASH_RETENTION_DAYS`, the finished parses no file keeps
past `PARSE_RETENTION_DAYS`, and the margin that keeps the dedup cache from answering with
a parse the next sweep would delete.

No upstream traffic: the parses are rows written here, the documents markdown.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from sightread.agents import PARSE
from sightread.auth.api_keys import create_api_key
from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import (
    ChatTurn,
    Conversation,
    CreditEntry,
    CreditPeriod,
    Document,
    Folder,
    Job,
    Result,
    UsageLog,
    User,
    utcnow,
)
from sightread.jobs.queue import find_cached_job
from sightread.jobs.sweeper import expire_trash, purge_parses
from sightread.workspace.documents import original_of
from sightread.workspace.layout import images_dir, results_root, trash_root, user_root
from tests.conftest import CSRF_HEADERS, DATABASE_URL, KANO_BASE, KANO_CHAT_URL, PARSE_MODEL
from tests.test_document_images import _image, _insert
from tests.test_library_trash import _document, _folder, _read, _trash, _write
from tests.test_markdown_documents import _upload_markdown
from tests.test_parse_end_to_end import _drain_queue, _vision_stub

postgres_only = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="the job's foreign-key actions need PostgreSQL",
)


@pytest.fixture
async def writer(make_client, tmp_path) -> AsyncClient:
    """Signed in, with a workspace of its own and no upstream connected."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"))
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _stranger(
    make_client, tmp_path, email: str = "stranger@example.com", **overrides
) -> AsyncClient:
    """A second account on the same deployment, signed in."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"), **overrides)
    async with client.app.state.sessionmaker() as db:
        user = User(google_sub=email, email=email)
        db.add(user)
        await db.flush()
        token = await create_session(db, user)
        await db.commit()
        client.user_id = user.id
    client.cookies.set(SESSION_COOKIE, token)
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _age(client: AsyncClient, model: type, row_id: int, *, days: float) -> None:
    """Put an item in the Trash `days` ago."""
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(model)
            .where(model.id == row_id)
            .values(trashed_at=utcnow() - timedelta(days=days))
        )
        await db.commit()


async def _expire(client: AsyncClient) -> int:
    return await expire_trash(client.app.state.sessionmaker, client.app.state.settings)


# --- the Trash ----------------------------------------------------------------------------


async def test_the_trash_deletes_for_good_what_it_kept_past_its_time(writer) -> None:
    # Uploads keep originals; the edits give each a directory, so a Trash entry.
    old = (await _upload_markdown(writer, b"# Old\n", name="old.md")).json()
    await _write(writer, old["id"], "# Old, edited\n")
    assert (await _insert(writer, old["id"], _image("PNG"))).status_code == 201
    box = await _folder(writer, "Box")
    inner = await _folder(writer, "Inner", box["id"])
    boxed = (
        await _upload_markdown(writer, b"# Boxed\n", name="boxed.md", folder_id=inner["id"])
    ).json()
    await _write(writer, boxed["id"], "# Boxed, edited\n")
    young = (await _upload_markdown(writer, b"# Young\n", name="young.md")).json()
    await _write(writer, young["id"], "# Young, edited\n")
    stays = await _document(writer, "stays.md", None, "# Stays\n")
    for kind, row in (("documents", old), ("folders", box), ("documents", young)):
        assert (await _trash(writer, kind, row["id"])).status_code == 200
    assert sorted(path.name for path in trash_root(writer.root).iterdir()) == [
        f"document-{old['id']}",
        f"document-{young['id']}",
        f"folder-{box['id']}",
    ]
    assert (images_dir(writer.root) / str(old["id"])).is_dir()
    # Past the 30 days by an hour, and a day short of them.
    await _age(writer, Document, old["id"], days=30.05)
    await _age(writer, Folder, box["id"], days=30.05)
    await _age(writer, Document, young["id"], days=29)

    assert await _expire(writer) == 2

    async with writer.app.state.sessionmaker() as db:
        for model, row_id in (
            (Document, old["id"]),
            (Folder, box["id"]),
            (Folder, inner["id"]),
            (Document, boxed["id"]),
        ):
            assert await db.get(model, row_id) is None
    # Their entries, originals and images went with the rows, as Delete Immediately
    # takes them.
    entries = [path.name for path in trash_root(writer.root).iterdir()]
    assert entries == [f"document-{young['id']}"]
    for row in (old, boxed):
        assert original_of(writer.root, row["id"]) is None
    assert not (images_dir(writer.root) / str(old["id"])).exists()
    # The younger item waits in the Trash, whole; the live file is untouched.
    library = await _read(writer)
    assert [row["id"] for row in library["trash"]["documents"]] == [young["id"]]
    assert library["trash"]["folders"] == []
    assert [row["id"] for row in library["documents"]] == [stays["id"]]
    assert original_of(writer.root, young["id"]) is not None
    # Nothing more is due.
    assert await _expire(writer) == 0


async def test_a_trash_with_a_conversation_still_answering_waits_while_the_others_empty(
    writer, make_client, tmp_path
) -> None:
    stranger = await _stranger(make_client, tmp_path)
    mine = (await _upload_markdown(writer, b"# Mine\n", name="mine.md")).json()
    theirs = (await _upload_markdown(stranger, b"# Theirs\n", name="theirs.md")).json()
    for client, row in ((writer, mine), (stranger, theirs)):
        assert (await _trash(client, "documents", row["id"])).status_code == 200
        await _age(client, Document, row["id"], days=40)
    # A turn about the trashed file still running: deleting it now would pull the rows
    # and the directory from under the worker.
    async with writer.app.state.sessionmaker() as db:
        conversation = Conversation(user_id=writer.user_id, document_id=mine["id"], session="s")
        db.add(conversation)
        await db.flush()
        turn = ChatTurn(
            user_id=writer.user_id,
            conversation_id=conversation.id,
            message_id=1,
            status="running",
        )
        db.add(turn)
        await db.commit()
        turn_id = turn.id

    assert await _expire(writer) == 1
    assert [row["id"] for row in (await _read(writer))["trash"]["documents"]] == [mine["id"]]
    assert (await _read(stranger))["trash"]["documents"] == []
    assert not (trash_root(stranger.root) / f"document-{theirs['id']}").exists()

    # Once the turn is done, the next sweep takes it.
    async with writer.app.state.sessionmaker() as db:
        await db.execute(
            update(ChatTurn)
            .where(ChatTurn.id == turn_id)
            .values(status="succeeded", finished_at=utcnow())
        )
        await db.commit()
    assert await _expire(writer) == 1
    assert (await _read(writer))["trash"]["documents"] == []


async def test_a_trash_retention_of_zero_never_empties_the_trash(writer) -> None:
    writer.app.state.settings.trash_retention_days = 0
    kept = (await _upload_markdown(writer, b"# Kept\n", name="kept.md")).json()
    assert (await _trash(writer, "documents", kept["id"])).status_code == 200
    await _age(writer, Document, kept["id"], days=3650)

    assert await _expire(writer) == 0
    assert [row["id"] for row in (await _read(writer))["trash"]["documents"]] == [kept["id"]]
    assert original_of(writer.root, kept["id"]) is not None


# --- parses ---------------------------------------------------------------------------


async def _parse(
    db,
    user_id: int,
    *,
    sha256: str | None = None,
    status: str = "succeeded",
    finished_days: float | None = 40,
) -> uuid.UUID:
    """A parse row as the worker leaves it, finished `finished_days` ago (None: not
    finished), with its result when it succeeded."""
    job = Job(
        id=uuid.uuid4(),
        user_id=user_id,
        agent="parse",
        kind="pdf",
        filename="paper.pdf",
        media_type="application/pdf",
        size_bytes=8,
        sha256=sha256 or uuid.uuid4().hex * 2,
        model=PARSE_MODEL,
        connection_base_url=KANO_BASE,
        pipeline_version=PARSE.pipeline_version,
        bbox_format="yxyx_norm1000",
        status=status,
        page_count=1,
        pages_done=0 if finished_days is None else 1,
        created_at=utcnow() - timedelta(days=41),
        finished_at=None if finished_days is None else utcnow() - timedelta(days=finished_days),
    )
    db.add(job)
    await db.flush()
    if status == "succeeded":
        db.add(
            Result(job_id=job.id, markdown="# Paper\n", pages=[], figures=[], errors=[], meta={})
        )
        await db.flush()
    return job.id


def _leave_files(settings, root: Path, job_id: uuid.UUID) -> list[Path]:
    """What a parse leaves outside its rows: its crops and an attempt's, the workspace copy
    of its result, and upload-directory leftovers named for it."""
    figures, uploads = Path(settings.figures_dir), Path(settings.upload_dir)
    paths = [
        figures / str(job_id) / "p1_0_0_500_500.png",
        figures / f"{job_id}.attempt" / "p1_0_0_500_500.png",
        results_root(root) / str(job_id) / "result.md",
        uploads / f"{job_id}.pdf",
        uploads / f"{job_id}.retry-1",
        uploads / f"{job_id}.0a1b" / "page-1.png",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    return paths


@postgres_only
async def test_a_parse_no_file_keeps_is_deleted_after_its_retention(
    writer, make_client, tmp_path
) -> None:
    settings = writer.app.state.settings
    stranger = await _stranger(make_client, tmp_path)
    period_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with writer.app.state.sessionmaker() as db:
        uid = writer.user_id
        # Kept: a file's parse, a trashed file's, a re-convert waiting to be taken, an
        # earlier parse of a file's bytes, a young one, and the ones not finished.
        filed = await _parse(db, uid, sha256="f" * 64)
        trashed = await _parse(db, uid)
        reconverted = await _parse(db, uid)
        earlier = await _parse(db, uid, sha256="f" * 64, finished_days=90)
        young = await _parse(db, uid, finished_days=29)
        queued = await _parse(db, uid, status="queued", finished_days=None)
        running = await _parse(db, uid, status="running", finished_days=None)
        db.add_all(
            [
                Document(user_id=uid, job_id=filed, reconvert_job_id=reconverted, name="a.pdf"),
                Document(user_id=uid, job_id=trashed, name="b.pdf", trashed_at=utcnow()),
            ]
        )
        # Deleted: a parse no file names, a failed one, and one whose bytes only another
        # account's file holds — the rule never looks across accounts.
        orphan = await _parse(db, uid)
        failed = await _parse(db, uid, status="failed")
        # A row from before `finished_at` was always written counts from its creation.
        unstamped = await _parse(db, uid, finished_days=None)
        foreign = await _parse(db, uid, sha256="e" * 64)
        theirs = await _parse(db, stranger.user_id, sha256="e" * 64)
        db.add(Document(user_id=stranger.user_id, job_id=theirs, name="c.pdf"))
        # What the orphan cost, in the ledger and in the usage record.
        db.add(
            CreditPeriod(
                user_id=uid,
                period_start=period_start,
                period_end=period_start + timedelta(days=30),
                granted=150,
                spent=3,
            )
        )
        db.add(
            CreditEntry(
                user_id=uid,
                period_start=period_start,
                kind="parse",
                state="consumed",
                amount=3,
                job_id=orphan,
                settled_at=utcnow(),
            )
        )
        db.add(
            UsageLog(
                user_id=uid,
                job_id=orphan,
                agent="parse",
                model=PARSE_MODEL,
                prompt_tokens=10,
                completion_tokens=5,
                cost_source="unknown",
            )
        )
        user = await db.get(User, uid)
        _, key = await create_api_key(db, user, "reader")
        await db.commit()
    doomed = {orphan: _leave_files(settings, writer.root, orphan)}
    doomed[failed] = _leave_files(settings, writer.root, failed)
    kept_files = _leave_files(settings, writer.root, filed)
    bystander = tmp_path / "uploads" / "unrelated.pdf"
    bystander.write_bytes(b"x")
    bearer = {"Authorization": f"Bearer {key}"}
    assert (await writer.get(f"/v1/jobs/{orphan}", headers=bearer)).status_code == 200

    purged = await purge_parses(writer.app.state.sessionmaker, settings)

    assert purged == 4
    async with writer.app.state.sessionmaker() as db:
        left = set((await db.execute(select(Job.id))).scalars())
        assert left == {filed, trashed, reconverted, earlier, young, queued, running, theirs}
        assert not left & {orphan, failed, unstamped, foreign}
        assert (await db.execute(select(Result.job_id).where(Result.job_id == orphan))).all() == []
        # The cost stays where it was counted: the entry without its job, the period's
        # spent unchanged, the usage row too.
        entry = (await db.execute(select(CreditEntry))).scalar_one()
        assert (entry.kind, entry.amount, entry.job_id) == ("parse", 3, None)
        period = (await db.execute(select(CreditPeriod))).scalar_one()
        assert period.spent == 3
        usage = (await db.execute(select(UsageLog))).scalar_one()
        assert (usage.job_id, usage.prompt_tokens) == (None, 10)
    for paths in doomed.values():
        assert not [path for path in paths if path.exists()]
    assert all(path.exists() for path in kept_files)
    assert bystander.exists()
    # The data plane has nothing to answer with any more.
    gone = await writer.get(f"/v1/jobs/{orphan}", headers=bearer)
    assert gone.status_code == 404
    assert (await writer.get(f"/v1/jobs/{filed}", headers=bearer)).status_code == 200
    # Nothing more is due.
    assert await purge_parses(writer.app.state.sessionmaker, settings) == 0


async def test_a_parse_retention_of_zero_keeps_every_parse(writer) -> None:
    writer.app.state.settings.parse_retention_days = 0
    async with writer.app.state.sessionmaker() as db:
        ancient = await _parse(db, writer.user_id, finished_days=3650)
        await db.commit()
    assert await purge_parses(writer.app.state.sessionmaker, writer.app.state.settings) == 0
    async with writer.app.state.sessionmaker() as db:
        assert await db.get(Job, ancient) is not None


# --- the dedup cache's margin ----------------------------------------------------------


def _key(user_id: int, sha256: str) -> dict:
    return {
        "user_id": user_id,
        "agent": "parse",
        "sha256": sha256,
        "model": PARSE_MODEL,
        "connection_id": None,
        "connection_base_url": KANO_BASE,
        "pages_spec": "",
        "prompt_sha256": "",
    }


async def test_the_dedup_cache_never_answers_with_a_parse_about_to_be_deleted(
    sessionmaker,
) -> None:
    async with sessionmaker() as db:
        user = User(google_sub="dedup", email="dedup@example.com")
        db.add(user)
        await db.flush()
        # In the last day of its 30: the next sweep may delete it under the new file.
        late = await _parse(db, user.id, sha256="1" * 64, finished_days=29.5)
        young = await _parse(db, user.id, sha256="2" * 64, finished_days=1)
        # As old, but a file keeps it, so no sweep deletes it.
        filed = await _parse(db, user.id, sha256="3" * 64, finished_days=29.5)
        db.add(Document(user_id=user.id, job_id=filed, name="kept.pdf"))
        await db.commit()

        assert await find_cached_job(db, retention_days=30, **_key(user.id, "1" * 64)) is None
        assert (await find_cached_job(db, retention_days=30, **_key(user.id, "2" * 64))).id == young
        assert (await find_cached_job(db, retention_days=30, **_key(user.id, "3" * 64))).id == filed
        # At 1 day no parse is answered by its age alone; at 0 nothing is deleted, and
        # the cache answers as it always did.
        assert await find_cached_job(db, retention_days=1, **_key(user.id, "2" * 64)) is None
        assert (await find_cached_job(db, retention_days=1, **_key(user.id, "3" * 64))).id == filed
        assert (await find_cached_job(db, retention_days=0, **_key(user.id, "1" * 64))).id == late


@postgres_only
@respx.mock
async def test_a_library_cache_hit_purged_before_it_is_filed_is_refused_plainly(
    writer, documents, monkeypatch
) -> None:
    from sightread.routes import library

    respx.post(KANO_CHAT_URL).mock(side_effect=_vision_stub)
    settings = writer.app.state.settings
    sessionmaker = writer.app.state.sessionmaker
    body = documents["text_pdf"].read_bytes()

    async def upload():
        return await writer.post(
            "/api/library/documents",
            files={"file": ("text.pdf", body, "application/pdf")},
            headers=CSRF_HEADERS,
        )

    assert (await upload()).status_code == 201
    await _drain_queue(writer, sessionmaker)
    async with sessionmaker() as db:
        await db.execute(
            update(Job).values(
                finished_at=utcnow() - timedelta(days=settings.parse_retention_days + 10)
            )
        )
        await db.commit()
    submit = library.submit_parse

    async def raced(*args, **kwargs):
        # The cache answers with the parse only the file keeps; the file is deleted for
        # good and the sweep purges the parse before the upload takes the workspace lock.
        submission = await submit(*args, **kwargs)
        assert submission.cached is not None
        async with sessionmaker() as db:
            await db.execute(delete(Document))
            await db.commit()
        assert await purge_parses(sessionmaker, settings) == 1
        return submission

    monkeypatch.setattr(library, "submit_parse", raced)
    refused = await upload()

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["message"] == (
        "The earlier parse of this file was just deleted; upload it again"
    )
    async with sessionmaker() as db:
        assert (await db.execute(select(Document))).all() == []
    assert [path.name for path in Path(settings.upload_dir).iterdir()] == []
    # Uploaded again, it parses afresh.
    monkeypatch.undo()
    assert (await upload()).status_code == 201


@postgres_only
async def test_a_parse_whose_crops_will_not_go_stays_for_the_next_sweep(
    writer, monkeypatch
) -> None:
    from sightread.jobs import sweeper

    settings = writer.app.state.settings
    async with writer.app.state.sessionmaker() as db:
        stuck = await _parse(db, writer.user_id)
        other = await _parse(db, writer.user_id)
        await db.commit()
    stuck_files = _leave_files(settings, writer.root, stuck)
    remove = sweeper.discard_job_figures
    monkeypatch.setattr(
        sweeper,
        "discard_job_figures",
        lambda figures_dir, job_id: job_id != stuck and remove(figures_dir, job_id),
    )

    assert await purge_parses(writer.app.state.sessionmaker, settings) == 1

    # The row is the only thing that names the crops: it stays, and its files with it.
    async with writer.app.state.sessionmaker() as db:
        assert await db.get(Job, stuck) is not None
        assert await db.get(Job, other) is None
    assert all(path.exists() for path in stuck_files)
    monkeypatch.undo()
    assert await purge_parses(writer.app.state.sessionmaker, settings) == 1
    async with writer.app.state.sessionmaker() as db:
        assert await db.get(Job, stuck) is None
    assert not [path for path in stuck_files if path.exists()]
