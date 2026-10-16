"""`python -m sightread.worker` — the process that drains the job queue (docs/jobs.md).

Same codebase and same image as the API, run as its own process. One job at a time per
worker: concurrency inside a job comes from the page fan-out, and concurrency across jobs
comes from running more workers, which the FIFO `SKIP LOCKED` claim already supports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .config import get_settings
from .db.session import create_engine, create_sessionmaker
from .jobs.chat_runner import run_turn
from .jobs.queue import (
    claim_next_job,
    claim_next_turn,
    fail_orphaned_turns,
    fail_turn,
    next_worker_generation,
    recover_unsettled_commands,
    release_worker_lock,
    requeue_job,
    requeue_orphaned_jobs,
    try_worker_lock,
    watch_worker_lock,
)
from .jobs.runner import fail_job, run_job
from .jobs.sweeper import sweep_forever
from .parsing.figures import discard_job_figures
from .sandbox.client import SandboxClient

logger = logging.getLogger(__name__)

# How long an idle worker waits before asking the queue again.
IDLE_POLL_SECONDS = 2.0
# How long after a cleanup of orphaned turns that did not go through the next try waits.
ORPHAN_RETRY_SECONDS = 30.0
# How often a worker that found the worker lock held asks again (a rolling restart).
WORKER_LOCK_RETRY_SECONDS = 5.0
# How often the worker checks that its lock's session is still alive.
WORKER_LOCK_PING_SECONDS = 5.0


def render_worker_count(configured: int) -> int:
    """`RENDER_WORKERS=0` means "CPU count", resolved here (docs/deployment.md)."""
    return configured if configured > 0 else (os.cpu_count() or 1)


async def run_worker(stop: asyncio.Event) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    # One worker per deployment, enforced before anything is claimed or recovered: the
    # recovery below treats every `running` row as a dead worker's, which only holds
    # when no other worker runs (docs/jobs.md). A second worker waits for the lock —
    # the one being replaced in a rolling restart releases it as it stops.
    lock = None
    while lock is None and not stop.is_set():
        if engine.dialect.name != "postgresql":
            break
        lock = await try_worker_lock(engine)
        if lock is None:
            logger.warning("another worker holds the worker lock; waiting for it")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=WORKER_LOCK_RETRY_SECONDS)
    if stop.is_set():
        if lock is not None:
            await release_worker_lock(lock)
        await engine.dispose()
        return
    # The lock is the session's: lost with it, so the worker stops — and the loop below
    # cancels the job or turn in flight — rather than run on beside a replacement.
    lock_watch = (
        asyncio.create_task(watch_worker_lock(lock, stop, WORKER_LOCK_PING_SECONDS))
        if lock is not None
        else None
    )
    sessionmaker = create_sessionmaker(engine)
    # This worker's generation, written on every job it claims and carried on every
    # write to one: the seconds between losing the lock and noticing are the
    # replacement's already, and nothing written in them may land (docs/jobs.md).
    async with sessionmaker() as db:
        generation = await next_worker_generation(db)
    render_count = render_worker_count(settings.render_workers)
    render_slots = asyncio.Semaphore(render_count)
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    sweeper = asyncio.create_task(sweep_forever(upload_dir, stop))
    sandbox = SandboxClient(settings) if settings.sandbox_configured else None
    # Retried until it goes through: a database or runner blip at start must not leave
    # the turns a dead worker held unfinished until the next restart (docs/jobs.md).
    orphans_pending = True
    orphans_tried_at = -ORPHAN_RETRY_SECONDS
    # The users whose orphaned turn could not be stopped yet: no turn runs at all until
    # the cleanup has gone through, and no parse of theirs, since a parse finishing
    # writes their tree too (docs/jobs.md § Chat turns).
    orphaned_users: set[int] = set()
    logger.info("worker started; %d render slots", render_count)
    prefer_turns = False

    try:
        while not stop.is_set():
            if orphans_pending and time.monotonic() - orphans_tried_at >= ORPHAN_RETRY_SECONDS:
                orphans_tried_at = time.monotonic()
                try:
                    async with sessionmaker() as db:
                        # The parses a dead worker held go back to the queue first, their
                        # crops with their partial progress (docs/jobs.md).
                        for orphan in await requeue_orphaned_jobs(db):
                            discard_job_figures(Path(settings.figures_dir), orphan)
                            logger.warning("requeued job %s a previous worker left running", orphan)
                        finished, left = await fail_orphaned_turns(db, sandbox, settings)
                    # Then the markers that still stand: commands the runner could not
                    # confirm over in turns that ended (docs/workspace.md § Revisions).
                    left |= await recover_unsettled_commands(settings, sandbox)
                except SQLAlchemyError:
                    logger.exception("could not finish orphaned chat turns; will retry")
                else:
                    if finished:
                        logger.warning(
                            "finished %d chat turn(s) a previous worker left running", finished
                        )
                    if left:
                        logger.warning(
                            "orphaned chat turn(s) of %d user(s) wait for the sandbox runner; "
                            "no turn runs until they are stopped",
                            len(left),
                        )
                    orphaned_users = left
                    orphans_pending = bool(left)
            job_id = None
            turn_id = None
            try:
                async with sessionmaker() as db:
                    # Two queues, one worker: a parse never starves a conversation and the
                    # reverse (docs/jobs.md § Chat turns).
                    if prefer_turns and not orphans_pending:
                        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
                    if turn_id is None:
                        job_id = await claim_next_job(
                            db, settings.max_jobs_per_user, orphaned_users, generation
                        )
                    if job_id is None and turn_id is None and not (prefer_turns or orphans_pending):
                        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
            except SQLAlchemyError:
                # A database blip must not end the worker; wait and ask again.
                logger.exception("could not claim work")
            prefer_turns = not prefer_turns

            if job_id is None and turn_id is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=IDLE_POLL_SECONDS)
                continue

            if turn_id is not None:
                logger.info("running chat turn %s", turn_id)
                running = asyncio.create_task(
                    run_turn(sessionmaker, settings, turn_id, render_slots)
                )
                shutdown = asyncio.create_task(stop.wait())
                done, _ = await asyncio.wait(
                    {running, shutdown}, return_when=asyncio.FIRST_COMPLETED
                )
                shutdown.cancel()
                if running not in done:
                    # A turn is never requeued: its tool calls may already have run
                    # (docs/jobs.md § Chat turns).
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    async with sessionmaker() as db:
                        await fail_turn(db, turn_id, "worker stopped")
                    logger.info("failed turn %s on shutdown", turn_id)
                elif running.exception() is not None:
                    # The runner answers for the turn's own faults; what escaped it is
                    # ours, and the row must not stay `running` with nobody driving it.
                    logger.error(
                        "turn %s raised past its runner", turn_id, exc_info=running.exception()
                    )
                    async with sessionmaker() as db:
                        await fail_turn(db, turn_id, "internal error")
                continue

            logger.info("running job %s", job_id)
            running = asyncio.create_task(
                run_job(sessionmaker, settings, job_id, render_slots, generation)
            )
            shutdown = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({running, shutdown}, return_when=asyncio.FIRST_COMPLETED)
            shutdown.cancel()

            if running not in done:
                # Shutting down mid-run: hand the job back to the queue rather than
                # leaving it stuck in `running` (docs/jobs.md § Retention covers the file).
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                async with sessionmaker() as db:
                    requeued = await requeue_job(db, job_id, generation)
                if requeued:
                    # Backstop only: `run_job`'s own finally already discarded this
                    # attempt's crops, waiting out any crop thread under the guard — by
                    # the time the cancelled task has been gathered above, nothing can
                    # write them back. A job whose result was committed before the cancel
                    # landed is not requeued, and keeps its crops (docs/jobs.md).
                    discard_job_figures(Path(settings.figures_dir), job_id)
                    logger.info("requeued job %s on shutdown", job_id)
                else:
                    # Finished before the cancel landed, or another worker's since this
                    # one lost the lock — its pages and crops are not this one's to take.
                    logger.info("job %s is finished or another worker's; not requeued", job_id)
            elif running.exception() is not None:
                # The runner answers for the document's faults; what escaped it is ours,
                # and the row must not stay `running` with nobody driving it, against
                # its user's cap. Failed, not requeued: the parse ran and billed.
                logger.error("job %s raised past its runner", job_id, exc_info=running.exception())
                try:
                    await fail_job(sessionmaker, settings, job_id, "internal error", generation)
                except Exception:  # noqa: BLE001 - the next start requeues what stays running
                    logger.exception("could not fail job %s", job_id)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        if lock_watch is not None:
            lock_watch.cancel()
            await asyncio.gather(lock_watch, return_exceptions=True)
        if lock is not None:
            await release_worker_lock(lock)
        await engine.dispose()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop.set)
    await run_worker(stop)


if __name__ == "__main__":
    asyncio.run(main())
