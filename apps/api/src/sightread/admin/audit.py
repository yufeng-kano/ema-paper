"""The audit log's one writer (docs/admin.md § The audit log).

Every change made through the console adds its row here, in the change's own transaction:
the caller commits once, so neither exists without the other. A request that changes
nothing records nothing — the caller decides that before calling.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import AuditLog


def record(
    db: AsyncSession,
    *,
    actor_id: int,
    permission: str | None,
    action: str,
    target_type: str,
    target_id: int,
    before: dict | None,
    after: dict | None,
    ip: str | None,
) -> None:
    """Add one row to the caller's transaction. `before` and `after` hold ids, amounts,
    flags, permission strings and group names as JSON — never an address, a person's
    name, content, a secret or a redeem code; who a row is about is joined from `users`
    when the log is read."""
    db.add(
        AuditLog(
            actor_id=actor_id,
            permission=permission,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            before=before,
            after=after,
            ip=ip,
        )
    )
