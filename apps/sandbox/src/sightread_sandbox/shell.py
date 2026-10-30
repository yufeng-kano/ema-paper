"""A persistent shell session (docs/sandbox.md § The runner).

The bash tool's contract is a shell whose working directory, environment and background
processes carry from one command to the next, with `restart` as the only reset. One
`ShellSession` is one such shell: a long-lived `bash` child whose stdin we write commands
to and whose combined output we read back up to a sentinel line that carries the exit
code. The backend decides what the child is — `bash` in a directory, or `docker exec` into
the user's VM — and nothing else differs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from asyncio import LimitOverrunError
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

READ_CHUNK_BYTES = 65_536
# The one line a supervisor prints before it reads anything: its own pid, as the VM
# numbers it, which the runner holds for the kill inside the VM and names to the other
# shells' sweeps (docs/sandbox.md § The runner). Read before any command is sent, so no
# command can forge it.
PID_PREFIX = "__SR_PID "
# Where the supervisor and the runner's own scripts look programs up: the image's system
# directories alone. The VM's PATH starts in the workspace — the default Python
# environment, ~/.local/bin — which commands write, so a `ps`, `bash` or `pgrep` a command
# put there must not stand in for the system's in what frames and ends the commands
# (docs/sandbox.md § The runner). The commands themselves keep the VM's PATH.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class ExecOutcome:
    output: str
    exit_code: int | None
    timed_out: bool
    truncated: bool


class BoundedOutput:
    """Collects a command's output keeping its head and its tail within `max_bytes`,
    whatever shape the output has — one endless line is cut the same as a flood of them."""

    def __init__(self, max_bytes: int) -> None:
        self._keep = max(max_bytes // 2 - 64, 1)
        self._head = bytearray()
        self._tail = bytearray()
        self.omitted = 0

    def add(self, data: bytes) -> None:
        room = self._keep - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if not data:
            return
        self._tail += data
        excess = len(self._tail) - self._keep
        if excess > 0:
            del self._tail[:excess]
            self.omitted += excess

    def render(self) -> tuple[str, bool]:
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        if not self.omitted:
            return head + tail, False
        return f"{head}\n[... {self.omitted} bytes omitted by the sandbox ...]\n{tail}", True


# The default end-of-command cleanup: everything in the shell's process group but the
# shell. What the local backend uses — on a host, nothing wider would be safe. The VM
# backend sweeps the whole VM instead (backends.VM_SWEEP).
GROUP_CLEANUP = (
    "for __sr_pid in $(pgrep -g $$); do"
    ' [ "$__sr_pid" != "$$" ] && kill -KILL "$__sr_pid" 2>/dev/null; done'
)

# What the child bash runs for each command: the session's saved working directory and
# exported variables restored, the command sourced (so its `cd` and `export` land in
# this shell), its background jobs waited for, the state saved back. `$1` is the state
# directory. A command that `exit`s ends this shell with its status and saves nothing.
_RUN = """\
cd "$(cat "$1/cwd" 2>/dev/null || pwd)" 2>/dev/null || cd /
[ -f "$1/env" ] && . "$1/env"
. "$1/cmd.sh"
__sr_status=$?
wait
pwd > "$1/cwd"
export -p > "$1/env"
exit $__sr_status
"""


def supervisor(cleanup: str) -> str:
    """The persistent shell: a POSIX sh that owns each command's lifecycle, so the
    command can neither read the framing that follows it nor forge the status line
    (docs/sandbox.md § The runner). It reports its pid first, then reads one command at
    a time from stdin — the per-command marker the host chose, the pids of the shells
    the cleanup must keep (`$__sr_keep_in`), the command, the marker again — runs the
    command in a fresh bash child with stdin at /dev/null, and only once that child is
    gone runs `cleanup` and prints the marker with the status. Its own programs come
    from `SAFE_PATH`; the child is given the PATH the supervisor was started with."""
    return f"""\
__sr_path=$PATH
PATH={SAFE_PATH}
__sr_state=$(mktemp -d "${{TMPDIR:-/tmp}}/.sr-state-XXXXXX") || exit 1
trap 'rm -rf "$__sr_state"' EXIT
printf '{PID_PREFIX}%s\\n' "$$"
cat > "$__sr_state/run.sh" <<'__SR_RUN__'
{_RUN}__SR_RUN__
while IFS= read -r __sr_marker; do
    [ -n "$__sr_marker" ] || continue
    IFS= read -r __sr_keep_in || break
    : > "$__sr_state/cmd.sh"
    while IFS= read -r __sr_line; do
        [ "$__sr_line" = "$__sr_marker" ] && break
        printf '%s\\n' "$__sr_line" >> "$__sr_state/cmd.sh"
    done
    PATH=$__sr_path /bin/bash --norc --noprofile "$__sr_state/run.sh" "$__sr_state" </dev/null
    __sr_status=$?
    {cleanup}
    [ "${{__sr_left:-0}}" = 0 ] || exit 5
    printf '\\n%s %s\\n' "$__sr_marker" "$__sr_status"
