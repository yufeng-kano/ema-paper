"""`/api/kano` — connecting Kano Proxy, the built-in upstream (docs/auth.md § 6).

Connect and callback are browser navigations in a redirect chain, so every outcome is a
302 back to the web Agent page with a non-secret `?kano=` status, never a JSON error.
Logs carry the user id and the outcome only — never a code, state, verifier, the client
secret or a key (docs/auth.md § Logging rule).
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ..auth.crypto import (
    KANO_CONNECT_VERIFIER_CONTEXT,
    decrypt_secret,
    encrypt_kano_proxy_key,
    encrypt_secret,
    hash_token,
    mask_secret,
)
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..auth.sessions import SESSION_COOKIE, resolve_session
from ..config import Settings
from ..db.models import KanoConnectState, KanoProxyKey, utcnow
from ..upstream.kano import KanoConnectError, authorize_url, exchange_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kano", tags=["control"])

# Parked in the short-lived signed cookie sign-in uses, so the callback returns the user
# to the Agent page in the locale they started from (docs/auth.md § 6).
CONNECT_LOCALE_KEY = "kano_connect_locale"


def _back_to_agents(request: Request, settings: Settings, outcome: str) -> RedirectResponse:
    locale = str(request.session.pop(CONNECT_LOCALE_KEY, ""))
    return RedirectResponse(
        f"{settings.web_url_for_locale(locale)}/settings/agent?kano={outcome}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("")
async def kano_state(user: SessionUser, db: DbSession, settings: AppSettings):
    row = (
        await db.execute(select(KanoProxyKey).where(KanoProxyKey.user_id == user.id))
    ).scalar_one_or_none()
    return {
        "available": settings.kano_proxy_configured,
        "connected": row is not None,
        "masked": row.masked if row else None,
    }


@router.get("/connect")
async def connect(
    request: Request, user: SessionUser, db: DbSession, settings: AppSettings, locale: str = ""
):
    """Start a connect: a single-use state bound to this user, then Kano Proxy's consent."""
    request.session[CONNECT_LOCALE_KEY] = locale
    if not settings.kano_proxy_configured:
        logger.warning("kano connect user=%s: Kano Proxy is not configured", user.id)
        return _back_to_agents(request, settings, "error")

    now = utcnow()
    # Opportunistic cleanup: every connect tidies up this user's abandoned attempts.
    await db.execute(
        delete(KanoConnectState).where(
            KanoConnectState.user_id == user.id, KanoConnectState.expires_at <= now
        )
    )
    state = secrets.token_urlsafe(32)
    # 86 characters of the unreserved set — inside PKCE's 43-128 (RFC 7636 § 4.1).
    verifier = secrets.token_urlsafe(64)
    db.add(
        KanoConnectState(
            user_id=user.id,
            state_hash=hash_token(state),
            verifier_ciphertext=encrypt_secret(
                settings.secret_key, verifier, KANO_CONNECT_VERIFIER_CONTEXT
            ),
            created_at=now,
            expires_at=now + timedelta(seconds=settings.kano_connect_ttl_seconds),
        )
    )
    await db.commit()
    logger.info("kano connect user=%s: sent to consent", user.id)
    return RedirectResponse(
        authorize_url(settings, state, verifier), status_code=status.HTTP_302_FOUND
    )


@router.get("/callback")
async def callback(
    request: Request,
    db: DbSession,
    settings: AppSettings,
    code: str = "",
    state: str = "",
    error: str = "",
):
    """Finish a connect: consume the state, exchange the code, store the key."""
    session_token = request.cookies.get(SESSION_COOKIE)
    user = await resolve_session(db, session_token) if session_token else None
    if user is None:
        logger.warning("kano callback: no session")
        return _back_to_agents(request, settings, "error")

    # Consumed by the statement that finds it: a replay, or a second concurrent callback,
    # finds nothing. A state bound to another user does not match and is left alone.
    verifier_ciphertext = (
        await db.execute(
            delete(KanoConnectState)
            .where(
                KanoConnectState.state_hash == hash_token(state),
                KanoConnectState.user_id == user.id,
                KanoConnectState.expires_at > utcnow(),
            )
            .returning(KanoConnectState.verifier_ciphertext)
        )
    ).scalar_one_or_none()
    await db.commit()

    if error:
        outcome = "denied" if error == "access_denied" else "error"
        logger.info("kano callback user=%s: %s", user.id, outcome)
        return _back_to_agents(request, settings, outcome)
    if verifier_ciphertext is None or not state:
        logger.warning("kano callback user=%s: state rejected", user.id)
        return _back_to_agents(request, settings, "error")
    if not code or not settings.kano_proxy_configured:
        logger.warning("kano callback user=%s: no code or not configured", user.id)
        return _back_to_agents(request, settings, "error")

    verifier = decrypt_secret(
        settings.secret_key, verifier_ciphertext, KANO_CONNECT_VERIFIER_CONTEXT
    )
    try:
        key = await exchange_code(settings, code, verifier)
    except KanoConnectError as exc:
        logger.warning("kano callback user=%s: exchange failed: %s", user.id, exc.reason)
        return _back_to_agents(request, settings, "error")

    # Reconnecting replaces the stored key; Kano Proxy has already revoked the old one.
    row = (
        await db.execute(select(KanoProxyKey).where(KanoProxyKey.user_id == user.id))
    ).scalar_one_or_none()
    ciphertext = encrypt_kano_proxy_key(settings.secret_key, key)
    if row is None:
        db.add(KanoProxyKey(user_id=user.id, ciphertext=ciphertext, masked=mask_secret(key)))
    else:
        row.ciphertext = ciphertext
        row.masked = mask_secret(key)
        row.updated_at = utcnow()
    try:
        await db.commit()
    except IntegrityError:
        # Two connects of one user finishing at once; the other one stored its key.
        await db.rollback()
        logger.warning("kano callback user=%s: concurrent connect", user.id)
        return _back_to_agents(request, settings, "error")
    logger.info("kano callback user=%s: connected", user.id)
    return _back_to_agents(request, settings, "connected")


@router.delete("", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(user: SessionUser, db: DbSession) -> Response:
    """Forget the stored key here. It stays listed on Kano Proxy's Access page."""
    await db.execute(delete(KanoProxyKey).where(KanoProxyKey.user_id == user.id))
    await db.commit()
    logger.info("kano disconnect user=%s", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
