"""The console's overview, jobs and usage (docs/admin.md § Overview, jobs and usage): whether
the worker runs, read from `pg_locks` alone; a stuck job put back with its audit row and
nothing else; the jobs list with an address only for `users.read`; usage per UTC day and
model; and the overview's checks of Kano Proxy and Kano Pay, stubbed with respx.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import select, text

from sightread.admin import jobs as admin_jobs
from sightread.config import Settings
from sightread.db.models import AuditLog, CreditEntry, Job, JobPage, UsageLog, utcnow
from sightread.jobs import queue
from sightread.routes import admin as admin_routes
from tests.conftest import CSRF_HEADERS, KANO_API_KEY, KANO_MODELS_URL, PAY_SETTINGS
from tests.test_admin import (
    OWNERS,
    _account,
    _add,
    _audit,
    _error,
    _group,
    _last_audit_id,
    _owner,
    postgres_only,
)
from tests.test_pay import CATALOG_URL, catalog

JOB_FIELDS = {
    "agent": "parse",
    "kind": "pdf",
    "filename": "confidential-thesis.pdf",
    "media_type": "application/pdf",
    "size_bytes": 2048,
    "sha256": "a" * 64,
    "pages_spec": "",
    "model": "vendor/vision-model",
    "bbox_format": "yxyx_norm1000",
    "prompt": "SECRET-PROMPT transcribe page {page}",
    "prompt_sha256": "p" * 64,
    "page_count": 3,
}


async def _job(client: AsyncClient, user_id: int, **fields) -> Job:
    async with client.app.state.sessionmaker() as db:
        job = Job(user_id=user_id, **{**JOB_FIELDS, "status": "queued", **fields})
        db.add(job)
        await db.commit()
        return job


def _worker(monkeypatch, state: str, generation: int | None = None) -> None:
    """Whether the worker runs, as the lock would say: the routes and the requeue read it
    through `queue.worker_status`, the one place it is patched."""

    async def status(db) -> queue.WorkerStatus:
        since = utcnow() if state == "running" else None
        return queue.WorkerStatus(state, since, generation)

    monkeypatch.setattr(queue, "worker_status", status)


async def _requeue(client: AsyncClient, job_id) -> httpx.Response:
    return await client.post(f"/api/admin/jobs/{job_id}/requeue", headers=CSRF_HEADERS)


def test_the_probe_timeout_the_usage_range_and_the_failed_window_are_validated() -> None:
    assert Settings().admin_probe_timeout_seconds == 5
    assert Settings().admin_usage_max_days == 366
    assert Settings().admin_failed_window_hours == 24
    # The Usage page's longest preset, 90 days, is the least cap that refuses none of them.
    assert Settings(admin_usage_max_days=90).admin_usage_max_days == 90
    assert Settings(admin_failed_window_hours=8760).admin_failed_window_hours == 8760
    for bad in (
        {"admin_probe_timeout_seconds": 0},
        {"admin_usage_max_days": 89},
        {"admin_failed_window_hours": 0},
        # A window that reaches back past year 1 would fail every overview read.
        {"admin_failed_window_hours": 8761},
        {"admin_failed_window_hours": 999_999_999},
    ):
        with pytest.raises(ValueError):
            Settings(**bad)


# --- whether the worker runs ----------------------------------------------------------


def test_the_worker_lock_key_splits_into_its_pg_locks_columns() -> None:
    assert (queue.WORKER_LOCK_CLASSID << 32) | queue.WORKER_LOCK_OBJID == queue.WORKER_LOCK_KEY
    assert (queue.WORKER_LOCK_CLASSID, queue.WORKER_LOCK_OBJID) == (1, 3_017_056_705)


async def test_off_postgresql_the_worker_state_is_unknown(sessionmaker) -> None:
    if sessionmaker.kw["bind"].dialect.name == "postgresql":
        pytest.skip("the SQLite fallback has no lock to read")
    async with sessionmaker() as db:
        assert await queue.worker_status(db) == queue.WorkerStatus("unknown")


@postgres_only
async def test_the_worker_runs_while_another_session_holds_its_lock(sessionmaker) -> None:
    engine = sessionmaker.kw["bind"]
    async with sessionmaker() as db:
        assert await queue.worker_status(db) == queue.WorkerStatus("stopped")
        # The probe took nothing: a worker starting now still gets the lock.
        held = await queue.try_worker_lock(engine)
        assert held is not None
        try:
            # No generation taken yet: the worker is about to take the first.
            running = await queue.worker_status(db)
            assert (running.state, running.generation) == ("running", None)
            assert running.since is not None and running.since <= utcnow()

            generation = await queue.next_worker_generation(db)
            await db.commit()
            assert (await queue.worker_status(db)).generation == generation
            # Reading it again uses no generation up.
            assert (await queue.worker_status(db)).generation == generation
        finally:
            await queue.release_worker_lock(held)
        assert (await queue.worker_status(db)).state == "stopped"


@postgres_only
async def test_another_lock_with_the_same_halves_is_not_the_worker(sessionmaker) -> None:
    """`pg_advisory_lock(1, objid)` lists the same `classid` and `objid` as the worker's
    bigint lock, with `objsubid` 2: it is somebody else's."""
    engine = sessionmaker.kw["bind"]
    connection = await engine.connect()
    try:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        second_half = queue.WORKER_LOCK_OBJID - (1 << 32)  # the same 32 bits as an int4
        await connection.execute(
            text("SELECT pg_advisory_lock(:first, :second)"),
            {"first": queue.WORKER_LOCK_CLASSID, "second": second_half},
        )
        async with sessionmaker() as db:
            assert (await queue.worker_status(db)).state == "stopped"
    finally:
        await connection.close()


