"""The worker's periodic tasks wait between their beats here, so a shutdown cuts the wait
short (docs/jobs.md)."""

from __future__ import annotations

import asyncio
import contextlib


async def pause(stop: asyncio.Event, seconds: float) -> bool:
    """Wait `seconds`, or until `stop` is set if that comes first; True when it is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    return stop.is_set()
