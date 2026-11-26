"""The migration chain, run as real upgrades (docs/database.md § Migrations, docs/testing.md
§ Backend). PostgreSQL only — the chain is PostgreSQL DDL — so this module skips unless
`TEST_DATABASE_URL` points at one (tests/conftest.py). Each test gets its own scratch
databases on that server, dropped afterwards.

A migration that rewrites existing rows adds its upgrade test here: upgrade a scratch
database to the revision before it, write synthetic existing rows, upgrade, and check what
it left.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from sightread.db.models import Base
from tests.conftest import TEST_DATABASE_URL, postgres_only

pytestmark = postgres_only

API_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_TIMEOUT_SECONDS = 300


def _revision_module(revision: str):
    """A revision file loaded as a module, for the data it writes out — the constants a
    migration freezes, which the app's own may since have moved past."""
    (path,) = (API_DIR / "migrations" / "versions").glob(f"{revision}_*.py")
    spec = importlib.util.spec_from_file_location(f"revision_{revision}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alembic(url: str, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv: this interpreter running alembic
        [sys.executable, "-m", "alembic", *args],
        cwd=API_DIR,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
        timeout=ALEMBIC_TIMEOUT_SECONDS,
    )


@asynccontextmanager
async def _scratch_database():
    server = make_url(TEST_DATABASE_URL)
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


@pytest_asyncio.fixture
async def scratch_url():
    async with _scratch_database() as url:
        yield url


@pytest_asyncio.fixture
async def head_engine(scratch_url):
    """An engine on a scratch database upgraded to head."""
    _alembic(scratch_url, "upgrade", "head")
    engine = create_async_engine(scratch_url)
    yield engine
    await engine.dispose()


async def _count(connection, table: str) -> int:
    """Rows of a table this module names or the catalog lists — never a value from outside."""
    return (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608


# The catalog a schema is compared by: every column with its type, nullability and
# default; every constraint by name with its definition (PK, UNIQUE, FK with its actions,
# CHECK); every index with its definition, partial predicates included; the sequences; and
# any enum or domain type. Alembic's own table is left out.
CATALOG = {
    "columns": (
        "SELECT table_name, column_name, data_type, character_maximum_length, "
        "numeric_precision, numeric_scale, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
    ),
    "constraints": (
        "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
        "AND conrelid::regclass::text <> 'alembic_version'"
    ),
    "indexes": (
        "SELECT tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
    ),
    "sequences": (
        "SELECT sequencename, data_type, start_value, min_value, max_value, increment_by, "
        "cycle FROM pg_sequences WHERE schemaname = 'public'"
    ),
    "types": (
        "SELECT t.typname, t.typtype FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd')"
    ),
}


async def _catalog(engine: AsyncEngine) -> dict[str, set[tuple]]:
    async with engine.connect() as connection:
        return {
            part: {tuple(row) for row in await connection.execute(text(query))}
            for part, query in CATALOG.items()
        }


async def test_the_upgraded_head_is_the_schema_the_models_build(head_engine) -> None:
    """A database the chain built and one `create_all` built — the one every other test
    runs on — carry the same catalog, names included, compared both ways."""
    async with _scratch_database() as models_url:
        models_engine = create_async_engine(models_url)
        try:
            async with models_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            built = await _catalog(models_engine)
        finally:
            await models_engine.dispose()
    migrated = await _catalog(head_engine)
    for part in CATALOG:
        assert migrated[part] - built[part] == set(), f"{part} only the migrations create"
        assert built[part] - migrated[part] == set(), f"{part} only the models create"
    assert ("worker_generation_seq",) in {row[:1] for row in migrated["sequences"]}


async def test_a_fresh_database_holds_the_built_in_group_and_nothing_else(head_engine) -> None:
    async with head_engine.connect() as connection:
        groups = (
            await connection.execute(
                text("SELECT name, permissions = '[]'::jsonb, builtin FROM groups")
            )
        ).all()
        assert groups == [("owner", True, True)]
        tables = (
            await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename NOT IN ('groups', 'alembic_version')"
                )
            )
        ).scalars()
        for table in tables:
            assert await _count(connection, table) == 0, table


