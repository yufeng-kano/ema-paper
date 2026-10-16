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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

READ_CHUNK_BYTES = 65_536


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


def supervisor(cleanup: str, pid_file: str | None = None) -> str:
    """The persistent shell: a POSIX sh that owns each command's lifecycle, so the
    command can neither read the framing that follows it nor forge the status line
    (docs/sandbox.md § The runner). It reads one command at a time from stdin, framed by
    the per-command marker the host chose, runs it in a fresh bash child with stdin at
    /dev/null, and only once that child is gone runs `cleanup` and prints the marker
    with the status. `pid_file`, when given, is where it records its pid for a kill
    inside the VM."""
    record = f'echo "$$" > {pid_file}' if pid_file else ":"
    return f"""\
__sr_state=$(mktemp -d "${{TMPDIR:-/tmp}}/.sr-state-XXXXXX") || exit 1
trap 'rm -rf "$__sr_state"' EXIT
{record}
cat > "$__sr_state/run.sh" <<'__SR_RUN__'
{_RUN}__SR_RUN__
while IFS= read -r __sr_marker; do
    [ -n "$__sr_marker" ] || continue
    : > "$__sr_state/cmd.sh"
    while IFS= read -r __sr_line; do
        [ "$__sr_line" = "$__sr_marker" ] && break
        printf '%s\\n' "$__sr_line" >> "$__sr_state/cmd.sh"
    done
    bash --norc --noprofile "$__sr_state/run.sh" "$__sr_state" </dev/null
    __sr_status=$?
    {cleanup}
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
        on_timeout: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._max_bytes = max_bytes
        # What ends the command where it really runs: ending the host-side client does
        # not end a process inside the VM (docs/sandbox.md § The runner).
        self._on_timeout = on_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

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

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, 9)
        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=5)

    async def kill(self) -> None:
        """End the shell and whatever it is running, where it runs: the backend's kill
        inside the VM first (closing the host-side client ends nothing there), then the
        client. What `restart` and a dropped `exec` request do (docs/sandbox.md)."""
        if self.alive and self._on_timeout is not None:
            with contextlib.suppress(Exception):
                await self._on_timeout()
        await self.close()

    async def run(self, command: str, *, timeout: float) -> ExecOutcome:
        """One command, serialized per session. A timeout kills the shell: whatever it was
        doing cannot be trusted to have stopped, so the next command gets a fresh one."""
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
            script = f"{marker}\n{command}\n{marker}\n"
            try:
                process.stdin.write(script.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                await self.close()
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
                            # The shell exited (the command ran `exit`, or it died).
                            await self.close()
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
