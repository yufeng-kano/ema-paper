"""The app's database engine and sessions (sightread.db.session)."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql
from sqlalchemy.exc import DBAPIError

from sightread.db.models import User
from sightread.db.session import commit_or_conflict
from sightread.errors import ApiError
from tests.conftest import base_settings
from tests.factories import user_row


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///sightread.db",
        "postgresql+psycopg://sightread:a-password-never-shown@pg:5432/sightread",
        "postgresql://sightread:a-password-never-shown@pg:5432/sightread",
    ],
)
def test_a_database_other_than_postgresql_through_asyncpg_is_refused_at_start(url: str) -> None:
    """One line at start, not a driver error at the first connection, and never the URL
    itself: it carries the password (docs/deployment.md)."""
    with pytest.raises(ValueError, match="postgresql\\+asyncpg://") as refused:
        base_settings(database_url=url)
    assert "a-password-never-shown" not in str(refused.value)
    assert "sightread.db" not in str(refused.value)


async def test_a_failed_statement_never_carries_the_readers_text(sessionmaker) -> None:
    """The engine hides bound values, so a database error logged with its traceback cannot
    write a memory's text — or instructions, or a message — into the log. The fixture's
    engine is the app's own (`create_engine`)."""
    async with sessionmaker.kw["bind"].connect() as connection:
        with pytest.raises(DBAPIError) as failed:
            await connection.execute(
                sql("insert into no_such_table (text) values (:text)"),
                {"text": "the reader reads with a screen reader"},
            )
    assert "screen reader" not in str(failed.value)


async def test_a_commit_a_constraint_refuses_is_rolled_back_and_answered_409(
    sessionmaker,
) -> None:
    """`commit_or_conflict`: the loser of a race on a unique value rolls back and is answered
    409 with the caller's message, and its session stays usable."""
    async with sessionmaker() as first:
        first.add(user_row("taken@example.com"))
        await first.commit()
    async with sessionmaker() as db:
        db.add(user_row("taken@example.com"))
        with pytest.raises(ApiError) as refused:
            await commit_or_conflict(db, "That account was just made elsewhere")
        assert (refused.value.status_code, refused.value.error_type, refused.value.message) == (
            409,
            "invalid_request",
            "That account was just made elsewhere",
        )
        db.add(user_row("free@example.com"))
        await commit_or_conflict(db, "unused")
    async with sessionmaker() as db:
        emails = (await db.execute(select(User.email).order_by(User.email))).scalars().all()
    assert emails == ["free@example.com", "taken@example.com"]
