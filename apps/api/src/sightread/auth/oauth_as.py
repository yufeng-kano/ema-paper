"""OAuth 2.1 authorization server state (docs/auth.md § 4).

Claude custom connectors expect an OAuth 2.1 AS in front of a remote MCP server, so this
app is one — the smallest one that satisfies the flow: open Dynamic Client Registration,
public clients only (PKCE S256, no client secret to leak), authorization codes bound to
the browser session of a signed-in user, opaque access and refresh tokens.

Every credential lands in `oauth_grants` as a SHA-256 hash, exactly like API keys and web
sessions: this server verifies tokens, it never has to replay them. Codes and tokens never
appear in logs or error messages (docs/auth.md § Logging rule).
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from authlib.oauth2.rfc7636 import create_s256_code_challenge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import OAuthClient, OAuthGrant, User, utcnow
from .crypto import hash_token

# Grant kinds stored in `oauth_grants.kind`.
CODE = "code"
ACCESS = "access"
REFRESH = "refresh"

CODE_TTL = timedelta(minutes=5)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)

# Two scopes (docs/auth.md § 4): `parse`, the `parse` tool and the job routes, and, since
# 2026-11-01, `library`, reading the Files library and saving a document's text and notes.
# A grant is made for both, the consent page saying both; one approved before `library`
# existed keeps `parse` alone through every refresh until it is approved again.
SCOPE = "parse"
LIBRARY_SCOPE = "library"
GRANTED_SCOPE = f"{SCOPE} {LIBRARY_SCOPE}"
CODE_CHALLENGE_METHOD = "S256"
CLIENT_ID_CHARS = 32


class RegistrationError(Exception):
    """A Dynamic Client Registration request we refuse (RFC 7591 § 3.2.2)."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


@dataclass
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str


def valid_redirect_uri(uri: str, allow_localhost: bool) -> bool:
    """`https://` only, plus `http://localhost` when the deployment is local (docs/auth.md).

    A registered redirect URI is where an authorization code is handed out, so plain http
    is refused everywhere a network can see it.
    """
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    return (
        allow_localhost
        and parsed.scheme == "http"
        and parsed.hostname in ("localhost", "127.0.0.1")
    )


async def register_client(
    db: AsyncSession, *, client_name: str, redirect_uris: list[str], allow_localhost: bool
) -> OAuthClient:
    """Open DCR (RFC 7591): anyone may register, but only as a public PKCE client."""
    if not redirect_uris:
        raise RegistrationError("invalid_redirect_uri", "At least one redirect_uri is required")
    for uri in redirect_uris:
        if not valid_redirect_uri(uri, allow_localhost):
            raise RegistrationError("invalid_redirect_uri", f"redirect_uri must use https: {uri}")

    client = OAuthClient(
        client_id=secrets.token_urlsafe(24)[:CLIENT_ID_CHARS],
        client_name=client_name[:255] or "MCP client",
        redirect_uris=redirect_uris,
    )
    db.add(client)
    await db.flush()
    return client


async def get_client(db: AsyncSession, client_id: str) -> OAuthClient | None:
    return (
        await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    ).scalar_one_or_none()


async def issue_code(
    db: AsyncSession,
    *,
    client: OAuthClient,
    user: User,
    redirect_uri: str,
    code_challenge: str,
) -> str:
    """Mint an authorization code bound to this client, user, redirect URI and PKCE challenge."""
    code = secrets.token_urlsafe(32)
    db.add(
        OAuthGrant(
            client_id=client.client_id,
            user_id=user.id,
            kind=CODE,
            token_hash=hash_token(code),
            pkce_challenge=code_challenge,
            redirect_uri=redirect_uri,
            scope=GRANTED_SCOPE,
            expires_at=utcnow() + CODE_TTL,
        )
    )
    await db.flush()
    return code


async def _live_grant(db: AsyncSession, kind: str, presented: str) -> OAuthGrant | None:
    """Look a grant up by hash and reject anything expired or already spent."""
    token_hash = hash_token(presented)
    row = (
        await db.execute(
            select(OAuthGrant).where(OAuthGrant.kind == kind, OAuthGrant.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.token_hash, token_hash):
        return None
    if row.revoked_at is not None or row.expires_at <= utcnow():
        return None
    return row


async def _issue_tokens(
    db: AsyncSession, *, client_id: str, user_id: int, scope: str
) -> IssuedTokens:
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(
        OAuthGrant(
            client_id=client_id,
            user_id=user_id,
            kind=ACCESS,
            token_hash=hash_token(access_token),
            scope=scope,
            expires_at=now + ACCESS_TTL,
        )
    )
    db.add(
        OAuthGrant(
            client_id=client_id,
            user_id=user_id,
            kind=REFRESH,
            token_hash=hash_token(refresh_token),
            scope=scope,
            expires_at=now + REFRESH_TTL,
        )
    )
    await db.flush()
    return IssuedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=int(ACCESS_TTL.total_seconds()),
        scope=scope,
    )


async def exchange_code(
    db: AsyncSession, *, code: str, code_verifier: str, client_id: str, redirect_uri: str | None
) -> IssuedTokens | None:
    """Authorization code + PKCE verifier → tokens. None means "invalid_grant"."""
    grant = await _live_grant(db, CODE, code)
    if grant is None or grant.client_id != client_id:
        return None
    if redirect_uri is not None and grant.redirect_uri != redirect_uri:
        return None
    if not grant.pkce_challenge or not hmac.compare_digest(
        create_s256_code_challenge(code_verifier), grant.pkce_challenge
    ):
        return None

    # A code is single use; burning it before the tokens exist keeps a replay useless.
    grant.revoked_at = utcnow()
    return await _issue_tokens(
        db, client_id=grant.client_id, user_id=grant.user_id, scope=grant.scope
    )


async def refresh_tokens(
    db: AsyncSession, *, refresh_token: str, client_id: str
) -> IssuedTokens | None:
    """Rotate a refresh token: the presented one dies, a new pair is issued."""
    grant = await _live_grant(db, REFRESH, refresh_token)
    if grant is None or grant.client_id != client_id:
        return None
    grant.revoked_at = utcnow()
    return await _issue_tokens(
        db, client_id=grant.client_id, user_id=grant.user_id, scope=grant.scope
    )


async def resolve_access_token(db: AsyncSession, presented: str) -> tuple[User, set[str]] | None:
    """The bearer path for `/v1/*` and `/mcp`: an access token resolves to its user and the
    scopes its grant holds."""
    grant = await _live_grant(db, ACCESS, presented)
    if grant is None:
        return None
    user = (await db.execute(select(User).where(User.id == grant.user_id))).scalar_one_or_none()
    return None if user is None else (user, set(grant.scope.split()))
