"""Web session auth round trip and the dev-login guard (docs/auth.md § 1)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from sightread.auth.sessions import SESSION_COOKIE
from sightread.config import Settings

from .conftest import CSRF_HEADERS


async def test_healthz_needs_no_database(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_dev_login_round_trip(client: AsyncClient) -> None:
    login = await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    assert login.status_code == 200
    assert login.json()["user"]["email"] == "dev@localhost"

    cookie = login.cookies[SESSION_COOKIE]
    assert cookie
    set_cookie = login.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie

    me = await client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "dev@localhost"
    assert set(body) == {
        "user",
        "limits",
        "plan",
        "credits_remaining",
        "period_end",
        "chat_min_credits",
        "pack_checkout_available",
        "auto_convert",
        "default_view",
    }

    logout = await client.post("/api/auth/logout", headers=CSRF_HEADERS)
    assert logout.status_code == 204

    after = await client.get("/api/me")
    assert after.status_code == 401
    assert after.json() == {"error": {"type": "auth", "message": "Not signed in"}}


async def test_session_cookie_is_not_reusable_after_logout(client: AsyncClient) -> None:
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    stolen = client.cookies[SESSION_COOKIE]
    await client.post("/api/auth/logout", headers=CSRF_HEADERS)

    client.cookies.set(SESSION_COOKIE, stolen, domain="testserver")
    replayed = await client.get("/api/me")
    assert replayed.status_code == 401


async def test_anonymous_control_plane_is_rejected(client: AsyncClient) -> None:
    response = await client.get("/api/keys")
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "auth"


@pytest.mark.parametrize(
    ("app_env", "auth_dev_mode"),
    [("production", True), ("local", False)],
)
async def test_dev_login_route_does_not_exist(
    make_client, app_env: str, auth_dev_mode: bool
) -> None:
    client = make_client(app_env=app_env, auth_dev_mode=auth_dev_mode)
    response = await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    assert response.status_code == 404


def test_sign_in_returns_to_the_locale_it_started_from() -> None:
    """The Google round trip is the one leg that drops the locale (docs/auth.md § 1)."""
    settings = Settings(web_url="https://sightread.example", web_locale_prefixes="zh-TW,ja")

    assert settings.web_url_for_locale("zh-TW") == "https://sightread.example/zh-TW"
    assert settings.web_url_for_locale("ja") == "https://sightread.example/ja"


def test_an_unlisted_locale_falls_back_to_the_default_root() -> None:
    """The value arrives in a query string, so anything outside the allowlist is ignored
    rather than pasted into the destination — that is what stops it being an open
    redirect."""
    settings = Settings(web_url="https://sightread.example", web_locale_prefixes="zh-TW")

    for hostile in ("", "en", "..", "../../evil", "//evil.example", "https://evil.example"):
        assert settings.web_url_for_locale(hostile) == "https://sightread.example"
