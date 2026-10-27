"""The operator console's routes, `/api/admin/*` (docs/admin.md § Routes).

Mounted by main.py only while `OWNER_EMAILS` names an address. Every route takes the
browser session alone (`require_console_session`, on the router) and checks its own
permission (`require_permission`); a mutation also needs `X-Requested-With`. The rules
and the writes are `sightread.admin`'s; this module reads and answers. No route returns
a document, a note, a conversation or any other content of an account.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..admin import grants, groups
from ..admin.deps import (
    ConsoleSession,
    Operator,
    reauth_due,
    require_console_session,
    require_permission,
)
from ..admin.permissions import ALL_PERMISSIONS, AREAS, normalize
from ..auth.deps import AppSettings, CsrfGuard, DbSession
from ..billing import ledger
from ..billing.plans import FREE_PLAN_ID, PAID_STATUSES
from ..config import Settings
from ..db.models import (
    AuditLog,
    Group,
    GroupMember,
    Job,
    PlanGrant,
    Subscription,
    User,
    utcnow,
)
from ..errors import ApiError
from ..workspace.documents import measure_storage
from ..workspace.layout import user_root

router = APIRouter(
    prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_console_session)]
)

UsersReader = Annotated[Operator, Depends(require_permission("users.read"))]
UsersWriter = Annotated[Operator, Depends(require_permission("users.write"))]
GroupsReader = Annotated[Operator, Depends(require_permission("groups.read"))]
GroupsWriter = Annotated[Operator, Depends(require_permission("groups.write"))]
AuditReader = Annotated[Operator, Depends(require_permission("audit.read"))]

# A group's name (docs/admin.md § Groups); the web app checks the same rule as it is typed.
GROUP_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
GROUP_NAME_MAX_CHARS = 32
# The highest `?page=` a list takes (docs/admin.md § Routes): its offset, at the largest
# page size, stays well inside the 64-bit integer PostgreSQL's OFFSET takes.
PAGE_MAX = 2**31 - 1
# The highest account id a users route takes: `users.id` is a 32-bit integer, and a larger
# id would fail in the database instead of finding no account (docs/admin.md § Routes).
USER_ID_MAX = 2**31 - 1
# How many of an account's newest jobs its page lists (docs/admin.md § Users and grants).
RECENT_JOBS = 5
# A grant's `reason`, the operator's note (docs/admin.md § Users and grants).
GRANT_REASON_MAX_CHARS = 200


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


# --- users and grants -----------------------------------------------------------------


def _on_plan(settings: Settings, plan_id: str, now: datetime):
    """The accounts whose current period at `now` is `plan_id`'s, as SQL — the period
    decision (`plans.resolve_period`) read over every account at once: a paid
    subscription running now on a configured plan, else the granted days of a configured
    plan, running or waiting in the bank, else Free."""
    configured = list(settings.plans)
    paid = select(Subscription.user_id).where(
        Subscription.status.in_(PAID_STATUSES),
        Subscription.period_start <= now,
        Subscription.period_end > now,
        Subscription.plan_id.in_(configured),
    )
    granted = select(PlanGrant.user_id).where(
        PlanGrant.plan_id.in_(configured),
        or_(PlanGrant.period_end > now, PlanGrant.days_banked > 0),
    )
    if plan_id == FREE_PLAN_ID:
        return and_(User.id.not_in(paid), User.id.not_in(granted))
    return or_(
        User.id.in_(paid.where(Subscription.plan_id == plan_id)),
        and_(User.id.not_in(paid), User.id.in_(granted.where(PlanGrant.plan_id == plan_id))),
    )


@router.get("/users")
async def list_users(
    operator: UsersReader,
    db: DbSession,
    settings: AppSettings,
    page: int = Query(default=1, ge=1, le=PAGE_MAX),
    page_size: int | None = Query(default=None, ge=1),
    q: str = "",
    plan: str | None = None,
):
    """Newest sign-up first, `ADMIN_PAGE_SIZE` a page unless the query asks for fewer.
    `q` keeps the accounts whose address or name contains it in any letter case; `plan`
    those whose current period is that plan's. Each row's storage is measured now, one
    after another; one that cannot be measured reads null."""
    plan_ids = [FREE_PLAN_ID, *settings.plans]
    if plan is not None and plan not in plan_ids:
        raise ApiError(422, "invalid_request", f"plan: one of {', '.join(plan_ids)}")
    size = min(page_size or settings.admin_page_size, settings.admin_page_size)
    now = utcnow()
    filters = []
    needle = q.strip()
    if needle:
        filters.append(
            or_(
                User.email.icontains(needle, autoescape=True),
                User.name.icontains(needle, autoescape=True),
            )
        )
    if plan is not None:
        filters.append(_on_plan(settings, plan, now))
    total = await db.scalar(select(func.count()).select_from(User).where(*filters))
    rows = list(
        await db.scalars(
            select(User)
            .where(*filters)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    )
    jobs = dict(
        (
            await db.execute(
                select(Job.user_id, func.count())
                .where(Job.user_id.in_([user.id for user in rows]))
                .group_by(Job.user_id)
            )
        ).all()
    )
    items = []
    for user in rows:
        balance = await ledger.read_balance(db, settings, user.id, now)
        try:
            usage = await measure_storage(
                db, settings, user.id, user_root(settings.workspaces_dir, user.id)
            )
            storage = usage.total
        except (ApiError, OSError):
            storage = None
        items.append(
            {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "plan": {"id": balance.period.plan.id, "source": balance.period.source},
                "credits_available": balance.available,
                "storage_bytes": storage,
                "jobs": jobs.get(user.id, 0),
                "created_at": user.created_at,
            }
        )
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "plans": [
            {
                "id": plan_id,
                "accounts": await db.scalar(
                    select(func.count()).select_from(User).where(_on_plan(settings, plan_id, now))
                ),
            }
            for plan_id in plan_ids
        ],
        "grant": {
            "credit_usd": float(settings.credit_usd),
            "valid_days": settings.admin_grant_valid_days,
            "max_credits": settings.admin_grant_max_credits,
            "max_days": settings.admin_grant_max_days,
        },
    }


@router.get("/users/{user_id}")
async def get_user(
    user_id: Annotated[int, Path(ge=1, le=USER_ID_MAX)],
    operator: UsersReader,
    db: DbSession,
    settings: AppSettings,
):
    """What the account has, never what it wrote: no route here returns a page of a
    result, a document, a note or a conversation."""
    user = await db.get(User, user_id)
    if user is None:
        raise ApiError(404, "invalid_request", "No such account")
    now = utcnow()
    balance = await ledger.read_balance(db, settings, user.id, now)
    period = balance.period
    subscription = await db.get(Subscription, user.id)
    # By source: the period's, then the packs bought, redeemed and granted, each with its
    # earliest expiry among the packs that still hold credits. Not the order a charge takes
    # them in, which is the period's first and then every pack by expiry, whatever its
    # source (docs/billing.md § Top-up packs).
    period_left = max(0, balance.remaining)
    sources = [
        {
            "source": "period",
            "remaining": period_left,
            "expires_at": period.end if period_left > 0 else None,
        }
    ]
    for source in (ledger.PACK_SOURCE_PAY, ledger.PACK_SOURCE_CODE, ledger.PACK_SOURCE_GRANT):
        held = [pack for pack in balance.packs if pack.source == source and pack.remaining > 0]
        sources.append(
            {
                "source": source,
                "remaining": sum(pack.remaining for pack in held),
                "expires_at": min((pack.expires_at for pack in held), default=None),
            }
        )
    try:
        usage = await measure_storage(
            db, settings, user.id, user_root(settings.workspaces_dir, user.id)
        )
        storage = {
            "bytes": usage.total,
            "quota_bytes": ledger.storage_limit(settings, period.plan),
        }
    except (ApiError, OSError):
        storage = None
    # The columns the page shows, named one by one: a job's prompt, error and pages are
    # never read here.
    recent = (
        await db.execute(
            select(
                Job.id,
                Job.filename,
                Job.status,
                Job.page_count,
                Job.pages_done,
                Job.created_at,
                Job.finished_at,
            )
            .where(Job.user_id == user.id)
            .order_by(Job.created_at.desc(), Job.id)
            .limit(RECENT_JOBS)
        )
    ).all()
    member_of = None
    if "groups.read" in operator.permissions:
        member_of = [
            {"id": group_id, "name": name, "builtin": builtin}
            for group_id, name, builtin in (
                await db.execute(
                    select(Group.id, Group.name, Group.builtin)
                    .join(GroupMember, GroupMember.group_id == Group.id)
                    .where(GroupMember.user_id == user.id)
                    .order_by(Group.builtin.desc(), Group.name)
                )
            ).all()
        ]
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "created_at": user.created_at,
        },
        "plan": {
            "id": period.plan.id,
            "source": period.source,
            "start": period.start,
            "end": period.end,
        },
        "subscription": None
        if subscription is None
        else {
            "status": subscription.status,
            "plan_id": subscription.plan_id,
            "period_end": subscription.period_end,
            "cancel_at_period_end": subscription.cancel_at_period_end,
        },
        "credits": {"available": balance.available, "sources": sources},
        "day_bank": grants.day_bank(balance.grant),
        "storage": storage,
        "jobs": {
            "total": await db.scalar(
                select(func.count()).select_from(Job).where(Job.user_id == user.id)
            ),
            "recent": [
                {
                    "id": job.id,
                    "filename": job.filename,
                    "status": job.status,
                    "page_count": job.page_count,
                    "pages_done": job.pages_done,
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                }
                for job in recent
            ],
        },
        "groups": member_of,
    }


class CreditGrant(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    kind: Literal["credits"]
    credits: int = Field(ge=1)
    valid_days: int | None = Field(default=None, ge=1)
    request_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=GRANT_REASON_MAX_CHARS)

    @field_validator("reason")
    @classmethod
    def _none_when_empty(cls, value: str | None) -> str | None:
        return value or None


class DayGrant(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    kind: Literal["days"]
    plan: str = Field(min_length=1)
    days: int = Field(ge=1)
    request_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=GRANT_REASON_MAX_CHARS)

    @field_validator("reason")
    @classmethod
    def _none_when_empty(cls, value: str | None) -> str | None:
        return value or None


@router.post(
    "/users/{user_id}/grants", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED
)
async def grant_to_user(
    user_id: Annotated[int, Path(ge=1, le=USER_ID_MAX)],
    body: Annotated[CreditGrant | DayGrant, Body(discriminator="kind")],
    operator: UsersWriter,
    db: DbSession,
    settings: AppSettings,
    response: Response,
):
    """Credits or days of a paid plan, through the ledger, the grant and its record in one
    transaction. 200, and nothing written, for a `request_id` sent again."""
    if isinstance(body, CreditGrant):
        if body.credits > settings.admin_grant_max_credits:
            raise ApiError(
                422,
                "invalid_request",
                f"credits: from 1 to {settings.admin_grant_max_credits}",
            )
        valid_days = body.valid_days or settings.admin_grant_valid_days
        if valid_days > settings.admin_grant_max_days:
            raise ApiError(
                422, "invalid_request", f"valid_days: from 1 to {settings.admin_grant_max_days}"
            )
        pack, available, made = await grants.grant_credits(
            db,
            settings,
            operator,
            user_id,
            credits=body.credits,
            valid_days=valid_days,
            request_id=body.request_id,
            reason=body.reason,
        )
        await db.commit()
        if not made:
            response.status_code = status.HTTP_200_OK
        return {
            "kind": "credits",
            "pack": {"id": pack.id, "credits": pack.credits, "expires_at": pack.expires_at},
            "credits_available": available,
        }
    if body.plan not in settings.plans:
        raise ApiError(
            422, "invalid_request", f"plan: one of {', '.join(settings.plans) or 'none'}"
        )
    if body.days > settings.admin_grant_max_days:
        raise ApiError(422, "invalid_request", f"days: from 1 to {settings.admin_grant_max_days}")
    plan_id, days, decided, made = await grants.grant_days(
        db,
        settings,
        operator,
        user_id,
        plan_id=body.plan,
        days=body.days,
        request_id=body.request_id,
        reason=body.reason,
    )
    await db.commit()
    if not made:
        response.status_code = status.HTTP_200_OK
    return {
        "kind": "days",
        "plan": plan_id,
        "days": days,
        "day_bank": grants.day_bank(decided.grant),
        "period": grants.period_summary(decided.period),
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
