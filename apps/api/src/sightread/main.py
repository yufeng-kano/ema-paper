"""FastAPI wiring: `/api` (control plane, including `/api/library`), `/v1` (data plane),
`/oauth` plus `/.well-known/*` (authorization server) and `/mcp` (MCP shell) — one app, one
process.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .auth.oidc import build_oauth
from .body_limit import BodyLimitMiddleware
from .config import Settings, get_settings
from .db.session import create_engine, create_sessionmaker
from .errors import install_error_handlers
from .mcp import mcp_session_manager, mount_mcp
from .routes import (
    attachments,
    billing,
    chat,
    control,
    extensions,
    kano,
    library,
    memories,
    oauth,
    v1,
)

# Signed cookie holding only the transient OIDC state/PKCE verifier during a login
# round trip. The durable credential is the server-side session row.
OIDC_STATE_COOKIE = "sr_oidc"
OIDC_STATE_MAX_AGE_SECONDS = 600


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # `create_engine` does not connect, so startup succeeds without a live database.
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        try:
            # The MCP session manager owns the task group every streamable-HTTP request
            # runs in, so it lives exactly as long as the app does.
            async with mcp_session_manager(app):
                yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Ema Paper", lifespan=lifespan)
    app.state.settings = settings
    app.state.oauth = build_oauth(settings)
    # Page previews render in this process: Poppler and a decode, a few at a time
    # (docs/api.md § Limits).
    app.state.preview_slots = asyncio.Semaphore(max(1, settings.page_preview_renders))

    # Before the session layer, so an oversized body is refused before anything reads it.
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=settings.json_body_max_bytes,
        parse_json_max_bytes=settings.parse_json_body_max_bytes,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=OIDC_STATE_COOKIE,
        max_age=OIDC_STATE_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=True,
    )

    install_error_handlers(app)

    app.include_router(control.router)
    app.include_router(billing.router)
    if settings.dev_login_enabled:
        app.include_router(control.dev_router)
    app.include_router(kano.router)
    app.include_router(library.router)
    app.include_router(chat.router)
    app.include_router(attachments.router)
    app.include_router(extensions.router)
    app.include_router(memories.router)
    app.include_router(v1.router)
    app.include_router(oauth.router)
    mount_mcp(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        """Liveness only — deliberately does not touch the database."""
        return {"ok": True}

    return app


app = create_app()
