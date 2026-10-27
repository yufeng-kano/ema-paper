"""The operator console's skeleton (docs/admin.md § Acceptance): the console off without
`OWNER_EMAILS`, browser sessions only, permissions from groups, the re-login window, owners
joining at sign-in, groups and members with their no-escalation rules, and the audit log.

Google exists only as a patched Authlib client: no test leaves the process.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy import delete, func, select, text, update

from sightread.admin.deps import require_permission
from sightread.admin.groups import join_owner_group
from sightread.admin.permissions import ALL_PERMISSIONS, AREAS, normalize
from sightread.auth import upload_tickets
from sightread.auth.deps import require_csrf_header
from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.config import Settings
from sightread.db.models import AuditLog, Group, GroupMember, User, UserSession, utcnow
from sightread.routes import admin as admin_routes
from tests.conftest import CSRF_HEADERS, DATABASE_URL
from tests.test_oauth_as import _connected

postgres_only = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="foreign-key actions and row locks need PostgreSQL",
)

# The dev login signs in as dev@localhost, counted as verified (docs/auth.md § 1).
OWNERS = "dev@localhost"


async def _owner(make_client, **settings) -> AsyncClient:
    """The dev login on a console whose OWNER_EMAILS lists it: an owner."""
    client = make_client(**{"owner_emails": OWNERS, **settings})
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    return client


async def _account(make_client, email: str, name: str | None = None, **settings) -> AsyncClient:
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


async def _group(owner: AsyncClient, name: str, permissions: list[str]) -> dict:
    created = await owner.post(
        "/api/admin/groups", json={"name": name, "permissions": permissions}, headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _add(owner: AsyncClient, group_id: int, **body):
    return await owner.post(
        f"/api/admin/groups/{group_id}/members", json=body, headers=CSRF_HEADERS
    )


async def _builtin_id(client: AsyncClient) -> int:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(select(Group.id).where(Group.builtin.is_(True)))


async def _audit(client: AsyncClient, after_id: int = 0) -> list[AuditLog]:
    async with client.app.state.sessionmaker() as db:
        return list(
            await db.scalars(select(AuditLog).where(AuditLog.id > after_id).order_by(AuditLog.id))
        )


async def _last_audit_id(client: AsyncClient) -> int:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(select(func.coalesce(func.max(AuditLog.id), 0)))


async def _age_sessions(client: AsyncClient, user_id: int, hours: float) -> None:
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id)
            .values(created_at=utcnow() - timedelta(hours=hours))
        )
        await db.commit()


def _error(response) -> dict:
    return response.json()["error"]


async def _google_sign_in(client: AsyncClient, monkeypatch, claims: dict):
    """The real callback, with Authlib's token exchange answering `claims` in place of
    Google (nothing leaves the process)."""

    async def exchange(request):
        return {"userinfo": claims}

    monkeypatch.setattr(client.app.state.oauth.google, "authorize_access_token", exchange)
    return await client.get("/api/auth/callback")


# --- the permissions themselves -------------------------------------------------------


def test_the_permission_list_is_two_per_area_and_audit_reads_only() -> None:
    assert [key for key, _ in AREAS] == ["users", "codes", "jobs", "groups", "audit", "settings"]
    assert ALL_PERMISSIONS == (
        "users.read",
        "users.write",
        "codes.read",
        "codes.write",
        "jobs.read",
        "jobs.write",
        "groups.read",
        "groups.write",
        "audit.read",
        "settings.read",
        "settings.write",
    )


def test_a_stored_list_is_normalized_and_an_unknown_permission_refused() -> None:
    # Write brings read, a repeat counts once, and the order is ALL_PERMISSIONS'.
    assert normalize(["audit.read", "codes.write", "codes.write", "users.read"]) == [
        "users.read",
        "codes.read",
        "codes.write",
        "audit.read",
    ]
    assert normalize([]) == []
    with pytest.raises(ValueError, match="audit.write"):
        normalize(["audit.write"])
    with pytest.raises(ValueError):
        normalize(["users.read", "owner"])
    with pytest.raises(ValueError):
        require_permission("users.delete")


def test_the_console_settings_are_validated() -> None:
    settings = Settings(owner_emails=" Owner@Example.test , ,second@example.test,")
    assert settings.owner_email_set == {"owner@example.test", "second@example.test"}
    assert settings.console_enabled
    assert not Settings(owner_emails=" , ").console_enabled
    assert Settings().admin_reauth_hours == 12 and Settings().admin_page_size == 50
    for bad in ({"admin_reauth_hours": 0}, {"admin_page_size": 0}, {"admin_page_size": 501}):
        with pytest.raises(ValueError):
            Settings(**bad)


# --- off until configured -------------------------------------------------------------


@pytest.mark.parametrize("owner_emails", ["", " , "])
async def test_without_owner_emails_the_console_does_not_exist(make_client, owner_emails) -> None:
    client = make_client(owner_emails=owner_emails)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)

    for response in (
        await client.get("/api/admin/me"),
        await client.get("/api/admin/audit"),
        await client.post("/api/admin/groups", json={"name": "ops"}, headers=CSRF_HEADERS),
    ):
        assert response.status_code == 404
        assert response.json() == {"error": {"type": "invalid_request", "message": "Not Found"}}
    me = (await client.get("/api/me")).json()
    assert me["console"] is False
    # Nobody joined anything either: the dev login lists no owner here.
    async with client.app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(GroupMember)) == 0


# --- browser sessions only ------------------------------------------------------------


async def test_no_session_or_an_unknown_or_expired_one_is_401(make_client) -> None:
    owner = await _owner(make_client)
    anonymous = make_client(owner_emails=OWNERS)
    assert (await anonymous.get("/api/admin/me")).status_code == 401

    anonymous.cookies.set(SESSION_COOKIE, "not-a-session")
    assert _error(await anonymous.get("/api/admin/groups")) == {
        "type": "auth",
        "message": "Not signed in",
    }

    async with owner.app.state.sessionmaker() as db:
        await db.execute(
            update(UserSession)
            .where(UserSession.user_id == owner.user_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await db.commit()
    assert (await owner.get("/api/admin/me")).status_code == 401


async def _api_key(owner: AsyncClient) -> str:
    created = await owner.post("/api/keys", json={"name": "script"}, headers=CSRF_HEADERS)
    return created.json()["key"]


async def _access_token(owner: AsyncClient) -> str:
    _, tokens = await _connected(owner)
    return tokens["access_token"]


async def _upload_ticket(owner: AsyncClient) -> str:
    async with owner.app.state.sessionmaker() as db:
        user = await db.get(User, owner.user_id)
        _, token = await upload_tickets.mint(db, owner.app.state.settings, user)
    return token


@pytest.mark.parametrize("mint", [_api_key, _access_token, _upload_ticket])
async def test_an_authorization_header_is_refused_even_beside_a_valid_cookie(
    make_client, mint
) -> None:
    owner = await _owner(make_client)
    credential = await mint(owner)
    bearer = {"Authorization": f"Bearer {credential}"}
    # The cookie alone opens the console; the credential is valid where it belongs.
    assert (await owner.get("/api/admin/groups")).status_code == 200
    assert owner.cookies.get(SESSION_COOKIE)

    for response in (
        await owner.get("/api/admin/me", headers=bearer),
        await owner.get("/api/admin/groups", headers=bearer),
        await owner.post(
            "/api/admin/groups",
            json={"name": "via-bearer", "permissions": []},
            headers={**bearer, **CSRF_HEADERS},
        ),
    ):
        assert response.status_code == 401
        assert _error(response)["type"] == "auth"
    async with owner.app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Group)) == 1


# --- permissions ----------------------------------------------------------------------


async def test_an_account_in_no_group_reads_its_empty_list_and_nothing_else(make_client) -> None:
    await _owner(make_client)
    reader = await _account(make_client, "reader@example.test", "Reader")

    me = await reader.get("/api/admin/me")
    assert me.status_code == 200
    assert me.json() == {
        "user": {
            "id": reader.user_id,
            "email": "reader@example.test",
            "name": "Reader",
            "picture": None,
        },
        "permissions": [],
        "reauth_required": False,
        "reauth_hours": 12,
    }
    for path in ("/api/admin/groups", "/api/admin/groups/1", "/api/admin/audit"):
        refused = await reader.get(path)
        assert refused.status_code == 403
        assert _error(refused)["type"] == "permission_denied"
    assert (await reader.get("/api/me")).json()["console"] is False


async def test_permissions_add_up_over_groups_and_decide_each_route(make_client) -> None:
    owner = await _owner(make_client)
    member = await _account(make_client, "member@example.test")
    auditors = await _group(owner, "auditors", ["audit.read"])
    empty = await _group(owner, "empty", [])

    # A group that gives nothing gives no console.
    await _add(owner, empty["id"], user_id=member.user_id)
    assert (await member.get("/api/me")).json()["console"] is False

    await _add(owner, auditors["id"], user_id=member.user_id)
    assert (await member.get("/api/me")).json()["console"] is True
    assert (await member.get("/api/admin/audit")).status_code == 200
    refused = await member.get("/api/admin/groups")
    assert refused.status_code == 403
    assert _error(refused) == {
        "type": "permission_denied",
        "message": "This needs the groups.read permission",
    }

    readers = await _group(owner, "readers", ["groups.read"])
    await _add(owner, readers["id"], user_id=member.user_id)
    assert (await member.get("/api/admin/me")).json()["permissions"] == [
        "groups.read",
        "audit.read",
    ]
    assert (await member.get("/api/admin/groups")).status_code == 200
    # Read is not write.
    denied = await member.post(
        "/api/admin/groups", json={"name": "mine", "permissions": []}, headers=CSRF_HEADERS
    )
    assert denied.status_code == 403 and _error(denied)["type"] == "permission_denied"


async def test_the_builtin_group_holds_every_permission_whatever_its_stored_list(
    make_client,
) -> None:
    owner = await _owner(make_client)
    async with owner.app.state.sessionmaker() as db:
        await db.execute(
            update(Group).where(Group.builtin.is_(True)).values(permissions=["audit.read"])
        )
        await db.commit()

    me = (await owner.get("/api/admin/me")).json()
    assert me["permissions"] == list(ALL_PERMISSIONS)
    listed = (await owner.get("/api/admin/groups")).json()
    builtin = listed["groups"][0]
    assert builtin["builtin"] is True
    assert builtin["permissions"] == list(ALL_PERMISSIONS)
    assert builtin["stored_permissions"] == ["audit.read"]


# --- the re-login window --------------------------------------------------------------


async def test_a_session_past_the_window_must_sign_in_again(make_client) -> None:
    owner = await _owner(make_client)
    outsider = await _account(make_client, "outsider@example.test")
    await _age_sessions(owner, owner.user_id, 12.1)
    await _age_sessions(outsider, outsider.user_id, 12.1)

    for response in (
        await owner.get("/api/admin/groups"),
        await owner.post(
            "/api/admin/groups", json={"name": "late", "permissions": []}, headers=CSRF_HEADERS
        ),
    ):
        assert response.status_code == 403
        assert _error(response) == {
            "type": "reauth_required",
            "message": "Sign in again to use the console",
        }
    # The permission is checked first: no one is sent to sign in again for nothing.
    assert _error(await outsider.get("/api/admin/groups"))["type"] == "permission_denied"
    me = (await owner.get("/api/admin/me")).json()
    assert me["reauth_required"] is True and me["reauth_hours"] == 12
    # The rest of the app and the account menu go on with the old session.
    assert (await owner.get("/api/me")).json()["console"] is True

    # A new sign-in is a new session, and the window starts over.
    assert (await owner.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    assert (await owner.get("/api/admin/groups")).status_code == 200
    assert (await owner.get("/api/admin/me")).json()["reauth_required"] is False
    assert [row.action for row in await _audit(owner)] == ["groups.owner_joined"]


async def test_the_window_is_admin_reauth_hours(make_client) -> None:
    owner = await _owner(make_client, admin_reauth_hours=2)
    await _age_sessions(owner, owner.user_id, 1.5)
    assert (await owner.get("/api/admin/groups")).status_code == 200
    await _age_sessions(owner, owner.user_id, 2.5)
    assert _error(await owner.get("/api/admin/groups"))["type"] == "reauth_required"
    assert (await owner.get("/api/admin/me")).json()["reauth_hours"] == 2


# --- owners join at sign-in -----------------------------------------------------------


async def test_a_verified_listed_google_address_joins_the_owner_group_once(
    make_client, monkeypatch
) -> None:
    client = make_client(owner_emails=" someone@example.test, owner@example.test ")
    claims = {
        "sub": "google-owner",
        "email": "Owner@Example.TEST",
        "email_verified": True,
        "name": "The Owner",
    }

    first = await _google_sign_in(client, monkeypatch, claims)
    assert first.status_code == 302
    me = (await client.get("/api/me")).json()
    user_id = me["user"]["id"]
    assert me["console"] is True
    assert (await client.get("/api/admin/me")).json()["permissions"] == list(ALL_PERMISSIONS)

    # The database was built from the models, so the sign-in made the built-in group.
    async with client.app.state.sessionmaker() as db:
        group = (await db.scalars(select(Group))).one()
        assert (group.name, group.builtin, group.permissions) == ("owner", True, [])
        membership = (await db.scalars(select(GroupMember))).one()
        assert (membership.group_id, membership.user_id) == (group.id, user_id)
        assert membership.added_by == user_id
    [joined] = await _audit(client)
    assert (joined.actor_id, joined.permission, joined.action) == (
        user_id,
        None,
        "groups.owner_joined",
    )
    assert (joined.target_type, joined.target_id) == ("group", str(group.id))
    assert (joined.before, joined.after, joined.ip) == (None, {"user_id": user_id}, "127.0.0.1")

    # Signing in again changes nothing and records nothing.
    assert (await _google_sign_in(client, monkeypatch, claims)).status_code == 302
    assert len(await _audit(client)) == 1
    async with client.app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(GroupMember)) == 1


@pytest.mark.parametrize("verified", [False, None, "true"])
async def test_an_unverified_address_never_joins(make_client, monkeypatch, verified) -> None:
    client = make_client(owner_emails="owner@example.test")
    claims = {"sub": "google-owner", "email": "owner@example.test", "name": "Not Verified"}
    if verified is not None:
        claims["email_verified"] = verified

    # Anyone may still sign in; the check is the owner join's alone.
    assert (await _google_sign_in(client, monkeypatch, claims)).status_code == 302
    assert (await client.get("/api/me")).json()["console"] is False
    assert (await client.get("/api/admin/me")).json()["permissions"] == []
    async with client.app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(GroupMember)) == 0
    assert await _audit(client) == []


async def test_an_unlisted_verified_address_does_not_join(make_client, monkeypatch) -> None:
    client = make_client(owner_emails="owner@example.test")
    claims = {"sub": "g", "email": "owner@example.test.evil", "email_verified": True}
    assert (await _google_sign_in(client, monkeypatch, claims)).status_code == 302
    assert (await client.get("/api/admin/me")).json()["permissions"] == []


async def test_the_seeded_builtin_group_is_the_one_joined(make_client) -> None:
    """The migration seeds the group; the code finds it by its flag, not its name."""
    client = make_client(owner_emails=OWNERS)
    async with client.app.state.sessionmaker() as db:
        seeded = Group(name="founders", permissions=[], builtin=True)
        db.add(seeded)
        await db.commit()
        seeded_id = seeded.id
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    async with client.app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Group)) == 1
        assert await db.scalar(select(GroupMember.group_id)) == seeded_id


async def test_taking_an_address_out_of_owner_emails_revokes_nothing(make_client) -> None:
    owner = await _owner(make_client)
    later = make_client(owner_emails="someone-else@example.test")
    later.cookies.set(SESSION_COOKIE, owner.cookies.get(SESSION_COOKIE))
    assert (await later.get("/api/admin/me")).json()["permissions"] == list(ALL_PERMISSIONS)
    # Nor does signing in again while unlisted.
    assert (await later.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    assert (await later.get("/api/admin/me")).json()["permissions"] == list(ALL_PERMISSIONS)


@postgres_only
async def test_owner_sign_ins_at_once_never_fail(make_client, monkeypatch) -> None:
    """Two different owners on a database with no built-in group race to create it, and
    the same owner twice races to join: each sign-in succeeds, one group, one membership
    and one record per person."""
    clients = [make_client(owner_emails="a@example.test,b@example.test") for _ in range(4)]
    claims = [
        {"sub": "google-a", "email": "a@example.test", "email_verified": True},
        {"sub": "google-b", "email": "b@example.test", "email_verified": True},
    ]
    # Each account exists already, so the race is the join's, not the account's.
    async with clients[0].app.state.sessionmaker() as db:
        for claim in claims:
            db.add(User(google_sub=claim["sub"], email=claim["email"]))
        await db.commit()

    for client, claim in zip(clients, claims * 2, strict=True):

        async def exchange(request, claim=claim):
            return {"userinfo": claim}

        monkeypatch.setattr(client.app.state.oauth.google, "authorize_access_token", exchange)
    answers = await asyncio.gather(*(client.get("/api/auth/callback") for client in clients))

    assert [answer.status_code for answer in answers] == [302] * 4
    async with clients[0].app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Group)) == 1
        assert await db.scalar(select(func.count()).select_from(GroupMember)) == 2
    assert sorted(row.action for row in await _audit(clients[0])) == ["groups.owner_joined"] * 2


# --- groups ---------------------------------------------------------------------------


async def test_creating_renaming_editing_and_deleting_a_group_each_record_once(
    make_client,
) -> None:
    owner = await _owner(make_client)
    start = await _last_audit_id(owner)

    created = await _group(owner, "support", ["users.write", "audit.read"])
    assert created == {
        "id": created["id"],
        "name": "support",
        "builtin": False,
        "permissions": ["users.read", "users.write", "audit.read"],
        "stored_permissions": ["users.read", "users.write", "audit.read"],
        "member_count": 0,
        "updated_at": created["updated_at"],
        "updated_by": {"id": owner.user_id, "name": "Local Developer"},
    }
    [row] = await _audit(owner, start)
    assert (row.actor_id, row.permission, row.action, row.target_type, row.target_id) == (
        owner.user_id,
        "groups.write",
        "groups.create",
        "group",
        str(created["id"]),
    )
    assert row.before is None
    assert row.after == {"name": "support", "permissions": created["permissions"]}
    assert row.ip == "127.0.0.1"
    async with owner.app.state.sessionmaker() as db:
        # A side with nothing is SQL NULL, not the JSON value null.
        assert await db.scalar(
            text('SELECT "before" IS NULL FROM audit_log WHERE id = :id'), {"id": row.id}
        )

    path = f"/api/admin/groups/{created['id']}"
    start = await _last_audit_id(owner)
    renamed = await owner.patch(path, json={"name": "helpdesk"}, headers=CSRF_HEADERS)
    assert renamed.status_code == 200 and renamed.json()["name"] == "helpdesk"
    [row] = await _audit(owner, start)
    assert (row.action, row.before, row.after) == (
        "groups.update",
        {"name": "support"},
        {"name": "helpdesk"},
    )

    start = await _last_audit_id(owner)
    edited = await owner.patch(
        path,
        json={
            "name": "helpdesk",
            "permissions": ["codes.write", "users.read"],
            "based_on": ["users.read", "users.write", "audit.read"],
        },
        headers=CSRF_HEADERS,
    )
    assert edited.status_code == 200
    assert edited.json()["permissions"] == ["users.read", "codes.read", "codes.write"]
    [row] = await _audit(owner, start)
    # Only what changed: the name stayed.
    assert row.before == {"permissions": ["users.read", "users.write", "audit.read"]}
    assert row.after == {"permissions": ["users.read", "codes.read", "codes.write"]}

    # Nothing changes, nothing is recorded — the same values, a read unticked while its
    # write stays, a repeat, or an empty body.
    start = await _last_audit_id(owner)
    stored = ["users.read", "codes.read", "codes.write"]
    for body in (
        {"name": "helpdesk", "permissions": stored, "based_on": stored},
        {"permissions": ["users.read", "codes.write"], "based_on": stored},
        {"permissions": ["codes.write", "users.read", "codes.write"], "based_on": stored},
        {},
    ):
        same = await owner.patch(path, json=body, headers=CSRF_HEADERS)
        assert same.status_code == 200 and same.json() == edited.json()
    assert await _audit(owner, start) == []

    member = await _account(make_client, "member@example.test")
    await _add(owner, created["id"], user_id=member.user_id)
    start = await _last_audit_id(owner)
    deleted = await owner.delete(path, headers=CSRF_HEADERS)
    assert deleted.status_code == 204
    [row] = await _audit(owner, start)
    assert (row.action, row.target_type, row.target_id, row.after) == (
        "groups.delete",
        "group",
        str(created["id"]),
        None,
    )
    assert row.before == {
        "name": "helpdesk",
        "permissions": ["users.read", "codes.read", "codes.write"],
        "member_ids": [member.user_id],
    }
    assert (await owner.get(path)).status_code == 404
    async with owner.app.state.sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(GroupMember)
                .where(GroupMember.user_id == member.user_id)
            )
            == 0
        )
    assert (await member.get("/api/admin/me")).json()["permissions"] == []


@pytest.mark.parametrize(
    "name", ["", "Ops", "-ops", "_ops", "ops team", "ops.team", "o" * 33, "ops\n", "öps"]
)
async def test_a_name_outside_the_rule_is_422(make_client, name) -> None:
    owner = await _owner(make_client)
    group = await _group(owner, "valid-name_1", [])
    for response in (
        await owner.post(
            "/api/admin/groups", json={"name": name, "permissions": []}, headers=CSRF_HEADERS
        ),
        await owner.patch(
            f"/api/admin/groups/{group['id']}", json={"name": name}, headers=CSRF_HEADERS
        ),
    ):
        assert response.status_code == 422
        assert _error(response)["type"] == "invalid_request"
    assert [row.action for row in await _audit(owner)] == ["groups.owner_joined", "groups.create"]


async def test_an_unknown_permission_is_422(make_client) -> None:
    owner = await _owner(make_client)
    created = await owner.post(
        "/api/admin/groups",
        json={"name": "ops", "permissions": ["audit.write"]},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 422
    assert "audit.write" in _error(created)["message"]
    group = await _group(owner, "ops", [])
    edited = await owner.patch(
        f"/api/admin/groups/{group['id']}",
        json={"permissions": ["groups.admin"]},
        headers=CSRF_HEADERS,
    )
    assert edited.status_code == 422


async def test_a_permission_list_comes_with_the_list_it_was_edited_from(make_client) -> None:
    """A whole list written over a change made since it was read would undo that change
    unseen: `based_on` says what it was edited from, and a stale one changes nothing."""
    owner = await _owner(make_client)
    group = await _group(owner, "ops", ["codes.read"])
    path = f"/api/admin/groups/{group['id']}"
    start = await _last_audit_id(owner)
    for body in ({"permissions": ["codes.read", "audit.read"]}, {"based_on": ["codes.read"]}):
        response = await owner.patch(path, json=body, headers=CSRF_HEADERS)
        assert response.status_code == 422, body

    # One page ticks users.read; another, still showing the old list, then ticks audit.read.
    first = await owner.patch(
        path,
        json={"permissions": ["users.read", "codes.read"], "based_on": ["codes.read"]},
        headers=CSRF_HEADERS,
    )
    assert first.status_code == 200
    stale = await owner.patch(
        path,
        json={"permissions": ["codes.read", "audit.read"], "based_on": ["codes.read"]},
        headers=CSRF_HEADERS,
    )
    assert stale.status_code == 409
    assert _error(stale)["type"] == "admin_conflict" and _error(stale)["reason"] == "stale"
    assert (await owner.get(path)).json()["group"]["permissions"] == ["users.read", "codes.read"]
    assert [row.action for row in await _audit(owner, start)] == ["groups.update"]

    # Read again, the same tick keeps the other page's; `based_on` is normalized too.
    again = await owner.patch(
        path,
        json={
            "permissions": ["audit.read", "codes.read", "users.read"],
            "based_on": ["codes.read", "users.read", "codes.read"],
        },
        headers=CSRF_HEADERS,
    )
    assert again.status_code == 200
    assert again.json()["permissions"] == ["users.read", "codes.read", "audit.read"]


async def test_a_taken_name_is_409_name_taken(make_client) -> None:
    owner = await _owner(make_client)
    first = await _group(owner, "ops", [])
    second = await _group(owner, "billing", [])
    start = await _last_audit_id(owner)

    for response in (
        await owner.post(
            "/api/admin/groups", json={"name": "ops", "permissions": []}, headers=CSRF_HEADERS
        ),
        # The built-in group's name is a name like any other.
        await owner.post(
            "/api/admin/groups", json={"name": "owner", "permissions": []}, headers=CSRF_HEADERS
        ),
        await owner.patch(
            f"/api/admin/groups/{second['id']}", json={"name": "ops"}, headers=CSRF_HEADERS
        ),
    ):
        assert response.status_code == 409
        assert _error(response) == {
            "type": "admin_conflict",
            "message": "Another group already has that name",
            "reason": "name_taken",
        }
    assert await _audit(owner, start) == []
    assert (await owner.get(f"/api/admin/groups/{first['id']}")).json()["group"]["name"] == "ops"
    assert (await owner.get(f"/api/admin/groups/{second['id']}")).json()["group"][
        "name"
    ] == "billing"


async def test_the_builtin_group_is_never_renamed_edited_or_deleted(make_client) -> None:
    owner = await _owner(make_client)
    builtin = await _builtin_id(owner)
    path = f"/api/admin/groups/{builtin}"
    start = await _last_audit_id(owner)

    for response in (
        await owner.patch(path, json={"name": "root"}, headers=CSRF_HEADERS),
        await owner.patch(
            path, json={"permissions": ["audit.read"], "based_on": []}, headers=CSRF_HEADERS
        ),
        await owner.patch(path, json={"name": "owner"}, headers=CSRF_HEADERS),
        await owner.patch(path, json={}, headers=CSRF_HEADERS),
        await owner.delete(path, headers=CSRF_HEADERS),
    ):
        assert response.status_code == 409
        assert _error(response)["type"] == "admin_conflict"
        assert _error(response)["reason"] == "builtin"
    assert await _audit(owner, start) == []
    group = (await owner.get(path)).json()["group"]
    assert (group["name"], group["builtin"], group["stored_permissions"]) == ("owner", True, [])
    assert group["updated_by"] is None


async def test_groups_list_the_builtin_first_with_the_matrix_and_members_in_join_order(
    make_client,
) -> None:
    owner = await _owner(make_client)
    zeta = await _group(owner, "zeta", ["jobs.read"])
    alpha = await _group(owner, "alpha", [])
    first = await _account(make_client, "first@example.test", "First")
    second = await _account(make_client, "second@example.test", "Second")
    await _add(owner, zeta["id"], user_id=second.user_id)
    await _add(owner, zeta["id"], user_id=first.user_id)

    listed = (await owner.get("/api/admin/groups")).json()
    assert [group["name"] for group in listed["groups"]] == ["owner", "alpha", "zeta"]
    assert [group["member_count"] for group in listed["groups"]] == [1, 0, 2]
    assert listed["permissions"] == list(ALL_PERMISSIONS)
    assert listed["areas"] == [
        {"key": "users", "levels": ["read", "write"]},
        {"key": "codes", "levels": ["read", "write"]},
        {"key": "jobs", "levels": ["read", "write"]},
        {"key": "groups", "levels": ["read", "write"]},
        {"key": "audit", "levels": ["read"]},
        {"key": "settings", "levels": ["read", "write"]},
    ]
    assert listed["groups"][2]["updated_by"] == {"id": owner.user_id, "name": "Local Developer"}

    detail = (await owner.get(f"/api/admin/groups/{zeta['id']}")).json()
    assert detail["group"] == listed["groups"][2]
    members = detail["members"]
    assert [(m["user_id"], m["email"], m["name"]) for m in members] == [
        (second.user_id, "second@example.test", "Second"),
        (first.user_id, "first@example.test", "First"),
    ]
    assert all(m["added_by"] == {"id": owner.user_id, "name": "Local Developer"} for m in members)
    owners = (await owner.get(f"/api/admin/groups/{await _builtin_id(owner)}")).json()["members"]
    # A sign-in's owner join records the member as their own adder.
    assert owners[0]["added_by"] == {"id": owner.user_id, "name": "Local Developer"}
    assert (await owner.get(f"/api/admin/groups/{alpha['id'] + 100}")).status_code == 404


async def test_the_last_editor_of_a_group_is_who_changed_it(make_client) -> None:
    owner = await _owner(make_client)
    deputy = await _account(make_client, "deputy@example.test", "Deputy")
    group = await _group(owner, "ops", ["audit.read"])
    await _add(owner, await _builtin_id(owner), user_id=deputy.user_id)

    await deputy.patch(
        f"/api/admin/groups/{group['id']}", json={"name": "ops2"}, headers=CSRF_HEADERS
    )
    # Adding a member is not a change of the group's own fields.
    await _add(owner, group["id"], user_id=deputy.user_id)
    got = (await owner.get(f"/api/admin/groups/{group['id']}")).json()["group"]
    assert got["updated_by"] == {"id": deputy.user_id, "name": "Deputy"}


# --- members --------------------------------------------------------------------------


async def test_adding_and_removing_members_by_address_or_id(make_client) -> None:
    owner = await _owner(make_client)
    group = await _group(owner, "ops", ["audit.read"])
    person = await _account(make_client, "Person@Example.test", "Person")

    start = await _last_audit_id(owner)
    added = await _add(owner, group["id"], email="  person@EXAMPLE.test ")
    assert added.status_code == 201
    assert added.json() == {
        "user_id": person.user_id,
        "email": "Person@Example.test",
        "name": "Person",
        "added_at": added.json()["added_at"],
        "added_by": {"id": owner.user_id, "name": "Local Developer"},
    }
    [row] = await _audit(owner, start)
    assert (row.actor_id, row.permission, row.action, row.target_type, row.target_id) == (
        owner.user_id,
        "groups.write",
        "groups.add_member",
        "user",
        str(person.user_id),
    )
    assert (row.before, row.after) == (None, {"group_id": group["id"], "group": "ops"})
    assert (await person.get("/api/admin/me")).json()["permissions"] == ["audit.read"]

    # Already in the group: 200, nothing changes, nothing is recorded.
    start = await _last_audit_id(owner)
    again = await _add(owner, group["id"], user_id=person.user_id)
    assert again.status_code == 200 and again.json() == added.json()
    assert await _audit(owner, start) == []

    removed = await owner.delete(
        f"/api/admin/groups/{group['id']}/members/{person.user_id}", headers=CSRF_HEADERS
    )
    assert removed.status_code == 204
    [row] = await _audit(owner, start)
    assert (row.action, row.target_type, row.target_id) == (
        "groups.remove_member",
        "user",
        str(person.user_id),
    )
    assert (row.before, row.after) == ({"group_id": group["id"], "group": "ops"}, None)
    assert (await person.get("/api/admin/me")).json()["permissions"] == []
    # Gone already: 404, nothing recorded.
    again = await owner.delete(
        f"/api/admin/groups/{group['id']}/members/{person.user_id}", headers=CSRF_HEADERS
    )
    assert again.status_code == 404
    assert len(await _audit(owner, start)) == 1


async def test_who_cannot_be_added(make_client) -> None:
    owner = await _owner(make_client)
    group = await _group(owner, "ops", [])
    path = f"/api/admin/groups/{group['id']}/members"
    await _account(make_client, "twice@example.test")
    await _account(make_client, "TWICE@example.test")
    start = await _last_audit_id(owner)

    nobody = await _add(owner, group["id"], email="never-signed-in@example.test")
    assert nobody.status_code == 404 and _error(nobody)["type"] == "invalid_request"
    assert (await _add(owner, group["id"], user_id=987654)).status_code == 404
    ambiguous = await _add(owner, group["id"], email="twice@example.test")
    assert ambiguous.status_code == 409
    assert _error(ambiguous)["reason"] == "email_ambiguous"
    for body in ({}, {"email": "a@example.test", "user_id": 1}, {"email": "   "}):
        response = await owner.post(path, json=body, headers=CSRF_HEADERS)
        assert response.status_code == 422, body
    unknown_group = await _add(owner, group["id"] + 100, user_id=owner.user_id)
    assert unknown_group.status_code == 404
    assert await _audit(owner, start) == []


async def test_the_builtin_group_keeps_its_last_member(make_client) -> None:
    owner = await _owner(make_client)
    builtin = await _builtin_id(owner)
    path = f"/api/admin/groups/{builtin}/members/{owner.user_id}"
    start = await _last_audit_id(owner)

    last = await owner.delete(path, headers=CSRF_HEADERS)
    assert last.status_code == 409
    assert _error(last)["reason"] == "last_owner"
    assert await _audit(owner, start) == []

    # With a second owner, either may go — and the first may remove themselves.
    second = await _account(make_client, "second@example.test")
    assert (await _add(owner, builtin, user_id=second.user_id)).status_code == 201
    assert (await owner.delete(path, headers=CSRF_HEADERS)).status_code == 204
    assert (await owner.get("/api/admin/me")).json()["permissions"] == []
    alone = await second.delete(
        f"/api/admin/groups/{builtin}/members/{second.user_id}", headers=CSRF_HEADERS
    )
    assert alone.status_code == 409 and _error(alone)["reason"] == "last_owner"


async def _owner_count(client: AsyncClient, builtin: int) -> int:
    async with client.app.state.sessionmaker() as db:
        return await db.scalar(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id == builtin)
        )


async def _until_a_request_waits_on_a_lock(client: AsyncClient) -> None:
    """Until another connection of this database waits on a lock (PostgreSQL)."""
    for _ in range(250):
        async with client.app.state.sessionmaker() as probe:
            waiting = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
        if waiting:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no request waited on the lock")


@postgres_only
async def test_two_owners_removing_each_other_at_once_leave_one(make_client) -> None:
    """The second removal runs under the group's row lock after the first, and reads its
    operator's permissions there: removed by the first, they hold nothing any more."""
    first = await _owner(make_client)
    second = await _account(make_client, "second@example.test")
    builtin = await _builtin_id(first)
    await _add(first, builtin, user_id=second.user_id)

    answers = await asyncio.gather(
        first.delete(f"/api/admin/groups/{builtin}/members/{second.user_id}", headers=CSRF_HEADERS),
        second.delete(f"/api/admin/groups/{builtin}/members/{first.user_id}", headers=CSRF_HEADERS),
    )

    assert sorted(answer.status_code for answer in answers) == [204, 403]
    assert await _owner_count(first, builtin) == 1


