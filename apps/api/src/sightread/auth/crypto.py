"""Credential cryptography (docs/auth.md).

Two rules, strictly separated:

- Anything we only ever *verify* (session tokens, API keys) is stored as a SHA-256 hash.
- The secrets we must *replay* (the user's Kano Proxy key, each provider connection's key,
  and a pending Kano Proxy connect's PKCE verifier) are stored as AES-256-GCM ciphertext,
  keyed by HKDF-SHA256 over ``SECRET_KEY`` with a per-purpose context string.

Nothing here ever logs or returns plaintext.
"""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KANO_PROXY_KEY_CONTEXT = b"kano-proxy-key-v1"
CONNECTION_KEY_CONTEXT = b"provider-connection-key-v1"
KANO_CONNECT_VERIFIER_CONTEXT = b"kano-connect-verifier-v1"
NONCE_BYTES = 12


def hash_token(token: str) -> str:
    """SHA-256 hex digest used for session tokens, API keys and OAuth grant tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aead(secret_key: str, context: bytes) -> AESGCM:
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=context,
    ).derive(secret_key.encode("utf-8"))
    return AESGCM(derived)


def encrypt_secret(secret_key: str, plaintext: str, context: bytes) -> bytes:
    """Return ``nonce || ciphertext``; a fresh random nonce is used for every encryption."""
    nonce = os.urandom(NONCE_BYTES)
    return nonce + _aead(secret_key, context).encrypt(nonce, plaintext.encode("utf-8"), None)


def decrypt_secret(secret_key: str, blob: bytes, context: bytes) -> str:
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return _aead(secret_key, context).decrypt(nonce, ciphertext, None).decode("utf-8")


def encrypt_kano_proxy_key(secret_key: str, plaintext: str) -> bytes:
    return encrypt_secret(secret_key, plaintext, KANO_PROXY_KEY_CONTEXT)


def decrypt_kano_proxy_key(secret_key: str, blob: bytes) -> str:
    return decrypt_secret(secret_key, blob, KANO_PROXY_KEY_CONTEXT)


def encrypt_connection_key(secret_key: str, plaintext: str) -> bytes:
    return encrypt_secret(secret_key, plaintext, CONNECTION_KEY_CONTEXT)


def decrypt_connection_key(secret_key: str, blob: bytes) -> str:
    return decrypt_secret(secret_key, blob, CONNECTION_KEY_CONTEXT)


def mask_secret(plaintext: str) -> str:
    """Display form: leading provider prefix, elision, last four characters."""
    if len(plaintext) <= 12:
        return "..." + plaintext[-2:]
    return f"{plaintext[:8]}...{plaintext[-4:]}"
