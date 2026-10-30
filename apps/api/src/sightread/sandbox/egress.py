"""The sandbox's sites, written where the egress proxy reads them (docs/sandbox.md § Network).

The proxy holds no setting of the database and no secret: the worker, which reads the
settings in effect anyway, writes the list in effect to a file on a volume the two share.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from ..runtime_settings import RuntimeSettings

logger = logging.getLogger(__name__)

HEADER = "# The sandbox's sites, from SANDBOX_EGRESS_HOSTS (docs/sandbox.md § Network)\n"


def write_hosts(path: Path, hosts: tuple[str, ...]) -> None:
    """The file replaced whole — written beside it, then renamed over it — so the proxy
    never reads half a list. One entry a line; the proxy skips the `#` header."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(HEADER + "".join(f"{host}\n" for host in hosts), encoding="utf-8")
    # Readable by the proxy, which runs as an unprivileged user.
    partial.chmod(0o644)
    os.replace(partial, path)


async def write_hosts_forever(
    runtime: RuntimeSettings, path: Path, interval: float, stop: asyncio.Event
) -> None:
    """At start and every `interval` seconds after, the rows read again — on a reader of
    the caller's that nothing else refreshes, so a change saved on the page reaches the
    file within the interval — and the file replaced when the list changed. A write that
    fails is logged once and tried again at the next beat; the proxy keeps the list it
    read last."""
    written: tuple[str, ...] | None = None
    failing = False
    while not stop.is_set():
        runtime.invalidate()
        hosts = (await runtime.current()).egress_hosts
        if hosts != written:
            try:
                await asyncio.to_thread(write_hosts, path, hosts)
            except OSError:
                if not failing:
                    logger.warning("could not write the sandbox's sites to %s", path, exc_info=True)
                    failing = True
            else:
                written, failing = hosts, False
                logger.info("the sandbox's sites: %s", ", ".join(hosts) or "none")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
