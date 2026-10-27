"""Redeem codes from the console (docs/admin.md § Redeem codes): creating and disabling
through `billing/codes.py` — the functions `python -m sightread.codes` calls, with the
same rules — each change with its audit row. They write into the caller's transaction
and never commit: the route commits once, the codes and their row together.

A record holds ids, amounts and dates, never a code — plain or masked — nor its label,
free text the operator typed, which may name a person.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import codes
from ..config import Settings
from ..db.models import RedeemCode
from ..errors import ApiError
from . import audit

if TYPE_CHECKING:
    from .deps import Operator


async def create_codes(
    db: AsyncSession,
    operator: Operator,
    settings: Settings,
    *,
    kind: str,
    count: int,
    max_uses: int,
    redeem_by: datetime | None,
    label: str,
    credits: int | None,
    valid_days: int | None,
    plan_id: str | None,
    days: int | None,
) -> list[RedeemCode]:
    """A batch, as the CLI's `create` makes it; what `create_codes` refuses is a 422. One
    `codes.create` record for the batch, its target the first code."""
    try:
        rows = await codes.create_codes(
            db,
            settings,
            kind=kind,
            count=count,
            max_uses=max_uses,
            redeem_by=redeem_by,
            label=label,
            credits=credits,
            valid_days=valid_days,
            plan_id=plan_id,
            days=days,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_request", str(exc)) from None
    rows.sort(key=lambda row: row.id)
    first = rows[0]
    if first.kind == codes.CREDITS:
        grant = {"credits": first.credits, "valid_days": first.valid_days}
    else:
        grant = {"plan_id": first.plan_id, "days": first.days}
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="codes.write",
        action="codes.create",
        target_type="code",
        target_id=first.id,
        before=None,
        after={
            "count": len(rows),
            "kind": first.kind,
            **grant,
            "max_uses": first.max_uses,
            "redeem_by": first.redeem_by.isoformat() if first.redeem_by else None,
            "ids": [row.id for row in rows],
        },
        ip=operator.ip,
    )
    return rows


async def disable_code(db: AsyncSession, operator: Operator, code_id: int) -> RedeemCode:
    """Dead from now on, under its row lock; recorded only when this disabled it — a code
    already disabled changes nothing."""
    row, disabled = await codes.disable_by_id(db, code_id)
    if row is None:
        raise ApiError(404, "invalid_request", "No such code")
    if disabled:
        audit.record(
            db,
            actor_id=operator.user.id,
            permission="codes.write",
            action="codes.disable",
            target_type="code",
            target_id=row.id,
            before={"disabled_at": None},
            after={"disabled_at": row.disabled_at.isoformat()},
            ip=operator.ip,
        )
        await db.flush()
    return row