done
"""


class ShellSession:
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        max_bytes: int,
        on_timeout: Callable[..., Awaitable[None]] | None = None,
        start_timeout: float = 30.0,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._max_bytes = max_bytes
        # What ends the command where it really runs: ending the host-side client does
        # not end a process inside the VM (docs/sandbox.md § The runner). Called with
        # the supervisor's pid, as the VM numbers it.
        self._on_timeout = on_timeout
        self._start_timeout = start_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        # The supervisor's pid where it runs, reported on its first line.
        self.pid: int | None = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def busy(self) -> bool:
        """Whether a command is running in this shell right now."""
        return self._lock.locked()

    async def start(self) -> None:
        await self.close()
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Its own process group, so a timeout can kill the whole tree.
            start_new_session=True,
        )
        assert self._process.stdout is not None
        # The pid line, before anything else is sent: a shell that does not report one
        # is not a supervisor, and no command runs in it.
        try:
            line = await asyncio.wait_for(self._process.stdout.readline(), self._start_timeout)
            text = line.decode("utf-8", errors="replace").strip()
            if not text.startswith(PID_PREFIX):
                raise ValueError(text)
            self.pid = int(text[len(PID_PREFIX) :])
        except (TimeoutError, ValueError, LimitOverrunError) as exc:
            await self.close()
            raise RuntimeError("the shell did not report its pid") from exc

    async def close(self) -> None:
        self.pid = None
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, 9)
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=5)

    async def kill(self, *, locked: bool = False) -> None:
        """End the shell and whatever it is running, where it runs: the backend's kill
        inside the VM first (closing the host-side client ends nothing there — and a
        client already gone says nothing about what runs inside), then the client. What
        `restart`, a dropped `exec` request and a status line that never came do
        (docs/sandbox.md). `locked` says the caller holds the sandbox lock, which the
        backend's fallback stop must then not take again."""
        if self._on_timeout is not None and self.pid is not None:
            with contextlib.suppress(Exception):
                await self._on_timeout(pid=self.pid, locked=locked)
        await self.close()

    async def run(self, command: str, *, timeout: float, keep: Iterable[int] = ()) -> ExecOutcome:
        """One command, serialized per session. A timeout kills the shell: whatever it was
        doing cannot be trusted to have stopped, so the next command gets a fresh one.
        `keep` names the user's other live shells, by the pids they reported, to the
        cleanup that follows the command (docs/sandbox.md § The runner)."""
        async with self._lock:
            if not self.alive:
                await self.start()
            process = self._process
            assert process is not None and process.stdin is not None and process.stdout is not None
            if "\0" in command:
                return ExecOutcome("the command contains a NUL byte", None, False, False)
            # The command goes to the supervisor framed by a marker it alone knows — the
            # command's own stdin is /dev/null, and the marker is read before the command
            # starts — so the status line that follows is the supervisor's, never the
            # command's (docs/sandbox.md § The runner). A marker that happens to appear
            # in the command (it cannot, at this length, but still) is drawn again.
            while True:
                marker = f"__SR_DONE_{secrets.token_hex(8)}__"
                if marker not in command:
                    break
            keep_line = " ".join(str(pid) for pid in keep)
            script = f"{marker}\n{keep_line}\n{command}\n{marker}\n"
            try:
                process.stdin.write(script.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                await self.kill()
                return ExecOutcome("the shell is gone; it will be restarted", None, False, False)

            collected = BoundedOutput(self._max_bytes)
            exit_code: int | None = None
            marker_bytes = marker.encode()
            pending = b""
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        # Fixed-size reads, never `readline`: a command that prints one
                        # newline-free value longer than the stream's line limit would
                        # otherwise fail the exec instead of being cut like any other
                        # flood (docs/sandbox.md § The runner).
                        chunk = await process.stdout.read(READ_CHUNK_BYTES)
                        if not chunk:
                            # The supervisor ended without its status line (a command
                            # killed it): what it left running is ended where it runs
                            # before the lost status is reported, like a timeout.
                            await self.kill()
                            break
                        pending += chunk
                        found = False
                        while True:
                            newline = pending.find(b"\n")
                            if newline == -1:
                                break
                            line, pending = pending[: newline + 1], pending[newline + 1 :]
                            if line.startswith(marker_bytes):
                                try:
                                    exit_code = int(line[len(marker_bytes) :].strip() or 0)
                                except ValueError:
                                    exit_code = None
                                found = True
                                break
                            collected.add(line)
                        if found:
                            break
                        # A partial line past the cap is output too; keep only the tail
                        # where a marker could still begin. The marker always starts a
                        # line, and a line this long is not the marker's, whatever its
                        # first byte — so the head goes, every time.
                        if len(pending) > self._max_bytes:
                            collected.add(pending[: -len(marker_bytes)])
                            pending = pending[-len(marker_bytes) :]
            except asyncio.CancelledError:
                # The request was dropped mid-command (the worker died): nothing will
                # commit what the command goes on to write, so it goes with the shell.
                await asyncio.shield(self.kill())
                raise
            except TimeoutError:
                await self.kill()
                if pending:
                    collected.add(pending)
                output, truncated = collected.render()
                return ExecOutcome(output.rstrip("\n"), None, True, truncated)

            output, truncated = collected.render()
            return ExecOutcome(output.rstrip("\n"), exit_code, False, truncated)
