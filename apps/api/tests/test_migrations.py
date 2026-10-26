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


def test_the_name_migration_suffix_keeps_counting_past_a_column_long_extension() -> None:
    """A legacy extension nearly as long as the column leaves no room for a stem; the
    candidates must still change with the counter, or the upgrade never ends."""
    import importlib.util

    path = API_DIR / "migrations" / "versions" / "c8f28f7cd45e_normalize_library_names.py"
    spec = importlib.util.spec_from_file_location("normalize_library_names", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    extension = "x" * 253
    name = f"a.{extension}"
    taken = {name, f" (2).{extension}"[:255]}
    chosen = module._free(taken, name, 255)
    assert chosen not in taken and len(chosen) <= 255 and chosen.startswith(" (3).")


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

    # Up to the last revision that still has the connections (the operator upstream
    # migration drops them, docs/database.md § Migrations).
    _alembic(scratch_url, "upgrade", LEDGER)

    async with engine.connect() as db:
        settings = (
            await db.execute(
                text(
                    "SELECT user_id, agent, connection_id, model, prompt_preset_id, prompt "
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
            (await db.execute(text("SELECT * FROM jobs WHERE id = :id"), {"id": historical_job}))
            .mappings()
            .one()
        )
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
        # OpenRouter user: Kano Proxy, no model (its catalog is not OpenRouter's), preset kept
        # — as the agent's own prompt since the agent prompt migration moved it there.
        (openrouter_user, "parse", None, None, None, "Only tables."),
        # Connection user: the connection and the model it carried.
        (connection_user, "parse", connection_id, "grok/grok-4.5", None, None),
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


BEFORE_CHAT = "837c0e9a9a4f"
CHAT = "f74c75c0416e"


async def test_chat_migration_backfills_the_agent_of_every_usage_row(scratch_url) -> None:
    """`usage_log.agent` comes from the row's job; a row without one stays `parse`
    (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", BEFORE_CHAT)

    engine = create_async_engine(scratch_url)
    job_id = uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'c@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, size_bytes, "
                "sha256, pages_spec, model, pipeline_version, bbox_format, status, pages_done, "
                "prompt_sha256, created_at) VALUES (:id, :user, 'parse', 'image', 'a.png', "
                "'image/png', 1, 'h', '', 'm', 1, 'yxyx_norm1000', 'succeeded', 1, '', now())"
            ),
            {"id": job_id, "user": user_id},
        )
        for job in (job_id, None):
            await db.execute(
                text(
                    "INSERT INTO usage_log (user_id, job_id, model, prompt_tokens, "
                    "completion_tokens, cost, cost_source, created_at) "
                    "VALUES (:user, :job, 'm', 1, 1, NULL, 'unknown', now())"
                ),
                {"user": user_id, "job": job},
            )
        await db.execute(
            text("INSERT INTO agent_settings (user_id, agent, model) VALUES (:user, 'parse', 'm')"),
            {"user": user_id},
        )

    _alembic(scratch_url, "upgrade", CHAT)

    async with engine.connect() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT agent, turn_id, cache_read_tokens, cache_write_tokens "
                    "FROM usage_log ORDER BY id"
                )
            )
        ).all()
        effort = (await db.execute(text("SELECT effort FROM agent_settings"))).scalar_one()
        tables = {
            row[0]
            for row in (
                await db.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
        }
    assert [tuple(row) for row in rows] == [("parse", None, 0, 0), ("parse", None, 0, 0)]
    assert effort == "medium"
    assert {"conversations", "messages", "chat_turns", "chat_events"} <= tables
    await engine.dispose()


BEFORE_AGENT_PROMPT = "d843179c6c58"
AGENT_PROMPT = "90dc3c3c5c36"


def _agent_prompt_migration():
    import importlib.util

    path = (
        API_DIR
        / "migrations"
        / "versions"
        / "90dc3c3c5c36_skills_subagents_attachments_and_agent_.py"
    )
    spec = importlib.util.spec_from_file_location("agent_prompt_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def test_the_agent_prompt_migration_moves_every_selected_preset(scratch_url) -> None:
    """From the last schema without `agent_settings.prompt`, with presets selected: a parse
    preset becomes the parse prompt as it was, a chat preset the chat prompt behind that
    release's default, the presets stay, unselected (docs/database.md § The agent prompt
    migration) — and a downgrade takes back what the upgrade made."""
    _alembic(scratch_url, "upgrade", BEFORE_AGENT_PROMPT)

    engine = create_async_engine(scratch_url)
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

        async def preset(user_id: int, agent: str, name: str, body: str) -> int:
            return (
                await db.execute(
                    text(
                        "INSERT INTO prompt_presets (user_id, agent, name, text, created_at, "
                        "updated_at) VALUES (:user, :agent, :name, :text, now(), now()) "
                        "RETURNING id"
                    ),
                    {"user": user_id, "agent": agent, "name": name, "text": body},
                )
            ).scalar_one()

        reader, plain, tokened = await user("reader"), await user("plain"), await user("tokened")
        parse_preset = await preset(reader, "parse", "Tables", "  Only tables from page {page}. ")
        chat_preset = await preset(reader, "chat", "Haiku", "Answer in haiku.")
        await preset(plain, "parse", "Unused", "Never selected.")
        # Appended verbatim until now: as part of the template its token would be rendered.
        verbatim = await preset(tokened, "chat", "Literal", "Quote {document_path} as is.")
        for user_id, agent, chosen in (
            (reader, "parse", parse_preset),
            (reader, "chat", chat_preset),
            (plain, "parse", None),
            (tokened, "chat", verbatim),
        ):
            await db.execute(
                text(
                    "INSERT INTO agent_settings (user_id, agent, model, prompt_preset_id) "
                    "VALUES (:user, :agent, 'm', :preset)"
                ),
                {"user": user_id, "agent": agent, "preset": chosen},
            )

    _alembic(scratch_url, "upgrade", AGENT_PROMPT)

    async with engine.connect() as db:
        settings = {
            (row.user_id, row.agent): (row.prompt, row.prompt_preset_id)
            for row in (
                await db.execute(
                    text("SELECT user_id, agent, prompt, prompt_preset_id FROM agent_settings")
                )
            ).all()
        }
        presets = (await db.execute(text("SELECT count(*) FROM prompt_presets"))).scalar_one()
    default = _agent_prompt_migration().CHAT_DEFAULT_PROMPT
    assert default.startswith("You are the document agent inside Sightread")
    assert settings == {
        (reader, "parse"): ("Only tables from page {page}.", None),
        (reader, "chat"): (f"{default.strip()}\n\nAnswer in haiku.", None),
        (plain, "parse"): (None, None),
        (tokened, "chat"): (None, verbatim),
    }
    assert presets == 4

    async with engine.begin() as db:
        # The new tables take rows, and a user's names are their own.
        await db.execute(
            text(
                "INSERT INTO skills (user_id, name, description, argument_hint, body, "
                "created_at, updated_at) VALUES (:user, 'mine', '', '', 'x', now(), now())"
            ),
            {"user": reader},
        )
        defaults = (
            await db.execute(text("SELECT user_invocable, model_invocable FROM skills"))
        ).one()
    assert tuple(defaults) == (True, True)
    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO skills (user_id, name, description, argument_hint, body, "
                    "created_at, updated_at) VALUES (:user, 'mine', '', '', 'y', now(), now())"
                ),
                {"user": reader},
            )

    async with engine.begin() as db:
        # A prompt written after the upgrade, which no preset holds.
        await db.execute(
            text(
                "UPDATE agent_settings SET prompt = 'Edited after the upgrade.' "
                "WHERE user_id = :user AND agent = 'parse'"
            ),
            {"user": plain},
        )

    _alembic(scratch_url, "downgrade", BEFORE_AGENT_PROMPT)
    async with engine.connect() as db:
        # A rollback leaves every agent running what it ran: the moved presets are selected
        # again, and the later prompt is a preset of its own.
        selected = {
            (row.user_id, row.agent): row.prompt_preset_id
            for row in (
                await db.execute(
                    text("SELECT user_id, agent, prompt_preset_id FROM agent_settings")
                )
            ).all()
        }
        restored = (
            await db.execute(
                text(
                    "SELECT p.name, p.text FROM agent_settings s "
                    "JOIN prompt_presets p ON p.id = s.prompt_preset_id "
                    "WHERE s.user_id = :user AND s.agent = 'parse'"
                ),
                {"user": plain},
            )
        ).one()
        tables = {
            row[0]
            for row in (
                await db.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            ).all()
        }
        columns = {
            row[0]
            for row in (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_settings'"
                    )
                )
            ).all()
        }
    assert selected[(reader, "parse")] == parse_preset
    assert selected[(reader, "chat")] == chat_preset
    assert selected[(tokened, "chat")] == verbatim
    assert tuple(restored) == ("Custom prompt", "Edited after the upgrade.")
    assert not {"skills", "subagents", "chat_attachments"} & tables
    assert "prompt" not in columns
    await engine.dispose()


BEFORE_SUBAGENT_REMOVAL = "d4b5d736a2ff"
SUBAGENT_REMOVAL = "ecd8f04df825"


async def test_the_removed_built_in_subagents_take_the_readers_edits_with_them(
    scratch_url,
) -> None:
    """The rows under `verifier`, `reader` and `translation-checker` go; the edit of the
    built-in that stays, and a subagent of the reader's own, are left as they were
    (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", BEFORE_SUBAGENT_REMOVAL)

    engine = create_async_engine(scratch_url)
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 's@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        for name, writes in (
            ("verifier", False),
            ("reader", False),
            ("translation-checker", True),
            ("translator", True),
            ("tables", False),
        ):
            await db.execute(
                text(
                    "INSERT INTO subagents (user_id, name, description, model, writes, tools, "
                    "prompt, created_at, updated_at) VALUES (:user, :name, '', 'm', :writes, "
                    "'[\"view_page\"]', 'x', now(), now())"
                ),
                {"user": user_id, "name": name, "writes": writes},
            )

    _alembic(scratch_url, "upgrade", SUBAGENT_REMOVAL)

    async with engine.connect() as db:
        names = (await db.execute(text("SELECT name FROM subagents ORDER BY name"))).scalars()
        assert list(names) == ["tables", "translator"]
    await engine.dispose()


INSTRUCTIONS_AND_MEMORIES = "09bcc2e96c63"


async def test_the_memory_migration_keeps_every_row_and_scopes_memories(scratch_url) -> None:
    """Existing settings and conversations read as before — no instructions, memory on and
    not paused, no snapshot yet — and a memory goes with its document but outlives the
    conversation that saved it (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", SUBAGENT_REMOVAL)

    engine = create_async_engine(scratch_url)
    job_id = uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'm@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO agent_settings (user_id, agent, model, prompt) "
                "VALUES (:user, 'chat', 'm', 'Mine.')"
            ),
            {"user": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, size_bytes, "
                "sha256, pages_spec, model, pipeline_version, bbox_format, status, pages_done, "
                "prompt_sha256, created_at) VALUES (:id, :user, 'parse', 'image', 'a.png', "
                "'image/png', 1, 'h', '', 'm', 1, 'yxyx_norm1000', 'succeeded', 1, '', now())"
            ),
            {"id": job_id, "user": user_id},
        )
        document_id = (
            await db.execute(
                text(
                    "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                    "updated_at) VALUES (:user, NULL, :job, 'a.png', now(), now()) RETURNING id"
                ),
                {"user": user_id, "job": job_id},
            )
        ).scalar_one()
        conversation_id = (
            await db.execute(
                text(
                    "INSERT INTO conversations (user_id, document_id, title, session, model, "
                    "created_at, updated_at) VALUES (:user, :document, 'Thread', 's', 'm', "
                    "now(), now()) RETURNING id"
                ),
                {"user": user_id, "document": document_id},
            )
        ).scalar_one()

    _alembic(scratch_url, "upgrade", INSTRUCTIONS_AND_MEMORIES)

    async with engine.begin() as db:
        indexes = dict(
            (
                await db.execute(
                    text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'memories'")
                )
            ).all()
        )
        # What a document's delete and a conversation's look memories up by is indexed, so
        # neither scans every reader's memories.
        assert "(document_id) WHERE (document_id IS NOT NULL)" in indexes["ix_memories_document_id"]
        assert (
            "(conversation_id) WHERE (conversation_id IS NOT NULL)"
            in (indexes["ix_memories_conversation_id"])
        )
        setting = (
            (
                await db.execute(
                    text(
                        "SELECT prompt, instructions, answer_language, translate_language, "
                        "memory_auto, memory_paused FROM agent_settings"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert dict(setting) == {
            "prompt": "Mine.",
            "instructions": None,
            "answer_language": None,
            "translate_language": None,
            "memory_auto": True,
            "memory_paused": False,
        }
        snapshot = (await db.execute(text("SELECT memory_ids FROM conversations"))).scalar_one()
        assert snapshot is None
        for scope in (document_id, None):
            await db.execute(
                text(
                    "INSERT INTO memories (user_id, kind, text, document_id, conversation_id, "
                    "source_ref, created_at, updated_at) VALUES (:user, 'document', :text, "
                    ":document, :conversation, 'toolu_1', now(), now())"
                ),
                {
                    "user": user_id,
                    "text": f"scoped to {scope}",
                    "document": scope,
                    "conversation": conversation_id,
                },
            )
    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO memories (user_id, kind, text, created_at, updated_at) "
                    "VALUES (:user, 'fact', 'x', now(), now())"
                ),
                {"user": user_id},
            )

    async with engine.begin() as db:
        await db.execute(text("DELETE FROM conversations WHERE id = :id"), {"id": conversation_id})
        kept = (
            await db.execute(
                text("SELECT document_id, conversation_id, pinned FROM memories ORDER BY id")
            )
        ).all()
        # Deleting the conversation keeps what it saved, its source cleared.
        assert [tuple(row) for row in kept] == [(document_id, None, False), (None, None, False)]
        await db.execute(text("DELETE FROM documents WHERE id = :id"), {"id": document_id})
        left = (await db.execute(text("SELECT text FROM memories"))).scalars().all()
        # A note about one document goes with it, never widened to every document.
        assert left == ["scoped to None"]

    _alembic(scratch_url, "downgrade", SUBAGENT_REMOVAL)
    async with engine.connect() as db:
        tables = set(
            (
                await db.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ).scalars()
        )
        columns = set(
            (
                await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name IN ('agent_settings', 'conversations')"
                    )
                )
            ).scalars()
        )
    added = {
        "instructions",
        "answer_language",
        "translate_language",
        "memory_auto",
        "memory_paused",
        "memory_ids",
    }
    assert "memories" not in tables
    assert not added & columns
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()


MARKDOWN_DOCUMENTS = "a19738bb7f6e"


async def test_the_markdown_migration_keeps_every_job_and_refuses_to_downgrade_over_one(
    scratch_url,
) -> None:
    """Existing documents keep their jobs; a document without one can be stored after the
    upgrade, and the downgrade refuses while it stands rather than delete it to bring the
    constraint back (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", INSTRUCTIONS_AND_MEMORIES)

    engine = create_async_engine(scratch_url)
    job_id = uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'm@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, size_bytes, "
                "sha256, pages_spec, model, pipeline_version, bbox_format, status, pages_done, "
                "prompt_sha256, created_at) VALUES (:id, :user, 'parse', 'pdf', 'a.pdf', "
                "'application/pdf', 1, 'h', '', 'm', 1, 'yxyx_norm1000', 'succeeded', 1, '', "
                "now())"
            ),
            {"id": job_id, "user": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                "updated_at) VALUES (:user, NULL, :job, 'a.pdf', now(), now())"
            ),
            {"user": user_id, "job": job_id},
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                    "updated_at) VALUES (:user, NULL, NULL, 'notes.md', now(), now())"
                ),
                {"user": user_id},
            )

    _alembic(scratch_url, "upgrade", MARKDOWN_DOCUMENTS)

    async with engine.begin() as db:
        kept = (await db.execute(text("SELECT name, job_id FROM documents"))).all()
        assert [tuple(row) for row in kept] == [("a.pdf", job_id)]
        markdown_id = (
            await db.execute(
                text(
                    "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                    "updated_at) VALUES (:user, NULL, NULL, 'notes.md', now(), now()) "
                    "RETURNING id"
                ),
                {"user": user_id},
            )
        ).scalar_one()

    with pytest.raises(subprocess.CalledProcessError) as refused:
        _alembic(scratch_url, "downgrade", INSTRUCTIONS_AND_MEMORIES)
    assert b"1 markdown document(s) have no job" in refused.value.stderr
    async with engine.begin() as db:
        # Refused whole: the row is still there, and so is the revision.
        version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert version == MARKDOWN_DOCUMENTS
        await db.execute(text("DELETE FROM documents WHERE id = :id"), {"id": markdown_id})

    _alembic(scratch_url, "downgrade", INSTRUCTIONS_AND_MEMORIES)
    async with engine.connect() as db:
        nullable = (
            await db.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'job_id'"
                )
            )
        ).scalar_one()
    assert nullable == "NO"
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()


