"""Project API keys: creation, hashing at rest, bearer auth, revocation (docs/auth.md § 2)."""

from __future__ import annotations

import hashlib

from httpx import AsyncClient
from sqlalchemy import select

from sightread.db.models import ApiKey

from .conftest import CSRF_HEADERS


async def test_key_is_shown_once_and_stored_hashed(signed_in: AsyncClient, sessionmaker) -> None:
    created = await signed_in.post("/api/keys", json={"name": "laptop"}, headers=CSRF_HEADERS)
    assert created.status_code == 201
    body = created.json()
    plaintext = body["key"]

    assert plaintext.startswith("sr_")
    assert len(plaintext) == len("sr_") + 32
    assert body["prefix"] == f"sr_...{plaintext[-4:]}"

    async with sessionmaker() as db:
        row = (await db.execute(select(ApiKey))).scalar_one()
    assert row.key_hash == hashlib.sha256(plaintext.encode()).hexdigest()
    assert plaintext not in row.key_hash

    listed = await signed_in.get("/api/keys")
    assert listed.status_code == 200
    keys = listed.json()["keys"]
    assert len(keys) == 1
    assert "key" not in keys[0]
    assert keys[0]["prefix"] == body["prefix"]


async def test_bearer_key_authenticates_the_data_plane(signed_in: AsyncClient) -> None:
    created = await signed_in.post("/api/keys", json={"name": "ci"}, headers=CSRF_HEADERS)
    plaintext = created.json()["key"]

    # Reaching request validation (rather than 401) proves the key authenticated.
    accepted = await signed_in.post("/v1/parse", headers={"Authorization": f"Bearer {plaintext}"})
    assert accepted.status_code == 400
    assert accepted.json()["error"]["type"] == "invalid_request"

    rejected = await signed_in.post("/v1/parse", headers={"Authorization": "Bearer sr_wrongkey"})
    assert rejected.status_code == 401
    assert rejected.json()["error"]["type"] == "auth"

    # A session cookie is no data-plane credential.
    missing = await signed_in.post("/v1/parse")
    assert missing.status_code == 401
    assert missing.json()["error"]["type"] == "auth"
    assert missing.headers["www-authenticate"] == "Bearer"


async def test_revoked_key_stops_working(signed_in: AsyncClient) -> None:
    created = await signed_in.post("/api/keys", json={"name": "temp"}, headers=CSRF_HEADERS)
    key_id = created.json()["id"]
    plaintext = created.json()["key"]

    revoked = await signed_in.delete(f"/api/keys/{key_id}", headers=CSRF_HEADERS)
    assert revoked.status_code == 204

    denied = await signed_in.post("/v1/parse", headers={"Authorization": f"Bearer {plaintext}"})
    assert denied.status_code == 401

    listed = await signed_in.get("/api/keys")
    assert listed.json()["keys"] == []

    again = await signed_in.delete(f"/api/keys/{key_id}", headers=CSRF_HEADERS)
    assert again.status_code == 404
