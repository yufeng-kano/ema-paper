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

from sightread.agents import PARSE
from sightread.db.models import Job, User, utcnow
from sightread.jobs.queue import (
    claim_next_job,
    count_running_jobs,
    enqueue_job,
    find_cached_job,
    normalize_pages_spec,
    parse_pages_spec,
    requeue_job,
)
from sightread.jobs.sweeper import MAX_AGE_SECONDS, sweep_uploads
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
        assert (await find_cached_job(db, **key)).id == job.id
        assert await find_cached_job(db, **{**key, "sha256": "b" * 64}) is None
        assert await find_cached_job(db, **{**key, "model": "other/model"}) is None
        assert await find_cached_job(db, **{**key, "pages_spec": "1-2"}) is None
        assert await find_cached_job(db, **{**key, "prompt_sha256": "q" * 64}) is None
        # A different upstream is a different parse, even for the same model id — and an
        # edited endpoint URL repoints what a connection id means, so it misses too.
        assert await find_cached_job(db, **{**key, "connection_id": 12345}) is None
        assert (
            await find_cached_job(db, **{**key, "connection_base_url": "https://new.example/v1"})
            is None
        )

        other = await _user(db, "second@example.com")
        # Never across users, even for identical bytes (docs/jobs.md § Dedup).
        assert await find_cached_job(db, **{**key, "user_id": other.id}) is None


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
        assert await find_cached_job(db, **key) is None
        # The historical row is there and would match an OpenRouter-era key: the snapshot
        # is exactly what keeps it out.
        historical = await find_cached_job(db, **{**key, "connection_base_url": None})
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


async def test_requeue_forgets_partial_progress(sessionmaker) -> None:
    async with sessionmaker() as db:
        user = await _user(db)
        job = await enqueue_job(
            db, user_id=user.id, sha256="5" * 64, source_path="unused-5.pdf", **JOB_DEFAULTS
        )
        job.status = "running"
        job.pages_done = 1
        job.started_at = utcnow()
        await db.commit()
        job_id = job.id

    async with sessionmaker() as db:
        assert await requeue_job(db, job_id) is True
    async with sessionmaker() as db:
        requeued = await db.get(Job, job_id)
        assert (requeued.status, requeued.pages_done, requeued.started_at) == ("queued", 0, None)
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

    old = time.time() - MAX_AGE_SECONDS - 60
    for entry in (stale, stale_dir):
        os.utime(entry, (old, old))

    assert sweep_uploads(tmp_path) == 2
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
