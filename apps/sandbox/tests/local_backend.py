"""A backend for the test suite alone: bash in the workspace directory, no VM, no Docker.

It isolates nothing, and no setting selects it: the tests hand it to `create_app`, which
otherwise always runs `DockerBackend` (docs/testing.md).
"""

from __future__ import annotations

import asyncio
import time

from sightread_sandbox.backends import CACHE_DIR, Backend, UserSandbox
from sightread_sandbox.config import Settings
from sightread_sandbox.shell import supervisor

# The end-of-command cleanup on a host: everything in the shell's process group but the
# shell — nothing wider would be safe there. The VM backend sweeps the whole VM instead
# (backends.VM_SWEEP).
GROUP_CLEANUP = (
    "for __sr_pid in $(pgrep -g $$); do"
    ' [ "$__sr_pid" != "$$" ] && kill -KILL "$__sr_pid" 2>/dev/null; done'
)


class LocalBackend(Backend):
    """bash in the workspace directory."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._running: set[int] = set()

    async def is_running(self, user_id: int) -> bool:
        return user_id in self._running

    async def ensure_running(self, sandbox: UserSandbox) -> None:
        sandbox.known_stopped = False
        workspace = self.workspace(sandbox.user_id)
        await asyncio.to_thread(lambda: (workspace / CACHE_DIR).mkdir(parents=True, exist_ok=True))
        if sandbox.user_id not in self._running:
            self._running.add(sandbox.user_id)
            sandbox.started_at = time.monotonic()

    def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
        return ["sh", "-c", supervisor(self.cleanup())], str(self.workspace(user_id))

    def cleanup(self) -> str:
        return GROUP_CLEANUP

    async def stop_vm(self, sandbox: UserSandbox) -> bool:
        self._running.discard(sandbox.user_id)
        return True
