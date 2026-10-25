"""Queue behaviour: page specs, dedup lookup, claiming and the retention sweeper.

The `SKIP LOCKED` claim can only be exercised on PostgreSQL, so those tests skip unless
`TEST_DATABASE_URL` points at one (docs/testing.md).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from sightread.agents import PARSE
from sightread.db.models import Document, Job, JobPage, User, utcnow
from sightread.jobs.queue import (
    claim_next_job,
    count_running_jobs,
    enqueue_job,
    find_cached_job,
    normalize_pages_spec,
    parse_pages_spec,
    requeue_job,
)
from sightread.jobs.sweeper import sweep_uploads
from tests.conftest import DATABASE_URL

postgres_only = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="needs TEST_DATABASE_URL pointing at PostgreSQL",
)

JOB_DEFAULTS = {
    "kind": "pdf",
    "filename": "paper.pdf",
    "media_type": "application/pdf",
    "size_bytes": 2048,
    "pages_spec": "",
    "agent": "parse",
    "model": "vendor/model",
    "connection_id": None,
    # The built-in upstream's snapshot is the Kano Proxy origin (docs/jobs.md § Dedup).
    "connection_base_url": "https://kano.test",
    "bbox_format": "yxyx_norm1000",
    "prompt": "Transcribe page {page} in {bbox_format}.",
    "prompt_sha256": "p" * 64,
    "page_count": 2,
}


async def _user(db, email: str = "queue@example.com") -> User:
    user = User(google_sub=email, email=email)
    db.add(user)
    await db.flush()
    return user


def test_parse_pages_spec() -> None:
    assert parse_pages_spec("", 3) == [1, 2, 3]
    assert parse_pages_spec("1-3,5", 8) == [1, 2, 3, 5]
    assert parse_pages_spec("2,2,1", 8) == [1, 2]
    assert normalize_pages_spec(" 1 - 3 , 5 ") == "1-3,5"

    for bad in ("1-", "a", "0", "3-1", "9"):
        with pytest.raises(ValueError):
            parse_pages_spec(bad, 8)


async def test_dedup_lookup_matches_the_full_key(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="a" * 64, source_path="unused-a.pdf", **JOB_DEFAULTS
        )
        job.status = "succeeded"
        job.finished_at = utcnow()
        await db.commit()

        key = {
            "user_id": user.id,
            "agent": "parse",
            "sha256": "a" * 64,
            "model": "vendor/model",
            "connection_id": None,
            "connection_base_url": "https://kano.test",
            "pages_spec": "",
            "prompt_sha256": "p" * 64,
        }
        assert (await find_cached_job(db, retention_days=30, **key)).id == job.id
        assert await find_cached_job(db, retention_days=30, **{**key, "sha256": "b" * 64}) is None
        assert (
            await find_cached_job(db, retention_days=30, **{**key, "model": "other/model"}) is None
        )
        assert await find_cached_job(db, retention_days=30, **{**key, "pages_spec": "1-2"}) is None
        assert (
            await find_cached_job(db, retention_days=30, **{**key, "prompt_sha256": "q" * 64})
            is None
        )
        # A different upstream is a different parse, even for the same model id — and an
        # edited endpoint URL repoints what a connection id means, so it misses too.
        assert (
            await find_cached_job(db, retention_days=30, **{**key, "connection_id": 12345}) is None
        )
        assert (
            await find_cached_job(
                db, retention_days=30, **{**key, "connection_base_url": "https://new.example/v1"}
            )
            is None
        )

        other = await _user(db, "second@example.com")
        # Never across users, even for identical bytes (docs/jobs.md § Dedup).
        assert await find_cached_job(db, retention_days=30, **{**key, "user_id": other.id}) is None


async def test_dedup_never_crosses_agents_or_answers_from_openrouter_history(
    sessionmaker,
) -> None:
    """Two succeeded results for the same bytes, model and prompt that must never answer
    a new parse upload: another agent's, and one OpenRouter produced before the Kano Proxy
    migration (connection_id and connection_base_url both NULL, docs/jobs.md § Dedup)."""
    async with sessionmaker() as db:
        user = await _user(db)
        shared = {
            **JOB_DEFAULTS,
            "user_id": user.id,
            "sha256": "d" * 64,
            "source_path": None,
            "status": "succeeded",
            "pipeline_version": PARSE.pipeline_version,
            "pages_done": 2,
            "created_at": utcnow(),
            "finished_at": utcnow(),
        }
        db.add(Job(**{**shared, "agent": "summarize"}))
        db.add(Job(**{**shared, "connection_base_url": None}))
        await db.commit()

        key = {
            "user_id": user.id,
            "agent": "parse",
            "sha256": "d" * 64,
            "model": "vendor/model",
            "connection_id": None,
            "connection_base_url": "https://kano.test",
            "pages_spec": "",
            "prompt_sha256": "p" * 64,
        }
        assert await find_cached_job(db, retention_days=30, **key) is None
        # The historical row is there and would match an OpenRouter-era key: the snapshot
        # is exactly what keeps it out.
        historical = await find_cached_job(
            db, retention_days=30, **{**key, "connection_base_url": None}
        )
        assert historical is not None and historical.agent == "parse"


async def test_dedup_ignores_unfinished_jobs(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        await enqueue_job(
            db, user_id=user.id, sha256="c" * 64, source_path="unused-c.pdf", **JOB_DEFAULTS
        )
        await db.commit()

        assert (
            await find_cached_job(
                db,
                retention_days=30,
                user_id=user.id,
                agent="parse",
                sha256="c" * 64,
                model="vendor/model",
                connection_id=None,
                connection_base_url="https://kano.test",
                pages_spec="",
                prompt_sha256="p" * 64,
            )
            is None
        )


async def test_claim_is_fifo_and_marks_the_job_running(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        first = await enqueue_job(
            db, user_id=user.id, sha256="1" * 64, source_path="unused-1.pdf", **JOB_DEFAULTS
        )
        second = await enqueue_job(
            db, user_id=user.id, sha256="2" * 64, source_path="unused-2.pdf", **JOB_DEFAULTS
        )
        second.created_at = first.created_at + timedelta(seconds=5)
        await db.commit()

    async with sessionmaker() as db:
        claimed = await claim_next_job(db, max_jobs_per_user=2)
    assert claimed == first.id

    async with sessionmaker() as db:
        assert (await db.get(Job, first.id)).status == "running"
        assert await count_running_jobs(db, first.user_id) == 1


async def test_claim_honours_the_per_user_cap(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        running = await enqueue_job(
            db, user_id=user.id, sha256="3" * 64, source_path="unused-3.pdf", **JOB_DEFAULTS
        )
        running.status = "running"
        await enqueue_job(
            db, user_id=user.id, sha256="4" * 64, source_path="unused-4.pdf", **JOB_DEFAULTS
        )
        await db.commit()

    async with sessionmaker() as db:
        assert await claim_next_job(db, max_jobs_per_user=1) is None
    async with sessionmaker() as db:
        assert await claim_next_job(db, max_jobs_per_user=2) is not None


async def test_requeue_keeps_the_pages_that_succeeded(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="5" * 64, source_path="unused-5.pdf", **JOB_DEFAULTS
        )
        job.status = "running"
        job.pages_done = 2
        job.started_at = utcnow()
        db.add_all(
            [
                JobPage(job_id=job.id, page_no=1, status="succeeded", markdown="# One"),
                JobPage(job_id=job.id, page_no=2, status="failed", error="upstream call failed"),
            ]
        )
        await db.commit()
        job_id = job.id

    async with sessionmaker() as db:
        assert await requeue_job(db, job_id) is True
    async with sessionmaker() as db:
        requeued = await db.get(Job, job_id)
        assert (requeued.status, requeued.pages_done, requeued.started_at) == ("queued", 1, None)
        # The page the key paid for stays for the next attempt; the failed one is
        # forgotten, to be sent again (docs/jobs.md § Queue).
        kept = (await db.execute(select(JobPage.page_no).where(JobPage.job_id == job_id))).all()
        assert kept == [(1,)]
        # A job whose result was committed before the shutdown's cancel landed is not
        # requeued, and the worker leaves its crops alone (docs/jobs.md § Retention).
        requeued.status = "succeeded"
        requeued.pages_done = 3
        await db.commit()
    async with sessionmaker() as db:
        assert await requeue_job(db, job_id) is False
        done = await db.get(Job, job_id)
        assert (done.status, done.pages_done) == ("succeeded", 3)


@postgres_only
async def test_one_worker_holds_the_worker_lock_at_a_time(sessionmaker) -> None:
    from sightread.jobs.queue import release_worker_lock, try_worker_lock

    engine = sessionmaker.kw["bind"]
    first = await try_worker_lock(engine)
    assert first is not None
    # A second worker finds the lock held and must not take the first one's jobs back.
    assert await try_worker_lock(engine) is None
    await release_worker_lock(first)
    second = await try_worker_lock(engine)
    assert second is not None
    await release_worker_lock(second)


async def test_a_worker_stops_when_its_lock_session_is_gone() -> None:
    from sightread.jobs.queue import watch_worker_lock

    class Session:
        alive = True

        async def execute(self, statement):
            if not self.alive:
                raise RuntimeError("connection is closed")

            class Result:
                def scalar_one(self):
                    return 1

            return Result()

    stop = asyncio.Event()
    session = Session()
    watching = asyncio.ensure_future(watch_worker_lock(session, stop, 0.02))
    await asyncio.sleep(0.08)
    assert not stop.is_set() and not watching.done()
    # The session is lost: PostgreSQL has released the lock with it, and a waiting worker
    # may hold it now, so this one stops and its loop cancels the work in flight.
    session.alive = False
    await asyncio.wait_for(watching, 2)
    assert stop.is_set()


@postgres_only
async def test_a_worker_starting_requeues_the_jobs_a_dead_one_left(sessionmaker) -> None:
    from sightread.jobs.queue import requeue_orphaned_jobs

    async with sessionmaker() as db:
        user = await _user(db)
        held = await enqueue_job(
            db, user_id=user.id, sha256="6" * 64, source_path="unused-6.pdf", **JOB_DEFAULTS
        )
        held.status = "running"
        held.started_at = utcnow()
        queued = await enqueue_job(
            db, user_id=user.id, sha256="7" * 64, source_path="unused-7.pdf", **JOB_DEFAULTS
        )
        await db.commit()
        held_id, queued_id = held.id, queued.id
    async with sessionmaker() as db:
        # One worker per deployment: a job still running when it starts is a dead
        # worker's, and goes back to the queue rather than counting against its user
        # for good (docs/jobs.md).
        assert await requeue_orphaned_jobs(db) == [held_id]
        assert (await db.get(Job, held_id)).status == "queued"
        assert (await db.get(Job, queued_id)).status == "queued"
        assert await requeue_orphaned_jobs(db) == []


async def test_a_job_whose_task_raised_is_failed_unless_already_done(
    sessionmaker, tmp_path
) -> None:
    from sightread.jobs.runner import fail_job

    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="8" * 64, source_path="unused-8.pdf", **JOB_DEFAULTS
        )
        job.status = "running"
        done = await enqueue_job(
            db, user_id=user.id, sha256="9" * 64, source_path="unused-9.pdf", **JOB_DEFAULTS
        )
        done.status = "succeeded"
        await db.commit()
        job_id, done_id = job.id, done.id
    from sightread.config import Settings
    from tests.conftest import KANO_BASE, TEST_SECRET_KEY

    settings = Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
    )
    await fail_job(sessionmaker, settings, job_id, "internal error")
    await fail_job(sessionmaker, settings, done_id, "internal error")
    async with sessionmaker() as db:
        failed = await db.get(Job, job_id)
        assert (failed.status, failed.error) == ("failed", "internal error")
        assert failed.finished_at is not None
        # A result committed before what raised stays a result.
        assert (await db.get(Job, done_id)).status == "succeeded"


@postgres_only
async def test_two_claimers_never_take_the_same_job(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        for index in range(2):
            await enqueue_job(
                db,
                user_id=user.id,
                sha256=str(index) * 64,
                source_path=f"unused-{index}.pdf",
                **JOB_DEFAULTS,
            )
        await db.commit()

    async def claim() -> uuid.UUID | None:
        async with sessionmaker() as db:
            return await claim_next_job(db, max_jobs_per_user=2)

    first, second = await asyncio.gather(claim(), claim())

    assert first is not None and second is not None
    assert first != second


def test_sweeper_removes_only_stale_entries(tmp_path) -> None:
    fresh = tmp_path / "fresh.pdf"
    fresh.write_bytes(b"x")
    stale = tmp_path / "stale.pdf"
    stale.write_bytes(b"x")
    stale_dir = tmp_path / "abandoned-job"
    stale_dir.mkdir()
    (stale_dir / "page-1.png").write_bytes(b"x")

    max_age = 3600
    old = time.time() - max_age - 60
    for entry in (stale, stale_dir):
        os.utime(entry, (old, old))

    assert sweep_uploads(tmp_path, max_age=max_age) == 2
    assert fresh.exists()
    assert not stale.exists()
    assert not stale_dir.exists()


def test_a_discarded_jobs_figures_cannot_be_written_back(tmp_path) -> None:
    """The crop guard: after `_discard_figures`, a late crop thread writes nothing
    (docs/parsing.md § Figure crops)."""
    from PIL import Image

    from sightread.jobs.runner import RunState, VisionBudget, _discard_figures, _locked_save_figures

    image = tmp_path / "page.png"
    Image.new("RGB", (100, 100), (10, 10, 10)).save(image, format="PNG")
    state = RunState(budget=VisionBudget(1), figures_dir=tmp_path / "job")

    _locked_save_figures(state, "![fig](sightread://p1/0,0,500,500)", image, 1)
    assert (tmp_path / "job" / "p1_0_0_500_500.png").is_file()

    _discard_figures(state)
    assert not (tmp_path / "job").exists()

    _locked_save_figures(state, "![fig](sightread://p1/0,0,500,500)", image, 1)
    assert not (tmp_path / "job").exists()


async def test_a_job_of_a_user_with_an_orphaned_turn_is_not_claimed(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="3" * 64, source_path="unused-3.pdf", **JOB_DEFAULTS
        )
        await db.commit()
    async with sessionmaker() as db:
        # A command of theirs the worker could not stop yet may still write their tree.
        assert await claim_next_job(db, 2, {user.id}) is None
    async with sessionmaker() as db:
        assert await claim_next_job(db, 2, {user.id + 1000}) == job.id


async def test_a_stale_workers_writes_are_fenced_by_its_generation(sessionmaker, tmp_path) -> None:
    """A worker that lost the worker lock goes on for seconds; the replacement has
    requeued and claimed its job by then, and nothing the stale one writes may land —
    except the usage its calls billed (docs/jobs.md § Queue)."""
    from sqlalchemy import func, select

    from sightread.config import Settings
    from sightread.db.models import JobPage, UsageLog
    from sightread.jobs.queue import next_worker_generation, requeue_orphaned_jobs
    from sightread.jobs.runner import PageOutcome, _record_page, fail_job
    from sightread.upstream.client import Usage
    from tests.conftest import KANO_BASE, TEST_SECRET_KEY

    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="c" * 64, source_path="unused-c.pdf", **JOB_DEFAULTS
        )
        await db.commit()
        job_id = job.id
    async with sessionmaker() as db:
        stale = await next_worker_generation(db)
        assert await claim_next_job(db, 2, None, stale) == job_id
        assert (await db.get(Job, job_id)).worker_generation == stale
    # The replacement starts: every running row is a dead worker's to it.
    async with sessionmaker() as db:
        assert await requeue_orphaned_jobs(db) == [job_id]
        assert (await db.get(Job, job_id)).worker_generation is None
        fresh = await next_worker_generation(db)
        assert fresh > stale
        assert await claim_next_job(db, 2, None, fresh) == job_id

    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
    outcome = PageOutcome(
        page=1,
        width_pt=1.0,
        height_pt=1.0,
        method="vision",
        markdown="x",
        model=job.model,
        usage=[
            (
                job.model,
                Usage(prompt_tokens=1, completion_tokens=1, cost=None, cost_source="unknown"),
            )
        ],
    )
    settings = Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
    )

    async def counts(db) -> tuple[int, int]:
        pages = select(func.count()).select_from(JobPage).where(JobPage.job_id == job_id)
        usage = select(func.count()).select_from(UsageLog).where(UsageLog.job_id == job_id)
        return (await db.execute(pages)).scalar_one(), (await db.execute(usage)).scalar_one()

    # The stale worker finishes a page, is cancelled, fails: none of it lands but the bill.
    await _record_page(sessionmaker, job, outcome, stale)
    async with sessionmaker() as db:
        row = await db.get(Job, job_id)
        assert (row.pages_done, row.status, row.worker_generation) == (0, "running", fresh)
        assert await counts(db) == (0, 1)
        assert await requeue_job(db, job_id, stale) is False
    await fail_job(sessionmaker, settings, job_id, "internal error", stale)
    async with sessionmaker() as db:
        assert (await db.get(Job, job_id)).status == "running"
    # The owner's writes land.
    await _record_page(sessionmaker, job, outcome, fresh)
    await fail_job(sessionmaker, settings, job_id, "internal error", fresh)
    async with sessionmaker() as db:
        row = await db.get(Job, job_id)
        assert (row.pages_done, row.status, row.error) == (1, "failed", "internal error")
        assert await counts(db) == (1, 2)


def test_an_attempts_crops_are_its_own_until_published(tmp_path) -> None:
    """Two attempts of one job — a stale worker's and the replacement's — crop into
    directories of their own; a discard takes only its own, and the publish of the one
    whose result committed puts it under the job's name (docs/jobs.md § Queue)."""
    from sightread.jobs.runner import RunState, VisionBudget, _discard_figures, _publish_figures
    from sightread.parsing.figures import discard_job_figures

    job_id = uuid.uuid4()
    figures = tmp_path / "figures"
    stale = RunState(budget=VisionBudget(1), figures_dir=figures / f"{job_id}.stale")
    fresh = RunState(budget=VisionBudget(1), figures_dir=figures / f"{job_id}.fresh")
    for state in (stale, fresh):
        state.figures_dir.mkdir(parents=True)
        (state.figures_dir / "p1_0_0_10_10.png").write_bytes(b"png")
    _discard_figures(stale)
    assert not stale.figures_dir.exists() and fresh.figures_dir.is_dir()
    _publish_figures(fresh, figures, job_id)
    assert (figures / str(job_id) / "p1_0_0_10_10.png").read_bytes() == b"png"
    assert not fresh.figures_dir.exists()
    # A requeue at a worker's start removes the published crops and any attempt left.
    (figures / f"{job_id}.dead").mkdir()
    discard_job_figures(figures, job_id)
    assert list(figures.iterdir()) == []