async def test_the_downgrade_to_base_leaves_nothing(scratch_url) -> None:
    _alembic(scratch_url, "upgrade", "head")
    _alembic(scratch_url, "downgrade", "base")
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as connection:
            relations = (
                await connection.execute(
                    text(
                        "SELECT c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public'"
                    )
                )
            ).scalars()
            assert set(relations) <= {"alembic_version", "alembic_version_pkc"}
            types = (
                await connection.execute(
                    text(
                        "SELECT t.typname FROM pg_type t "
                        "JOIN pg_namespace n ON n.oid = t.typnamespace "
                        "WHERE n.nspname = 'public'"
                    )
                )
            ).scalars()
            assert set(types) <= {"alembic_version", "_alembic_version"}
            assert await _count(connection, "alembic_version") == 0
    finally:
        await engine.dispose()


async def _refused(engine: AsyncEngine, sql: str, **params) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as db:
            await db.execute(text(sql), params)


async def _user(db, n: int) -> int:
    return (
        await db.execute(
            text(
                "INSERT INTO users (google_sub, email, created_at) "
                "VALUES (:sub, :email, now()) RETURNING id"
            ),
            {"sub": f"sub-{n}", "email": f"u{n}@example.com"},
        )
    ).scalar_one()


async def _job(db, user_id: int) -> uuid.UUID:
    job_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, size_bytes, "
            "sha256, pages_spec, model, pipeline_version, bbox_format, prompt, status, "
            "pages_done, created_at) VALUES (:id, :user, 'parse', 'pdf', 'a.pdf', "
            "'application/pdf', 1, 'x', '', 'm', 0, 'yxyx_norm1000', 'p', 'succeeded', 1, now())"
        ),
        {"id": job_id, "user": user_id},
    )
    return job_id


