"""Credential hashing (docs/auth.md).

Anything we only ever *verify* — session tokens, API keys, OAuth grant tokens — is stored
as a SHA-256 hash. Nothing is ever replayed, so nothing is stored encrypted: the upstream
key is the operator's, in the environment (docs/auth.md § 3).
"""

from __future__ import annotations

import hashlib


def hash_token(token: str) -> str:
    """SHA-256 hex digest used for session tokens, API keys and OAuth grant tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
