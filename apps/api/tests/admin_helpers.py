"""What the operator console's tests share: an owner and other accounts signed in on clients
of their own, groups and their members made through the console, and reads of the audit
log and of an error answer."""

from __future__ import annotations

import uuid

from httpx import AsyncClient, Response
from sqlalchemy import func, select

from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.db.models import AuditLog, User
from tests.conftest import CSRF_HEADERS, sign_in

# The dev login signs in as dev@localhost, counted as verified (docs/auth.md § 1).
OWNERS = "dev@localhost"


async def owner_client(make_client, **settings) -> AsyncClient:
    """The dev login on a console whose OWNER_EMAILS lists it: an owner."""
    client = make_client(**{"owner_emails": OWNERS, **settings})
    return await sign_in(client)


async def account_client(
    make_client, email: str, name: str | None = None, **settings
) -> AsyncClient:
    """Another account, signed in on its own client, in no group."""
    client = make_client(**{"owner_emails": OWNERS, **settings})
    async with client.app.state.sessionmaker() as db:
        user = User(google_sub=f"sub-{uuid.uuid4().hex}", email=email, name=name)
        db.add(user)
        await db.flush()
        token = await create_session(db, user)
        await db.commit()
        client.user_id = user.id
    client.cookies.set(SESSION_COOKIE, token)
    return client


async def create_group(owner: AsyncClient, name: str, permissions: list[str]) -> dict:
    created = await owner.post(
        "/api/admin/groups", json={"name": name, "permissions": permissions}, headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    return created.json()


async def add_member(owner: AsyncClient, group_id: int, **body) -> Response:
    return await owner.post(
        f"/api/admin/groups/{group_id}/members", json=body, headers=CSRF_HEADERS
    )


async def audit_rows(client: AsyncClient, after_id: int = 0) -> list[AuditLog]:
    async with client.app.state.sessionmaker() as db:
        return list(
            await db.scalars(select(AuditLog).where(AuditLog.id > after_id).order_by(AuditLog.id))
        )


async def last_audit_id(client: AsyncClient) -> int:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(select(func.coalesce(func.max(AuditLog.id), 0)))


def error_of(response: Response) -> dict:
    return response.json()["error"]
