"""The operator console's routes, `/api/admin/*` (docs/admin.md § Routes).

Mounted by main.py only while `OWNER_EMAILS` names an address. Every route takes the
browser session alone (`require_console_session`, on the router) and checks its own
permission (`require_permission`); a mutation also needs `X-Requested-With`. The rules
and the writes are `sightread.admin`'s; this module reads and answers. No route returns
a document, a note, a conversation or any other content of an account.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..admin import groups
from ..admin.deps import (
    ConsoleSession,
    Operator,
    reauth_due,
    require_console_session,
    require_permission,
)
from ..admin.permissions import ALL_PERMISSIONS, AREAS, normalize
from ..auth.deps import AppSettings, CsrfGuard, DbSession
from ..db.models import AuditLog, Group, GroupMember, User
from ..errors import ApiError

router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_console_session)]
)

GroupsReader = Annotated[Operator, Depends(require_permission("groups.read"))]
GroupsWriter = Annotated[Operator, Depends(require_permission("groups.write"))]
AuditReader = Annotated[Operator, Depends(require_permission("audit.read"))]

# A group's name (docs/admin.md § Groups); the web app checks the same rule as it is typed.
GROUP_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
GROUP_NAME_MAX_CHARS = 32
# The highest `?page=` a list takes (docs/admin.md § Routes): its offset, at the largest
# page size, stays well inside the 64-bit integer PostgreSQL's OFFSET takes.
PAGE_MAX = 2**31 - 1


@router.get("/me")
async def console_me(operator: ConsoleSession, settings: AppSettings):
    """How the pages learn what the account holds and whether the session is past the
    window: refused on neither (docs/admin.md § Signing in)."""
    user = operator.user
    return {
        "user": {"id": user.id, "email": user.email, "name": user.name, "picture": user.picture},
        "permissions": list(operator.permissions),
        "reauth_required": reauth_due(operator, settings),
        "reauth_hours": settings.admin_reauth_hours,
    }


# --- groups ---------------------------------------------------------------------------


async def _group_payloads(db: AsyncSession, rows: list[Group]) -> list[dict]:
    """`<group>`: the effective and the stored permissions, the member count, and who
    last created or changed it — the actor of its newest `groups.create` or
    `groups.update` record, null for the built-in group and for a deleted account."""
    ids = [group.id for group in rows]
    counts = dict(
        (
            await db.execute(
                select(GroupMember.group_id, func.count())
                .where(GroupMember.group_id.in_(ids))
                .group_by(GroupMember.group_id)
            )
        ).all()
    )
    newest = (
        select(AuditLog.target_id, func.max(AuditLog.id).label("id"))
        .where(
            AuditLog.target_type == "group",
            AuditLog.target_id.in_([str(group_id) for group_id in ids]),
            AuditLog.action.in_(("groups.create", "groups.update")),
        )
        .group_by(AuditLog.target_id)
        .subquery()
    )
    editors = {
        int(target_id): {"id": user_id, "name": name}
        for target_id, user_id, name in (
            await db.execute(
                select(newest.c.target_id, User.id, User.name)
                .select_from(newest)
                .join(AuditLog, AuditLog.id == newest.c.id)
                .join(User, User.id == AuditLog.actor_id)
            )
        ).all()
    }
    return [
        {
            "id": group.id,
            "name": group.name,
            "builtin": group.builtin,
            "permissions": groups.effective_permissions(group),
            "stored_permissions": list(group.permissions),
            "member_count": counts.get(group.id, 0),
            "updated_at": group.updated_at,
            "updated_by": editors.get(group.id),
        }
        for group in rows
    ]


async def _member_payloads(db: AsyncSession, *where) -> list[dict]:
    """`<member>`s in the order they joined; `added_by` null once that account is
    deleted."""
    adder = aliased(User)
    rows = (
        await db.execute(
            select(GroupMember, User, adder)
            .join(User, User.id == GroupMember.user_id)
            .outerjoin(adder, adder.id == GroupMember.added_by)
            .where(*where)
            .order_by(GroupMember.added_at, GroupMember.user_id)
        )
    ).all()
    return [
        {
            "user_id": member.user_id,
            "email": user.email,
            "name": user.name,
            "added_at": member.added_at,
            "added_by": None if by is None else {"id": by.id, "name": by.name},
        }
        for member, user, by in rows
    ]


@router.get("/groups")
async def list_groups(operator: GroupsReader, db: DbSession):
    rows = list(await db.scalars(select(Group).order_by(Group.builtin.desc(), Group.name)))
    return {
        "groups": await _group_payloads(db, rows),
        "permissions": list(ALL_PERMISSIONS),
        "areas": [{"key": key, "levels": list(levels)} for key, levels in AREAS],
    }


@router.get("/groups/{group_id}")
async def get_group(group_id: int, operator: GroupsReader, db: DbSession):
    group = await db.get(Group, group_id)
    if group is None:
        raise ApiError(404, "invalid_request", "No such group")
    return {
        "group": (await _group_payloads(db, [group]))[0],
        "members": await _member_payloads(db, GroupMember.group_id == group.id),
    }


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=GROUP_NAME_MAX_CHARS, pattern=GROUP_NAME_PATTERN)
    permissions: list[str] = Field(default_factory=list)

    @field_validator("permissions")
    @classmethod
    def _normalized(cls, value: list[str]) -> list[str]:
        return normalize(value)


class GroupPatch(BaseModel):
    """A new name, a new permission list, or both. `permissions` is the whole new list and
    `based_on` the list it was edited from, as the caller read it: they come together."""

    name: str | None = Field(
        default=None, min_length=1, max_length=GROUP_NAME_MAX_CHARS, pattern=GROUP_NAME_PATTERN
    )
    permissions: list[str] | None = None
    based_on: list[str] | None = None

    @field_validator("permissions", "based_on")
    @classmethod
    def _normalized(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize(value)

    @model_validator(mode="after")
    def _together(self) -> GroupPatch:
        if (self.permissions is None) != (self.based_on is None):
            raise ValueError("send permissions and based_on together")
        return self


@router.post("/groups", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_group(body: GroupCreate, operator: GroupsWriter, db: DbSession):
    group = await groups.create_group(db, operator, name=body.name, permissions=body.permissions)
    await db.commit()
    return (await _group_payloads(db, [group]))[0]


@router.patch("/groups/{group_id}", dependencies=[CsrfGuard])
async def update_group(group_id: int, body: GroupPatch, operator: GroupsWriter, db: DbSession):
    group = await groups.update_group(
        db,
        operator,
        group_id,
        name=body.name,
        permissions=body.permissions,
        based_on=body.based_on,
    )
    await db.commit()
    return (await _group_payloads(db, [group]))[0]


@router.delete(
    "/groups/{group_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_group(group_id: int, operator: GroupsWriter, db: DbSession) -> Response:
    await groups.delete_group(db, operator, group_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MemberAdd(BaseModel):
    """Exactly one of the two: an address, or an account id."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str | None = Field(default=None, min_length=1, max_length=320)
    user_id: int | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> MemberAdd:
        if (self.email is None) == (self.user_id is None):
            raise ValueError("send exactly one of email and user_id")
        return self