LIBRARY_TRASH = "593bad709660"


async def test_the_trash_migration_frees_trashed_names_and_refuses_to_downgrade_over_them(
    scratch_url,
) -> None:
    """Every existing row stays live; after the upgrade a row in the Trash no longer holds
    its name, and the downgrade refuses while anything is in the Trash rather than choose
    which of two same-named rows survives (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", MARKDOWN_DOCUMENTS)

    engine = create_async_engine(scratch_url)
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 't@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        folder_id = (
            await db.execute(
                text(
                    "INSERT INTO folders (user_id, parent_id, name, created_at, updated_at) "
                    "VALUES (:user, NULL, 'Papers', now(), now()) RETURNING id"
                ),
                {"user": user_id},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                "updated_at) VALUES (:user, :folder, NULL, 'a.md', now(), now())"
            ),
            {"user": user_id, "folder": folder_id},
        )

    _alembic(scratch_url, "upgrade", LIBRARY_TRASH)

    async with engine.begin() as db:
        marks = (
            await db.execute(
                text("SELECT trashed_at FROM folders UNION ALL SELECT trashed_at FROM documents")
            )
        ).scalars()
        assert list(marks) == [None, None]
        # In the Trash, a folder and a file free their names, at the top level and inside
        # a folder.
        await db.execute(text("UPDATE folders SET trashed_at = now()"))
        await db.execute(text("UPDATE documents SET trashed_at = now()"))
        await db.execute(
            text(
                "INSERT INTO folders (user_id, parent_id, name, created_at, updated_at) "
                "VALUES (:user, NULL, 'Papers', now(), now())"
            ),
            {"user": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                "updated_at) VALUES (:user, :folder, NULL, 'a.md', now(), now())"
            ),
            {"user": user_id, "folder": folder_id},
        )
    # Two live rows still cannot share a name.
    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO folders (user_id, parent_id, name, created_at, updated_at) "
                    "VALUES (:user, NULL, 'Papers', now(), now())"
                ),
                {"user": user_id},
            )

    with pytest.raises(subprocess.CalledProcessError) as refused:
        _alembic(scratch_url, "downgrade", MARKDOWN_DOCUMENTS)
    assert b"2 item(s) are in the Trash" in refused.value.stderr
    async with engine.begin() as db:
        version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        assert version == LIBRARY_TRASH
        await db.execute(text("DELETE FROM documents WHERE trashed_at IS NOT NULL"))
        # Put back under a free name: nothing left in the Trash, the downgrade goes ahead.
        await db.execute(
            text("UPDATE folders SET name = 'Old papers', trashed_at = NULL WHERE id = :id"),
            {"id": folder_id},
        )

    _alembic(scratch_url, "downgrade", MARKDOWN_DOCUMENTS)
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()


RETRY_FAILED_PAGES = "556f2408fba9"


async def test_the_failed_pages_migration_keeps_every_row_and_falls_back_to_nothing(
    scratch_url,
) -> None:
    """Existing settings take no fallback, and existing jobs and pages keep every value —
    a page that succeeded keeps the markdown a retry reads. The downgrade drops only what
    the upgrade added (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", LIBRARY_TRASH)

    engine = create_async_engine(scratch_url)
    job_id = uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'f@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        await db.execute(
            text("INSERT INTO agent_settings (user_id, agent, model) VALUES (:user, 'parse', 'm')"),
            {"user": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, size_bytes, "
                "sha256, pages_spec, model, pipeline_version, bbox_format, status, pages_done, "
                "prompt_sha256, error, created_at) VALUES (:id, :user, 'parse', 'pdf', 'a.pdf', "
                "'application/pdf', 1, 'h', '', 'm', 1, 'yxyx_norm1000', 'failed', 2, '', "
                "'no page could be parsed', now())"
            ),
            {"id": job_id, "user": user_id},
        )
        await db.execute(
            text(
                "INSERT INTO job_pages (job_id, page_no, method, status, error, markdown) "
                "VALUES (:job, 1, 'vision', 'succeeded', NULL, '# One'), "
                "(:job, 2, 'vision', 'failed', 'upstream call failed', NULL)"
            ),
            {"job": job_id},
        )

    _alembic(scratch_url, "upgrade", RETRY_FAILED_PAGES)

    async with engine.begin() as db:
        setting = (
            await db.execute(
                text("SELECT model, fallback_model, auto_fallback FROM agent_settings")
            )
        ).one()
        assert tuple(setting) == ("m", None, False)
        job = (
            await db.execute(text("SELECT status, error, fallback_model, retry_model FROM jobs"))
        ).one()
        assert tuple(job) == ("failed", "no page could be parsed", None, None)
        pages = (
            await db.execute(
                text("SELECT page_no, status, markdown, model FROM job_pages ORDER BY page_no")
            )
        ).all()
        assert [tuple(row) for row in pages] == [
            (1, "succeeded", "# One", None),
            (2, "failed", None, None),
        ]

    _alembic(scratch_url, "downgrade", LIBRARY_TRASH)
    async with engine.connect() as db:
        added = (
            await db.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE (table_name = 'agent_settings' "
                    "AND column_name IN ('fallback_model', 'auto_fallback')) "
                    "OR (table_name = 'jobs' AND column_name IN ('fallback_model', 'retry_model')) "
                    "OR (table_name = 'job_pages' AND column_name = 'model')"
                )
            )
        ).all()
        assert added == []
        assert (await db.execute(text("SELECT count(*) FROM job_pages"))).scalar_one() == 2
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()