@postgres_only
async def test_an_owner_removing_the_other_and_themselves_at_once_stays(make_client) -> None:
    """The members are counted under the group's row lock, so the second removal sees the
    first: the group keeps its last member."""
    first = await _owner(make_client)
    second = await _account(make_client, "second@example.test")
    builtin = await _builtin_id(first)
    await _add(first, builtin, user_id=second.user_id)

    answers = await asyncio.gather(
        first.delete(f"/api/admin/groups/{builtin}/members/{second.user_id}", headers=CSRF_HEADERS),
        first.delete(f"/api/admin/groups/{builtin}/members/{first.user_id}", headers=CSRF_HEADERS),
    )

    assert sorted(answer.status_code for answer in answers) == [204, 409]
    assert await _owner_count(first, builtin) == 1


@postgres_only
async def test_an_owner_removed_while_their_request_waits_holds_nothing_when_it_runs(
    make_client,
) -> None:
    """The permissions a request arrived with were read before the group's row lock; read
    again under it, a removal that committed while the request waited counts."""
    first = await _owner(make_client)
    second = await _account(make_client, "second@example.test")
    third = await _account(make_client, "third@example.test")
    builtin = await _builtin_id(first)
    await _add(first, builtin, user_id=second.user_id)
    start = await _last_audit_id(first)

    async with first.app.state.sessionmaker() as db:
        # The first owner's removal of the second, holding the group's row lock as the
        # route does until it commits.
        await db.execute(select(Group).where(Group.id == builtin).with_for_update())
        await db.execute(
            delete(GroupMember).where(
                GroupMember.group_id == builtin, GroupMember.user_id == second.user_id
            )
        )
        # Meanwhile the second, still an owner when their request is checked, adds a third.
        request = asyncio.create_task(_add(second, builtin, user_id=third.user_id))
        await _until_a_request_waits_on_a_lock(first)
        await db.commit()
    answer = await request

    assert answer.status_code == 403 and _error(answer)["type"] == "permission_denied"
    assert (await third.get("/api/admin/me")).json()["permissions"] == []
    assert await _audit(first, start) == []