@router.post(
    "/groups/{group_id}/members", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED
)
async def add_group_member(
    group_id: int, body: MemberAdd, operator: GroupsWriter, db: DbSession, response: Response
):
    """201 for a new member; 200, and nothing recorded, for someone already in the group."""
    member_id, joined = await groups.add_member(
        db, operator, group_id, email=body.email, user_id=body.user_id
    )
    await db.commit()
    if not joined:
        response.status_code = status.HTTP_200_OK
    members = await _member_payloads(
        db, GroupMember.group_id == group_id, GroupMember.user_id == member_id
    )
    return members[0]


@router.delete(
    "/groups/{group_id}/members/{user_id}",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_group_member(
    group_id: int, user_id: int, operator: GroupsWriter, db: DbSession
) -> Response:
    await groups.remove_member(db, operator, group_id, user_id)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- the audit log --------------------------------------------------------------------


@router.get("/audit")
async def list_audit(
    operator: AuditReader,
    db: DbSession,
    settings: AppSettings,
    page: int = Query(default=1, ge=1, le=PAGE_MAX),
    page_size: int | None = Query(default=None, ge=1),
    area: str | None = None,
    q: str = "",
):
    """Newest first, `ADMIN_PAGE_SIZE` a page unless the query asks for fewer. `area`
    keeps one area's actions; `q` an actor's address or name, a user target's address or
    a group target's name, containing it in any letter case, or a `target_id` equal to
    it. Who a row
    names is read now, from `users` and `groups`."""
    if area is not None and area not in {key for key, _ in AREAS}:
        raise ApiError(422, "invalid_request", f"area: one of {', '.join(k for k, _ in AREAS)}")
    size = min(page_size or settings.admin_page_size, settings.admin_page_size)
    actor = aliased(User)
    target_user = aliased(User)
    target_group = aliased(Group)
    filters = []
    if area is not None:
        filters.append(AuditLog.action.startswith(f"{area}.", autoescape=True))
    needle = q.strip()
    if needle:
        filters.append(
            or_(
                actor.email.icontains(needle, autoescape=True),
                actor.name.icontains(needle, autoescape=True),
                target_user.email.icontains(needle, autoescape=True),
                target_group.name.icontains(needle, autoescape=True),
                AuditLog.target_id == needle,
            )
        )

    def joined(*columns):
        return (
            select(*columns)
            .select_from(AuditLog)
            .outerjoin(actor, actor.id == AuditLog.actor_id)
            .outerjoin(
                target_user,
                and_(
                    AuditLog.target_type == "user",
                    AuditLog.target_id == cast(target_user.id, String),
                ),
            )
            .outerjoin(
                target_group,
                and_(
                    AuditLog.target_type == "group",
                    AuditLog.target_id == cast(target_group.id, String),
                ),
            )
            .where(*filters)
        )

    total = await db.scalar(select(func.count()).select_from(joined(AuditLog.id).subquery()))
    rows = (
        await db.execute(
            joined(AuditLog, actor, target_user.email, target_group.name)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    ).all()
    return {
        "items": [
            {
                "id": entry.id,
                "created_at": entry.created_at,
                "actor": None
                if who is None
                else {"id": who.id, "email": who.email, "name": who.name},
                "permission": entry.permission,
                "action": entry.action,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
                # One of the two joins matches, by the target's type; null once it is gone.
                "target_label": user_email or group_name,
                "before": entry.before,
                "after": entry.after,
                "ip": entry.ip,
            }
            for entry, who, user_email, group_name in rows
        ],
        "total": total,
        "page": page,
        "page_size": size,
    }
