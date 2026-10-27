"""Groups and their members (docs/admin.md § Groups, § Permissions): what an account
holds, the built-in group and the sign-in owner join, and every change the console makes
to them with its no-escalation rules and its audit row.

Code here reads a group's permissions and its `builtin` flag — never its name. The
changes write into the caller's transaction and never commit: the route commits once,
the change and its audit row together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import Group, GroupMember, User
from ..errors import ApiError
from . import audit
from .permissions import ALL_PERMISSIONS

if TYPE_CHECKING:
    from .deps import Operator

# The name the built-in group is created with when the database has none. Only ever
# written, never compared: the group is found by its flag.
BUILTIN_GROUP_NAME = "owner"


def effective_permissions(group: Group) -> list[str]:
    """What a group gives, in `ALL_PERMISSIONS` order: every permission for the built-in
    group, whatever its stored list, those added later included."""
    if group.builtin:
        return list(ALL_PERMISSIONS)
    return [permission for permission in ALL_PERMISSIONS if permission in group.permissions]


async def permissions_of(db: AsyncSession, user_id: int) -> tuple[str, ...]:
    """The union of the account's groups, in `ALL_PERMISSIONS` order; empty for an
    account in no group."""
    rows = (
        await db.execute(
            select(Group.builtin, Group.permissions)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == user_id)
        )
    ).all()
    held: set[str] = set()
    for builtin, permissions in rows:
        held.update(ALL_PERMISSIONS if builtin else permissions)
    return tuple(permission for permission in ALL_PERMISSIONS if permission in held)


async def builtin_group(db: AsyncSession) -> Group:
    """The built-in group under its row lock, created when there is none — the migration
    seeds it, a database built from the models has none. A creation racing this one is
    absorbed by the partial unique index and read back."""
    query = select(Group).where(Group.builtin.is_(True)).with_for_update()
    group = (await db.execute(query)).scalar_one_or_none()
    if group is not None:
        return group
    try:
        async with db.begin_nested():
            group = Group(name=BUILTIN_GROUP_NAME, permissions=[], builtin=True)
            db.add(group)
            await db.flush()
    except IntegrityError:
        group = (await db.execute(query)).scalar_one()
    return group


async def join_owner_group(
    db: AsyncSession, settings: Settings, user: User, *, verified: bool, ip: str | None
) -> None:
    """At sign-in: an account whose address Google verified and `OWNER_EMAILS` lists, in
    any letter case, joins the built-in group with one audit row, in the sign-in's own
    transaction. Joining again changes nothing; the group's row lock makes two sign-ins
    at once take turns, so neither fails."""
    if not verified or user.email.strip().lower() not in settings.owner_email_set:
        return
    group = await builtin_group(db)
    if await db.get(GroupMember, (group.id, user.id)) is not None:
        return
    db.add(GroupMember(group_id=group.id, user_id=user.id, added_by=user.id))
    audit.record(
        db,
        actor_id=user.id,
        permission=None,
        action="groups.owner_joined",
        target_type="group",
        target_id=group.id,
        before=None,
        after={"user_id": user.id},
        ip=ip,
    )
    await db.flush()


# --- the console's changes ------------------------------------------------------------


async def _locked_group(
    db: AsyncSession, operator: Operator, group_id: int
) -> tuple[Group, set[str]]:
    """The group under its row lock, so changes to one group and its members take turns —
    the built-in group's last member is counted under it — and what the operator holds
    now. The permissions the request arrived with were read before the lock; read again
    after it, a change to the operator's own groups that committed while this waited
    counts, so an owner removed meanwhile no longer acts as one."""
    group = (
        await db.execute(select(Group).where(Group.id == group_id).with_for_update())
    ).scalar_one_or_none()
    if group is None:
        raise ApiError(404, "invalid_request", "No such group")
    held = set(await permissions_of(db, operator.user.id))
    if "groups.write" not in held:
        raise ApiError(403, "permission_denied", "This needs the groups.write permission")
    return group, held


def _builtin_refused() -> ApiError:
    return ApiError(
        409,
        "admin_conflict",
        "The built-in group cannot be renamed, edited or deleted",
        details={"reason": "builtin"},
    )


async def _flush_name(db: AsyncSession) -> None:
    """Write a new or renamed group; a name another group holds is the unique
    constraint's to refuse, a race included."""
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError:
        raise ApiError(
            409,
            "admin_conflict",
            "Another group already has that name",
            details={"reason": "name_taken"},
        ) from None


async def create_group(
    db: AsyncSession, operator: Operator, *, name: str, permissions: list[str]
) -> Group:
    """`permissions` normalized. Only a permission the operator holds may be ticked."""
    if not set(permissions) <= set(operator.permissions):
        raise ApiError(
            403, "permission_denied", "You can only hand out permissions you hold yourself"
        )
    group = Group(name=name, permissions=permissions)
    db.add(group)
    await _flush_name(db)
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="groups.write",
        action="groups.create",
        target_type="group",
        target_id=group.id,
        before=None,
        after={"name": name, "permissions": permissions},
        ip=operator.ip,
    )
    return group