async def test_a_run_whose_snapshot_update_is_fenced_out_ends_there(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    """The endpoint snapshot a run writes at its start is fenced like every other write:
    a replacement that claimed the job between the read and the update keeps its own,
    and the stale run starts nothing (docs/jobs.md § Queue)."""
    from types import SimpleNamespace

    from sqlalchemy import Update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from sightread.config import Settings
    from sightread.jobs import runner
    from sightread.jobs.queue import next_worker_generation
    from tests.conftest import KANO_BASE, TEST_SECRET_KEY

    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="d" * 64, source_path="unused-d.pdf", **JOB_DEFAULTS
        )
        await db.commit()
        job_id = job.id
    async with sessionmaker() as db:
        stale = await next_worker_generation(db)
        assert await claim_next_job(db, 2, None, stale) == job_id

    monkeypatch.setattr(
        runner, "kano_connection", lambda settings: SimpleNamespace(snapshot="https://moved.test")
    )

    class Interposed(AsyncSession):
        """The replacement requeues and claims the job between this run's read of the row
        and its first write — the snapshot reconcile, which must then miss."""

        async def execute(self, statement, *args, **kwargs):
            if isinstance(statement, Update) and not replaced:
                replaced.append(True)
                async with sessionmaker() as other:
                    fresh = await next_worker_generation(other)
                    assert await requeue_job(other, job_id) is True
                    assert await claim_next_job(other, 2, None, fresh) == job_id
            return await super().execute(statement, *args, **kwargs)

    replaced: list[bool] = []
    interposed = async_sessionmaker(
        sessionmaker.kw["bind"], class_=Interposed, expire_on_commit=False, autoflush=False
    )
    settings = Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        upload_dir=str(tmp_path / "uploads"),
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
    )
    await runner.run_job(interposed, settings, job_id, asyncio.Semaphore(1), stale)
    assert replaced
    async with sessionmaker() as db:
        row = await db.get(Job, job_id)
    assert (row.status, row.connection_base_url) == ("running", JOB_DEFAULTS["connection_base_url"])
    assert not (tmp_path / "uploads").exists()


