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
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .shell import GROUP_CLEANUP, ExecOutcome, ShellSession, supervisor

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
    # A VM the runner could not stop after a timeout it could not end inside: no command
    # runs there until Docker reports it gone (docs/sandbox.md § The runner).
    unstoppable: bool = False
    # A stop in progress: a kill inside the VM that fails then leaves the removal to it
    # rather than starting a second one (docs/sandbox.md § The runner).
    stopping: bool = False


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

    async def is_gone(self, user_id: int) -> bool:
        """Whether the VM is positively known to be absent or stopped. An inspection that
        fails is not "gone": it is unknown, and unknown keeps a closed sandbox closed."""
        return not await self.is_running(user_id)

    def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
        """The shell's argv and working directory."""
        raise NotImplementedError

    def on_timeout(self, user_id: int) -> Callable[..., Awaitable[None]] | None:
        """What kills a timed-out command where it runs; None when closing the host-side
        process is enough (the local backend). Called with the supervisor's `pid` as the
        VM numbers it, and `locked=True` when the caller already holds the sandbox lock."""
        return None

    def cleanup(self) -> str:
        """What the supervisor runs after every command to end what the command left
        running (docs/sandbox.md § The runner): the shell's process group, by default."""
        return GROUP_CLEANUP

    async def stop_vm(self, sandbox: UserSandbox) -> bool:
        """Remove the VM; True when the removal itself succeeded."""
        raise NotImplementedError

    # --- the shared behaviour -----------------------------------------------------------

    async def exec(
        self, user_id: int, session: str, command: str, *, timeout: float
    ) -> ExecOutcome:
        sandbox = self._user(user_id)
        async with sandbox.lock:
            if sandbox.unstoppable:
                if not await self.is_gone(user_id):
                    raise RuntimeError("the sandbox VM could not be stopped; no command runs there")
                sandbox.unstoppable = False
            await self.ensure_running(sandbox)
            shell = sandbox.sessions.get(session)
            if shell is None:
                await self._make_room(sandbox)
                argv, cwd = self.shell_argv(user_id)
                shell = ShellSession(
                    argv,
                    cwd=cwd,
                    max_bytes=self.settings.sandbox_output_max_bytes,
                    on_timeout=self.on_timeout(user_id),
                    start_timeout=self.settings.sandbox_docker_timeout_seconds,
                )
                sandbox.sessions[session] = shell
            if not shell.alive:
                # Under the lock, so the pid it reports is in the table before any other
                # shell's cleanup is told what to keep (docs/sandbox.md § The runner).
                await shell.start()
            # The user's live shells, by the pids they reported: what the cleanup after
            # this command must not end. The API runs one command per user at a time, so
            # a shell this list misses is one opened while nothing was running.
            keep = [other.pid for other in sandbox.sessions.values() if other.pid is not None]
            sandbox.session_used[session] = time.monotonic()
        sandbox.last_used = time.monotonic()
        sandbox.last_used_at = _now()
        outcome = await shell.run(command, timeout=timeout, keep=keep)
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
            # Killed inside the VM as `restart` does, under the lock `exec` holds: a
            # supervisor merely closed from the host would linger as a live shell the
            # sweep preserves, until the pid limit (docs/sandbox.md § Isolation).
            await shell.kill(locked=True)

    async def restart(self, user_id: int, session: str) -> None:
        """Kill that shell — the command inside the VM first — and forget it; the next
        command of the session opens a fresh one (docs/sandbox.md § The runner). Under
        the sandbox lock, so the shell table never changes under a stop walking it."""
        sandbox = self._user(user_id)
        async with sandbox.lock:
            shell = sandbox.sessions.pop(session, None)
            sandbox.session_used.pop(session, None)
            if shell is not None:
                await shell.kill(locked=True)

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
            await self._stop_locked(sandbox)

    async def _stop_locked(self, sandbox: UserSandbox) -> None:
        """End the shells and remove the VM — and confirm the removal: closing the
        host-side clients ends nothing inside, so a shell running a command is killed
        inside the VM first, by the pid the runner holds, and a removal that failed or
        timed out still leaves a VM that may be writing the workspace. Such a sandbox
        is marked unstoppable and takes no command until Docker positively reports it
        gone (docs/sandbox.md § The runner)."""
        sandbox.stopping = True
        try:
            for shell in sandbox.sessions.values():
                if shell.busy:
                    await shell.kill(locked=True)
                else:
                    await shell.close()
        finally:
            sandbox.stopping = False
        sandbox.sessions.clear()
        sandbox.session_used.clear()
        if not await self.stop_vm(sandbox) and not await self.is_gone(sandbox.user_id):
            sandbox.unstoppable = True
            logger.error("sandbox of user %d cannot be stopped; refusing commands", sandbox.user_id)
            raise RuntimeError("the sandbox VM could not be stopped")
        sandbox.started_at = None

    def _idle(self, sandbox: UserSandbox) -> bool:
        if any(shell.busy for shell in sandbox.sessions.values()):
            return False  # a command is running: not idle, whatever the clock says
        return time.monotonic() - sandbox.last_used >= self.settings.sandbox_idle_seconds

    async def reset(self, user_id: int) -> None:
        """Stop, and forget what was installed. Documents are untouched. One hold of the
        sandbox lock across the stop and the deletion: no command starts into a cache
        that is being removed."""
        sandbox = self._user(user_id)
        async with sandbox.lock:
            await self._stop_locked(sandbox)
            await asyncio.to_thread(_remove_cache, self.workspace(user_id) / CACHE_DIR)

    async def stop_idle(self) -> int:
        """Stop every VM idle past the configured window; returns how many."""
        stopped = 0
        for user_id, sandbox in list(self._users.items()):
            if not self._idle(sandbox) or sandbox.lock.locked():
                continue
            if not await self.is_running(user_id):
                continue
            async with sandbox.lock:
                # Decided again under the lock: an exec that slipped in since the look
                # above has marked its shell busy, and must not have its VM pulled away.
                if not self._idle(sandbox):
                    continue
                try:
                    await self._stop_locked(sandbox)
                except RuntimeError:
                    continue  # logged and marked by the stop; the sweep goes on
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

