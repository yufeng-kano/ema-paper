"""Google OIDC client (docs/auth.md § 1).

Authorization Code + PKCE via Authlib. The transient `state`/`code_verifier`/`nonce` live
in a short-lived signed Starlette session cookie; the durable credential is the
server-side session row created after the callback.
"""

from __future__ import annotations

from authlib.integrations.starlette_client import OAuth
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..db.models import User

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
DEV_USER_EMAIL = "dev@localhost"
DEV_USER_SUB = "dev-local"

# Key in the transient signed cookie holding the request to resume after sign-in. Set by
# `/oauth/authorize` when a connector flow needs a web session first (docs/auth.md § 4).
POST_LOGIN_KEY = "post_login_path"

# The locale the visitor was reading the sign-in page in, parked across the Google round
# trip so the callback can return them to the same one. Google is the only leg that loses
# it: the locale otherwise lives in the URL (docs/auth.md § 1).
POST_LOGIN_LOCALE_KEY = "post_login_locale"


def build_oauth(settings: Settings) -> OAuth:
    oauth = OAuth()
    oauth.register(
        name="google",
        server_metadata_url=GOOGLE_METADATA_URL,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        client_kwargs={"scope": "openid email profile", "code_challenge_method": "S256"},
    )
    return oauth


async def upsert_user(
    db: AsyncSession,
    google_sub: str,
    email: str,
    name: str | None,
    picture: str | None = None,
) -> User:
    """Users are keyed by the Google `sub`; email, name and picture are refreshed on each
    sign-in."""
    user = (
        await db.execute(select(User).where(User.google_sub == google_sub))
    ).scalar_one_or_none()
    if user is None:
        user = User(google_sub=google_sub, email=email, name=name, picture=picture)
        db.add(user)
        await db.flush()
        return user
    user.email = email
    user.name = name
    user.picture = picture
    await db.flush()
    return user
