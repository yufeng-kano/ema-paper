"""The deploy script's two queries, run as written in it on PostgreSQL.

`deploy/deploy.sh` counts the work a swap would interrupt or hold up, and finds the worker
by the worker lock it holds, read from `pg_locks` (docs/deployment.md § Release flow). Both
are SQL inside a shell heredoc, which no type check reaches, so they are run here as the
script spells them: a renamed column or a changed `WORKER_LOCK_KEY` fails this test rather
than a deploy (docs/testing.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sightread.db.models import ChatTurn, Conversation, Document, Job, User
from sightread.jobs.queue import release_worker_lock, try_worker_lock
from tests.conftest import DATABASE_URL

postgres_only = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="needs TEST_DATABASE_URL pointing at PostgreSQL",
)

DEPLOY_SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "deploy.sh"


def _heredoc(delimiter: str) -> str:
    """The heredoc between its `<<'DELIMITER'` line and its closing delimiter."""
    body = DEPLOY_SCRIPT.read_text().split(f"<<'{delimiter}'\n", 1)[1]
    return body.split(f"\n{delimiter}\n", 1)[0]


@postgres_only
async def test_the_deploy_script_counts_running_work_and_a_database_dump(sessionmaker) -> None:
    query = text(_heredoc("IN_FLIGHT_SQL"))
    engine = sessionmaker.kw["bind"]
    async with engine.connect() as connection:
        assert tuple((await connection.execute(query)).one()) == (0, 0, 0)

    async with sessionmaker() as db:
        user = User(google_sub="deploy-user", email="deploy@example.test")
        db.add(user)
        await db.flush()
        document = Document(user_id=user.id, name="d.md")
        db.add(document)
        await db.flush()
        conversation = Conversation(user_id=user.id, document_id=document.id, session="s")
        db.add(conversation)
        await db.flush()
        for status in ("running", "queued"):
            db.add(
                Job(
                    user_id=user.id,
                    agent="parse",
                    kind="image",
                    filename="a.png",
                    media_type="image/png",
                    size_bytes=1,
                    sha256=status,
                    status=status,
                    model="m",
                    bbox_format="yxyx_norm1000",
                )
            )
        # A cancelled turn the worker has not wound down yet still runs; a queued one never
        # started, and the swap interrupts nothing of it.
        for message_id, status in enumerate(("running", "cancelled", "queued"), start=1):
            db.add(
                ChatTurn(
                    user_id=user.id,
                    conversation_id=conversation.id,
                    message_id=message_id,
                    status=status,
                )
            )
        await db.commit()
    async with engine.connect() as connection:
        assert tuple((await connection.execute(query)).one()) == (1, 2, 0)

    # A session as pg_dump opens it, by its default application name, in this database:
    # the daily backup pass's dump, whose locks would hold a migration back.
    dumping = create_async_engine(
        engine.url, connect_args={"server_settings": {"application_name": "pg_dump"}}
    )
    try:
        async with dumping.connect() as dump, engine.connect() as connection:
            await dump.execute(text("SELECT 1"))
            assert tuple((await connection.execute(query)).one()) == (1, 2, 1)
    finally:
        await dumping.dispose()
    async with engine.connect() as connection:
        assert tuple((await connection.execute(query)).one()) == (1, 2, 0)


@postgres_only
async def test_the_deploy_script_sees_the_worker_lock_while_a_worker_holds_it(
    sessionmaker,
) -> None:
    query = _heredoc("WORKER_LOCK_SQL")

    engine = sessionmaker.kw["bind"]
    async with engine.connect() as connection:
        assert (await connection.execute(text(query))).scalar_one() == 0
    lock = await try_worker_lock(engine)
    assert lock is not None
    try:
        # Another session reads it, as psql in the pg container does.
        async with engine.connect() as connection:
            assert (await connection.execute(text(query))).scalar_one() == 1
    finally:
        await release_worker_lock(lock)
    async with engine.connect() as connection:
        assert (await connection.execute(text(query))).scalar_one() == 0