@postgres_only
async def test_adding_an_account_mid_sign_in_neither_waits_nor_deadlocks(make_client) -> None:
    """A Google sign-in updates its account's row and inserts a session that references it,
    then waits on the owner group's row lock; adding that account to the owner group holds
    the group's lock, then locks the account's row — for key share, which neither of the
    sign-in's locks conflicts with, so neither waits on the other."""
    listed = "listed@example.test"
    first = await _owner(make_client, owner_emails=f"{OWNERS},{listed}")
    person = await _account(make_client, listed, "Listed", owner_emails=f"{OWNERS},{listed}")
    builtin = await _builtin_id(first)
    start = await _last_audit_id(first)

    async with first.app.state.sessionmaker() as db:
        # The sign-in, as the callback runs it, up to the owner join.
        user = await db.get(User, person.user_id)
        user.name = "Listed Again"
        await db.flush()
        await create_session(db, user)
        await db.flush()
        added = await asyncio.wait_for(_add(first, builtin, user_id=person.user_id), timeout=10)
        assert added.status_code == 201
        await join_owner_group(db, first.app.state.settings, user, verified=True, ip=None)
        await db.commit()

    assert (await person.get("/api/admin/me")).json()["permissions"] == list(ALL_PERMISSIONS)
    # Added once, by the first owner; the sign-in found the membership and wrote nothing.
    assert [row.action for row in await _audit(first, start)] == ["groups.add_member"]


