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
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecOutcome:
    output: str
    exit_code: int | None
    timed_out: bool
    truncated: bool


class BoundedOutput:
    """Collects a command's output keeping its head and its tail within `max_bytes`."""

    def __init__(self, max_bytes: int) -> None:
        self._keep = max(max_bytes // 2 - 64, 1)
        self._head: list[bytes] = []
        self._head_size = 0
        self._tail: list[bytes] = []
        self._tail_size = 0
        self.omitted = 0

    def add(self, line: bytes) -> None:
        if self._head_size < self._keep:
            self._head.append(line)
            self._head_size += len(line)
            return
        self._tail.append(line)
        self._tail_size += len(line)
        while self._tail and self._tail_size > self._keep:
            dropped = self._tail.pop(0)
            self._tail_size -= len(dropped)
            self.omitted += len(dropped)

    def render(self) -> tuple[str, bool]:
        head = b"".join(self._head).decode("utf-8", errors="replace")
        tail = b"".join(self._tail).decode("utf-8", errors="replace")
        if not self.omitted:
            return head + tail, False
        return f"{head}\n[... {self.omitted} bytes omitted by the sandbox ...]\n{tail}", True


class ShellSession:
    def __init__(self, argv: list[str], *, cwd: str | None = None, max_bytes: int) -> None:
        self._argv = argv
        self._cwd = cwd
        self._max_bytes = max_bytes
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

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

    async def run(self, command: str, *, timeout: float) -> ExecOutcome:
        """One command, serialized per session. A timeout kills the shell: whatever it was
        doing cannot be trusted to have stopped, so the next command gets a fresh one."""
        async with self._lock:
            if not self.alive:
                await self.start()
            process = self._process
            assert process is not None and process.stdin is not None and process.stdout is not None
            marker = f"__SR_DONE_{secrets.token_hex(8)}__"
            # The command runs in the shell itself (not a subshell), so `cd` and exports
            # persist; the marker line follows with the status of the last command.
            script = f"{command}\nprintf '\\n{marker} %s\\n' \"$?\"\n"
            try:
                process.stdin.write(script.encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                await self.close()
                return ExecOutcome("the shell is gone; it will be restarted", None, False, False)

            collected = BoundedOutput(self._max_bytes)
            exit_code: int | None = None
            marker_bytes = marker.encode()
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        line = await process.stdout.readline()
                        if not line:
                            # The shell exited (the command ran `exit`, or it died).
                            await self.close()
                            break
                        if line.startswith(marker_bytes):
                            try:
                                exit_code = int(line[len(marker_bytes) :].strip() or 0)
                            except ValueError:
                                exit_code = None
                            break
                        collected.add(line)
            except TimeoutError:
                await self.close()
                output, truncated = collected.render()
                return ExecOutcome(output.rstrip("\n"), None, True, truncated)

            output, truncated = collected.render()
            return ExecOutcome(output.rstrip("\n"), exit_code, False, truncated)