# --- requeue --------------------------------------------------------------------------


async def test_a_stuck_job_goes_back_with_its_finished_pages_and_one_audit_row(
    make_client, monkeypatch, tmp_path
) -> None:
    owner = await _owner(make_client)
    account = await _account(make_client, "reader@example.test")
    started = utcnow() - timedelta(minutes=40)
    job = await _job(
        owner,
        account.user_id,
        status="running",
        worker_generation=4,
        started_at=started,
        pages_done=2,
    )
    other = await _job(owner, account.user_id, status="running", worker_generation=4)
    async with owner.app.state.sessionmaker() as db:
        db.add_all(
            [
                JobPage(job_id=job.id, page_no=1, status="succeeded", markdown="# kept"),
                JobPage(job_id=job.id, page_no=2, status="failed", error="Upstream refused"),
                CreditEntry(
                    user_id=account.user_id,
                    period_start=started,
                    kind="parse",
                    state="reserved",
                    amount=3,
                    job_id=job.id,
                ),
            ]
        )
        await db.commit()
    figures = tmp_path / "figures"
    for directory in (
        figures / str(job.id),
        figures / f"{job.id}.attempt",
        figures / str(other.id),
    ):
        directory.mkdir(parents=True)
        (directory / "fig1.png").write_bytes(b"png")
    _worker(monkeypatch, "stopped")
    start = await _last_audit_id(owner)

    response = await _requeue(owner, job.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["id"], body["status"], body["pages_done"]) == (str(job.id), "queued", 1)
    assert (body["worker_generation"], body["started_at"], body["stuck"]) == (None, None, False)
    assert body["email"] == "reader@example.test"

    async with owner.app.state.sessionmaker() as db:
        stored = await db.get(Job, job.id)
        assert (stored.status, stored.pages_done, stored.worker_generation) == ("queued", 1, None)
        assert list(await db.scalars(select(JobPage.page_no).where(JobPage.job_id == job.id))) == [
            1
        ]
        # The credits stay reserved for the next attempt to settle.
        entry = await db.scalar(select(CreditEntry).where(CreditEntry.job_id == job.id))
        assert (entry.state, entry.amount) == ("reserved", 3)
        # The other job is untouched.
        assert (await db.get(Job, other.id)).status == "running"
    # This attempt's crops are gone; another job's stay.
    assert not (figures / str(job.id)).exists()
    assert not (figures / f"{job.id}.attempt").exists()
    assert (figures / str(other.id) / "fig1.png").exists()

    [row] = await _audit(owner, start)
    assert (row.actor_id, row.permission, row.action) == (
        owner.user_id,
        "jobs.write",
        "jobs.requeue",
    )
    assert (row.target_type, row.target_id) == ("job", str(job.id))
    assert row.before == {
        "user_id": account.user_id,
        "status": "running",
        "worker_generation": 4,
        "pages_done": 2,
    }
    assert row.after == {
        "user_id": account.user_id,
        "status": "queued",
        "worker_generation": None,
        "pages_done": 1,
    }
    assert "example.test" not in str(row.before) + str(row.after)

    # Queued now: a second requeue is refused and records nothing.
    again = await _requeue(owner, job.id)
    assert again.status_code == 409
    assert _error(again)["reason"] == "not_running"
    assert len(await _audit(owner, start)) == 1


