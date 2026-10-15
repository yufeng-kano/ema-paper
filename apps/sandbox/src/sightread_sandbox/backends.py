"""Where a user's shell runs (docs/sandbox.md).

`DockerBackend` keeps one container per user — a Kata microVM in production — with the
user's workspace at `/workspace`, and opens shells in it with `docker exec`. `LocalBackend`
runs bash in a directory and exists for the test suite alone: it isolates nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .shell import ExecOutcome, ShellSession

logger = logging.getLogger(__name__)

DOCKER = "docker"
CACHE_DIR = ".cache"


@dataclass
class UserSandbox:
    user_id: int
    sessions: dict[str, ShellSession] = field(default_factory=dict)
    # When each shell last ran a command: the longest idle one is evicted at the cap.
    session_used: dict[str, float] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)
    last_used_at: str | None = None
    started_at: float | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Backend:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._users: dict[int, UserSandbox] = {}

    def _user(self, user_id: int) -> UserSandbox:
        return self._users.setdefault(user_id, UserSandbox(user_id))

    async def startup(self) -> None:
        """What a backend does once when the runner starts; nothing, by default."""

    # --- what a backend decides ---------------------------------------------------------

    def workspace(self, user_id: int) -> Path:
        """The user's directory as this process sees it (docs/sandbox.md § Operations)."""
        return Path(self.settings.sandbox_workspaces_mount) / str(user_id)

    def host_workspace(self, user_id: int) -> Path:
        """The same directory as the Docker daemon sees it — what `-v` is given."""
        return Path(self.settings.sandbox_workspaces_dir) / str(user_id)

    async def ensure_running(self, sandbox: UserSandbox) -> None:
        raise NotImplementedError

    async def is_running(self, user_id: int) -> bool:
        raise NotImplementedError

    def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
        raise NotImplementedError

    async def stop_vm(self, sandbox: UserSandbox) -> None:
        raise NotImplementedError

    # --- the shared behaviour -----------------------------------------------------------

    async def exec(
        self, user_id: int, session: str, command: str, *, timeout: float
    ) -> ExecOutcome:
        sandbox = self._user(user_id)
        async with sandbox.lock:
            await self.ensure_running(sandbox)
            shell = sandbox.sessions.get(session)
            if shell is None:
                await self._make_room(sandbox)
                argv, cwd = self.shell_argv(user_id)
                shell = ShellSession(
                    argv, cwd=cwd, max_bytes=self.settings.sandbox_output_max_bytes
                )
                sandbox.sessions[session] = shell
            sandbox.session_used[session] = time.monotonic()
        sandbox.last_used = time.monotonic()
        sandbox.last_used_at = _now()
        outcome = await shell.run(command, timeout=timeout)
        sandbox.last_used = time.monotonic()
        sandbox.session_used[session] = sandbox.last_used
        return outcome

    async def _make_room(self, sandbox: UserSandbox) -> None:
        """Close the longest idle shell when the user is at the cap (docs/sandbox.md
        § Isolation). A shell running a command is not idle; with every shell busy the
        new one is refused."""
        while len(sandbox.sessions) >= self.settings.sandbox_max_shells:
            idle = sorted(
                (name for name, shell in sandbox.sessions.items() if not shell.busy),
                key=lambda name: sandbox.session_used.get(name, 0.0),
            )
            if not idle:
                raise RuntimeError("every shell of this sandbox is running a command")
            victim = idle[0]
            shell = sandbox.sessions.pop(victim)
            sandbox.session_used.pop(victim, None)
            await shell.close()

    async def restart(self, user_id: int, session: str) -> None:
        sandbox = self._user(user_id)
        shell = sandbox.sessions.pop(session, None)
        sandbox.session_used.pop(session, None)
        if shell is not None:
            await shell.close()

    async def status(self, user_id: int) -> dict:
        sandbox = self._users.get(user_id)
        running = await self.is_running(user_id)
        shells = sum(1 for shell in sandbox.sessions.values() if shell.alive) if sandbox else 0
        uptime = 0
        if running and sandbox and sandbox.started_at is not None:
            uptime = int(time.monotonic() - sandbox.started_at)
        return {
            "state": "running" if running else "stopped",
            "last_used_at": sandbox.last_used_at if sandbox else None,
            "shells": shells,
            "uptime_seconds": uptime,
        }

    async def stop(self, user_id: int) -> None:
        sandbox = self._user(user_id)
        async with sandbox.lock:
            for shell in sandbox.sessions.values():
                await shell.close()
            sandbox.sessions.clear()
            sandbox.session_used.clear()
            await self.stop_vm(sandbox)
            sandbox.started_at = None

    async def reset(self, user_id: int) -> None:
        """Stop, and forget what was installed. Documents are untouched."""
        await self.stop(user_id)
        cache = self.workspace(user_id) / CACHE_DIR
        await asyncio.to_thread(shutil.rmtree, cache, True)

    async def stop_idle(self) -> int:
        """Stop every VM idle past the configured window; returns how many."""
        stopped = 0
        for user_id, sandbox in list(self._users.items()):
            idle = time.monotonic() - sandbox.last_used
            if idle < self.settings.sandbox_idle_seconds or not await self.is_running(user_id):
                continue
            if sandbox.lock.locked() or any(shell.busy for shell in sandbox.sessions.values()):
                continue  # a command is running: not idle, whatever the clock says
            await self.stop(user_id)
            stopped += 1
        return stopped


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


