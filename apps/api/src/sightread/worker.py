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
    requeue_job,
)
from .jobs.runner import run_job
from .jobs.sweeper import sweep_forever
from .parsing.figures import discard_job_figures
from .sandbox.client import SandboxClient

logger = logging.getLogger(__name__)

# How long an idle worker waits before asking the queue again.
IDLE_POLL_SECONDS = 2.0
# How long after a cleanup of orphaned turns that did not go through the next try waits.
ORPHAN_RETRY_SECONDS = 30.0


def render_worker_count(configured: int) -> int:
    """`RENDER_WORKERS=0` means "CPU count", resolved here (docs/deployment.md)."""
    return configured if configured > 0 else (os.cpu_count() or 1)


async def run_worker(stop: asyncio.Event) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
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
    logger.info("worker started; %d render slots", render_count)
    prefer_turns = False

    try:
        while not stop.is_set():
            if orphans_pending and time.monotonic() - orphans_tried_at >= ORPHAN_RETRY_SECONDS:
                orphans_tried_at = time.monotonic()
                try:
                    async with sessionmaker() as db:
                        finished, left = await fail_orphaned_turns(db, sandbox)
                except SQLAlchemyError:
                    logger.exception("could not finish orphaned chat turns; will retry")
                else:
                    if finished:
                        logger.warning(
                            "finished %d chat turn(s) a previous worker left running", finished
                        )
                    if left:
                        logger.warning(
                            "%d orphaned chat turn(s) wait for the sandbox runner; will retry",
                            left,
                        )
                    orphans_pending = left > 0
            job_id = None
            turn_id = None
            try:
                async with sessionmaker() as db:
                    # Two queues, one worker: a parse never starves a conversation and the
                    # reverse (docs/jobs.md § Chat turns).
                    if prefer_turns:
                        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
                    if turn_id is None:
                        job_id = await claim_next_job(db, settings.max_jobs_per_user)
                    if job_id is None and turn_id is None and not prefer_turns:
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
            running = asyncio.create_task(run_job(sessionmaker, settings, job_id, render_slots))
            shutdown = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({running, shutdown}, return_when=asyncio.FIRST_COMPLETED)
            shutdown.cancel()

            if running not in done:
                # Shutting down mid-run: hand the job back to the queue rather than
                # leaving it stuck in `running` (docs/jobs.md § Retention covers the file).
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
                async with sessionmaker() as db:
                    await requeue_job(db, job_id)
                # Backstop only: `run_job`'s own finally already discarded this attempt's
                # crops, waiting out any crop thread under the guard — by the time the
                # cancelled task has been gathered above, nothing can write them back.
                discard_job_figures(Path(settings.figures_dir), job_id)
                logger.info("requeued job %s on shutdown", job_id)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
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
