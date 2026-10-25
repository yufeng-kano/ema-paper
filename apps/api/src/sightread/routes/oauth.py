"""OAuth 2.1 authorization server endpoints (docs/auth.md § 4).

Two audiences, two error shapes: `/.well-known/*`, `/oauth/register` and `/oauth/token` are
read by OAuth clients and answer in RFC form (`{"error": "...", "error_description": "..."}`),
while `/oauth/authorize` is read by a human in a browser and answers in HTML. The app's own
error envelope stays on `/v1` and `/api`, where its clients live.

The consent page is served from here on purpose: the browser lands on the API origin
holding the session cookie, and a redirect through the Nuxt app would only add a hop that
can lose the request (docs/auth.md).
"""

from __future__ import annotations

import html
import secrets
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth.deps import AppSettings, DbSession
from ..auth.oauth_as import (
    CODE_CHALLENGE_METHOD,
    SCOPE,
    RegistrationError,
    exchange_code,
    get_client,
    issue_code,
    refresh_tokens,
    register_client,
)
from ..auth.oidc import POST_LOGIN_KEY
from ..auth.sessions import SESSION_COOKIE, resolve_session
from ..config import Settings
from ..db.models import OAuthClient, User

router = APIRouter(tags=["oauth"])

# The Starlette session cookie also carries the consent CSRF token and, while a sign-in is
# in flight, the authorize request to come back to.
CONSENT_STATE_KEY = "oauth_consent"

MCP_PATH = "/mcp"
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _oauth_error(status_code: int, error: str, description: str) -> JSONResponse:
    """RFC 6749 § 5.2 / RFC 7591 § 3.2.2 error body — what an OAuth client knows how to read."""
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "error_description": description},
        headers=NO_STORE,
    )


# The product mark, inlined rather than linked: this page is the one surface a user meets
# before they trust us with an account, and a mark that arrives one request later (or not at
# all, behind a strict connector CSP) is worse than no mark.
MARK = (
    "<svg class='mark' viewBox='0 0 32 32' width='32' height='32' aria-hidden='true'>"
    "<defs><linearGradient id='mark-fill' x1='0' y1='0' x2='0' y2='1'>"
    "<stop offset='0' stop-color='#4799eb'/><stop offset='1' stop-color='#0071e3'/>"
    "</linearGradient></defs>"
    "<rect width='32' height='32' rx='9' fill='url(#mark-fill)'/>"
    "<g fill='none' stroke='#ffffff' stroke-width='2.2' stroke-linecap='round'"
    " stroke-linejoin='round'>"
    "<path d='M8 11V9.5A1.5 1.5 0 0 1 9.5 8H11'/>"
    "<path d='M21 8h1.5A1.5 1.5 0 0 1 24 9.5V11'/>"
    "<path d='M24 21v1.5a1.5 1.5 0 0 1-1.5 1.5H21'/>"
    "<path d='M11 24H9.5A1.5 1.5 0 0 1 8 22.5V21'/>"
    "<path d='M11.5 12.5h6M11.5 16h9M11.5 19.5h4.5'/>"
    "</g>"
    "</svg>"
)

