"""The sandbox runner's HTTP contract (docs/sandbox.md § The runner).

Reachable on the compose network only, behind a shared token. The API and the worker are
its only callers; a user never talks to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from .backends import Backend, build_backend
from .config import Settings, get_settings

logger = logging.getLogger(__name__)

IDLE_SWEEP_SECONDS = 60.0


class ExecBody(BaseModel):
    session: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    command: str = Field(min_length=1, max_length=65_536)
    timeout_seconds: int = Field(default=120, ge=1)


class SessionBody(BaseModel):
    session: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


def create_app(settings: Settings | None = None, backend: Backend | None = None) -> FastAPI:
    settings = settings or get_settings()
    backend = backend or build_backend(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def sweep() -> None:
            while True:
                await asyncio.sleep(IDLE_SWEEP_SECONDS)
                try:
                    stopped = await backend.stop_idle()
                    if stopped:
                        logger.info("stopped %d idle sandboxes", stopped)
                except Exception:  # noqa: BLE001 - the sweep must outlive one failure
                    logger.exception("idle sweep failed")

        await backend.startup()
        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="sightread-sandbox", lifespan=lifespan)
    app.state.backend = backend
    app.state.settings = settings

    async def require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        presented = header.removeprefix("Bearer ").strip()
        if not settings.sandbox_token or not secrets.compare_digest(
            presented, settings.sandbox_token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad token")

    guard = [Depends(require_token)]

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/sandboxes/{user_id}", dependencies=guard)
    async def sandbox_status(user_id: int) -> dict:
        try:
            return await backend.status(user_id)
        except RuntimeError as exc:
            # Docker could not say: unavailable, never a stopped VM nothing can start.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    @app.post("/sandboxes/{user_id}/exec", dependencies=guard)
    async def sandbox_exec(user_id: int, body: ExecBody) -> dict:
        timeout = min(body.timeout_seconds, settings.sandbox_command_timeout_max)
        try:
            outcome = await backend.exec(user_id, body.session, body.command, timeout=timeout)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return {
            "output": outcome.output,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "truncated": outcome.truncated,
        }

    @app.post("/sandboxes/{user_id}/restart", dependencies=guard)
    async def sandbox_restart(user_id: int, body: SessionBody) -> dict:
        await backend.restart(user_id, body.session)
        return {"ok": True}

    @app.post("/sandboxes/{user_id}/stop", dependencies=guard)
    async def sandbox_stop(user_id: int) -> Response:
        try:
            await backend.stop(user_id)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/sandboxes/{user_id}/reset", dependencies=guard)
    async def sandbox_reset(user_id: int) -> Response:
        try:
            await backend.reset(user_id)
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.sandbox_port)  # noqa: S104
