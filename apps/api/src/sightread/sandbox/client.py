"""HTTP client of the sandbox runner (docs/sandbox.md § The runner).

The API and the worker never touch Docker; they ask the runner, over the compose network
with a shared token, to run a command in the user's VM or to report on it. A runner that
is not configured or not reachable is `SandboxUnavailable` — the chat agent then runs
without its bash tool, and the Usage page says the sandbox is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import Settings

REQUEST_TIMEOUT_SECONDS = 30.0
# `exec` waits for the command itself plus the VM's boot; the runner enforces the
# command's own timeout and answers within it.
EXEC_GRACE_SECONDS = 60.0


class SandboxUnavailable(Exception):
    """No runner, or the runner did not answer. Never carries command output."""


@dataclass(frozen=True)
class ExecResult:
    output: str
    exit_code: int | None
    timed_out: bool
    truncated: bool


@dataclass(frozen=True)
class SandboxStatus:
    state: str  # stopped | starting | running | unavailable
    last_used_at: str | None
    shells: int
    uptime_seconds: int


class SandboxClient:
    def __init__(self, settings: Settings) -> None:
        self._base = settings.sandbox_url.rstrip("/")
        self._token = settings.sandbox_token
        self.configured = settings.sandbox_configured

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _call(self, method: str, path: str, *, json: dict | None, timeout: float) -> dict:
        if not self.configured:
            raise SandboxUnavailable("no sandbox runner is configured")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method, f"{self._base}{path}", json=json, headers=self._headers()
                )
        except httpx.HTTPError as exc:
            raise SandboxUnavailable("the sandbox runner was unreachable") from exc
        if response.status_code >= 400:
            raise SandboxUnavailable(f"the sandbox runner returned {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SandboxUnavailable("the sandbox runner answered unreadably") from exc
        return payload if isinstance(payload, dict) else {}

    async def exec(
        self, user_id: int, session: str, command: str, *, timeout_seconds: int
    ) -> ExecResult:
        payload = await self._call(
            "POST",
            f"/sandboxes/{user_id}/exec",
            json={"session": session, "command": command, "timeout_seconds": timeout_seconds},
            timeout=timeout_seconds + EXEC_GRACE_SECONDS,
        )
        return ExecResult(
            output=str(payload.get("output") or ""),
            exit_code=exit_code if isinstance(exit_code := payload.get("exit_code"), int) else None,
            timed_out=bool(payload.get("timed_out")),
            truncated=bool(payload.get("truncated")),
        )

    async def restart(self, user_id: int, session: str) -> None:
        await self._call(
            "POST",
            f"/sandboxes/{user_id}/restart",
            json={"session": session},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    async def status(self, user_id: int) -> SandboxStatus:
        payload = await self._call(
            "GET", f"/sandboxes/{user_id}", json=None, timeout=REQUEST_TIMEOUT_SECONDS
        )
        return SandboxStatus(
            state=str(payload.get("state") or "stopped"),
            last_used_at=payload.get("last_used_at")
            if isinstance(payload.get("last_used_at"), str)
            else None,
            shells=int(payload.get("shells") or 0),
            uptime_seconds=int(payload.get("uptime_seconds") or 0),
        )

    async def stop(self, user_id: int) -> None:
        await self._call(
            "POST", f"/sandboxes/{user_id}/stop", json=None, timeout=REQUEST_TIMEOUT_SECONDS
        )

    async def reset(self, user_id: int) -> None:
        await self._call(
            "POST", f"/sandboxes/{user_id}/reset", json=None, timeout=REQUEST_TIMEOUT_SECONDS
        )