# --- no escalation --------------------------------------------------------------------


async def test_an_operator_hands_out_and_takes_away_only_what_they_hold(make_client) -> None:
    owner = await _owner(make_client)
    deputy = await _account(make_client, "deputy@example.test")
    admins = await _group(owner, "admins", ["groups.write", "audit.read"])
    await _add(owner, admins["id"], user_id=deputy.user_id)
    support = await _group(owner, "support", ["users.read"])
    readers = await _group(owner, "readers", ["audit.read"])
    other = await _account(make_client, "other@example.test")
    await _add(owner, support["id"], user_id=other.user_id)
    start = await _last_audit_id(owner)

    def denied(response) -> bool:
        return response.status_code == 403 and _error(response)["type"] == "permission_denied"

    # (a) ticking or unticking a permission not held, on a new group or an existing one.
    assert denied(
        await deputy.post(
            "/api/admin/groups",
            json={"name": "mine", "permissions": ["users.read"]},
            headers=CSRF_HEADERS,
        )
    )
    assert denied(
        await deputy.patch(
            f"/api/admin/groups/{readers['id']}",
            json={"permissions": ["audit.read", "codes.read"], "based_on": ["audit.read"]},
            headers=CSRF_HEADERS,
        )
    )
    assert denied(
        await deputy.patch(
            f"/api/admin/groups/{support['id']}",
            json={"permissions": [], "based_on": ["users.read"]},
            headers=CSRF_HEADERS,
        )
    )
    # Writing brings reading: ticking users.write would hand out users.read too.
    assert denied(
        await deputy.patch(
            f"/api/admin/groups/{readers['id']}",
            json={
                "permissions": ["audit.read", "groups.write", "users.write"],
                "based_on": ["audit.read"],
            },
            headers=CSRF_HEADERS,
        )
    )
    # A group stronger than the operator keeps its list, even against a permission they
    # hold: its members are not theirs to give to or take from.
    assert denied(
        await deputy.patch(
            f"/api/admin/groups/{support['id']}",
            json={"permissions": ["users.read", "audit.read"], "based_on": ["users.read"]},
            headers=CSRF_HEADERS,
        )
    )
    # (b) the members of a group with a permission not held — the built-in group's too.
    assert denied(await _add(deputy, support["id"], user_id=deputy.user_id))
    assert denied(
        await deputy.delete(
            f"/api/admin/groups/{support['id']}/members/{other.user_id}", headers=CSRF_HEADERS
        )
    )
    assert denied(await _add(deputy, await _builtin_id(owner), user_id=deputy.user_id))
    # (d) by account id without users.read — ids walked one by one would list every
    # account's address and name — even into a group they manage.
    assert denied(await _add(deputy, readers["id"], user_id=other.user_id))
    # (c) deleting a group with a permission not held; the built-in group by a non-owner.
    assert denied(await deputy.delete(f"/api/admin/groups/{support['id']}", headers=CSRF_HEADERS))
    assert denied(
        await deputy.delete(f"/api/admin/groups/{await _builtin_id(owner)}", headers=CSRF_HEADERS)
    )
    assert await _audit(owner, start) == []

    # What they do hold, they may hand out, take away and manage; a name grants nothing.
    ticked = await deputy.patch(
        f"/api/admin/groups/{readers['id']}",
        json={"permissions": ["audit.read", "groups.write"], "based_on": ["audit.read"]},
        headers=CSRF_HEADERS,
    )
    assert ticked.status_code == 200
    assert ticked.json()["permissions"] == ["groups.read", "groups.write", "audit.read"]
    renamed = await deputy.patch(
        f"/api/admin/groups/{support['id']}", json={"name": "helpdesk"}, headers=CSRF_HEADERS
    )
    assert renamed.status_code == 200
    assert (await _add(deputy, readers["id"], email="other@example.test")).status_code == 201
    assert (
        await deputy.delete(
            f"/api/admin/groups/{readers['id']}/members/{other.user_id}", headers=CSRF_HEADERS
        )
    ).status_code == 204
    assert (
        await deputy.delete(f"/api/admin/groups/{readers['id']}", headers=CSRF_HEADERS)
    ).status_code == 204
    assert [row.action for row in await _audit(owner, start)] == [
        "groups.update",
        "groups.update",
        "groups.add_member",
        "groups.remove_member",
        "groups.delete",
    ]
    assert {row.actor_id for row in await _audit(owner, start)} == {deputy.user_id}