async def test_a_job_another_generation_left_is_stuck_while_the_worker_runs(
    make_client, monkeypatch
) -> None:
    owner = await _owner(make_client)
    stale = await _job(owner, owner.user_id, status="running", worker_generation=6)
    live = await _job(owner, owner.user_id, status="running", worker_generation=7)
    _worker(monkeypatch, "running", 7)
    start = await _last_audit_id(owner)

    refused = await _requeue(owner, live.id)
    assert refused.status_code == 409
    assert _error(refused) == {
        "type": "admin_conflict",
        "message": "The live worker is running this job",
        "reason": "not_stuck",
    }
    assert await _audit(owner, start) == []

    response = await _requeue(owner, stale.id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert [row.target_id for row in await _audit(owner, start)] == [str(stale.id)]


@pytest.mark.parametrize("status", ["queued", "succeeded", "failed"])
async def test_only_a_running_job_is_requeued(make_client, monkeypatch, status: str) -> None:
    owner = await _owner(make_client)
    job = await _job(owner, owner.user_id, status=status, error="kept as it is")
    _worker(monkeypatch, "stopped")
    start = await _last_audit_id(owner)

    response = await _requeue(owner, job.id)
    assert response.status_code == 409
    assert _error(response) == {
        "type": "admin_conflict",
        "message": f"The job is {status}, not running",
        "reason": "not_running",
    }
    async with owner.app.state.sessionmaker() as db:
        stored = await db.get(Job, job.id)
        assert (stored.status, stored.error) == (status, "kept as it is")
    assert await _audit(owner, start) == []


async def test_nothing_is_requeued_while_the_worker_state_is_unknown(
    make_client, monkeypatch
) -> None:
    owner = await _owner(make_client)
    job = await _job(owner, owner.user_id, status="running", worker_generation=1)
    _worker(monkeypatch, "unknown")
    start = await _last_audit_id(owner)

    response = await _requeue(owner, job.id)
    assert response.status_code == 409
    assert _error(response)["reason"] == "worker_unknown"
    async with owner.app.state.sessionmaker() as db:
        assert (await db.get(Job, job.id)).status == "running"
    assert await _audit(owner, start) == []


async def test_an_unknown_or_malformed_job_id_is_refused(make_client) -> None:
    owner = await _owner(make_client)
    start = await _last_audit_id(owner)
    missing = await _requeue(owner, uuid.uuid4())
    assert missing.status_code == 404
    assert _error(missing)["message"] == "No such job"
    assert (await _requeue(owner, "not-a-uuid")).status_code == 422
    assert await _audit(owner, start) == []


@postgres_only
async def test_requeue_reads_the_real_worker_lock(make_client) -> None:
    owner = await _owner(make_client)
    sessionmaker = owner.app.state.sessionmaker
    held = await queue.try_worker_lock(sessionmaker.kw["bind"])
    assert held is not None
    try:
        async with sessionmaker() as db:
            generation = await queue.next_worker_generation(db)
            await db.commit()
        live = await _job(owner, owner.user_id, status="running", worker_generation=generation)
        stale = await _job(
            owner, owner.user_id, status="running", worker_generation=generation + 100
        )
        assert _error(await _requeue(owner, live.id))["reason"] == "not_stuck"
        assert (await _requeue(owner, stale.id)).status_code == 200
    finally:
        await queue.release_worker_lock(held)
    # The worker gone, its own job is stuck too.
    assert (await _requeue(owner, live.id)).status_code == 200


# --- permissions ----------------------------------------------------------------------


async def test_jobs_routes_need_their_permissions(make_client, monkeypatch) -> None:
    owner = await _owner(make_client)
    outsider = await _account(make_client, "outsider@example.test")
    reader = await _account(make_client, "reader@example.test")
    readers = await _group(owner, "job-readers", ["jobs.read"])
    await _add(owner, readers["id"], user_id=reader.user_id)
    job = await _job(owner, reader.user_id, status="running", worker_generation=2)
    _worker(monkeypatch, "stopped")
    reads = (
        "/api/admin/overview",
        "/api/admin/jobs",
        "/api/admin/usage?from=2026-03-01&to=2026-03-02",
    )

    for path in reads:
        response = await outsider.get(path)
        assert response.status_code == 403, path
        assert _error(response)["type"] == "permission_denied"
        assert (await reader.get(path)).status_code == 200, path
    # Reading is not writing.
    start = await _last_audit_id(owner)
    refused = await _requeue(reader, job.id)
    assert refused.status_code == 403
    assert _error(refused)["type"] == "permission_denied"
    assert await _audit(owner, start) == []
    async with owner.app.state.sessionmaker() as db:
        assert (await db.get(Job, job.id)).status == "running"
    # Anonymous: the session first.
    anonymous = make_client(owner_emails=OWNERS)
    assert (await anonymous.get("/api/admin/jobs")).status_code == 401


async def test_an_address_needs_users_read_and_the_audit_needs_audit_read(make_client) -> None:
    owner = await _owner(make_client)
    reader = await _account(make_client, "reader@example.test")
    support = await _account(make_client, "support@example.test")
    readers = await _group(owner, "job-readers", ["jobs.read"])
    staff = await _group(owner, "support", ["jobs.read", "users.read", "audit.read"])
    await _add(owner, readers["id"], user_id=reader.user_id)
    await _add(owner, staff["id"], user_id=support.user_id)
    job = await _job(owner, reader.user_id)

    [hidden] = (await reader.get("/api/admin/jobs")).json()["items"]
    assert (hidden["id"], hidden["user_id"], hidden["email"]) == (
        str(job.id),
        reader.user_id,
        None,
    )
    [shown] = (await support.get("/api/admin/jobs")).json()["items"]
    assert shown["email"] == "reader@example.test"

    plain = (await reader.get("/api/admin/overview")).json()
    assert "audit" not in plain
    full = (await support.get("/api/admin/overview")).json()
    assert isinstance(full["audit"], list) and 0 < len(full["audit"]) <= 10
    assert {entry["action"] for entry in full["audit"]} <= {
        "groups.owner_joined",
        "groups.create",
        "groups.add_member",
    }


# --- the jobs list --------------------------------------------------------------------


async def test_the_jobs_list_pages_filters_and_shows_no_content(make_client) -> None:
    owner = await _owner(make_client, admin_page_size=2)
    base = utcnow() - timedelta(hours=1)
    jobs = [
        await _job(owner, owner.user_id, status="queued", created_at=base),
        await _job(
            owner,
            owner.user_id,
            status="failed",
            created_at=base + timedelta(minutes=1),
            finished_at=base + timedelta(minutes=2),
            error="Upstream refused the page",
            page_count=10,
            pages_spec="1-3,5",
            retry_model="vendor/retry-model",
        ),
        await _job(
            owner, owner.user_id, status="succeeded", created_at=base + timedelta(minutes=3)
        ),
        await _job(
            owner,
            owner.user_id,
            status="running",
            created_at=base + timedelta(minutes=4),
            page_count=None,
        ),
    ]
    async with owner.app.state.sessionmaker() as db:
        db.add(JobPage(job_id=jobs[2].id, page_no=1, status="succeeded", markdown="PAGE-TEXT"))
        await db.commit()

    first = await owner.get("/api/admin/jobs")
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["total"], body["page"], body["page_size"]) == (4, 1, 2)
    assert [item["id"] for item in body["items"]] == [str(jobs[3].id), str(jobs[2].id)]
    assert body["counts"] == {"queued": 1, "running": 1, "succeeded": 1, "failed": 1}
    assert body["worker"]["state"] in ("unknown", "stopped")
    second = (await owner.get("/api/admin/jobs?page=2")).json()
    assert [item["id"] for item in second["items"]] == [str(jobs[1].id), str(jobs[0].id)]
    failed = second["items"][0]
    assert (failed["pages"], failed["model"], failed["error"]) == (
        4,
        "vendor/retry-model",
        "Upstream refused the page",
    )
    assert body["items"][0]["pages"] is None
    # A selection the document cannot satisfy counts no pages, rather than failing the list.
    async with owner.app.state.sessionmaker() as db:
        (await db.get(Job, jobs[2].id)).pages_spec = "9"
        await db.commit()
    assert (await owner.get("/api/admin/jobs")).json()["items"][1]["pages"] is None
    assert (await owner.get("/api/admin/jobs?page=3")).json()["items"] == []

    only = (await owner.get("/api/admin/jobs?status=failed")).json()
    assert ([item["id"] for item in only["items"]], only["total"]) == ([str(jobs[1].id)], 1)
    # The page size is capped by ADMIN_PAGE_SIZE.
    assert (await owner.get("/api/admin/jobs?page_size=50")).json()["page_size"] == 2

    bad = await owner.get("/api/admin/jobs?status=cancelled")
    assert bad.status_code == 422
    assert (await owner.get("/api/admin/jobs?page=0")).status_code == 422

    everything = first.text + repr(second) + repr(only)
    for content in ("confidential-thesis", "SECRET-PROMPT", "PAGE-TEXT"):
        assert content not in everything