async def test_the_head_refuses_what_its_constraints_forbid(head_engine) -> None:
    """The constraints production relies on hold on a migrated head: the console's
    groups, the ledger's kinds, a pack's source, live names, a memory's kind, the
    foreign keys' actions (docs/database.md § Tables, § Rules)."""
    engine = head_engine
    async with engine.begin() as db:
        owner = await _user(db, 1)
        other = await _user(db, 2)
        group_id = (await db.execute(text("SELECT id FROM groups WHERE builtin"))).scalar_one()
        await db.execute(
            text(
                "INSERT INTO group_members (group_id, user_id, added_by, added_at) "
                "VALUES (:group, :owner, :owner, now()), (:group, :other, :owner, now())"
            ),
            {"group": group_id, "owner": owner, "other": other},
        )
        await db.execute(
            text(
                "INSERT INTO audit_log (actor_id, permission, action, target_type, target_id, "
                "created_at) VALUES (:owner, 'groups.write', 'groups.add_member', 'user', "
                ":other, now())"
            ),
            {"owner": owner, "other": str(other)},
        )
        await db.execute(
            text(
                "INSERT INTO runtime_settings (key, value, updated_by, updated_at) "
                "VALUES ('CREDIT_USD', '\"0.002\"'::jsonb, :owner, now())"
            ),
            {"owner": owner},
        )
        job_id = await _job(db, other)
        conversation_id = (
            await db.execute(
                text(
                    "INSERT INTO conversations (user_id, title, session, created_at, "
                    "updated_at) VALUES (:user, 't', 's', now(), now()) RETURNING id"
                ),
                {"user": other},
            )
        ).scalar_one()
        turn_id = uuid.uuid4()
        await db.execute(
            text(
                "INSERT INTO chat_turns (id, user_id, conversation_id, message_id, status, "
                "created_at) VALUES (:id, :user, :conversation, 1, 'succeeded', now())"
            ),
            {"id": turn_id, "user": other, "conversation": conversation_id},
        )
        await db.execute(
            text(
                "INSERT INTO credit_periods (user_id, period_start, period_end, granted, "
                "created_at, updated_at) VALUES (:user, '2026-11-01+00', '2026-12-01+00', "
                "150, now(), now())"
            ),
            {"user": other},
        )
        await db.execute(
            text(
                "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, "
                "job_id, turn_id, created_at) VALUES "
                "(:user, '2026-11-01+00', 'parse', 'consumed', 3, :job, NULL, now()), "
                "(:user, '2026-11-01+00', 'chat', 'consumed', 2, NULL, :turn, now())"
            ),
            {"user": other, "job": job_id, "turn": turn_id},
        )

    # One built-in group, and a group name once.
    await _refused(
        engine,
        "INSERT INTO groups (name, permissions, builtin, created_at, updated_at) "
        "VALUES ('second', '[]'::jsonb, true, now(), now())",
    )
    await _refused(
        engine,
        "INSERT INTO groups (name, permissions, builtin, created_at, updated_at) "
        "VALUES ('owner', '[]'::jsonb, false, now(), now())",
    )

    # A chat entry never names a job, a parse entry never a turn.
    entry = (
        "INSERT INTO credit_entries (user_id, period_start, kind, state, amount, job_id, "
        "turn_id, created_at) VALUES (:user, '2026-11-01+00', :kind, 'reserved', 1, :job, "
        ":turn, now())"
    )
    await _refused(engine, entry, user=other, kind="chat", job=job_id, turn=None)
    await _refused(engine, entry, user=other, kind="parse", job=None, turn=turn_id)

    # A pack's source is one of three.
    pack = (
        "INSERT INTO credit_packs (id, user_id, pack_id, credits, status, purchased_at, "
        "expires_at, source, created_at, updated_at) VALUES (:id, :user, 'p', 10, 'paid', "
        "now(), now() + interval '1 day', :source, now(), now())"
    )
    async with engine.begin() as db:
        for source in ("pay", "code", "grant"):
            await db.execute(text(pack), {"id": source, "user": other, "source": source})
    await _refused(engine, pack, id="gift", user=other, source="gift")

    # A memory's kind is one of four.
    await _refused(
        engine,
        "INSERT INTO memories (user_id, kind, text, created_at, updated_at) "
        "VALUES (:user, 'fact', 'x', now(), now())",
        user=other,
    )

    # Names are unique among the live rows of one parent, the root level included; a
    # trashed row holds none.
    folder = (
        "INSERT INTO folders (user_id, parent_id, name, trashed_at, created_at, updated_at) "
        "VALUES (:user, :parent, :name, :trashed, now(), now()) RETURNING id"
    )
    document = (
        "INSERT INTO documents (user_id, folder_id, name, trashed_at, created_at, "
        "updated_at) VALUES (:user, :folder, :name, :trashed, now(), now())"
    )
    async with engine.begin() as db:
        trashed = {"user": other, "trashed": datetime(2026, 11, 1, tzinfo=UTC)}
        live = {"user": other, "trashed": None}
        await db.execute(text(folder), {**trashed, "parent": None, "name": "Papers"})
        papers = (
            await db.execute(text(folder), {**live, "parent": None, "name": "Papers"})
        ).scalar_one()
        await db.execute(text(folder), {**trashed, "parent": papers, "name": "2026"})
        await db.execute(text(folder), {**live, "parent": papers, "name": "2026"})
        for folder_id in (None, papers):
            await db.execute(text(document), {**trashed, "folder": folder_id, "name": "a.md"})
            await db.execute(text(document), {**live, "folder": folder_id, "name": "a.md"})
    await _refused(engine, folder, user=other, parent=None, name="Papers", trashed=None)
    await _refused(engine, folder, user=other, parent=papers, name="2026", trashed=None)
    for folder_id in (None, papers):
        await _refused(engine, document, user=other, folder=folder_id, name="a.md", trashed=None)

    # A re-convert names a real job and lets go of it when the job is deleted.
    await _refused(
        engine,
        "UPDATE documents SET reconvert_job_id = :job WHERE folder_id IS NULL",
        job=uuid.uuid4(),
    )
    async with engine.begin() as db:
        reconvert = await _job(db, other)
        await db.execute(
            text("UPDATE documents SET reconvert_job_id = :job WHERE folder_id IS NULL"),
            {"job": reconvert},
        )
        await db.execute(text("DELETE FROM jobs WHERE id = :job"), {"job": reconvert})
        assert (
            await db.execute(
                text("SELECT count(*) FROM documents WHERE reconvert_job_id IS NOT NULL")
            )
        ).scalar_one() == 0

    # The ledger outlives a deleted job and a deleted conversation's turns.
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM jobs WHERE id = :job"), {"job": job_id})
        await db.execute(text("DELETE FROM conversations WHERE id = :id"), {"id": conversation_id})
        entries = (
            await db.execute(text("SELECT kind, job_id, turn_id FROM credit_entries ORDER BY kind"))
        ).all()
        assert entries == [("chat", None, None), ("parse", None, None)]

    # Deleting an account takes its memberships and ledger; what it did is kept without it.
    async with engine.begin() as db:
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": owner})
        members = (await db.execute(text("SELECT user_id, added_by FROM group_members"))).all()
        assert members == [(other, None)]
        record = (await db.execute(text("SELECT actor_id, target_id FROM audit_log"))).one()
        assert tuple(record) == (None, str(other))
        setting = (await db.execute(text("SELECT updated_by FROM runtime_settings"))).one()
        assert tuple(setting) == (None,)
        await db.execute(text("DELETE FROM users WHERE id = :id"), {"id": other})
        for table in ("group_members", "credit_entries", "credit_periods", "credit_packs"):
            assert await _count(db, table) == 0, table
        assert await _count(db, "groups") == 1