# --- the audit log --------------------------------------------------------------------


async def test_the_log_is_newest_first_paged_and_filtered(make_client) -> None:
    owner = await _owner(make_client, admin_page_size=3)
    member = await _account(
        make_client, "Member.Person@example.test", "Member Person", admin_page_size=3
    )
    ops = await _group(owner, "ops", ["audit.read"])
    # No digit in these names: the id search below must match the id alone.
    for name in ("ba", "bb", "bc"):
        await _group(owner, name, [])
    await _add(owner, ops["id"], user_id=member.user_id)
    # Six rows: the owner's join, four creations, one membership.

    first = (await owner.get("/api/admin/audit")).json()
    assert (first["total"], first["page"], first["page_size"]) == (6, 1, 3)
    assert [item["action"] for item in first["items"]] == [
        "groups.add_member",
        "groups.create",
        "groups.create",
    ]
    newest = first["items"][0]
    assert newest == {
        "id": newest["id"],
        "created_at": newest["created_at"],
        "actor": {"id": owner.user_id, "email": "dev@localhost", "name": "Local Developer"},
        "permission": "groups.write",
        "action": "groups.add_member",
        "target_type": "user",
        "target_id": str(member.user_id),
        "target_label": "Member.Person@example.test",
        "before": None,
        "after": {"group_id": ops["id"], "group": "ops"},
        "ip": "127.0.0.1",
    }
    assert first["items"][1]["target_label"] == "bc"
    second = (await owner.get("/api/admin/audit", params={"page": 2})).json()
    assert [item["action"] for item in second["items"]] == [
        "groups.create",
        "groups.create",
        "groups.owner_joined",
    ]
    assert second["items"][-1]["permission"] is None
    past = (await owner.get("/api/admin/audit", params={"page": 3})).json()
    assert (past["items"], past["total"]) == ([], 6)
    # A page size may ask for fewer, never more.
    assert len((await owner.get("/api/admin/audit", params={"page_size": 2})).json()["items"]) == 2
    capped = (await owner.get("/api/admin/audit", params={"page_size": 500})).json()
    assert capped["page_size"] == 3 and len(capped["items"]) == 3
    for params in (
        {"page": 0},
        {"page": 2**31},
        {"page": 10**19},
        {"page_size": 0},
        {"area": "billing"},
        {"area": ""},
    ):
        refused = await owner.get("/api/admin/audit", params=params)
        assert refused.status_code == 422, params
    # The highest page there is: far past the end, an empty page, not a database error.
    farthest = (await owner.get("/api/admin/audit", params={"page": 2**31 - 1})).json()
    assert (farthest["items"], farthest["total"]) == ([], 6)

    async def target_ids(**params) -> list[str]:
        """Every page of the filtered log."""
        found, page = [], 1
        while True:
            body = (await owner.get("/api/admin/audit", params={**params, "page": page})).json()
            found += [item["target_id"] for item in body["items"]]
            if page * body["page_size"] >= body["total"]:
                assert len(found) == body["total"]
                return found
            page += 1

    assert len(await target_ids(area="groups")) == 6
    assert await target_ids(area="users") == []
    # An actor's address or name, or a user target's address — a part of it, any case.
    assert len(await target_ids(q="DEV@LOCAL")) == 6
    assert len(await target_ids(q=" local developer ")) == 6
    assert await target_ids(q="member.PERSON") == [str(member.user_id)]
    # A group target's name, as the Target column prints it.
    by_name = await target_ids(q="OPS")
    assert by_name and set(by_name) == {str(ops["id"])}
    # An id, exactly: the group's creation, and nothing whose id merely contains it.
    by_id = await target_ids(q=str(ops["id"]))
    assert by_id and set(by_id) == {str(ops["id"])}
    # LIKE's wildcards are searched for literally.
    assert await target_ids(q="%") == []
    assert await target_ids(q="_") == []