FORMAT_CHECK = "6f91eb4561a5"


async def test_the_format_check_migration_checks_nothing_that_came_before(scratch_url) -> None:
    """Existing settings take the check's default (on); a job queued before it runs
    unchecked, and a result written before it has no reading copy. The downgrade drops
    only what the upgrade added (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", RETRY_FAILED_PAGES)

    engine = create_async_engine(scratch_url)
    queued, done = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'c@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        await db.execute(
            text("INSERT INTO agent_settings (user_id, agent, model) VALUES (:user, 'parse', 'm')"),
            {"user": user_id},
        )
        for job_id, status in ((queued, "queued"), (done, "succeeded")):
            await db.execute(
                text(
                    "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                    "size_bytes, sha256, pages_spec, model, pipeline_version, bbox_format, "
                    "status, pages_done, prompt_sha256, created_at) VALUES (:id, :user, "
                    "'parse', 'pdf', 'a.pdf', 'application/pdf', 1, 'h', '', 'm', 1, "
                    "'yxyx_norm1000', :status, 0, '', now())"
                ),
                {"id": job_id, "user": user_id, "status": status},
            )
        await db.execute(
            text(
                "INSERT INTO results (job_id, markdown, pages, figures, errors, meta, created_at) "
                "VALUES (:job, '<!-- page: 1 -->\nOne', '[]', '[]', '[]', '{}', now())"
            ),
            {"job": done},
        )

    _alembic(scratch_url, "upgrade", FORMAT_CHECK)

    async with engine.begin() as db:
        setting = (await db.execute(text("SELECT model, auto_check FROM agent_settings"))).one()
        assert tuple(setting) == ("m", True)
        jobs = (
            await db.execute(
                text(
                    "SELECT status, check_model, check_effort, check_prompt FROM jobs "
                    "ORDER BY status"
                )
            )
        ).all()
        assert [tuple(row) for row in jobs] == [
            ("queued", None, None, None),
            ("succeeded", None, None, None),
        ]
        result = (await db.execute(text("SELECT markdown, reading_markdown FROM results"))).one()
        assert tuple(result) == ("<!-- page: 1 -->\nOne", None)

    _alembic(scratch_url, "downgrade", RETRY_FAILED_PAGES)
    async with engine.connect() as db:
        added = (
            await db.execute(
                text(
                    "SELECT table_name || '.' || column_name FROM information_schema.columns "
                    "WHERE (table_name = 'agent_settings' AND column_name = 'auto_check') "
                    "OR (table_name = 'jobs' AND column_name IN "
                    "('check_model', 'check_effort', 'check_prompt')) "
                    "OR (table_name = 'results' AND column_name = 'reading_markdown')"
                )
            )
        ).all()
        assert added == []
        assert (await db.execute(text("SELECT count(*) FROM results"))).scalar_one() == 1
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()


FIGURE_CHECK = "fe01cb0fe911"
RECONVERT = "3241469fe768"


async def test_the_reconvert_migration_leaves_every_document_on_its_own_parse(
    scratch_url,
) -> None:
    """No existing document has a re-convert; the pointer names only a job, and a job
    that goes takes only the pointer with it, never the document. The downgrade drops
    only what the upgrade added (docs/database.md § Rules)."""
    _alembic(scratch_url, "upgrade", FIGURE_CHECK)

    engine = create_async_engine(scratch_url)
    parse, reconvert = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as db:
        user_id = (
            await db.execute(
                text(
                    "INSERT INTO users (google_sub, email, created_at) "
                    "VALUES ('sub', 'r@example.com', now()) RETURNING id"
                )
            )
        ).scalar_one()
        for job_id in (parse, reconvert):
            await db.execute(
                text(
                    "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                    "size_bytes, sha256, pages_spec, model, pipeline_version, bbox_format, "
                    "status, pages_done, prompt_sha256, created_at) VALUES (:id, :user, "
                    "'parse', 'pdf', 'a.pdf', 'application/pdf', 1, 'h', '', 'm', 1, "
                    "'yxyx_norm1000', 'succeeded', 1, '', now())"
                ),
                {"id": job_id, "user": user_id},
            )
        await db.execute(
            text(
                "INSERT INTO documents (user_id, folder_id, job_id, name, created_at, "
                "updated_at) VALUES (:user, NULL, :job, 'a.pdf', now(), now())"
            ),
            {"user": user_id, "job": parse},
        )

    _alembic(scratch_url, "upgrade", RECONVERT)

    async with engine.begin() as db:
        row = (await db.execute(text("SELECT job_id, reconvert_job_id FROM documents"))).one()
        assert tuple(row) == (parse, None)
    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE documents SET reconvert_job_id = :job"), {"job": uuid.uuid4()}
            )
    async with engine.begin() as db:
        await db.execute(text("UPDATE documents SET reconvert_job_id = :job"), {"job": reconvert})
        await db.execute(text("DELETE FROM jobs WHERE id = :job"), {"job": reconvert})
        row = (await db.execute(text("SELECT job_id, reconvert_job_id FROM documents"))).one()
        assert tuple(row) == (parse, None)

    _alembic(scratch_url, "downgrade", FIGURE_CHECK)
    async with engine.connect() as db:
        added = (
            await db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'documents' AND column_name = 'reconvert_job_id' "
                    "UNION ALL SELECT indexname FROM pg_indexes "
                    "WHERE indexname = 'ix_documents_reconvert_job_id'"
                )
            )
        ).all()
        assert added == []
        assert (await db.execute(text("SELECT count(*) FROM documents"))).scalar_one() == 1
    _alembic(scratch_url, "upgrade", "head")
    await engine.dispose()


BEFORE_BILLING = "3241469fe768"
BILLING = "bd4210aa38cf"
BILLING_TABLES = ("subscriptions", "checkouts", "pay_events", "credit_periods", "credit_entries")


async def _count(connection, table: str) -> int:
    """Rows of one of the tables named in this module — never a value from outside it."""
    return (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar()  # noqa: S608


async def test_the_billing_migration_adds_five_empty_tables_and_touches_nothing(scratch_url):
    """docs/billing.md § Acceptance: applied over existing users, jobs and usage rows,
    which it leaves as they are; the downgrade drops the five tables and nothing else."""
    _alembic(scratch_url, "upgrade", BEFORE_BILLING)
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, name, created_at) "
                    "VALUES (1, 'sub-1', 'a@example.com', 'A', now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                    "size_bytes, sha256, pages_spec, model, bbox_format, prompt_sha256, "
                    "pipeline_version, status, pages_done, retain_source, created_at) VALUES "
                    "('11111111-1111-1111-1111-111111111111', 1, 'parse', 'pdf', 'a.pdf', "
                    "'application/pdf', 1, 'h', 'all', 'm', 'yxyx_norm1000', 'p', 1, "
                    "'succeeded', 1, false, now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO usage_log (user_id, job_id, agent, model, prompt_tokens, "
                    "completion_tokens, cost, cost_source, created_at) VALUES (1, "
                    "'11111111-1111-1111-1111-111111111111', 'parse', 'm', 1, 1, NULL, "
                    "'unknown', now())"
                )
            )
        _alembic(scratch_url, "upgrade", BILLING)
        async with engine.begin() as connection:
            for table in BILLING_TABLES:
                assert await _count(connection, table) == 0, table
            assert await _count(connection, "users") == 1
            assert await _count(connection, "usage_log") == 1
            # The constraints hold: a chat entry without a turn, a parse entry with one.
            await connection.execute(
                text(
                    "INSERT INTO credit_periods (user_id, period_start, period_end, plan_id, "
                    "granted, spent, created_at, updated_at) VALUES (1, '2026-10-01+00', "
                    "'2026-11-01+00', NULL, 150, 0, now(), now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, "
                    "job_id, created_at) VALUES (1, '2026-10-01+00', 'parse', 'reserved', 3, "
                    "'11111111-1111-1111-1111-111111111111', now())"
                )
            )
        async with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, "
                        "created_at) VALUES (1, '2026-10-01+00', 'chat', 'consumed', 1, now())"
                    )
                )
        async with engine.begin() as connection:
            # A user deleted takes their ledger with them.
            await connection.execute(text("DELETE FROM users WHERE id = 1"))
            for table in ("credit_periods", "credit_entries"):
                assert await _count(connection, table) == 0
        _alembic(scratch_url, "downgrade", BEFORE_BILLING)
        async with engine.connect() as connection:
            names = {
                row[0]
                for row in await connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            }
        assert not names & set(BILLING_TABLES)
        assert {"users", "jobs", "usage_log"} <= names
    finally:
        await engine.dispose()


LEDGER = "1906f6693711"


async def test_the_ledger_migration_keeps_a_deleted_conversations_entries(scratch_url):
    """docs/database.md § Migrations: a chat entry outlives its turn after the upgrade,
    and the downgrade refuses while any such entry stands rather than delete it."""
    _alembic(scratch_url, "upgrade", BILLING)
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, name, created_at) "
                    "VALUES (1, 'sub-1', 'a@example.com', 'A', now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO documents (id, user_id, folder_id, job_id, name, created_at, "
                    "updated_at) VALUES (1, 1, NULL, NULL, 'a.md', now(), now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO conversations (id, user_id, document_id, title, session, "
                    "model, created_at, updated_at) VALUES (1, 1, 1, 'Thread', 's', 'm', "
                    "now(), now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO chat_turns (id, user_id, conversation_id, message_id, status, "
                    "locale, created_at, finished_at) VALUES "
                    "('22222222-2222-2222-2222-222222222222', 1, 1, 1, 'succeeded', 'en', "
                    "now(), now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO credit_periods (user_id, period_start, period_end, plan_id, "
                    "granted, spent, created_at, updated_at) VALUES (1, '2026-10-01+00', "
                    "'2026-11-01+00', NULL, 150, 2, now(), now())"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, "
                    "turn_id, created_at, settled_at) VALUES (1, '2026-10-01+00', 'chat', "
                    "'consumed', 2, '22222222-2222-2222-2222-222222222222', now(), now())"
                )
            )
        _alembic(scratch_url, "upgrade", LEDGER)
        async with engine.begin() as connection:
            # The thread goes; its cost stays, in the period and in the record.
            await connection.execute(text("DELETE FROM conversations WHERE id = 1"))
            assert await _count(connection, "chat_turns") == 0
            rows = (
                await connection.execute(
                    text("SELECT kind, state, amount, turn_id FROM credit_entries")
                )
            ).all()
            assert rows == [("chat", "consumed", 2, None)]
            spent = (await connection.execute(text("SELECT spent FROM credit_periods"))).scalar()
            assert spent == 2
        async with engine.connect() as connection:
            # A parse entry still never has a turn.
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO credit_entries (user_id, period_start, kind, state, "
                        "amount, job_id, turn_id, created_at) VALUES (1, '2026-10-01+00', "
                        "'parse', 'reserved', 1, NULL, NULL, now())"
                    )
                )
        with pytest.raises(subprocess.CalledProcessError) as refused:
            _alembic(scratch_url, "downgrade", BILLING)
        assert b"1 chat credit entries have no turn" in refused.value.stderr
        async with engine.begin() as connection:
            version = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar()
            assert version == LEDGER
            await connection.execute(text("DELETE FROM credit_entries"))
        _alembic(scratch_url, "downgrade", BILLING)
    finally:
        await engine.dispose()


OPERATOR_UPSTREAM = "8431924126b1"


async def test_the_operator_upstream_migration_drops_the_keys_and_keeps_the_history(
    scratch_url,
) -> None:
    """docs/database.md § Migrations: the three key tables and the two columns go, a
    parse row's model with them, a chat row's pick and a job's connection stay; the
    downgrade puts the tables and columns back empty."""
    _alembic(scratch_url, "upgrade", LEDGER)
    engine = create_async_engine(scratch_url)
    job_id = uuid.uuid4()
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, created_at) "
                    "VALUES (1, 'sub', 'o@example.com', now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO provider_connections (id, user_id, name, base_url, ciphertext, "
                    "masked, report_cost, created_at, updated_at) VALUES (7, 1, 'own', "
                    "'https://llm.example/v1', '\\x00'::bytea, '...ab', false, now(), now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO kano_proxy_keys (user_id, ciphertext, masked, updated_at) "
                    "VALUES (1, '\\x00'::bytea, '...cd', now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO kano_connect_states (user_id, state_hash, verifier_ciphertext, "
                    "created_at, expires_at) VALUES (1, 'h', '\\x00'::bytea, now(), now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO agent_settings (user_id, agent, connection_id, model, "
                    "fallback_model, auto_fallback) VALUES (1, 'parse', 7, 'vendor/old', "
                    "'vendor/older', true), (1, 'check', NULL, 'vendor/checker', NULL, false), "
                    "(1, 'chat', NULL, 'vendor/chosen', NULL, false)"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                    "size_bytes, sha256, pages_spec, model, connection_id, connection_base_url, "
                    "pipeline_version, bbox_format, status, pages_done, prompt_sha256, "
                    "created_at) VALUES (:id, 1, 'parse', 'pdf', 'a.pdf', 'application/pdf', 1, "
                    "'h', '', 'vendor/old', 7, 'https://llm.example/v1', 1, 'yxyx_norm1000', "
                    "'queued', 0, '', now())"
                ),
                {"id": job_id},
            )
        _alembic(scratch_url, "upgrade", OPERATOR_UPSTREAM)
        async with engine.begin() as db:
            tables = {
                row[0]
                for row in await db.execute(
                    text("SELECT table_name FROM information_schema.tables")
                )
            }
            assert not tables & {"provider_connections", "kano_proxy_keys", "kano_connect_states"}
            columns = {
                row[0]
                for row in await db.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'agent_settings'"
                    )
                )
            }
            assert not columns & {"connection_id", "fallback_model"}
            rows = (
                await db.execute(
                    text("SELECT agent, model, auto_fallback FROM agent_settings ORDER BY agent")
                )
            ).all()
            assert rows == [
                ("chat", "vendor/chosen", False),
                ("check", None, False),
                ("parse", None, True),
            ]
            job = (
                await db.execute(text("SELECT connection_id, connection_base_url FROM jobs"))
            ).one()
            assert job == (7, "https://llm.example/v1")
        _alembic(scratch_url, "downgrade", LEDGER)
        async with engine.begin() as db:
            for table in ("provider_connections", "kano_proxy_keys", "kano_connect_states"):
                assert await _count(db, table) == 0
            rows = (
                await db.execute(
                    text("SELECT agent, connection_id, fallback_model FROM agent_settings")
                )
            ).all()
            assert {row[1:] for row in rows} == {(None, None)}
    finally:
        await engine.dispose()


PARSE_RETENTION = "e014be293754"
RETENTION_INDEXES = {
    "ix_documents_job_id",
    "ix_usage_log_job_id",
    "ix_documents_trashed_at",
    "ix_folders_trashed_at",
}


async def _indexes(connection) -> set[str]:
    return {row[0] for row in await connection.execute(text("SELECT indexname FROM pg_indexes"))}


async def test_the_retention_migration_keeps_a_deleted_parses_ledger_entry(scratch_url) -> None:
    """docs/database.md § Migrations: after the upgrade a parse's credit entry outlives its
    job with `job_id` NULL, the period's `spent` unchanged, as its usage row does; a chat
    entry still never names a job; the downgrade refuses while such an entry stands rather
    than delete it from the billing record, and drops the indexes once it may go."""
    _alembic(scratch_url, "upgrade", OPERATOR_UPSTREAM)
    engine = create_async_engine(scratch_url)
    parse, other = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, created_at) "
                    "VALUES (1, 'sub', 'r@example.com', now())"
                )
            )
            for job_id in (parse, other):
                await db.execute(
                    text(
                        "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                        "size_bytes, sha256, pages_spec, model, pipeline_version, bbox_format, "
                        "status, pages_done, prompt_sha256, created_at, finished_at) VALUES "
                        "(:id, 1, 'parse', 'pdf', 'a.pdf', 'application/pdf', 1, 'h', '', "
                        "'vendor/m', 1, 'yxyx_norm1000', 'succeeded', 1, '', now(), now())"
                    ),
                    {"id": job_id},
                )
            await db.execute(
                text(
                    "INSERT INTO credit_periods (user_id, period_start, period_end, plan_id, "
                    "granted, spent, created_at, updated_at) VALUES (1, '2026-10-01+00', "
                    "'2026-11-01+00', NULL, 150, 3, now(), now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, "
                    "job_id, created_at, settled_at) VALUES (1, '2026-10-01+00', 'parse', "
                    "'consumed', 3, :job, now(), now())"
                ),
                {"job": parse},
            )
            await db.execute(
                text(
                    "INSERT INTO usage_log (user_id, job_id, agent, model, prompt_tokens, "
                    "completion_tokens, cost_source, created_at) VALUES (1, :job, 'parse', "
                    "'vendor/m', 10, 5, 'unknown', now())"
                ),
                {"job": parse},
            )
            assert not await _indexes(db) & RETENTION_INDEXES
        _alembic(scratch_url, "upgrade", PARSE_RETENTION)
        async with engine.begin() as db:
            assert await _indexes(db) >= RETENTION_INDEXES
            # The retention deletes the parse; what it cost stays, in the period and in
            # the record.
            await db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": parse})
            entries = (
                await db.execute(text("SELECT kind, state, amount, job_id FROM credit_entries"))
            ).all()
            assert entries == [("parse", "consumed", 3, None)]
            spent = (await db.execute(text("SELECT spent FROM credit_periods"))).scalar()
            assert spent == 3
            usage = (await db.execute(text("SELECT job_id, prompt_tokens FROM usage_log"))).all()
            assert usage == [(None, 10)]
        async with engine.connect() as db:
            # A chat entry still never names a job.
            with pytest.raises(IntegrityError):
                await db.execute(
                    text(
                        "INSERT INTO credit_entries (user_id, period_start, kind, state, "
                        "amount, job_id, created_at) VALUES (1, '2026-10-01+00', 'chat', "
                        "'reserved', 1, :job, now())"
                    ),
                    {"job": other},
                )
        with pytest.raises(subprocess.CalledProcessError) as refused:
            _alembic(scratch_url, "downgrade", OPERATOR_UPSTREAM)
        assert b"1 parse credit entries have no job" in refused.value.stderr
        async with engine.begin() as db:
            version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            assert version == PARSE_RETENTION
            await db.execute(text("DELETE FROM credit_entries"))
        _alembic(scratch_url, "downgrade", OPERATOR_UPSTREAM)
        async with engine.begin() as db:
            assert not await _indexes(db) & RETENTION_INDEXES
            # The cascade is back: a parse entry goes with its job again.
            await db.execute(
                text(
                    "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, "
                    "job_id, created_at) VALUES (1, '2026-10-01+00', 'parse', 'reserved', 1, "
                    ":job, now())"
                ),
                {"job": other},
            )
            await db.execute(text("DELETE FROM jobs WHERE id = :id"), {"id": other})
            assert await _count(db, "credit_entries") == 0
    finally:
        await engine.dispose()


LIBRARY_FILES = "da765b0142af"


async def test_the_library_files_migration_tells_the_kinds_apart_and_guards_the_files(
    scratch_url,
) -> None:
    """docs/database.md § Migrations: every row keeps what it was — a parse, or a markdown
    document — under `kind`, the new columns empty, the parse agent's switch on; the
    downgrade refuses while a file is kept in the library, since without `kind` its row
    would read as a markdown document with no text anywhere."""
    _alembic(scratch_url, "upgrade", PARSE_RETENTION)
    engine = create_async_engine(scratch_url)
    parse = uuid.uuid4()
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, created_at) "
                    "VALUES (1, 'sub', 'r@example.com', now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                    "size_bytes, sha256, pages_spec, model, pipeline_version, bbox_format, "
                    "status, pages_done, prompt_sha256, created_at, finished_at) VALUES "
                    "(:id, 1, 'parse', 'pdf', 'a.pdf', 'application/pdf', 1, 'h', '', "
                    "'vendor/m', 1, 'yxyx_norm1000', 'succeeded', 1, '', now(), now())"
                ),
                {"id": parse},
            )
            await db.execute(
                text(
                    "INSERT INTO documents (id, user_id, folder_id, job_id, name, created_at, "
                    "updated_at) VALUES (1, 1, NULL, :job, 'paper.pdf', now(), now()), "
                    "(2, 1, NULL, NULL, 'notes.md', now(), now())"
                ),
                {"job": parse},
            )
            await db.execute(
                text("INSERT INTO agent_settings (user_id, agent, model) VALUES (1, 'parse', NULL)")
            )
        _alembic(scratch_url, "upgrade", LIBRARY_FILES)
        async with engine.begin() as db:
            rows = (
                await db.execute(
                    text("SELECT id, kind, media_type, size_bytes FROM documents ORDER BY id")
                )
            ).all()
            assert rows == [(1, "parse", None, None), (2, "markdown", None, None)]
            assert (await db.execute(text("SELECT auto_convert FROM agent_settings"))).scalar()
            # A file names its type and size; a row that says nothing is a parse.
            await db.execute(
                text(
                    "INSERT INTO documents (id, user_id, folder_id, job_id, name, kind, "
                    "media_type, size_bytes, created_at, updated_at) VALUES (3, 1, NULL, NULL, "
                    "'clip.mov', 'file', 'video/quicktime', 3000000000, now(), now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO documents (id, user_id, folder_id, job_id, name, created_at, "
                    "updated_at) VALUES (4, 1, NULL, :job, 'b.pdf', now(), now())"
                ),
                {"job": parse},
            )
            kinds = (await db.execute(text("SELECT id, kind FROM documents ORDER BY id"))).all()
            assert kinds == [(1, "parse"), (2, "markdown"), (3, "file"), (4, "parse")]
        with pytest.raises(subprocess.CalledProcessError) as refused:
            _alembic(scratch_url, "downgrade", PARSE_RETENTION)
        assert b"1 file(s) are kept in the library" in refused.value.stderr
        async with engine.begin() as db:
            version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            assert version == LIBRARY_FILES
            await db.execute(text("DELETE FROM documents WHERE id = 3"))
        _alembic(scratch_url, "downgrade", PARSE_RETENTION)
        async with engine.begin() as db:
            columns = {
                (table, column)
                for table, column in (
                    await db.execute(
                        text(
                            "SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_name IN ('documents', 'agent_settings')"
                        )
                    )
                ).all()
            }
            assert not columns & {
                ("documents", "kind"),
                ("documents", "media_type"),
                ("documents", "size_bytes"),
                ("agent_settings", "auto_convert"),
            }
            assert (await db.execute(text("SELECT count(*) FROM documents"))).scalar() == 3
    finally:
        await engine.dispose()


READER_VIEWS = "283c259880d3"


async def test_the_reader_views_migration_turns_converting_off_and_adds_the_views(
    scratch_url,
) -> None:
    """The switch everyone had on by default goes off for everyone; the two view columns
    arrive on their defaults; the downgrade drops them and leaves the switch off."""
    _alembic(scratch_url, "upgrade", LIBRARY_FILES)
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, created_at) "
                    "VALUES (1, 'sub', 'r@example.com', now()), (2, 'sub2', 's@example.com', now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO agent_settings (user_id, agent, model, auto_convert) VALUES "
                    "(1, 'parse', NULL, true), (2, 'parse', NULL, false), (1, 'chat', NULL, true)"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO documents (id, user_id, folder_id, job_id, name, kind, "
                    "created_at, updated_at) VALUES (1, 1, NULL, NULL, 'notes.md', 'markdown', "
                    "now(), now())"
                )
            )
        _alembic(scratch_url, "upgrade", READER_VIEWS)
        async with engine.begin() as db:
            switches = (
                await db.execute(
                    text("SELECT user_id, agent, auto_convert FROM agent_settings ORDER BY 1, 2")
                )
            ).all()
            assert switches == [(1, "chat", False), (1, "parse", False), (2, "parse", False)]
            views = (await db.execute(text("SELECT id, default_view FROM users ORDER BY id"))).all()
            assert views == [(1, "markdown"), (2, "markdown")]
            assert (await db.execute(text("SELECT view FROM documents"))).scalar() is None
            # A row written without the switch is off, as the model would write it.
            await db.execute(
                text("INSERT INTO agent_settings (user_id, agent, model) VALUES (2, 'chat', NULL)")
            )
            assert not (
                await db.execute(
                    text(
                        "SELECT auto_convert FROM agent_settings"
                        " WHERE user_id = 2 AND agent = 'chat'"
                    )
                )
            ).scalar()
            await db.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, created_at)"
                    " VALUES (3, 'sub3', 't@example.com', now())"
                )
            )
            assert (
                await db.execute(text("SELECT default_view FROM users WHERE id = 3"))
            ).scalar() == "markdown"
        _alembic(scratch_url, "downgrade", LIBRARY_FILES)
        async with engine.begin() as db:
            columns = {
                (table, column)
                for table, column in (
                    await db.execute(
                        text(
                            "SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_name IN ('documents', 'users')"
                        )
                    )
                ).all()
            }
            assert not columns & {("documents", "view"), ("users", "default_view")}
            # The rows stay as the upgrade left them; only the default comes back.
            assert not (
                await db.execute(text("SELECT bool_or(auto_convert) FROM agent_settings"))
            ).scalar()
            await db.execute(
                text("INSERT INTO agent_settings (user_id, agent, model) VALUES (3, 'parse', NULL)")
            )
            assert (
                await db.execute(text("SELECT auto_convert FROM agent_settings WHERE user_id = 3"))
            ).scalar()
    finally:
        await engine.dispose()


CHAT_TOOL_CALLS = "8d1142bbd272"
COMPACTION = "d78cf7a42eea"


async def test_the_compaction_migration_leaves_every_row_as_a_message_turn(scratch_url) -> None:
    """Every turn that came before is a message turn, every reader has idle compaction on,
    every conversation has no summary, no context and no clock; the downgrade drops the
    columns and the partial index and keeps the rows (docs/database.md)."""
    _alembic(scratch_url, "upgrade", CHAT_TOOL_CALLS)
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO users (id, google_sub, email, created_at) "
                    "VALUES (1, 'sub', 'r@example.com', now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO agent_settings (user_id, agent, model) VALUES "
                    "(1, 'chat', 'm'), (1, 'parse', NULL)"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO documents (id, user_id, folder_id, job_id, name, kind, "
                    "created_at, updated_at) VALUES (1, 1, NULL, NULL, 'notes.md', 'markdown', "
                    "now(), now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO conversations (id, user_id, document_id, title, session, "
                    "model, created_at, updated_at) VALUES (1, 1, 1, 'Thread', 's', 'm', "
                    "now(), now())"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO chat_turns (id, user_id, conversation_id, message_id, status, "
                    "locale, created_at, finished_at) VALUES "
                    "('22222222-2222-2222-2222-222222222222', 1, 1, 1, 'succeeded', 'en', "
                    "now(), now())"
                )
            )
            # Skills named after the composer's commands, and one whose name the rename
            # would otherwise take.
            await db.execute(
                text(
                    "INSERT INTO skills (user_id, name, description, argument_hint, body, "
                    "created_at, updated_at) VALUES "
                    "(1, 'compact', '', '', 'x', now(), now()), "
                    "(1, 'compact-2', '', '', 'y', now(), now()), "
                    "(1, 'new', '', '', 'z', now(), now()), "
                    "(1, 'mine', '', '', 'w', now(), now())"
                )
            )
        _alembic(scratch_url, "upgrade", COMPACTION)
        async with engine.begin() as db:
            assert (await db.execute(text("SELECT kind FROM chat_turns"))).scalar() == "message"
            names = (await db.execute(text("SELECT name, body FROM skills ORDER BY body"))).all()
            assert names == [("mine", "w"), ("compact-3", "x"), ("compact-2", "y"), ("new-2", "z")]
            switches = (
                await db.execute(text("SELECT agent, idle_compact FROM agent_settings ORDER BY 1"))
            ).all()
            assert switches == [("chat", True), ("parse", True)]
            assert (
                await db.execute(
                    text(
                        "SELECT memory_file, compacted_message_id, context_tokens, "
                        "cache_warm_until FROM conversations"
                    )
                )
            ).one() == (None, None, None, None)
            # A turn written without a kind is a message turn, as the model would write it.
            await db.execute(
                text(
                    "INSERT INTO chat_turns (id, user_id, conversation_id, message_id, status, "
                    "locale, created_at) VALUES "
                    "('33333333-3333-3333-3333-333333333333', 1, 1, 2, 'queued', 'en', now())"
                )
            )
            assert (
                await db.execute(
                    text(
                        "SELECT kind FROM chat_turns "
                        "WHERE id = '33333333-3333-3333-3333-333333333333'"
                    )
                )
            ).scalar() == "message"
            index = (
                await db.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE indexname = 'ix_conversations_cache_warm_until'"
                    )
                )
            ).scalar()
            assert index is not None and "cache_warm_until IS NOT NULL" in index
            await db.execute(
                text(
                    "UPDATE conversations SET compacted_message_id = 7, context_tokens = 120000, "
                    "cache_warm_until = now(), memory_file = 'notes' WHERE id = 1"
                )
            )
        _alembic(scratch_url, "downgrade", CHAT_TOOL_CALLS)
        async with engine.begin() as db:
            columns = {
                (table, column)
                for table, column in (
                    await db.execute(
                        text(
                            "SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_name IN ('conversations', 'chat_turns', 'agent_settings')"
                        )
                    )
                ).all()
            }
            assert not columns & {
                ("conversations", "memory_file"),
                ("conversations", "compacted_message_id"),
                ("conversations", "context_tokens"),
                ("conversations", "cache_warm_until"),
                ("chat_turns", "kind"),
                ("agent_settings", "idle_compact"),
            }
            assert (
                await db.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes "
                        "WHERE indexname = 'ix_conversations_cache_warm_until'"
                    )
                )
            ).scalar() == 0
            assert await _count(db, "chat_turns") == 2
            assert await _count(db, "conversations") == 1
    finally:
        await engine.dispose()