async def _preset(db, user: int, agent: str, name: str, body: str) -> int:
    return (
        await db.execute(
            text(
                "INSERT INTO prompt_presets (user_id, agent, name, text, created_at, updated_at) "
                "VALUES (:user, :agent, :name, :text, now(), now()) RETURNING id"
            ),
            {"user": user, "agent": agent, "name": name, "text": body},
        )
    ).scalar_one()


async def _selected(db, user: int, agent: str, preset: int, **columns: str | None) -> None:
    await db.execute(
        text(
            "INSERT INTO agent_settings (user_id, agent, prompt, prompt_preset_id, instructions) "
            "VALUES (:user, :agent, :prompt, :preset, :instructions)"
        ),
        {
            "user": user,
            "agent": agent,
            "preset": preset,
            "prompt": columns.get("prompt"),
            "instructions": columns.get("instructions"),
        },
    )


async def test_the_presets_in_force_move_into_prompts_and_instructions(scratch_url) -> None:
    """774fee25f32b: a preset still in force moves to where its agent keeps reading it —
    `parse` and `check` into `prompt` verbatim, `chat` to the head of `instructions`,
    uncapped — and nothing else moves; the downgrade restores the table and the column,
    empty."""
    _alembic(scratch_url, "upgrade", "4449f68533c0")
    long_preset = "Answer as {document_path} says.\n" + "x" * 9000
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as db:
            one, two, three, four = [await _user(db, n) for n in range(1, 5)]
            # In force: the row has no prompt of its own.
            parse = await _preset(db, one, "parse", "p", "Tables of page {page}.\n")
            await _selected(db, one, "parse", parse)
            check = await _preset(db, one, "check", "c", "Check it.")
            await _selected(db, one, "check", check, prompt="")
            chat = await _preset(db, one, "chat", "t", "Use {document_path} as is.")
            await _selected(db, one, "chat", chat, instructions="Be brief.")
            long = await _preset(db, two, "chat", "long", long_preset)
            await _selected(db, two, "chat", long)
            terse = await _preset(db, three, "chat", "t", "Terse.")
            await _selected(db, three, "chat", terse, instructions=" ")
            # Not in force: a prompt of its own, a blank chat preset, an unselected preset.
            never = await _preset(db, two, "parse", "p", "Never ran.")
            await _selected(db, two, "parse", never, prompt="Mine {page}.")
            never = await _preset(db, three, "check", "c", "Never ran.")
            await _selected(db, three, "check", never, prompt="Own check.", instructions="Kept.")
            blank = await _preset(db, four, "chat", "blank", " \n ")
            await _selected(db, four, "chat", blank, instructions="Kept as is.")
            await _preset(db, four, "parse", "unselected", "Nowhere.")
    finally:
        await engine.dispose()

    _alembic(scratch_url, "upgrade", "head")
    expected = {
        (one, "parse"): ("Tables of page {page}.\n", None),
        (one, "check"): ("Check it.", None),
        (one, "chat"): (None, "Use {document_path} as is.\n\nBe brief."),
        (two, "chat"): (None, long_preset),
        (three, "chat"): (None, "Terse."),
        (two, "parse"): ("Mine {page}.", None),
        (three, "check"): ("Own check.", "Kept."),
        (four, "chat"): (None, "Kept as is."),
    }
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as db:
            rows = await db.execute(
                text("SELECT user_id, agent, prompt, instructions FROM agent_settings")
            )
            assert {(row[0], row[1]): (row[2], row[3]) for row in rows} == expected
            left = await db.execute(
                text(
                    "SELECT to_regclass('prompt_presets'), count(*) "
                    "FROM information_schema.columns WHERE table_name = 'agent_settings' "
                    "AND column_name = 'prompt_preset_id'"
                )
            )
            assert tuple(left.one()) == (None, 0)
    finally:
        await engine.dispose()

    _alembic(scratch_url, "downgrade", "4449f68533c0")
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as db:
            assert await _count(db, "prompt_presets") == 0
            selected = await db.execute(
                text("SELECT count(*) FROM agent_settings WHERE prompt_preset_id IS NOT NULL")
            )
            assert selected.scalar_one() == 0
            assert await _count(db, "agent_settings") == len(expected)
    finally:
        await engine.dispose()
    _alembic(scratch_url, "upgrade", "head")