async def test_who_a_row_names_is_read_when_the_log_is_read(make_client) -> None:
    owner = await _owner(make_client)
    group = await _group(owner, "ops", [])
    await owner.patch(
        f"/api/admin/groups/{group['id']}", json={"name": "ops2"}, headers=CSRF_HEADERS
    )
    items = (await owner.get("/api/admin/audit", params={"area": "groups"})).json()["items"]
    # The group's name now, not when the row was written.
    assert [item["target_label"] for item in items[:2]] == ["ops2", "ops2"]
    await owner.delete(f"/api/admin/groups/{group['id']}", headers=CSRF_HEADERS)
    items = (await owner.get("/api/admin/audit")).json()["items"]
    assert [item["target_label"] for item in items[:3]] == [None, None, None]
    assert items[0]["before"]["name"] == "ops2"


async def test_no_record_holds_an_address_or_a_persons_name(make_client, monkeypatch) -> None:
    owner = await _owner(make_client)
    named = await _account(make_client, "private.person@example.test", "Private Person")
    group = await _group(owner, "ops", ["audit.read"])
    await _add(owner, group["id"], email="private.person@example.test")
    await owner.patch(
        f"/api/admin/groups/{group['id']}",
        json={"name": "ops2", "permissions": [], "based_on": ["audit.read"]},
        headers=CSRF_HEADERS,
    )
    await owner.delete(
        f"/api/admin/groups/{group['id']}/members/{named.user_id}", headers=CSRF_HEADERS
    )
    await _add(owner, group["id"], user_id=named.user_id)
    await owner.delete(f"/api/admin/groups/{group['id']}", headers=CSRF_HEADERS)
    signer = make_client(owner_emails="google.owner@example.test")
    await _google_sign_in(
        signer,
        monkeypatch,
        {
            "sub": "g",
            "email": "google.owner@example.test",
            "email_verified": True,
            "name": "Google Owner",
        },
    )

    rows = await _audit(owner)
    assert len(rows) == 8
    stored = json.dumps([[row.before, row.after] for row in rows]).lower()
    for secret in ("@", "private", "person", "google owner", "local developer", "dev@"):
        assert secret not in stored