# --- docker ---------------------------------------------------------------------------------


async def _docker(*args: str, timeout: float = 120.0) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        DOCKER,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return 1, "docker timed out"
    return process.returncode or 0, out.decode("utf-8", errors="replace")


CONTAINER_PREFIX = "sightread-sandbox-"


def adopted_user_ids(names: str) -> list[int]:
    """The user ids of the runner's containers in a `docker ps --format {{.Names}}` listing."""
    ids: list[int] = []
    for line in names.splitlines():
        name = line.strip()
        if name.startswith(CONTAINER_PREFIX) and name[len(CONTAINER_PREFIX) :].isdigit():
            ids.append(int(name[len(CONTAINER_PREFIX) :]))
    return ids


class DockerBackend(Backend):
    def container(self, user_id: int) -> str:
        return f"{CONTAINER_PREFIX}{user_id}"

    async def startup(self) -> None:
        """Take the VMs a previous runner left running into the table, last used now, so
        the idle sweep stops them on schedule (docs/sandbox.md § Lifecycle)."""
        code, out = await _docker(
            "ps", "--filter", f"name=^{CONTAINER_PREFIX}", "--format", "{{.Names}}", timeout=30
        )
        if code != 0:
            logger.warning("could not list running sandboxes: %s", out.strip())
            return
        for user_id in adopted_user_ids(out):
            sandbox = self._user(user_id)
            sandbox.started_at = time.monotonic()
            sandbox.last_used = time.monotonic()
        if self._users:
            logger.info("adopted %d running sandboxes", len(self._users))

    async def is_running(self, user_id: int) -> bool:
        code, out = await _docker(
            "inspect", "-f", "{{.State.Running}}", self.container(user_id), timeout=20
        )
        return code == 0 and out.strip() == "true"

    async def ensure_running(self, sandbox: UserSandbox) -> None:
        if await self.is_running(sandbox.user_id):
            if sandbox.started_at is None:
                sandbox.started_at = time.monotonic()
            return
        name = self.container(sandbox.user_id)
        # A stopped container of a previous run is removed rather than restarted: the
        # image may have moved on, and nothing of value lives outside /workspace.
        await _docker("rm", "-f", name, timeout=60)
        workspace = self.workspace(sandbox.user_id)
        await asyncio.to_thread(lambda: (workspace / CACHE_DIR).mkdir(parents=True, exist_ok=True))
        settings = self.settings
        host_workspace = self.host_workspace(sandbox.user_id)
        args = [
            "run",
            "-d",
            "--name",
            name,
            "--runtime",
            settings.sandbox_runtime,
            "--network",
            settings.sandbox_network,
            "--cpus",
            settings.sandbox_cpus,
            "--memory",
            settings.sandbox_memory,
            "--pids-limit",
            str(settings.sandbox_pids),
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,size=1g",  # noqa: S108 - the VM's own scratch space
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "-v",
            f"{host_workspace}:/workspace",
            "-w",
            "/workspace",
            "-e",
            f"UV_CACHE_DIR=/workspace/{CACHE_DIR}/uv",
            "-e",
            f"HOME=/workspace/{CACHE_DIR}/home",
        ]
        if settings.sandbox_index_url:
            args += ["-e", f"UV_INDEX_URL={settings.sandbox_index_url}"]
        args += [settings.sandbox_image, "sleep", "infinity"]
        code, out = await _docker(*args, timeout=180)
        if code != 0:
            logger.error("could not start sandbox for user %d: %s", sandbox.user_id, out.strip())
            raise RuntimeError("the sandbox VM could not be started")
        sandbox.started_at = time.monotonic()

    def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
        return (
            [
                DOCKER,
                "exec",
                "-i",
                "-w",
                "/workspace",
                self.container(user_id),
                "bash",
                "--norc",
                "--noprofile",
            ],
            None,
        )

    async def stop_vm(self, sandbox: UserSandbox) -> None:
        await _docker("rm", "-f", self.container(sandbox.user_id), timeout=120)


# --- local (tests only) ---------------------------------------------------------------------


class LocalBackend(Backend):
    """bash in the workspace directory. No isolation whatsoever — never deployed."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._running: set[int] = set()

    async def is_running(self, user_id: int) -> bool:
        return user_id in self._running

    async def ensure_running(self, sandbox: UserSandbox) -> None:
        workspace = self.workspace(sandbox.user_id)
        await asyncio.to_thread(lambda: (workspace / CACHE_DIR).mkdir(parents=True, exist_ok=True))
        if sandbox.user_id not in self._running:
            self._running.add(sandbox.user_id)
            sandbox.started_at = time.monotonic()

    def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
        return ["bash", "--norc", "--noprofile"], str(self.workspace(user_id))

    async def stop_vm(self, sandbox: UserSandbox) -> None:
        self._running.discard(sandbox.user_id)


def build_backend(settings: Settings) -> Backend:
    if settings.sandbox_backend == "local":
        return LocalBackend(settings)
    return DockerBackend(settings)
