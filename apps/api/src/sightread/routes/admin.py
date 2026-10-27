"""The operator console's routes, `/api/admin/*` (docs/admin.md § Routes).

Mounted by main.py only while `OWNER_EMAILS` names an address. Every route takes the
browser session alone (`require_console_session`, on the router) and checks its own
permission (`require_permission`); a mutation also needs `X-Requested-With`. The rules
and the writes are `sightread.admin`'s; this module reads and answers. No route returns
a document, a note, a conversation or any other content of an account.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Path, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import String, and_, case, cast, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..admin import codes as admin_codes
from ..admin import grants, groups
from ..admin import jobs as admin_jobs
from ..admin import settings as admin_settings
from ..admin.deps import (
    ConsoleSession,
    Operator,
    reauth_due,
    require_console_session,
    require_permission,
)
from ..admin.permissions import ALL_PERMISSIONS, AREAS, normalize
from ..auth.deps import AppSettings, CsrfGuard, DbSession
from ..billing import codes, ledger
from ..billing.plans import FREE_PLAN_ID, PAID_STATUSES
from ..config import Settings
from ..db.models import (
    COST_SOURCE_ESTIMATED,
    COST_SOURCE_OPENROUTER,
    COST_SOURCE_UNKNOWN,
    AuditLog,
    Group,
    GroupMember,
    Job,
    PlanGrant,
    RedeemCode,
    Redemption,
    Subscription,
    UsageLog,
    User,
    utcnow,
)
from ..errors import ApiError
from ..jobs import queue
from ..workspace.documents import measure_storage
from ..workspace.layout import user_root
from .control import usage_totals

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


# --- redeem codes ---------------------------------------------------------------------

CodesReader = Annotated[Operator, Depends(require_permission("codes.read"))]
CodesWriter = Annotated[Operator, Depends(require_permission("codes.write"))]
JobsReader = Annotated[Operator, Depends(require_permission("jobs.read"))]
JobsWriter = Annotated[Operator, Depends(require_permission("jobs.write"))]


async def _code_payloads(
    db: AsyncSession, settings: Settings, rows: list[RedeemCode]
) -> list[dict]:
    """`<code>`: masked — the plaintext is the create answer's alone — with its status now
    and who disabled it, the actor of its newest `codes.disable` record (null for a code
    disabled with the CLI, which records nothing, and for a deleted account)."""
    now = utcnow()
    disabled = [str(row.id) for row in rows if row.disabled_at is not None]
    newest = (
        select(AuditLog.target_id, func.max(AuditLog.id).label("id"))
        .where(
            AuditLog.target_type == "code",
            AuditLog.target_id.in_(disabled),
            AuditLog.action == "codes.disable",
        )
        .group_by(AuditLog.target_id)
        .subquery()
    )
    disablers = {
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
            "id": row.id,
            "masked": codes.mask(row.code, settings.redeem_code_prefix),
            "kind": row.kind,
            "credits": row.credits,
            "valid_days": row.valid_days,
            "plan_id": row.plan_id,
            "days": row.days,
            "uses": row.uses,
            "max_uses": row.max_uses,
            "redeem_by": row.redeem_by,
            "label": row.label,
            "status": codes.status(row, now, settings.plans),
            "created_at": row.created_at,
            "disabled_at": row.disabled_at,
            "disabled_by": disablers.get(row.id),
        }
        for row in rows
    ]


@router.get("/codes")
async def list_codes(
    operator: CodesReader,
    db: DbSession,
    settings: AppSettings,
    page: int = Query(default=1, ge=1, le=PAGE_MAX),
    page_size: int | None = Query(default=None, ge=1),
    status_filter: str = Query(default="active", alias="status"),
    q: str = "",
):
    """Newest first, `ADMIN_PAGE_SIZE` a page unless the query asks for fewer. `status`
    `active` keeps the codes redeemable now, `all` every one; `q` a label containing it in
    any letter case — never the code, which a search would give away a character at a
    time. Beside the page, the title's figures and what the create dialog is drawn from."""
    if status_filter not in ("active", "all"):
        raise ApiError(422, "invalid_request", "status: one of active, all")
    size = min(page_size or settings.admin_page_size, settings.admin_page_size)
    now = utcnow()
    active = codes.is_active(now, settings.plans)
    filters = []
    if status_filter == "active":
        filters.append(active)
    needle = q.strip()
    if needle:
        filters.append(RedeemCode.label.icontains(needle, autoescape=True))
    total = await db.scalar(select(func.count()).select_from(RedeemCode).where(*filters))
    rows = list(
        await db.scalars(
            select(RedeemCode)
            .where(*filters)
            .order_by(RedeemCode.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "items": await _code_payloads(db, settings, rows),
        "total": total,
        "page": page,
        "page_size": size,
        "summary": {
            "active": await db.scalar(select(func.count()).select_from(RedeemCode).where(active)),
            "labels": await db.scalar(
                select(func.count(distinct(RedeemCode.label))).where(active, RedeemCode.label != "")
            ),
            "redeemed_this_month": await db.scalar(
                select(func.count())
                .select_from(Redemption)
                .where(Redemption.redeemed_at >= month_start)
            ),
        },
        "create": {
            "plans": list(settings.plans),
            "max_count": settings.redeem_codes_max_count,
            "max_days": codes.MAX_DAYS,
            "valid_days": codes.DEFAULT_VALID_DAYS,
            "credit_usd": str(settings.credit_usd),
        },
    }


class CodesCreate(BaseModel):
    """`python -m sightread.codes create`'s fields: `kind` for `--credits` or `--plan`,
    `max_uses` for `--uses`, `redeem_by` a date. Their rules are `create_codes`', which the
    CLI shares (docs/admin.md § Redeem codes)."""

    kind: str
    credits: int | None = None
    valid_days: int = codes.DEFAULT_VALID_DAYS
    plan_id: str | None = None
    days: int | None = None
    count: int = 1
    max_uses: int = 1
    redeem_by: date | None = None
    label: str = ""


@router.post("/codes", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_codes(
    body: CodesCreate, operator: CodesWriter, db: DbSession, settings: AppSettings
):
    """The one answer that holds the codes in plaintext, as printed."""
    rows = await admin_codes.create_codes(
        db,
        operator,
        settings,
        kind=body.kind,
        count=body.count,
        max_uses=body.max_uses,
        redeem_by=None if body.redeem_by is None else codes.end_of_utc_day(body.redeem_by),
        label=body.label,
        credits=body.credits,
        valid_days=body.valid_days,
        plan_id=body.plan_id,
        days=body.days,
    )
    await db.commit()
    first = rows[0]
    return {
        "codes": [
            {"id": row.id, "code": codes.display(row.code, settings.redeem_code_prefix)}
            for row in rows
        ],
        "kind": first.kind,
        "credits": first.credits,
        "valid_days": first.valid_days,
        "plan_id": first.plan_id,
        "days": first.days,
        "max_uses": first.max_uses,
        "redeem_by": first.redeem_by,
        "label": first.label,
    }


@router.post("/codes/{code_id}/disable", dependencies=[CsrfGuard])
async def disable_code(code_id: int, operator: CodesWriter, db: DbSession, settings: AppSettings):
    """The code, disabled; one already disabled as it is, nothing recorded."""
    row = await admin_codes.disable_code(db, operator, code_id)
    await db.commit()
    return (await _code_payloads(db, settings, [row]))[0]


# --- overview, jobs and usage --------------------------------------------------------

JobsReader = Annotated[Operator, Depends(require_permission("jobs.read"))]
JobsWriter = Annotated[Operator, Depends(require_permission("jobs.write"))]

JOB_STATUSES = ("queued", "running", "succeeded", "failed")
# The newest audit records the overview shows, to a caller who holds `audit.read`: the audit
# list's first page, so `ADMIN_PAGE_SIZE` caps them as it caps every console read.
OVERVIEW_AUDIT_RECORDS = 10

# `usage_log` summed as `usage_totals` reads it (docs/api.md § Usage aggregates): billed,
# estimated and unpriced cost kept apart — an estimate is never added to a billed amount,
# and a call nobody priced is counted, never priced at 0.
USAGE_FIGURES = (
    func.count(),
    func.sum(UsageLog.prompt_tokens),
    func.sum(UsageLog.completion_tokens),
    func.sum(case((UsageLog.cost_source == COST_SOURCE_OPENROUTER, UsageLog.cost))),
    func.sum(case((UsageLog.cost_source == COST_SOURCE_ESTIMATED, UsageLog.cost))),
    func.count(case((UsageLog.cost_source == COST_SOURCE_ESTIMATED, 1))),
    func.count(case((UsageLog.cost_source == COST_SOURCE_UNKNOWN, 1))),
    func.sum(UsageLog.cache_read_tokens),
    func.sum(UsageLog.cache_write_tokens),
)


def _job_payload(job: Job, email: str | None, worker: queue.WorkerStatus) -> dict:
    """`<job>`: when, whose, how far, on what model, how it ended and why — the runner's
    fixed phrases, never the document — and whether a requeue would take it. Never the
    file's name, the prompt or a page's text."""
    # `page_count` is the document's; `pages_spec` picks among them ("" is all). A spec the
    # document cannot satisfy — a job the runner fails for it — counts no pages.
    try:
        pages = (
            None
            if job.page_count is None
            else len(queue.parse_pages_spec(job.pages_spec, job.page_count))
        )
    except ValueError:
        pages = None
    return {
        "id": job.id,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "user_id": job.user_id,
        "email": email,
        "status": job.status,
        "pages": pages,
        "pages_done": job.pages_done,
        "model": job.retry_model or job.model,
        "error": job.error,
        "worker_generation": job.worker_generation,
        "stuck": queue.job_is_stuck(job, worker),
    }


@router.get("/overview")
async def overview(operator: JobsReader, db: DbSession, settings: AppSettings):
    """What the deployment is doing (docs/admin.md § Overview, jobs and usage): the jobs
    waiting, running and lately failed, the worker, today's usage (the UTC day so far), the
    accounts, Kano Proxy and Kano Pay — and the newest audit records, only for a caller
    who also holds `audit.read`."""
    now = utcnow()
    window = settings.admin_failed_window_hours
    today = datetime.combine(now.date(), time.min, UTC)
    # The oldest queued job's `created_at`: its age, which a reader's retry and a requeue
    # keep, not how long it has waited this time (docs/admin.md).
    queued, oldest_queued = (
        await db.execute(
            select(func.count(), func.min(Job.created_at)).where(Job.status == "queued")
        )
    ).one()
    running = list(await db.scalars(select(Job).where(Job.status == "running")))
    failed, last_failed = (
        await db.execute(
            select(func.count(), func.max(Job.finished_at)).where(
                Job.status == "failed",
                Job.finished_at >= now - timedelta(hours=window),
            )
        )
    ).one()
    worker = await queue.worker_status(db)
    models = (
        await db.execute(
            select(UsageLog.model, *USAGE_FIGURES)
            .where(UsageLog.created_at >= today)
            .group_by(UsageLog.model)
            .order_by(UsageLog.model)
        )
    ).all()
    totals = (await db.execute(select(*USAGE_FIGURES).where(UsageLog.created_at >= today))).one()
    accounts = await db.scalar(select(func.count()).select_from(User))
    newest = None
    if "audit.read" in operator.permissions:
        newest = await list_audit(
            operator,
            db,
            settings,
            page=1,
            page_size=OVERVIEW_AUDIT_RECORDS,
            area=None,
            q="",
        )
    # Unknown off PostgreSQL, where no lock says whether a worker runs.
    stuck = None
    if worker.state != "unknown":
        stuck = sum(queue.job_is_stuck(job, worker) for job in running)
    # Every database read is done, so one that failed has answered without asking a catalog;
    # and the session's connection goes back to the pool before the probes, so a slow
    # upstream holds none. The two run together, each bounded by ADMIN_PROBE_TIMEOUT_SECONDS
    # and answering its own failure, never raising.
    await db.close()
    kano_proxy, kano_pay = await asyncio.gather(
        admin_jobs.kano_proxy_status(settings), admin_jobs.kano_pay_status(settings)
    )
    answer = {
        "day": now.date(),
        "accounts": accounts,
        "jobs": {
            "queued": queued,
            "running": len(running),
            "stuck": stuck,
            "oldest_queued_at": oldest_queued,
            "failed": failed,
            "failed_hours": window,
            "last_failed_at": last_failed,
        },
        "worker": asdict(worker),
        "usage": {
            "models": [{"model": row[0], **usage_totals(row[1:])} for row in models],
            "totals": usage_totals(totals),
        },
        "connections": {"kano_proxy": kano_proxy, "kano_pay": kano_pay},
    }
    if newest is not None:
        answer["audit"] = newest["items"]
    return answer


@router.get("/jobs")
async def list_jobs(
    operator: JobsReader,
    db: DbSession,
    settings: AppSettings,
    page: int = Query(default=1, ge=1, le=PAGE_MAX),
    page_size: int | None = Query(default=None, ge=1),
    status_filter: str | None = Query(default=None, alias="status"),
):
    """The jobs on record, newest first, `ADMIN_PAGE_SIZE` a page unless the query asks for
    fewer; `status` keeps one status. Beside the page, every status's count and the
    worker's state. The account's address only to a caller who also holds `users.read`."""
    if status_filter is not None and status_filter not in JOB_STATUSES:
        raise ApiError(422, "invalid_request", f"status: one of {', '.join(JOB_STATUSES)}")
    size = min(page_size or settings.admin_page_size, settings.admin_page_size)
    filters = [] if status_filter is None else [Job.status == status_filter]
    total = await db.scalar(select(func.count()).select_from(Job).where(*filters))
    rows = (
        await db.execute(
            select(Job, User.email)
            .join(User, User.id == Job.user_id)
            .where(*filters)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(size)
            .offset((page - 1) * size)
        )
    ).all()
    counts = dict((await db.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    worker = await queue.worker_status(db)
    # An account's address is the users area's to show; without it the page names the id.
    emails = "users.read" in operator.permissions
    return {
        "items": [_job_payload(job, email if emails else None, worker) for job, email in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "counts": {job_status: counts.get(job_status, 0) for job_status in JOB_STATUSES},
        "worker": asdict(worker),
    }


@router.post("/jobs/{job_id}/requeue", dependencies=[CsrfGuard])
async def requeue_stuck_job(
    job_id: uuid.UUID, operator: JobsWriter, db: DbSession, settings: AppSettings
):
    """A stuck job back in the queue, with its audit row in the same commit; anything
    else 404 or 409 `admin_conflict` (docs/admin.md § Overview, jobs and usage)."""
    job = await admin_jobs.requeue(db, settings, operator, job_id)
    await db.commit()
    email = (
        await db.scalar(select(User.email).where(User.id == job.user_id))
        if "users.read" in operator.permissions
        else None
    )
    return _job_payload(job, email, await queue.worker_status(db))


@router.get("/usage")
async def usage(
    operator: JobsReader,
    db: DbSession,
    settings: AppSettings,
    first: Annotated[date, Query(alias="from")],
    last: Annotated[date, Query(alias="to")],
):
    """`usage_log` per UTC day and model from `from` to `to`, both included, the newest
    day first, with the range's totals — every account's calls, of every agent."""
    if first > last:
        raise ApiError(422, "invalid_request", "from: a date no later than to")
    if (last - first).days + 1 > settings.admin_usage_max_days:
        raise ApiError(
            422,
            "invalid_request",
            f"The range spans more than {settings.admin_usage_max_days} days",
        )
    # Aware UTC bounds (the column refuses naive ones); the database session is pinned to
    # UTC, so `date()` is the UTC day on PostgreSQL and SQLite alike.
    window = (
        UsageLog.created_at >= datetime.combine(first, time.min, UTC),
        UsageLog.created_at <= datetime.combine(last, time.max, UTC),
    )
    day = func.date(UsageLog.created_at)
    rows = (
        await db.execute(
            select(day, UsageLog.model, *USAGE_FIGURES)
            .where(*window)
            .group_by(day, UsageLog.model)
            .order_by(day.desc(), UsageLog.model)
        )
    ).all()
    totals = (await db.execute(select(*USAGE_FIGURES).where(*window))).one()
    return {
        "from": first,
        "to": last,
        # PostgreSQL answers a date, SQLite a string.
        "rows": [
            {"date": str(row[0])[:10], "model": row[1], **usage_totals(row[2:])} for row in rows
        ],
        "totals": usage_totals(totals),
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


# --- settings ---------------------------------------------------------------------------

SettingsReader = Annotated[Operator, Depends(require_permission("settings.read"))]
SettingsWriter = Annotated[Operator, Depends(require_permission("settings.write"))]


class SettingChange(BaseModel):
    """A value for one key, the text `.env` would hold, trimmed; `confirm` once the
    operator has seen what it takes away (docs/admin.md § Settings)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    value: str = Field(max_length=admin_settings.VALUE_MAX_CHARS)
    confirm: bool = False


class SettingReset(BaseModel):
    confirm: bool = False


@router.get("/settings")
async def list_settings(operator: SettingsReader, db: DbSession, request: Request):
    """The twelve settings the database may override, where each comes from, and whether
    each secret of `.env` is set — never its value."""
    return await admin_settings.payload(db, request.app.state.settings)


@router.put("/settings/{key}", dependencies=[CsrfGuard])
async def put_setting(
    key: str,
    body: SettingChange,
    operator: SettingsWriter,
    db: DbSession,
    request: Request,
):
    """Store a value over `.env`'s, checked as at start: 422 in the checks' words, 409
    `impact` until confirmed when it takes away what accounts have. A value the key
    already holds writes nothing."""
    base = request.app.state.settings
    changed = await admin_settings.change(
        db,
        base,
        operator,
        key,
        body.value,
        confirm=body.confirm,
        free_accounts=lambda effective: _on_plan(effective, FREE_PLAN_ID, utcnow()),
    )
    if changed:
        await db.commit()
        request.app.state.runtime_settings.invalidate()
    return await admin_settings.payload(db, base)


@router.delete("/settings/{key}", dependencies=[CsrfGuard])
async def reset_setting(
    key: str,
    operator: SettingsWriter,
    db: DbSession,
    request: Request,
    body: SettingReset | None = None,
):
    """Restore a key to `.env`: its row deleted, checked and confirmed as a change is. A
    key the database does not override writes nothing."""
    base = request.app.state.settings
    changed = await admin_settings.change(
        db,
        base,
        operator,
        key,
        None,
        confirm=body is not None and body.confirm,
        free_accounts=lambda effective: _on_plan(effective, FREE_PLAN_ID, utcnow()),
    )
    if changed:
        await db.commit()
        request.app.state.runtime_settings.invalidate()
    return await admin_settings.payload(db, base)
