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


async def test_kano_proxy_migration_moves_settings_to_the_parse_agent(scratch_url) -> None:
    """From the last OpenRouter schema with existing users (docs/database.md § The Kano
    Proxy migration): a connection user keeps connection and model, an OpenRouter user
    lands on Kano Proxy without a model, presets carry over, history stays readable."""
    _alembic(scratch_url, "upgrade", COST_SOURCE)

    engine = create_async_engine(scratch_url)
    historical_job = uuid.uuid4()
    async with engine.begin() as db:

        async def user(sub: str) -> int:
            return (
                await db.execute(
                    text(
                        "INSERT INTO users (google_sub, email, created_at) "
                        "VALUES (:sub, :email, now()) RETURNING id"
                    ),
                    {"sub": sub, "email": f"{sub}@example.com"},
                )
            ).scalar_one()

        openrouter_user, connection_user, untouched_user = (
            await user("openrouter"),
            await user("connection"),
            await user("untouched"),
        )
        await db.execute(
            text(
                "INSERT INTO openrouter_keys (user_id, ciphertext, masked, updated_at) "
                "VALUES (:user, '\\x00', 'sk-or…', now())"
            ),
            {"user": openrouter_user},
        )
        preset_id = (
            await db.execute(
                text(
                    "INSERT INTO prompt_presets (user_id, name, text, created_at, updated_at) "
                    "VALUES (:user, 'Tables', 'Only tables.', now(), now()) RETURNING id"
                ),
                {"user": openrouter_user},
            )
        ).scalar_one()
        connection_id = (
            await db.execute(
                text(
                    "INSERT INTO provider_connections (user_id, name, base_url, ciphertext, "
                    "masked, model, report_cost, created_at, updated_at) VALUES (:user, "
                    "'custom', 'https://proxy.example/openai/v1', '\\x00', '…cret', "
                    "'grok/grok-4.5', false, now(), now()) RETURNING id"
                ),
                {"user": connection_user},
            )
        ).scalar_one()
        for user_id, model, profile, connection, preset in (
            (openrouter_user, "google/gemini-2.5-flash", "gemini-yxyx", None, preset_id),
            (connection_user, None, None, connection_id, None),
            (untouched_user, "google/gemini-2.5-flash", None, None, None),
        ):
            await db.execute(
                text(
                    "INSERT INTO user_settings (user_id, default_model, default_profile, "
                    "default_connection_id, prompt_preset_id) "
                    "VALUES (:user, :model, :profile, :connection, :preset)"
                ),
                {
                    "user": user_id,
                    "model": model,
                    "profile": profile,
                    "connection": connection,
                    "preset": preset,
                },
            )
        await db.execute(
            text(
                "INSERT INTO jobs (id, user_id, kind, filename, media_type, size_bytes, "
                "sha256, pages_spec, model, profile, profile_version, pipeline_version, "
                "bbox_format, status, pages_done, created_at) VALUES "
                "(:id, :user, 'image', 'a.png', 'image/png', 1, 'h', '', "
                "'google/gemini-2.5-flash', 'gemini-yxyx', 1, 4, 'yxyx_norm1000', "
                "'succeeded', 1, now())"
            ),
            {"id": historical_job, "user": openrouter_user},
        )
        await db.execute(
            text(
                "INSERT INTO usage_log (user_id, job_id, model, prompt_tokens, "
                "completion_tokens, cost, cost_source, created_at) VALUES "
                "(:user, :job, 'google/gemini-2.5-flash', 1, 1, 0.0042, 'openrouter', now())"
            ),
            {"user": openrouter_user, "job": historical_job},
        )

    _alembic(scratch_url, "upgrade", "head")

    async with engine.connect() as db:
        settings = (
            await db.execute(
                text(
                    "SELECT user_id, agent, connection_id, model, prompt_preset_id "
                    "FROM agent_settings ORDER BY user_id"
                )
            )
        ).all()
        tables = set(
            (
                await db.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ).scalars()
        )
        connection_columns = set(
            (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'provider_connections'"
                    )
                )
            ).scalars()
        )
        job = (
            await db.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": historical_job})
        ).mappings().one()
        preset_agent = (await db.execute(text("SELECT agent FROM prompt_presets"))).scalar_one()
        usage = (await db.execute(text("SELECT cost_source, cost FROM usage_log"))).one()
        dedup = (
            await db.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_dedup'")
            )
        ).scalar_one()
        unvalidated = (
            await db.execute(
                text("SELECT conname FROM pg_constraint WHERE contype = 'f' AND NOT convalidated")
            )
        ).all()

    assert [tuple(row) for row in settings] == [
        # OpenRouter user: Kano Proxy, no model (its catalog is not OpenRouter's), preset kept.
        (openrouter_user, "parse", None, None, preset_id),
        # Connection user: the connection and the model it carried.
        (connection_user, "parse", connection_id, "grok/grok-4.5", None),
        # A user who selected neither connection nor preset gets no row (defaults).
    ]
    assert {"user_settings", "openrouter_keys"}.isdisjoint(tables)
    assert {"agent_settings", "kano_proxy_keys", "kano_connect_states"} <= tables
    assert "model" not in connection_columns
    assert job["agent"] == "parse"
    assert job["source_job_id"] is None
    assert "profile" not in job and "profile_version" not in job
    assert preset_agent == "parse"
    assert (usage.cost_source, usage.cost) == ("openrouter", Decimal("0.004200"))
    assert "agent" in dedup and "profile" not in dedup
    assert unvalidated == []

    # The new foreign keys behave: a deleted connection points its agent back at Kano
    # Proxy, a deleted user takes their settings along.
    async with engine.begin() as db:
        await db.execute(
            text("DELETE FROM provider_connections WHERE id = :id"), {"id": connection_id}
        )
        assert (
            await db.execute(
                text("SELECT connection_id FROM agent_settings WHERE user_id = :user"),
                {"user": connection_user},
            )
        ).scalar_one() is None
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": openrouter_user})
        assert (
            await db.execute(
                text("SELECT count(*) FROM agent_settings WHERE user_id = :user"),
                {"user": openrouter_user},
            )
        ).scalar_one() == 0

    # Down and up again with rows in place.
    _alembic(scratch_url, "downgrade", COST_SOURCE)
    async with engine.connect() as db:
        restored = (
            await db.execute(
                text("SELECT user_id, default_connection_id FROM user_settings ORDER BY user_id")
            )
        ).all()
    assert [tuple(row) for row in restored] == [(connection_user, None)]
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()