async def update_group(
    db: AsyncSession,
    operator: Operator,
    group_id: int,
    *,
    name: str | None,
    permissions: list[str] | None,
    based_on: list[str] | None,
) -> Group:
    """A rename and a new permission list (whole, normalized), either or both; only what
    differs is written and recorded, and a request that changes nothing records nothing.
    Changing the list takes holding every permission the group gives and every one it
    ticks, so a group stronger than the operator keeps its list; a rename takes
    `groups.write` alone — a name grants nothing. The built-in group refuses whatever the
    body. `based_on` is the list the new one was edited from: when it is no longer the
    stored one, someone changed it meanwhile, and writing the whole list would undo their
    change unseen."""
    group, held = await _locked_group(db, operator, group_id)
    before: dict = {}
    after: dict = {}
    current = effective_permissions(group)
    if permissions is not None and permissions != current:
        if not set(current) | set(permissions) <= held:
            raise ApiError(
                403,
                "permission_denied",
                "You can only change the permissions of a group whose permissions you all "
                "hold, to permissions you hold",
            )
        before["permissions"], after["permissions"] = current, permissions
    if group.builtin:
        raise _builtin_refused()
    if "permissions" in after and based_on != current:
        raise ApiError(
            409,
            "admin_conflict",
            "The group's permissions changed since they were read; read them again",
            details={"reason": "stale"},
        )
    if name is not None and name != group.name:
        before["name"], after["name"] = group.name, name
    if not after:
        return group
    if "name" in after:
        group.name = name
    if "permissions" in after:
        group.permissions = permissions
    await _flush_name(db)
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="groups.write",
        action="groups.update",
        target_type="group",
        target_id=group.id,
        before=before,
        after=after,
        ip=operator.ip,
    )
    return group


async def delete_group(db: AsyncSession, operator: Operator, group_id: int) -> None:
    """Only by someone holding every permission it gives; its members keep what their
    other groups give them."""
    group, held = await _locked_group(db, operator, group_id)
    permissions = effective_permissions(group)
    if not set(permissions) <= held:
        raise ApiError(
            403,
            "permission_denied",
            "You can only delete a group whose permissions you all hold",
        )
    if group.builtin:
        raise _builtin_refused()
    member_ids = sorted(
        await db.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group.id))
    )
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="groups.write",
        action="groups.delete",
        target_type="group",
        target_id=group.id,
        before={"name": group.name, "permissions": permissions, "member_ids": member_ids},
        after=None,
        ip=operator.ip,
    )
    # Explicit rather than left to the foreign key, which the SQLite test fallback does
    # not enforce.
    await db.execute(delete(GroupMember).where(GroupMember.group_id == group.id))
    await db.delete(group)
    await db.flush()


def _refuse_unless_manager(held: set[str], group: Group) -> None:
    """Members are added and removed only by someone holding every permission of the
    group — of the built-in group, every permission there is."""
    if not set(effective_permissions(group)) <= held:
        raise ApiError(
            403,
            "permission_denied",
            "You can only manage the members of a group whose permissions you all hold",
        )


async def add_member(
    db: AsyncSession,
    operator: Operator,
    group_id: int,
    *,
    email: str | None,
    user_id: int | None,
) -> tuple[int, bool]:
    """The account by its address (trimmed, any letter case, exact) or its id; returns its
    id and whether it joined now — someone already in the group changes nothing.

    By id needs `users.read` as well: ids can be walked one by one, and every answer names
    an account, which is the users list's to show. The account's row is locked for key
    share, so a deletion of it waits rather than racing the insert, while its sign-in —
    which holds the same row's key share for its session and then waits on the built-in
    group's lock this request holds — is never waited on in turn."""
    group, held = await _locked_group(db, operator, group_id)
    _refuse_unless_manager(held, group)
    if user_id is not None and "users.read" not in held:
        raise ApiError(
            403, "permission_denied", "Adding someone by account id needs the users.read permission"
        )
    if email is not None:
        # `users.email` is not unique: a Google account deleted and made again signs in
        # as a new account with the same address.
        found = (
            await db.scalars(
                select(User.id)
                .where(func.lower(User.email) == email.strip().lower())
                .limit(2)
                .with_for_update(read=True, key_share=True)
            )
        ).all()
        if not found:
            raise ApiError(404, "invalid_request", "No account uses that address")
        if len(found) > 1:
            raise ApiError(
                409,
                "admin_conflict",
                "Several accounts use that address; add the person by account id",
                details={"reason": "email_ambiguous"},
            )
        member_id = found[0]
    else:
        member_id = await db.scalar(
            select(User.id).where(User.id == user_id).with_for_update(read=True, key_share=True)
        )
        if member_id is None:
            raise ApiError(404, "invalid_request", "No such account")
    if await db.get(GroupMember, (group.id, member_id)) is not None:
        return member_id, False
    db.add(GroupMember(group_id=group.id, user_id=member_id, added_by=operator.user.id))
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="groups.write",
        action="groups.add_member",
        target_type="user",
        target_id=member_id,
        before=None,
        after={"group_id": group.id, "group": group.name},
        ip=operator.ip,
    )
    await db.flush()
    return member_id, True


async def remove_member(db: AsyncSession, operator: Operator, group_id: int, user_id: int) -> None:
    """The built-in group keeps its last member, counted under the group's row lock so two
    removals at once cannot both pass."""
    group, held = await _locked_group(db, operator, group_id)
    member = await db.get(GroupMember, (group.id, user_id))
    if member is None:
        raise ApiError(404, "invalid_request", "That account is not in the group")
    _refuse_unless_manager(held, group)
    if group.builtin:
        members = await db.scalar(
            select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group.id)
        )
        if members <= 1:
            raise ApiError(
                409,
                "admin_conflict",
                "The built-in group keeps its last member",
                details={"reason": "last_owner"},
            )
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="groups.write",
        action="groups.remove_member",
        target_type="user",
        target_id=user_id,
        before={"group_id": group.id, "group": group.name},
        after=None,
        ip=operator.ip,
    )
    await db.delete(member)
    await db.flush()
