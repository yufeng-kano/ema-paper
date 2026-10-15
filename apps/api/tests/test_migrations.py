"""Migration backfills, run as a real upgrade from the previous revision with existing
rows in place (docs/database.md). PostgreSQL only — the migration chain is PostgreSQL SQL —
so this module skips unless `TEST_DATABASE_URL` points at one (tests/conftest.py). Each
test gets its own scratch database on that server, dropped afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import DATABASE_URL

pytestmark = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="migration tests need TEST_DATABASE_URL on PostgreSQL",
)

API_DIR = Path(__file__).resolve().parents[1]
BEFORE_COST_SOURCE = "cd77f4fe600f"
COST_SOURCE = "5f1845730aa0"


def _alembic(url: str, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv: this interpreter running alembic
        [sys.executable, "-m", "alembic", *args],
        cwd=API_DIR,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )


@pytest_asyncio.fixture
async def scratch_url():
    server = make_url(DATABASE_URL)
    name = f"sightread_migration_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(server, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def test_cost_source_backfill_separates_openrouter_from_discarded_connection_costs(
    scratch_url,
) -> None:
    _alembic(scratch_url, "upgrade", BEFORE_COST_SOURCE)

    engine = create_async_engine(scratch_url)
    openrouter_job, connection_job = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'a@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        connection_id = (
            await db.execute(
                text(
                    "INSERT INTO provider_connections (user_id, name, base_url, ciphertext, "
                    "masked, model, created_at, updated_at) VALUES (:user, 'kano', "
                    "'https://proxy.example/openai/v1', '\\x00', '…cret', 'm', now(), now()) "
                    "RETURNING id"
                ),
                {"user": user_id},
            )
        ).scalar_one()
        for job_id, job_connection in ((openrouter_job, None), (connection_job, connection_id)):
            await db.execute(
                text(
                    "INSERT INTO jobs (id, user_id, kind, filename, media_type, size_bytes, "
                    "sha256, pages_spec, model, profile_version, pipeline_version, "
                    "bbox_format, connection_id, status, pages_done, created_at) VALUES "
                    "(:id, :user, 'image', 'a.png', 'image/png', 1, 'h', '', 'm', 0, 1, "
                    "'yxyx_norm1000', :connection, 'succeeded', 1, now())"
                ),
                {"id": job_id, "user": user_id, "connection": job_connection},
            )
        for job_id, cost in (
            (openrouter_job, "0.004200"),
            (openrouter_job, "0"),  # OpenRouter really billed nothing
            (connection_job, "0"),  # the connection's cost was discarded, not zero
            (None, "0.001000"),
        ):
            await db.execute(
                text(
                    "INSERT INTO usage_log (user_id, job_id, model, prompt_tokens, "
                    "completion_tokens, cost, created_at) "
                    "VALUES (:user, :job, 'm', 1, 1, :cost, now())"
                ),
                {"user": user_id, "job": job_id, "cost": Decimal(cost)},
            )

    _alembic(scratch_url, "upgrade", COST_SOURCE)

    async with engine.connect() as db:
        rows = (
            await db.execute(text("SELECT job_id, cost_source, cost FROM usage_log ORDER BY id"))
        ).all()
        report_cost = (
            await db.execute(text("SELECT report_cost FROM provider_connections"))
        ).scalar_one()
    assert [(row.job_id, row.cost_source, row.cost) for row in rows] == [
        (openrouter_job, "openrouter", Decimal("0.004200")),
        (openrouter_job, "openrouter", Decimal("0")),
        (connection_job, "unknown", None),
        (None, "openrouter", Decimal("0.001000")),
    ]
    # Existing connections stay off: nothing they report is recorded until the user opts in.
    assert report_cost is False

    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO usage_log (user_id, model, prompt_tokens, completion_tokens, "
                    "cost, cost_source, created_at) "
                    "VALUES (:user, 'm', 1, 1, 0, 'unknown', now())"
                ),
                {"user": user_id},
            )

    # Down and up again: the old schema gets back the 0 it used to hold for a connection.
    _alembic(scratch_url, "downgrade", BEFORE_COST_SOURCE)
    async with engine.connect() as db:
        costs = (await db.execute(text("SELECT cost FROM usage_log ORDER BY id"))).scalars().all()
    assert costs == [Decimal("0.004200"), Decimal("0"), Decimal("0"), Decimal("0.001000")]
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()
