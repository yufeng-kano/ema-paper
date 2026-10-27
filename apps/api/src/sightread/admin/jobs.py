"""The console's jobs and usage (docs/admin.md § Overview, jobs and usage): a stuck job put
back in the queue with its audit row, and the overview's checks of Kano Proxy and Kano Pay.

What the queue is and whether the worker runs stay `jobs/queue.py`'s, the one module that
knows the queue is PostgreSQL; this calls it. The requeue writes into the caller's
transaction and never commits: the route commits once, the change and its audit row
together.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..billing import pay
from ..config import Settings
from ..db.models import Job
from ..errors import ApiError
from ..jobs import queue
from ..parsing.figures import discard_job_figures
from ..upstream.client import fetch_models
from . import audit

if TYPE_CHECKING:
    from .deps import Operator

logger = logging.getLogger(__name__)

# What a connection says when its client raised something it does not expect: the error
# itself, which may name the URL, goes to the API's log only.
UNEXPECTED = "Unexpected error (see the API's log)"


def _refused(reason: str, message: str) -> ApiError:
    return ApiError(409, "admin_conflict", message, details={"reason": reason})


async def requeue(
    db: AsyncSession, settings: Settings, operator: Operator, job_id: uuid.UUID
) -> Job:
    """Put a stuck job back in the queue, as a worker's start-up puts back an orphan
    (docs/jobs.md § Queue): under the job's row lock, its crops discarded before the
    commit — a worker claiming it a moment later must not lose a new attempt's — and its
    succeeded pages kept. Only a `running` job no live worker drives; anything else is
    refused with nothing changed and nothing recorded."""
    job = (
        await db.execute(select(Job).where(Job.id == job_id).with_for_update())
    ).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    if job.status != "running":
        raise _refused("not_running", f"The job is {job.status}, not running")
    worker = await queue.worker_status(db)
    if worker.state == "unknown":
        raise _refused("worker_unknown", "Whether the worker runs cannot be read here")
    if not queue.job_is_stuck(job, worker):
        raise _refused("not_stuck", "The live worker is running this job")

    before = {
        "user_id": job.user_id,
        "status": job.status,
        "worker_generation": job.worker_generation,
        "pages_done": job.pages_done,
    }
    await asyncio.to_thread(discard_job_figures, Path(settings.figures_dir), job.id)
    await queue.requeue_job(db, job.id, commit=False)
    await db.refresh(job)
    audit.record(
        db,
        actor_id=operator.user.id,
        permission="jobs.write",
        action="jobs.requeue",
        target_type="job",
        target_id=str(job.id),
        before=before,
        after={
            "user_id": job.user_id,
            "status": job.status,
            "worker_generation": job.worker_generation,
            "pages_done": job.pages_done,
        },
        ip=operator.ip,
    )
    return job


# --- the connections ------------------------------------------------------------------


async def kano_proxy_status(settings: Settings) -> dict:
    """Kano Proxy's model catalog, asked once — free, on the operator's key — and given
    `ADMIN_PROBE_TIMEOUT_SECONDS`: how many models it lists, how many of those the price
    table prices, and the parse model. A failure answers the client's own message, which
    never holds the key or the URL; one the client does not expect answers `UNEXPECTED` and
    is logged, never raised into the overview."""
    status = {"state": "ok", "message": None, "models": None, "priced": None, "parse_model": None}
    if not settings.kano_proxy_configured:
        return {**status, "state": "unconfigured"}
    try:
        async with asyncio.timeout(settings.admin_probe_timeout_seconds):
            catalog = await fetch_models(
                settings.kano_proxy_api_base,
                settings.kano_proxy_api_key,
                max_bytes=settings.upstream_response_max_bytes,
            )
    except ApiError as exc:
        return {**status, "state": "failed", "message": exc.message}
    except TimeoutError:
        timeout = settings.admin_probe_timeout_seconds
        return {**status, "state": "failed", "message": f"No answer within {timeout:g} seconds"}
    except Exception:  # noqa: BLE001 - one connection's failure never fails the overview
        logger.exception("the overview's Kano Proxy check failed unexpectedly")
        return {**status, "state": "failed", "message": UNEXPECTED}
    listed = {entry["id"] for entry in catalog}
    return {
        **status,
        "models": len(listed),
        "priced": len(listed & set(settings.model_prices)),
        "parse_model": settings.parse_model or None,
    }


async def kano_pay_status(settings: Settings) -> dict:
    """Kano Pay's catalog, asked once — read-only, no account named — and given
    `ADMIN_PROBE_TIMEOUT_SECONDS`, never the remembered one (`app.state.pay_catalog`),
    which would say Pay answers for minutes after it stopped: whether checkout is open and
    how many plans and packs are on sale, configured here and priced there. A failure the
    client does not expect — a catalog shaped so that reading it raises — answers
    `UNEXPECTED` and is logged, never raised into the overview."""
    status = {
        "state": "ok",
        "message": None,
        "checkout_enabled": None,
        "plans": None,
        "packs": None,
    }
    if not settings.kano_pay_configured:
        return {**status, "state": "unconfigured"}
    client = pay.PayClient(settings)
    client.timeout = settings.admin_probe_timeout_seconds
    try:
        async with asyncio.timeout(settings.admin_probe_timeout_seconds):
            catalog = await client.catalog()
    except (pay.PayRefused, pay.PayUnavailable) as exc:
        # Their messages name the status or the path, never the URL or the key.
        return {**status, "state": "failed", "message": str(exc)}
    except TimeoutError:
        timeout = settings.admin_probe_timeout_seconds
        return {**status, "state": "failed", "message": f"No answer within {timeout:g} seconds"}
    except Exception:  # noqa: BLE001 - one connection's failure never fails the overview
        logger.exception("the overview's Kano Pay check failed unexpectedly")
        return {**status, "state": "failed", "message": UNEXPECTED}
    return {
        **status,
        "checkout_enabled": catalog.checkout_enabled,
        "plans": len(pay.on_sale(settings, catalog)),
        "packs": len(pay.packs_on_sale(settings, catalog)),
    }