# What runs as pid 1 in the VM: an init that reaps. A command's leftovers the sweep kills
# are reparented to pid 1 and would sit there as zombies, against the VM's pid limit, if
# pid 1 were `sleep infinity` (docs/sandbox.md § The image). The same text is the image's
# CMD; the runner passes it so the two cannot drift.
REAPER = (
    "import os, time\n"
    "while True:\n"
    "    try:\n"
    "        os.wait()\n"
    "    except ChildProcessError:\n"
    "        time.sleep(1)\n"
)


# Kill every process in the VM but pid 1, the live shells and the exec wrappers they hang
# from (their ancestors) — and the caller's own chain. The live shells are the pids in
# `$__sr_keep_in`, which the runner sends: each supervisor reported its own pid before
# any command ran in it, so nothing a command can write names a process to keep. The VM
# is the scope a process cannot leave: a job that `setsid`s out of its shell's session is
# still here, and goes (docs/sandbox.md § The runner). Plain sh, no bashisms: it runs in
# the shell at the end of every command and in a separate `docker exec` after a timeout.
# Repeated until a pass finds nothing to kill: a process forking while the list is
# walked has children the snapshot missed, reparented to pid 1 once their parent is
# gone, and they go on the next pass. `$__sr_left` is what the last pass still had to
# kill — zero when the VM is verified clean; a VM not clean after fifty passes makes the
# caller fail closed. `$__sr_kill` names the kill command for the tests' fake process
# table; unset, it is the shell's own.
VM_SWEEP = """
__sr_pass=0
__sr_left=0
while [ "$__sr_pass" -lt 50 ]; do
    __sr_pass=$((__sr_pass + 1))
    __sr_keep=" 1 "
    for __sr_p in $$ $__sr_keep_in; do
        case "$__sr_p" in *[!0-9]*) continue;; esac
        while [ -n "$__sr_p" ] && [ "$__sr_p" != 0 ] && [ "$__sr_p" != 1 ]; do
            case "$__sr_keep" in *" $__sr_p "*) break;; esac
            __sr_keep="$__sr_keep$__sr_p "
            __sr_p=$(ps -o ppid= -p "$__sr_p" 2>/dev/null | tr -d ' ')
        done
    done
    __sr_left=0
    # Live processes only: a zombie waits for pid 1 to reap it and cannot be killed.
    __sr_live=$(ps -eo pid=,stat= 2>/dev/null | sed -n 's/^ *\\([0-9][0-9]*\\)  *[^Z ].*$/\\1/p')
    for __sr_p in $__sr_live; do
        case "$__sr_keep" in *" $__sr_p "*) continue;; esac
        ${__sr_kill:-kill} -KILL "$__sr_p" 2>/dev/null
        __sr_left=$((__sr_left + 1))
    done
    [ "$__sr_left" = 0 ] && break
done
"""