async def test_jobs_lose_the_provider_connection_and_always_hold_a_prompt(scratch_url) -> None:
    """185b2581586c: a provider connection's job loses its endpoint snapshot — so it never
    answers a dedup lookup and the worker fails it — and keeps its status, a queued one's
    included, for the worker to fail through its finish; a job with no prompt takes the
    parse agent's default template, verbatim; `connection_id` and `source_job_id` go, and
    the dedup index with the first. The downgrade restores both columns, empty."""
    _alembic(scratch_url, "upgrade", "774fee25f32b")
    jobs = {
        # name: (connection_id, connection_base_url, status, prompt)
        "provider, succeeded": (7, "https://kano.test", "succeeded", "Mine {page}."),
        "provider, queued": (8, "https://llm.example/v1", "queued", None),
        "kano": (None, "https://kano.test", "succeeded", None),
        "openrouter": (None, None, "failed", "Old {page}."),
    }
    ids = {name: uuid.uuid4() for name in jobs}
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as db:
            user = await _user(db, 1)
            for name, (connection_id, base_url, status, prompt) in jobs.items():
                await db.execute(
                    text(
                        "INSERT INTO jobs (id, user_id, agent, kind, filename, media_type, "
                        "size_bytes, sha256, pages_spec, model, pipeline_version, "
                        "bbox_format, connection_id, connection_base_url, prompt, status, "
                        "pages_done, created_at) VALUES (:id, :user, 'parse', 'pdf', 'a.pdf', "
                        "'application/pdf', 1, 'x', '', 'm', 10, 'yxyx_norm1000', "
                        ":connection, :base_url, :prompt, :status, 0, now())"
                    ),
                    {
                        "id": ids[name],
                        "user": user,
                        "connection": connection_id,
                        "base_url": base_url,
                        "prompt": prompt,
                        "status": status,
                    },
                )
    finally:
        await engine.dispose()

    _alembic(scratch_url, "upgrade", "head")
    default = _revision_module("185b2581586c").PARSE_DEFAULT_PROMPT
    expected = {
        "provider, succeeded": (None, "succeeded", "Mine {page}."),
        "provider, queued": (None, "queued", default),
        "kano": ("https://kano.test", "succeeded", default),
        "openrouter": (None, "failed", "Old {page}."),
    }
    gone = (
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'jobs' "
        "AND column_name IN ('connection_id', 'source_job_id')"
    )
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as db:
            rows = await db.execute(
                text("SELECT id, connection_base_url, status, prompt FROM jobs")
            )
            names = {job_id: name for name, job_id in ids.items()}
            assert {names[row[0]]: tuple(row[1:]) for row in rows} == expected
            assert (await db.execute(text(gone))).scalar_one() == 0
            nullable = await db.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'jobs' AND column_name = 'prompt'"
                )
            )
            assert nullable.scalar_one() == "NO"
            index = await db.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_jobs_dedup'")
            )
            assert "connection_id" not in index.scalar_one()
    finally:
        await engine.dispose()

    _alembic(scratch_url, "downgrade", "774fee25f32b")
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as db:
            assert (await db.execute(text(gone))).scalar_one() == 2
            left = await db.execute(
                text(
                    "SELECT count(*) FROM jobs "
                    "WHERE connection_id IS NOT NULL OR source_job_id IS NOT NULL"
                )
            )
            assert left.scalar_one() == 0
            assert await _count(db, "jobs") == len(jobs)
    finally:
        await engine.dispose()
    _alembic(scratch_url, "upgrade", "head")