async def test_no_route_updates_or_deletes_a_record(make_client) -> None:
    owner = await _owner(make_client)
    [row] = await _audit(owner)
    for method in ("put", "patch", "delete"):
        for path in ("/api/admin/audit", f"/api/admin/audit/{row.id}"):
            response = await owner.request(method, path, headers=CSRF_HEADERS)
            assert response.status_code in (404, 405)
    assert [(r.id, r.after) for r in await _audit(owner)] == [(row.id, row.after)]


# --- CSRF -----------------------------------------------------------------------------


CONSOLE_MUTATIONS = [
    (
        "post",
        "/api/admin/users/{user}/grants",
        {"kind": "credits", "credits": 1, "request_id": "00000000-0000-4000-8000-000000000001"},
    ),
    ("post", "/api/admin/groups", {"name": "csrf", "permissions": []}),
    ("patch", "/api/admin/groups/{group}", {"name": "csrf"}),
    ("delete", "/api/admin/groups/{group}", None),
    ("post", "/api/admin/groups/{group}/members", {"user_id": 1}),
    ("delete", "/api/admin/groups/{group}/members/{user}", None),
]


def test_every_console_mutation_is_listed_and_guarded() -> None:
    """A mutation added later without the CSRF guard, or without its entry above, fails
    here."""
    mutations = set()
    for route in admin_routes.router.routes:
        assert isinstance(route, APIRoute)
        for method in route.methods - {"GET", "HEAD"}:
            mutations.add((method.lower(), route.path))
            calls = [dependency.call for dependency in route.dependant.dependencies]
            assert require_csrf_header in calls, route.path
    assert mutations == {
        (method, path.replace("{group}", "{group_id}").replace("{user}", "{user_id}"))
        for method, path, _ in CONSOLE_MUTATIONS
    }


