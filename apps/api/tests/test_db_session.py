"""The app's database engine (sightread.db.session)."""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql
from sqlalchemy.exc import DBAPIError

from sightread.db.session import create_engine


async def test_a_failed_statement_never_carries_the_readers_text(tmp_path) -> None:
    """The engine hides bound values, so a database error logged with its traceback cannot
    write a memory's text — or instructions, or a message — into the log."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'hidden.db'}")
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as failed:
                await connection.execute(
                    sql("insert into no_such_table (text) values (:text)"),
                    {"text": "the reader reads with a screen reader"},
                )
    finally:
        await engine.dispose()
    assert "screen reader" not in str(failed.value)
