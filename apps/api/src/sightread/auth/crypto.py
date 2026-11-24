"""Credential hashing and prefixed-token minting (docs/auth.md).

Anything we only ever *verify* — session tokens, API keys, OAuth grant tokens, upload and
document tickets — is stored as a SHA-256 hash. Nothing is ever replayed, so nothing is
stored encrypted: the upstream key is the operator's, in the environment (docs/auth.md § 3).
"""

from __future__ import annotations

import hashlib
import secrets

# The random part of an `sr_` / `srt_` / `srd_` token: token_urlsafe(24) is exactly 32
# url-safe characters.
_TOKEN_BYTES = 24


def hash_token(token: str) -> str:
    """SHA-256 hex digest every stored credential is looked up by."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(prefix: str) -> str:
    """A fresh `<prefix><32 url-safe chars>` token."""
    return prefix + secrets.token_urlsafe(_TOKEN_BYTES)