# The web app's tokens (apps/web/app/assets/css/main.css), narrowed to what one card needs.
# Duplicated on purpose: FastAPI renders this page, so it cannot import the Nuxt stylesheet,
# and pulling one in over the network would make consent depend on the web app being up.
# The face is the system's own, so nothing here waits on a font request a connector
# webview's CSP may refuse.
PAGE_STYLE = """
:root{color-scheme:light dark;
--bg:#f5f5f7;--surface:#ffffff;--sunken:#f5f5f7;--text:#1d1d1f;--muted:#6e6e73;
--accent:#0071e3;--accent-fg:#ffffff;--accent-hover:#0077ed;
--fill:rgb(118 118 128 / 12%);--fill-strong:rgb(118 118 128 / 20%);
--edge:rgb(0 0 0 / 8%);--shadow:rgb(0 0 0 / 10%)}
@media(prefers-color-scheme:dark){:root{
--bg:#000000;--surface:#1c1c1e;--sunken:#2c2c2e;--text:#f5f5f7;--muted:#98989d;
--accent:#0a84ff;--accent-hover:#409cff;
--fill:rgb(118 118 128 / 24%);--fill-strong:rgb(118 118 128 / 34%);
--edge:rgb(255 255 255 / 10%);--shadow:rgb(0 0 0 / 45%)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;
background:var(--bg);color:var(--text);
font:400 15px/1.5 -apple-system,BlinkMacSystemFont,system-ui,"Segoe UI",Roboto,
"Helvetica Neue","PingFang TC","Noto Sans TC",sans-serif;
-webkit-font-smoothing:antialiased}
.card{width:100%;max-width:26rem;padding:28px;border-radius:20px;background:var(--surface);
box-shadow:0 0 0 .5px var(--edge),0 14px 40px var(--shadow)}
.head{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.mark{flex:none}
h1{margin:0;font-size:20px;font-weight:600;letter-spacing:-0.01em}
p{margin:0 0 12px}
strong{font-weight:600}
.note{color:var(--muted);font-size:13px}
code{font:12px/1.5 ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
background:var(--fill);border-radius:4px;padding:1px 5px}
/* The billing line is the one fact a user can be surprised by later, so it is set apart
   from the sentence above it rather than buried at the end of it. */
.terms{margin:16px 0 0;padding:12px 14px;border-radius:14px;background:var(--sunken);
color:var(--muted);font-size:13px}
.actions{display:flex;gap:8px;margin-top:24px}
button{flex:1;height:36px;padding:0 16px;border:0;border-radius:999px;cursor:pointer;
font:500 14px/1 inherit;font-family:inherit;
transition:background-color 120ms cubic-bezier(.33,0,.2,1)}
@media(pointer:coarse){button{height:44px}}
button:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent);
outline-offset:1px}
.approve{background:var(--accent);color:var(--accent-fg)}
.approve:hover{background:var(--accent-hover)}
.deny{background:var(--fill);color:var(--text)}
.deny:hover{background:var(--fill-strong)}
"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    """The whole server-rendered surface: one self-contained card, no external requests.

    Everything the page needs — styles, mark, icons — ships in the document. A connector
    consent screen may be opened in a stripped-down webview, and a half-loaded page asking
    for account access is exactly the thing a user should not be asked to trust.
    """
    return HTMLResponse(
        status_code=status_code,
        content=(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<meta name='robots' content='noindex'>"
            # Named explicitly: this page is not Nuxt, so it inherits none of the web app's
            # head. Without these the client falls back to /favicon.ico and shows whatever
            # icon it already associates with the parent domain. `?v=` is the mark's
            # version, kept in step with the web app's (apps/web/nuxt.config.ts).
            "<link rel='icon' href='/favicon.svg?v=2' type='image/svg+xml'>"
            "<link rel='icon' href='/favicon.ico?v=2' sizes='48x48'>"
            "<link rel='apple-touch-icon' href='/apple-touch-icon.png?v=2'>"
            f"<title>{html.escape(title)}</title>"
            f"<style>{PAGE_STYLE}</style></head>"
            f"<body><main class='card'>"
            f"<div class='head'>{MARK}<h1>{html.escape(title)}</h1></div>"
            f"{body}</main></body></html>"
        ),
    )


# --- discovery ------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(settings: AppSettings) -> JSONResponse:
    """RFC 8414 metadata; the issuer is this API's public origin."""
    issuer = settings.app_url.rstrip("/")
    return JSONResponse(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "registration_endpoint": f"{issuer}/oauth/register",
            "scopes_supported": [SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": [CODE_CHALLENGE_METHOD],
        }
    )


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(settings: AppSettings) -> JSONResponse:
    """RFC 9728 metadata for the one protected resource: the MCP endpoint (docs/mcp.md).

    Also served under the path-suffixed form, which is where a client that starts from
    `<origin>/mcp` looks first.
    """
    issuer = settings.app_url.rstrip("/")
    return JSONResponse(
        {
            "resource": f"{issuer}{MCP_PATH}",
            "authorization_servers": [issuer],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }
    )


# --- dynamic client registration ------------------------------------------------------


@router.post("/oauth/register", status_code=201)
async def register(request: Request, db: DbSession, settings: AppSettings):
    """Open DCR (RFC 7591). Public clients only: no secret is issued, PKCE is mandatory."""
    try:
        body = await request.json()
    except ValueError:
        return _oauth_error(400, "invalid_client_metadata", "Body must be JSON")
    if not isinstance(body, dict):
        return _oauth_error(400, "invalid_client_metadata", "Body must be a JSON object")

    redirect_uris = body.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not all(isinstance(u, str) for u in redirect_uris):
        return _oauth_error(400, "invalid_redirect_uri", "redirect_uris must be a list of strings")

    name = body.get("client_name")
    try:
        client = await register_client(
            db,
            client_name=name if isinstance(name, str) else "",
            redirect_uris=redirect_uris,
            allow_localhost=settings.app_env == "local",
        )
    except RegistrationError as exc:
        return _oauth_error(400, exc.error, exc.description)
    await db.commit()

    return JSONResponse(
        status_code=201,
        headers=NO_STORE,
        content={
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        },
    )


# --- authorization ---------------------------------------------------------------------


async def _validated_request(
    db: DbSession, client_id: str, redirect_uri: str
) -> OAuthClient | HTMLResponse:
    """Client and redirect URI checks whose failure must *not* redirect (RFC 6749 § 4.1.2.1)."""
    client = await get_client(db, client_id) if client_id else None
    if client is None:
        return _page("Unknown client", "<p>This connector is not registered here.</p>", 400)
    if redirect_uri not in (client.redirect_uris or []):
        return _page(
            "Invalid redirect URI",
            "<p>The redirect URI does not match the one this connector registered.</p>",
            400,
        )
    return client


def _redirect_with_error(redirect_uri: str, state: str, error: str) -> RedirectResponse:
    query = {"error": error, **({"state": state} if state else {})}
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query)}", status_code=302)