# Kills the shell `$1` names and everything under it, children first, inside the VM —
# `pgrep` from procps, which the image installs — then sweeps, keeping the shells `$2`
# names (docs/sandbox.md § The runner).
KILL_TREE = f"""
kill_tree() {{
    for child in $(pgrep -P "$1"); do kill_tree "$child"; done
    kill -KILL "$1" 2>/dev/null
}}
pid="$1"
case "$pid" in ''|*[!0-9]*) exit 3;; esac
__sr_keep_in="$2"
kill_tree "$pid"
# The shell leads its own process group: whatever left the tree (a disowned job that
# was reparented) is still in the group, and goes with it; the sweep takes the rest.
kill -KILL -- "-$pid" 2>/dev/null
{VM_SWEEP}
kill -0 "$pid" 2>/dev/null && exit 4
[ "$__sr_left" = 0 ] || exit 5
exit 0
"""

# A VM a previous runner left running: its shells are gone with that process, so there is
# nothing to keep, and whatever still runs is a command nobody will commit.
ADOPT_SWEEP = f"""
__sr_keep_in=""
{VM_SWEEP}
[ "$__sr_left" = 0 ] || exit 5
exit 0
"""


def _remove_cache(cache: Path) -> None:
    """Whatever sits at `.cache` goes: the directory, or the file or link a command put
    there in its place, removed without following it, so the next start can make the
    directory again (docs/sandbox.md § The runner)."""
    try:
        mode = cache.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode):
        shutil.rmtree(cache, ignore_errors=True)
    else:
        with contextlib.suppress(FileNotFoundError):
            cache.unlink()


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
            # Fail closed: a VM this runner does not know of would take commands beside
            # whatever a previous runner's shell left running in it, unswept.
            raise RuntimeError(f"could not list running sandboxes: {out.strip()}")
        for user_id in adopted_user_ids(out):
            sandbox = self._user(user_id)
            sandbox.started_at = time.monotonic()
            sandbox.last_used = time.monotonic()
            # The old runner's shells are gone; a command one was running is not, and
            # nobody will commit what it writes. Everything in the VM but pid 1 goes
            # before the VM is taken in; a VM where that cannot be confirmed is stopped.
            code, out = await _docker(
                "exec", self.container(user_id), "sh", "-c", ADOPT_SWEEP, timeout=30
            )
            if code != 0:
                logger.warning(
                    "could not end what runs in adopted sandbox %s (%s); stopping it",
                    self.container(user_id),
                    out.strip(),
                )
                async with sandbox.lock:
                    with contextlib.suppress(RuntimeError):
                        await self._stop_locked(sandbox)
        if self._users:
            logger.info("adopted %d running sandboxes", len(self._users))

    async def is_running(self, user_id: int) -> bool:
        code, out = await _docker(
            "inspect", "-f", "{{.State.Running}}", self.container(user_id), timeout=20
        )
        return code == 0 and out.strip() == "true"

    async def is_gone(self, user_id: int) -> bool:
        code, out = await _docker(
            "inspect", "-f", "{{.State.Running}}", self.container(user_id), timeout=20
        )
        if code == 0:
            return out.strip() != "true"
        # Docker's own "no such object" is a positive absence; anything else (the daemon
        # away, a timeout) is unknown.
        return "No such object" in out or "No such container" in out

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
            f"/tmp:rw,exec,size={settings.sandbox_tmp_size}",  # noqa: S108 - the VM's scratch
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
        args += [settings.sandbox_image, "python3", "-c", REAPER]
        code, out = await _docker(*args, timeout=180)
        if code != 0:
            logger.error("could not start sandbox for user %d: %s", sandbox.user_id, out.strip())
            raise RuntimeError("the sandbox VM could not be started")
        sandbox.started_at = time.monotonic()

    def shell_argv(self, user_id: int) -> tuple[list[str], str | None]:
        # The shell reports its pid first, so a timeout can be ended inside the VM
        # (`on_timeout`), where ending the client here would not reach.
        # `setsid` makes it a session and process-group leader, so everything a command
        # starts is in its group and can be killed as one (docs/sandbox.md § The runner);
        # `-w` keeps the exec's own process alive when setsid has to fork.
        return (
            [
                DOCKER,
                "exec",
                "-i",
                "-w",
                "/workspace",
                self.container(user_id),
                "setsid",
                "-w",
                "sh",
                "-c",
                supervisor(self.cleanup()),
            ],
            None,
        )

    def on_timeout(self, user_id: int) -> Callable[..., Awaitable[None]]:
        container = self.container(user_id)

        async def kill_inside(*, pid: int, locked: bool = False) -> None:
            # The user's other live shells, which the sweep after the kill must keep.
            keep = " ".join(
                str(other.pid)
                for other in self._user(user_id).sessions.values()
                if other.pid is not None and other.pid != pid
            )
            code, out = await _docker(
                "exec", container, "sh", "-c", KILL_TREE, "sh", str(pid), keep, timeout=30
            )
            if code != 0:
                # Fail closed: a command that cannot be confirmed dead must not go on
                # writing the workspace after the timeout is reported, so the VM goes.
                logger.warning(
                    "could not kill a timed-out command in %s (%s); stopping the VM",
                    container,
                    out.strip(),
                )
                sandbox = self._user(user_id)
                if sandbox.stopping:
                    return  # the stop under way removes the VM, or marks it unstoppable
                # The sandbox lock is not reentrant: a caller that holds it (restart)
                # says so, and the stop runs under its hold rather than waiting forever.
                async with sandbox.lock if not locked else contextlib.nullcontext():
                    # A stop that fails marks the sandbox unstoppable itself.
                    with contextlib.suppress(RuntimeError):
                        await self._stop_locked(sandbox)

        return kill_inside

    def cleanup(self) -> str:
        return VM_SWEEP

    async def stop_vm(self, sandbox: UserSandbox) -> bool:
        code, out = await _docker(
            "rm",
            "-f",
            self.container(sandbox.user_id),
            timeout=self.settings.sandbox_docker_timeout_seconds,
        )
        if code != 0:
            logger.warning("could not remove sandbox of user %d: %s", sandbox.user_id, out.strip())
        return code == 0


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
        return ["sh", "-c", supervisor(self.cleanup())], str(self.workspace(user_id))

    async def stop_vm(self, sandbox: UserSandbox) -> bool:
        self._running.discard(sandbox.user_id)
        return True


def build_backend(settings: Settings) -> Backend:
    if settings.sandbox_backend == "local":
        return LocalBackend(settings)
    return DockerBackend(settings)
