"""The deploy script's worker check against the lock a worker really takes.

`deploy/deploy.sh` finds the worker by the worker lock it holds, read from `pg_locks`
(docs/deployment.md § Release flow). Its query spells `WORKER_LOCK_KEY` out, so it is run
here as written in the script: a changed key fails this test rather than a deploy's health
check (docs/testing.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from sightread.jobs.queue import release_worker_lock, try_worker_lock
from tests.conftest import DATABASE_URL

postgres_only = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="needs TEST_DATABASE_URL pointing at PostgreSQL",
)

DEPLOY_SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "deploy.sh"


@postgres_only
async def test_the_deploy_script_sees_the_worker_lock_while_a_worker_holds_it(
    sessionmaker,
) -> None:
    # The heredoc between its `<<'WORKER_LOCK_SQL'` line and its closing delimiter.
    script = DEPLOY_SCRIPT.read_text()
    body = script.split("<<'WORKER_LOCK_SQL'\n", 1)[1]
    query = body.split("\nWORKER_LOCK_SQL\n", 1)[0]

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