async def _session_user(request: Request, db: DbSession) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    return await resolve_session(db, token) if token else None


def _sign_in_page(request: Request, settings: Settings) -> RedirectResponse | HTMLResponse:
    """Send the browser to Google, remembering the authorize request to come back to."""
    if settings.google_oidc_configured:
        request.session[POST_LOGIN_KEY] = request.url.path + (
            f"?{request.url.query}" if request.url.query else ""
        )
        return RedirectResponse("/api/auth/login", status_code=302)
    hint = (
        "<p>This deployment has no Google sign-in configured. Sign in through the web app "
        f"at <code>{html.escape(settings.web_url)}</code> (dev login), then reload this page.</p>"
        if settings.dev_login_enabled
        else "<p>Sign-in is unavailable on this deployment.</p>"
    )
    return _page("Sign in required", hint, 401)


@router.get("/oauth/authorize")
async def authorize(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    client_id: str = "",
    redirect_uri: str = "",
    response_type: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    state: str = "",
    scope: str = "",
):
    """Consent screen for a signed-in user; anything else is a redirect or an error page.

    `resource` (RFC 8707) is accepted and ignored: this AS protects exactly one resource.
    """
    client = await _validated_request(db, client_id, redirect_uri)
    if isinstance(client, HTMLResponse):
        return client
    if response_type != "code":
        return _redirect_with_error(redirect_uri, state, "unsupported_response_type")
    if not code_challenge or code_challenge_method != CODE_CHALLENGE_METHOD:
        return _redirect_with_error(redirect_uri, state, "invalid_request")

    user = await _session_user(request, db)
    if user is None:
        return _sign_in_page(request, settings)

    consent_token = secrets.token_urlsafe(16)
    request.session[CONSENT_STATE_KEY] = consent_token
    fields = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "scope": scope or SCOPE,
        "consent_token": consent_token,
    }
    hidden = "".join(
        f"<input type='hidden' name='{name}' value='{html.escape(value)}'>"
        for name, value in fields.items()
    )
    body = (
        f"<p><strong>{html.escape(client.client_name)}</strong> wants to parse documents as "
        f"<code>{html.escape(user.email)}</code>.</p>"
        "<p class='note'>It will be able to submit documents and read their results. It "
        "cannot see your Kano Proxy or provider keys, and you can revoke it at any time.</p>"
        "<p class='terms'>Every page it parses runs on your own Kano Proxy account or "
        "provider connection.</p>"
        f"<form method='post' action='/oauth/authorize'>{hidden}"
        "<div class='actions'>"
        "<button class='deny' name='decision' value='deny' type='submit'>Deny</button>"
        "<button class='approve' name='decision' value='approve' type='submit'>Approve</button>"
        "</div></form>"
    )
    return _page("Authorize connector", body)


@router.post("/oauth/authorize")
async def authorize_decision(
    request: Request,
    db: DbSession,
    consent_token: Annotated[str, Form()] = "",
    decision: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "",
    code_challenge: Annotated[str, Form()] = "",
):
    """Approve or deny. The consent token pairs with the session cookie against CSRF."""
    expected = request.session.pop(CONSENT_STATE_KEY, None)
    if not expected or not secrets.compare_digest(expected, consent_token):
        return _page("Expired form", "<p>Start the connector flow again.</p>", 400)

    client = await _validated_request(db, client_id, redirect_uri)
    if isinstance(client, HTMLResponse):
        return client
    user = await _session_user(request, db)
    if user is None:
        return _page("Sign in required", "<p>Your session expired. Sign in and retry.</p>", 401)
    if decision != "approve":
        return _redirect_with_error(redirect_uri, state, "access_denied")

    code = await issue_code(
        db, client=client, user=user, redirect_uri=redirect_uri, code_challenge=code_challenge
    )
    await db.commit()
    query = {"code": code, **({"state": state} if state else {})}
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(query)}", status_code=302)


# --- token -----------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(
    db: DbSession,
    grant_type: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    code_verifier: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
):
    """Code + PKCE or a refresh token in, an access/refresh pair out (docs/auth.md § 4)."""
    if not client_id or await get_client(db, client_id) is None:
        return _oauth_error(401, "invalid_client", "Unknown client_id")

    if grant_type == "authorization_code":
        if not code or not code_verifier:
            return _oauth_error(400, "invalid_request", "code and code_verifier are required")
        issued = await exchange_code(
            db,
            code=code,
            code_verifier=code_verifier,
            client_id=client_id,
            redirect_uri=redirect_uri,
        )
    elif grant_type == "refresh_token":
        if not refresh_token:
            return _oauth_error(400, "invalid_request", "refresh_token is required")
        issued = await refresh_tokens(db, refresh_token=refresh_token, client_id=client_id)
    else:
        return _oauth_error(
            400, "unsupported_grant_type", "Use authorization_code or refresh_token"
        )

    if issued is None:
        await db.rollback()
        return _oauth_error(400, "invalid_grant", "The grant is invalid, expired or already used")
    await db.commit()

    return JSONResponse(
        headers=NO_STORE,
        content={
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": SCOPE,
        },
    )