async def test_a_running_job_is_marked_stuck_by_the_worker_state(make_client, monkeypatch) -> None:
    owner = await _owner(make_client)
    await _job(owner, owner.user_id, status="running", worker_generation=3)
    _worker(monkeypatch, "running", 3)
    [live] = (await owner.get("/api/admin/jobs")).json()["items"]
    assert live["stuck"] is False
    _worker(monkeypatch, "running", 4)
    [stale] = (await owner.get("/api/admin/jobs")).json()["items"]
    assert stale["stuck"] is True
    _worker(monkeypatch, "unknown")
    [unknown] = (await owner.get("/api/admin/jobs")).json()["items"]
    assert unknown["stuck"] is False


# --- usage ----------------------------------------------------------------------------


def _usage(user_id: int, model: str, when: datetime, cost: str | None, source: str, **tokens):
    return UsageLog(
        user_id=user_id,
        model=model,
        created_at=when,
        cost=None if cost is None else Decimal(cost),
        cost_source=source,
        prompt_tokens=tokens.get("prompt", 0),
        completion_tokens=tokens.get("completion", 0),
    )


async def test_usage_is_summed_per_utc_day_and_model_across_midnight(make_client) -> None:
    owner = await _owner(make_client)
    other = await _account(make_client, "other@example.test")

    def at(day: int, hour: int, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
        return datetime(2026, 3, day, hour, minute, second, micro, tzinfo=UTC)

    async with owner.app.state.sessionmaker() as db:
        db.add_all(
            [
                # Thirty seconds either side of midnight: two days.
                _usage(
                    owner.user_id,
                    "vendor/a",
                    at(14, 23, 59, 30),
                    "0.5",
                    "estimated",
                    prompt=100,
                    completion=10,
                ),
                _usage(
                    owner.user_id,
                    "vendor/a",
                    at(15, 0, 0, 30),
                    "0.25",
                    "estimated",
                    prompt=200,
                    completion=20,
                ),
                _usage(
                    other.user_id,
                    "vendor/a",
                    at(15, 12),
                    "1",
                    "openrouter",
                    prompt=300,
                    completion=30,
                ),
                _usage(other.user_id, "vendor/b", at(15, 8), None, "unknown", prompt=5),
                # Outside the range by a microsecond each way.
                _usage(owner.user_id, "vendor/a", at(13, 23, 59, 59, 999_999), "9", "estimated"),
                _usage(owner.user_id, "vendor/a", at(16, 0), "9", "estimated"),
            ]
        )
        await db.commit()

    response = await owner.get("/api/admin/usage?from=2026-03-14&to=2026-03-15")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from"], body["to"]) == ("2026-03-14", "2026-03-15")
    rows = [
        (
            row["date"],
            row["model"],
            row["calls"],
            row["prompt_tokens"],
            row["completion_tokens"],
            row["cost"],
            row["estimated_cost"],
            row["estimated_calls"],
            row["unpriced_calls"],
        )
        for row in body["rows"]
    ]
    assert rows == [
        ("2026-03-15", "vendor/a", 2, 500, 50, 1.0, 0.25, 1, 0),
        ("2026-03-15", "vendor/b", 1, 5, 0, 0.0, 0.0, 0, 1),
        ("2026-03-14", "vendor/a", 1, 100, 10, 0.0, 0.5, 1, 0),
    ]
    totals = body["totals"]
    assert (totals["calls"], totals["prompt_tokens"], totals["completion_tokens"]) == (4, 605, 60)
    assert (totals["cost"], totals["estimated_cost"]) == (1.0, 0.75)
    assert (totals["estimated_calls"], totals["unpriced_calls"]) == (2, 1)

    one_day = (await owner.get("/api/admin/usage?from=2026-03-14&to=2026-03-14")).json()
    assert [(row["date"], row["calls"]) for row in one_day["rows"]] == [("2026-03-14", 1)]
    empty = (await owner.get("/api/admin/usage?from=2026-01-01&to=2026-01-31")).json()
    assert (empty["rows"], empty["totals"]["calls"]) == ([], 0)


async def test_a_usage_range_is_checked_and_capped(make_client) -> None:
    owner = await _owner(make_client, admin_usage_max_days=90)
    assert (await owner.get("/api/admin/usage?from=2026-01-01&to=2026-03-31")).status_code == 200
    too_long = await owner.get("/api/admin/usage?from=2026-01-01&to=2026-04-01")
    assert too_long.status_code == 422
    assert _error(too_long)["message"] == "The range spans more than 90 days"
    backwards = await owner.get("/api/admin/usage?from=2026-03-02&to=2026-03-01")
    assert backwards.status_code == 422
    for query in ("from=2026-03-01", "to=2026-03-01", "from=2026-13-01&to=2026-03-01", ""):
        assert (await owner.get(f"/api/admin/usage?{query}")).status_code == 422, query


# --- the overview ---------------------------------------------------------------------


async def test_the_overview_counts_jobs_and_todays_usage(make_client, monkeypatch) -> None:
    owner = await _owner(make_client)
    await _account(make_client, "other@example.test")
    now = utcnow()
    midnight = datetime.combine(now.date(), time.min, UTC)
    oldest = now - timedelta(minutes=30)
    await _job(owner, owner.user_id, status="queued", created_at=oldest)
    await _job(owner, owner.user_id, status="queued", created_at=now - timedelta(minutes=5))
    await _job(owner, owner.user_id, status="running", worker_generation=5)
    await _job(owner, owner.user_id, status="running", worker_generation=4)
    last_failed = now - timedelta(hours=2)
    await _job(owner, owner.user_id, status="failed", finished_at=last_failed)
    await _job(owner, owner.user_id, status="failed", finished_at=now - timedelta(hours=30))
    await _job(owner, owner.user_id, status="succeeded", finished_at=now)
    async with owner.app.state.sessionmaker() as db:
        db.add_all(
            [
                _usage(owner.user_id, "vendor/a", now, "0.5", "estimated", prompt=10, completion=2),
                _usage(owner.user_id, "vendor/a", now, "0.25", "openrouter", prompt=1),
                _usage(owner.user_id, "vendor/b", now, None, "unknown"),
                # Before today's UTC midnight: yesterday's.
                _usage(
                    owner.user_id, "vendor/a", midnight - timedelta(seconds=1), "7", "estimated"
                ),
            ]
        )
        await db.commit()

    _worker(monkeypatch, "running", 5)
    response = await owner.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["day"] == now.date().isoformat()
    assert body["accounts"] == 2
    jobs = body["jobs"]
    assert (jobs["queued"], jobs["running"], jobs["stuck"]) == (2, 2, 1)
    assert datetime.fromisoformat(jobs["oldest_queued_at"]) == oldest
    assert (jobs["failed"], jobs["failed_hours"]) == (1, 24)
    assert datetime.fromisoformat(jobs["last_failed_at"]) == last_failed
    assert (body["worker"]["state"], body["worker"]["generation"]) == ("running", 5)
    usage = body["usage"]
    assert [(row["model"], row["calls"]) for row in usage["models"]] == [
        ("vendor/a", 2),
        ("vendor/b", 1),
    ]
    totals = usage["totals"]
    assert (totals["calls"], totals["prompt_tokens"], totals["completion_tokens"]) == (3, 11, 2)
    assert (totals["cost"], totals["estimated_cost"], totals["unpriced_calls"]) == (0.25, 0.5, 1)

    _worker(monkeypatch, "unknown")
    unknown = (await owner.get("/api/admin/overview")).json()
    assert (unknown["jobs"]["stuck"], unknown["worker"]) == (
        None,
        {"state": "unknown", "since": None, "generation": None},
    )
    _worker(monkeypatch, "stopped")
    assert (await owner.get("/api/admin/overview")).json()["jobs"]["stuck"] == 2

    # ADMIN_FAILED_WINDOW_HOURS reaches further back: the job that failed 30 hours ago counts.
    wider = await _owner(make_client, admin_failed_window_hours=48)
    wide = (await wider.get("/api/admin/overview")).json()["jobs"]
    assert (wide["failed"], wide["failed_hours"]) == (2, 48)


async def test_the_overview_shows_the_ten_newest_records_and_no_more_than_a_page(
    make_client,
) -> None:
    owner = await _owner(make_client)
    for n in range(11):
        await _group(owner, f"team-{n}", [])
    async with owner.app.state.sessionmaker() as db:
        newest = list(await db.scalars(select(AuditLog.id).order_by(AuditLog.id.desc()).limit(10)))
    audit = (await owner.get("/api/admin/overview")).json()["audit"]
    assert [entry["id"] for entry in audit] == newest
    # ADMIN_PAGE_SIZE caps every console read, this one too.
    small = await _owner(make_client, admin_page_size=3)
    audit = (await small.get("/api/admin/overview")).json()["audit"]
    assert [entry["id"] for entry in audit] == newest[:3]


async def test_every_database_read_comes_before_the_catalogs(make_client, monkeypatch) -> None:
    """The newest audit records are read with the rest, before Kano Proxy and Kano Pay are
    asked, and no pool connection is held while they are: a read that fails answers without
    asking either."""
    owner = await _owner(make_client)
    engine = owner.app.state.sessionmaker.kw["bind"]
    held: list[int] = []

    async def probe(settings: Settings) -> dict:
        held.append(engine.pool.checkedout())
        return {"state": "unconfigured"}

    monkeypatch.setattr(admin_jobs, "kano_proxy_status", probe)
    monkeypatch.setattr(admin_jobs, "kano_pay_status", probe)
    response = await owner.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    # The owner holds `audit.read`, and joining the owner group wrote a record.
    assert response.json()["audit"]
    assert held == [0, 0]

    async def broken(*args, **kwargs) -> dict:
        raise RuntimeError("the connection dropped")

    monkeypatch.setattr(admin_routes, "list_audit", broken)
    held.clear()
    with pytest.raises(RuntimeError, match="the connection dropped"):
        await owner.get("/api/admin/overview")
    assert held == []


async def test_the_overview_reports_both_connections(make_client) -> None:
    # The suite's Kano Proxy lists three priced models; Kano Pay is not configured.
    owner = await _owner(make_client)
    body = (await owner.get("/api/admin/overview")).json()
    assert body["connections"] == {
        "kano_proxy": {
            "state": "ok",
            "message": None,
            "models": 3,
            "priced": 3,
            "parse_model": "vendor/vision-model",
        },
        "kano_pay": {
            "state": "unconfigured",
            "message": None,
            "checkout_enabled": None,
            "plans": None,
            "packs": None,
        },
    }


@pytest.mark.own_catalog
async def test_each_overview_read_asks_each_catalog_once(make_client) -> None:
    owner = await _owner(make_client, **PAY_SETTINGS)
    with respx.mock(assert_all_called=True) as router:
        models = router.get(KANO_MODELS_URL).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "vendor/vision-model"}, {"id": "vendor/unpriced"}]}
            )
        )
        pay = router.get(CATALOG_URL).mock(
            return_value=httpx.Response(200, json=catalog(enabled=True, plans=("pro", "elsewhere")))
        )
        response = await owner.get("/api/admin/overview")
        assert response.status_code == 200, response.text
        assert (models.call_count, pay.call_count) == (1, 1)
        connections = response.json()["connections"]
        assert connections["kano_proxy"] == {
            "state": "ok",
            "message": None,
            "models": 2,
            "priced": 1,
            "parse_model": "vendor/vision-model",
        }
        # Only the plans configured here and priced there are on sale.
        assert connections["kano_pay"] == {
            "state": "ok",
            "message": None,
            "checkout_enabled": True,
            "plans": 1,
            "packs": 0,
        }
        # Never remembered: the next read asks again.
        await owner.get("/api/admin/overview")
        assert (models.call_count, pay.call_count) == (2, 2)
    for secret in (KANO_API_KEY, PAY_SETTINGS["kano_pay_api_key"]):
        assert secret not in response.text


