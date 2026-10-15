"""CSRF pairing on the cookie-authenticated control plane (docs/api.md).

SameSite=Lax stops cross-site form posts from carrying the cookie; the custom header is
the second half of the pair, since a cross-origin form cannot set one.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

MUTATIONS = [
    ("post", "/api/keys", {"name": "x"}),
    ("delete", "/api/keys/1", None),
    ("put", "/api/agents/parse/settings", {"model": None}),
    ("delete", "/api/kano", None),
    ("post", "/api/auth/logout", None),
    ("post", "/api/auth/dev-login", None),
]


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
async def test_mutations_require_the_custom_header(
    signed_in: AsyncClient, method: str, path: str, body: dict | None
) -> None:
    response = await signed_in.request(method, path, json=body)
    assert response.status_code == 403
    assert response.json() == {
        "error": {"type": "auth", "message": "Missing X-Requested-With header"}
    }


async def test_reads_do_not_require_the_custom_header(signed_in: AsyncClient) -> None:
    assert (await signed_in.get("/api/me")).status_code == 200
    assert (await signed_in.get("/api/keys")).status_code == 200
    assert (await signed_in.get("/api/jobs")).status_code == 200
