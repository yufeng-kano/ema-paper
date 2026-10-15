"""The Kano Proxy connect flow's two calls to Kano Proxy (docs/auth.md § 6): the consent
URL the browser is sent to, and the server-to-server code exchange.

The exchange is the one place a Kano Proxy key arrives in plaintext; it goes straight back
to the caller for encryption. Nothing here logs a code, a verifier, the client secret or a
key — failures carry Kano Proxy's error code and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import urlencode

import httpx

from ..config import Settings
from .client import REQUEST_TIMEOUT_SECONDS, ResponseTooLarge, _read_body_capped

# A token response is a few hundred bytes; anything near this is not one.
TOKEN_RESPONSE_MAX_BYTES = 64 * 1024


class KanoConnectError(Exception):
    """The exchange failed. `reason` is safe to log: an OAuth error code or a fixed phrase."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def code_challenge(verifier: str) -> str:
    """PKCE S256: base64url(sha256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(settings: Settings, state: str, verifier: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.kano_proxy_client_id,
            "redirect_uri": settings.kano_redirect_uri,
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{settings.kano_proxy_base_url}/connect/authorize?{query}"


async def exchange_code(settings: Settings, code: str, verifier: str) -> str:
    """Trade an approved code for the user's Kano Proxy key (`sk-kano-proxy-…`)."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.kano_redirect_uri,
        "client_id": settings.kano_proxy_client_id,
        "client_secret": settings.kano_proxy_client_secret,
        "code_verifier": verifier,
    }
    try:
        async with (
            httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client,
            client.stream(
                "POST", f"{settings.kano_proxy_base_url}/api/connect/token", data=form
            ) as response,
        ):
            body = await _read_body_capped(response, TOKEN_RESPONSE_MAX_BYTES)
            status = response.status_code
    except httpx.HTTPError as exc:
        raise KanoConnectError("unreachable") from exc
    except ResponseTooLarge as exc:
        raise KanoConnectError("oversized response") from exc

    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise KanoConnectError(f"unreadable response ({status})")
    if status != 200:
        error = payload.get("error")
        # Only the documented codes are echoed into a log line; anything else is a status.
        known = {"invalid_request", "invalid_grant", "unsupported_grant_type", "invalid_client"}
        raise KanoConnectError(error if error in known else f"status {status}")

    token = payload.get("access_token")
    token_type = payload.get("token_type")
    if not isinstance(token, str) or not token.strip():
        raise KanoConnectError("no access_token")
    if not isinstance(token_type, str) or token_type.lower() != "bearer":
        raise KanoConnectError("unexpected token_type")
    return token.strip()
