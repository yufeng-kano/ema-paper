"""Engine and request-scoped session plumbing.

The engine is created without connecting, so the app imports and starts (and `/healthz`
answers) even when PostgreSQL is not reachable yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    # Pin the PostgreSQL session to UTC so date bucketing in usage aggregates is UTC
    # regardless of the server's configured TimeZone.
    connect_args = (
        {"server_settings": {"timezone": "UTC"}}
        if database_url.startswith("postgresql+asyncpg")
        else {}
    )
    # A failed statement's log line never carries its bound values: they are the reader's
    # text — a memory, instructions, a message — and that is never logged (docs/product.md).
    return create_async_engine(
        database_url, pool_pre_ping=True, hide_parameters=True, connect_args=connect_args
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to the app's sessionmaker."""
    async with request.app.state.sessionmaker() as session:
        yield session