async def test_the_upgrade_revokes_every_grant_without_the_library(scratch_url) -> None:
    """d10760d56782: a grant whose scope lacks `library` is revoked, and an unspent ticket
    minted under one deleted, so nothing reaches the library it was not granted; a spent
    ticket keeps reading its job, and the downgrade gives every ticket left the library
    (docs/auth.md § 4, § 5)."""
    _alembic(scratch_url, "upgrade", "185b2581586c")
    engine = create_async_engine(scratch_url)
    revoked_before = datetime(2026, 11, 2, tzinfo=UTC)
    grant = (
        "INSERT INTO oauth_grants (client_id, user_id, kind, token_hash, scope, expires_at, "
        "revoked_at, created_at) VALUES ('c', :user, :kind, :hash, :scope, "
        "now() + interval '1 day', :revoked, now())"
    )
    ticket = (
        "INSERT INTO upload_tickets (user_id, token_hash, prefix, job_id, created_at, "
        "expires_at, spent_at, library_access) VALUES (:user, :hash, 'srt_...x', :job, "
        "now(), now() + interval '1 hour', :spent, :library)"
    )
    try:
        async with engine.begin() as db:
            user = await _user(db, 1)
            job_id = await _job(db, user)
            await db.execute(
                text(
                    "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, "
                    "created_at) VALUES ('c', 'Claude', '[]'::jsonb, now())"
                )
            )
            for kind, scope, revoked in (
                ("access", "parse", None),
                ("refresh", "parse", None),
                ("code", "", None),
                ("refresh", "parse", revoked_before),
                ("access", "parse library", None),
                ("refresh", "parse library", None),
                ("code", "library parse", None),
            ):
                await db.execute(
                    text(grant),
                    {
                        "user": user,
                        "kind": kind,
                        "hash": f"{kind}-{scope}-{revoked}",
                        "scope": scope,
                        "revoked": revoked,
                    },
                )
            for name, job, spent, library in (
                ("unspent-parse-only", None, None, False),
                ("spent-parse-only", job_id, revoked_before, False),
                ("unspent-library", None, None, True),
            ):
                await db.execute(
                    text(ticket),
                    {"user": user, "hash": name, "job": job, "spent": spent, "library": library},
                )

        _alembic(scratch_url, "upgrade", "d10760d56782")
        async with engine.connect() as db:
            grants = {
                (kind, scope, revoked_at is None, revoked_at == revoked_before)
                for kind, scope, revoked_at in await db.execute(
                    text("SELECT kind, scope, revoked_at FROM oauth_grants")
                )
            }
            tickets = set(
                (await db.execute(text("SELECT token_hash FROM upload_tickets"))).scalars()
            )
        assert grants == {
            ("access", "parse", False, False),
            ("refresh", "parse", False, False),
            ("code", "", False, False),
            ("refresh", "parse", False, True),
            ("access", "parse library", True, False),
            ("refresh", "parse library", True, False),
            ("code", "library parse", True, False),
        }
        assert tickets == {"spent-parse-only", "unspent-library"}

        _alembic(scratch_url, "downgrade", "-1")
        async with engine.connect() as db:
            restored = (
                await db.execute(text("SELECT token_hash, library_access FROM upload_tickets"))
            ).all()
        assert set(restored) == {("spent-parse-only", True), ("unspent-library", True)}
    finally:
        await engine.dispose()


async def test_the_upgrade_revokes_every_grant_that_cannot_organize(scratch_url) -> None:
    """697e710bbdda: a live grant whose scope lacks `organize` is revoked — its connector
    goes through the consent page again, which says it can move and trash — and a grant
    already holding it, or already revoked, is left as it was (docs/auth.md § 4)."""
    _alembic(scratch_url, "upgrade", "49f93a27d36f")
    engine = create_async_engine(scratch_url)
    revoked_before = datetime(2026, 11, 2, tzinfo=UTC)
    grant = (
        "INSERT INTO oauth_grants (client_id, user_id, kind, token_hash, scope, expires_at, "
        "revoked_at, created_at) VALUES ('c', :user, :kind, :hash, :scope, "
        "now() + interval '1 day', :revoked, now())"
    )
    try:
        async with engine.begin() as db:
            user = await _user(db, 1)
            await db.execute(
                text(
                    "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, "
                    "created_at) VALUES ('c', 'Claude', '[]'::jsonb, now())"
                )
            )
            for kind, scope, revoked in (
                ("access", "parse library", None),
                ("refresh", "parse library", None),
                ("code", "library parse", None),
                ("refresh", "parse library", revoked_before),
                ("access", "parse library organize", None),
                ("refresh", "organize parse library", None),
                ("code", "parse library organizer", None),
            ):
                await db.execute(
                    text(grant),
                    {
                        "user": user,
                        "kind": kind,
                        "hash": f"{kind}-{scope}-{revoked}",
                        "scope": scope,
                        "revoked": revoked,
                    },
                )

        _alembic(scratch_url, "upgrade", "697e710bbdda")
        async with engine.connect() as db:
            grants = {
                (kind, scope, revoked_at is None, revoked_at == revoked_before)
                for kind, scope, revoked_at in await db.execute(
                    text("SELECT kind, scope, revoked_at FROM oauth_grants")
                )
            }
        assert grants == {
            ("access", "parse library", False, False),
            ("refresh", "parse library", False, False),
            ("code", "library parse", False, False),
            ("refresh", "parse library", False, True),
            ("access", "parse library organize", True, False),
            ("refresh", "organize parse library", True, False),
            ("code", "parse library organizer", False, False),
        }
    finally:
        await engine.dispose()