@pytest.mark.own_catalog
@pytest.mark.parametrize(
    ("proxy", "pay", "proxy_message", "pay_message"),
    [
        (
            httpx.Response(500),
            httpx.Response(503),
            "The upstream returned 500 for /models",
            "Kano Pay answered 503 on /v1/catalog",
        ),
        (
            httpx.Response(401),
            httpx.Response(401),
            "Kano Proxy rejected the operator's key",
            "Kano Pay refused with 401",
        ),
        (
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            "Could not reach Kano Proxy",
            "Kano Pay unreachable on /v1/catalog",
        ),
    ],
)
async def test_a_connection_that_fails_is_reported_not_raised(
    make_client, proxy, pay, proxy_message: str, pay_message: str
) -> None:
    owner = await _owner(make_client, **PAY_SETTINGS)
    with respx.mock(assert_all_called=True) as router:
        if isinstance(proxy, Exception):
            router.get(KANO_MODELS_URL).mock(side_effect=proxy)
            router.get(CATALOG_URL).mock(side_effect=pay)
        else:
            router.get(KANO_MODELS_URL).mock(return_value=proxy)
            router.get(CATALOG_URL).mock(return_value=pay)
        response = await owner.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    connections = response.json()["connections"]
    assert (connections["kano_proxy"]["state"], connections["kano_proxy"]["message"]) == (
        "failed",
        proxy_message,
    )
    assert connections["kano_proxy"]["models"] is None
    assert (connections["kano_pay"]["state"], connections["kano_pay"]["message"]) == (
        "failed",
        pay_message,
    )
    assert connections["kano_pay"]["checkout_enabled"] is None
    assert "kano.test" not in response.text and "pay.test" not in response.text