@pytest.mark.parametrize(("method", "path", "body"), CONSOLE_MUTATIONS)
async def test_console_mutations_require_the_custom_header(
    make_client, method: str, path: str, body: dict | None
) -> None:
    owner = await _owner(make_client)
    group = await _group(owner, "target", [])
    outsider = await _account(make_client, "outsider@example.test")
    url = path.format(group=group["id"], user=owner.user_id)
    start = await _last_audit_id(owner)

    response = await owner.request(method, url, json=body)
    assert response.status_code == 403
    assert response.json() == {
        "error": {"type": "auth", "message": "Missing X-Requested-With header"}
    }
    # After the session (401) and before the permission (403 permission_denied).
    anonymous = make_client(owner_emails=OWNERS)
    assert (await anonymous.request(method, url, json=body)).status_code == 401
    assert _error(await outsider.request(method, url, json=body))["type"] == "auth"
    assert await _audit(owner, start) == []


# --- deleting an account --------------------------------------------------------------


@postgres_only
async def test_deleting_an_account_takes_its_memberships_and_keeps_its_records(
    make_client, tmp_path
) -> None:
    owner = await _owner(make_client, workspaces_dir=str(tmp_path / "ws"))
    member = await _account(make_client, "member@example.test", workspaces_dir=str(tmp_path / "ws"))
    group = await _group(owner, "ops", ["audit.read"])
    await _add(owner, group["id"], user_id=member.user_id)
    await _add(owner, group["id"], user_id=owner.user_id)
    builtin = await _builtin_id(owner)

    # The last owner's account is deleted without a refusal.
    deleted = await owner.delete("/api/account", headers=CSRF_HEADERS)
    assert deleted.status_code == 204, deleted.text

    async with owner.app.state.sessionmaker() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(GroupMember).where(GroupMember.group_id == builtin)
            )
            == 0
        )
        remaining = (await db.scalars(select(GroupMember))).all()
        assert [(m.user_id, m.added_by) for m in remaining] == [(member.user_id, None)]
        rows = (await db.scalars(select(AuditLog).order_by(AuditLog.id))).all()
    assert [row.action for row in rows] == [
        "groups.owner_joined",
        "groups.create",
        "groups.add_member",
        "groups.add_member",
    ]
    assert {row.actor_id for row in rows} == {None}
    assert rows[2].target_id == str(member.user_id)
    items = (await member.get("/api/admin/audit")).json()["items"]
    assert {item["actor"] is None for item in items} == {True}
    # The record about the deleted account no longer resolves to an address.
    assert items[0]["target_label"] is None

    # A listed address signing in again joins as a new account.
    assert (await owner.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    me = (await owner.get("/api/admin/me")).json()
    assert me["user"]["id"] != owner.user_id
    assert me["permissions"] == list(ALL_PERMISSIONS)