async def test_a_code_pack_keeps_its_redemption_through_its_id(scratch_url) -> None:
    """49f93a27d36f: `credit_packs.redemption_id` goes and every pack stays; the downgrade
    gives each code pack `redeem-<n>` whose redemption still exists `redemption_id` n back,
    and every other pack NULL, under the unique and the foreign key it had."""
    _alembic(scratch_url, "upgrade", "8ca4f1915c14")
    pack = (
        "INSERT INTO credit_packs (id, user_id, pack_id, credits, status, purchased_at, "
        "expires_at, source, redemption_id, created_at, updated_at) VALUES (:id, :user, "
        ":pack, 10, 'paid', now(), now() + interval '1 day', :source, :redemption, now(), now())"
    )
    engine = create_async_engine(scratch_url)
    try:
        async with engine.begin() as db:
            user = await _user(db, 1)
            redemptions = {}
            for code in ("KEPT", "GONE"):
                code_id = (
                    await db.execute(
                        text(
                            "INSERT INTO redeem_codes (code, kind, credits, valid_days, "
                            "created_at) VALUES (:code, 'credits', 10, 30, now()) RETURNING id"
                        ),
                        {"code": code},
                    )
                ).scalar_one()
                redemptions[code] = (
                    await db.execute(
                        text(
                            "INSERT INTO redemptions (code_id, user_id, kind, credits, "
                            "redeemed_at) VALUES (:code, :user, 'credits', 10, now()) "
                            "RETURNING id"
                        ),
                        {"code": code_id, "user": user},
                    )
                ).scalar_one()
            for redemption in redemptions.values():
                await db.execute(
                    text(pack),
                    {
                        "id": f"redeem-{redemption}",
                        "user": user,
                        "pack": "code",
                        "source": "code",
                        "redemption": redemption,
                    },
                )
            await db.execute(
                text(pack),
                {"id": "pur_1", "user": user, "pack": "p", "source": "pay", "redemption": None},
            )
            # A code deleted by hand takes its redemption with it; its pack keeps NULL.
            await db.execute(text("DELETE FROM redeem_codes WHERE code = 'GONE'"))
    finally:
        await engine.dispose()

    column = (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'credit_packs' AND column_name = 'redemption_id'"
    )
    _alembic(scratch_url, "upgrade", "head")
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as db:
            assert (await db.execute(text(column))).scalar_one() == 0
            ids = set((await db.execute(text("SELECT id FROM credit_packs"))).scalars())
            assert ids == {f"redeem-{n}" for n in redemptions.values()} | {"pur_1"}
    finally:
        await engine.dispose()

    _alembic(scratch_url, "downgrade", "8ca4f1915c14")
    engine = create_async_engine(scratch_url)
    try:
        async with engine.connect() as db:
            rows = await db.execute(text("SELECT id, redemption_id FROM credit_packs"))
            assert dict(rows.all()) == {
                f"redeem-{redemptions['KEPT']}": redemptions["KEPT"],
                f"redeem-{redemptions['GONE']}": None,
                "pur_1": None,
            }
            constraints = await db.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'credit_packs'::regclass AND conname IN "
                    "('uq_credit_packs_redemption_id', 'fk_credit_packs_redemption_id')"
                )
            )
            assert dict(constraints.all()) == {
                "uq_credit_packs_redemption_id": "UNIQUE (redemption_id)",
                "fk_credit_packs_redemption_id": (
                    "FOREIGN KEY (redemption_id) REFERENCES redemptions(id) ON DELETE SET NULL"
                ),
            }
    finally:
        await engine.dispose()
    _alembic(scratch_url, "upgrade", "head")