@pytest.mark.own_catalog
async def test_an_unexpected_error_fails_its_connection_not_the_overview(
    make_client, caplog
) -> None:
    owner = await _owner(make_client, **PAY_SETTINGS)
    with respx.mock(assert_all_called=True) as router:
        # Not an `httpx.HTTPError`: the Kano Proxy client lets it through.
        router.get(KANO_MODELS_URL).mock(side_effect=httpx.InvalidURL("bad"))
        # `plans` that is not a list: reading the catalog raises a TypeError.
        router.get(CATALOG_URL).mock(return_value=httpx.Response(200, json={"plans": 5}))
        response = await owner.get("/api/admin/overview")
    assert response.status_code == 200, response.text
    body = response.json()
    for name in ("kano_proxy", "kano_pay"):
        assert (body["connections"][name]["state"], body["connections"][name]["message"]) == (
            "failed",
            "Unexpected error (see the API's log)",
        )
    # The rest of the overview answers as usual, and the API's log has both errors.
    assert body["jobs"]["queued"] == 0
    assert "Kano Proxy check failed" in caplog.text and "Kano Pay check failed" in caplog.text
    assert KANO_API_KEY not in caplog.text


@pytest.mark.own_catalog
async def test_a_slow_connection_is_given_up_after_the_probe_timeout(make_client) -> None:
    owner = await _owner(make_client, admin_probe_timeout_seconds=0.05, **PAY_SETTINGS)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"data": []})

    # A call cut short is not counted as called.
    with respx.mock(assert_all_called=False) as router:
        router.get(KANO_MODELS_URL).mock(side_effect=slow)
        router.get(CATALOG_URL).mock(side_effect=slow)
        started = asyncio.get_running_loop().time()
        response = await owner.get("/api/admin/overview")
        elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 200, response.text
    assert elapsed < 2
    connections = response.json()["connections"]
    for name in ("kano_proxy", "kano_pay"):
        assert connections[name]["state"] == "failed"
        assert connections[name]["message"] == "No answer within 0.05 seconds"


@pytest.mark.own_catalog
async def test_an_unconfigured_kano_proxy_is_not_asked(make_client) -> None:
    owner = await _owner(make_client, kano_proxy_api_key="")
    with respx.mock(assert_all_called=False) as router:
        models = router.get(KANO_MODELS_URL).mock(return_value=httpx.Response(200, json={}))
        body = (await owner.get("/api/admin/overview")).json()
    assert models.call_count == 0
    assert body["connections"]["kano_proxy"]["state"] == "unconfigured"