async def test_the_crops_are_published_under_the_backup_lock(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    """The result's commit and the move of its crops under the job's name are one hold of
    the backup lock, so a snapshot never carries one without the other (docs/jobs.md
    § Queue, docs/workspace.md § Backups)."""
    import contextlib

    from sightread.config import Settings
    from sightread.jobs import runner
    from tests.conftest import KANO_BASE, TEST_SECRET_KEY

    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="e" * 64, source_path="unused-e.pdf", **JOB_DEFAULTS
        )
        job.status = "running"
        await db.commit()
        job_id = job.id
    held = False
    published_while_held: list[bool] = []

    @contextlib.asynccontextmanager
    async def recording(root):
        nonlocal held
        held = True
        try:
            yield
        finally:
            held = False

    order: list[str] = []

    async def publish() -> None:
        published_while_held.append(held)
        order.append("publish")

    async def materialize(db, settings_, row, **kwargs):
        # The copies take their crops from under the job's name: published first.
        order.append("materialize")

    monkeypatch.setattr(runner, "backup_shared", recording)
    monkeypatch.setattr(runner, "materialize_document", materialize)
    async with sessionmaker() as db:
        db.add(Document(user_id=user.id, folder_id=None, job_id=job_id, name="e.pdf"))
        await db.commit()
    settings = Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
    )
    assert await runner._finish(
        sessionmaker, job_id, status="succeeded", settings=settings, publish=publish
    )
    assert published_while_held == [True]
    assert order == ["publish", "materialize"]
    # A finish that wrote nothing publishes nothing.
    assert not await runner._finish(
        sessionmaker, job_id, status="succeeded", settings=settings, generation=99, publish=publish
    )
    assert published_while_held == [True]
