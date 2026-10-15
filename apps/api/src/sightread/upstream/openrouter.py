"""Upstream vision client — the only module that talks to a vision upstream (OpenRouter
or a user-defined OpenAI-compatible connection) and the only one that ever holds a
decrypted user key (docs/project-structure.md).

Key material is never logged and never appears in raised messages.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.crypto import decrypt_connection_key, decrypt_openrouter_key
from ..db.models import (
    COST_SOURCE_ESTIMATED,
    COST_SOURCE_OPENROUTER,
    COST_SOURCE_UNKNOWN,
    OpenRouterKey,
    ProviderConnection,
)
from ..errors import ApiError

BASE_URL = "https://openrouter.ai/api/v1"
MODELS_URL = f"{BASE_URL}/models"
KEY_URL = f"{BASE_URL}/key"
CHAT_URL = f"{BASE_URL}/chat/completions"

# The two upstream wire flavours (docs/api.md § Upstreams). Both speak OpenAI Chat
# Completions; only OpenRouter takes its `usage: {include}` extension field.
KIND_OPENROUTER = "openrouter"
KIND_OPENAI = "openai"

MODELS_CACHE_TTL_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 20.0
# A page of dense text can take a vision model a while; this is the whole-request ceiling.
CHAT_TIMEOUT_SECONDS = 180.0
# Fallback body cap when a caller has no Settings at hand; the deployed value is
# UPSTREAM_RESPONSE_MAX_BYTES (config.py), threaded in by control routes and the worker.
DEFAULT_RESPONSE_MAX_BYTES = 33_554_432

DATA_URL_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

# usage_log.cost is NUMERIC(12,6): a reported cost beyond it cannot be stored, and is no
# price anyway (docs/parsing.md § Upstream usage).
COST_QUANTUM = Decimal("0.000001")
MAX_REPORTED_COST = Decimal("999999.999999")

# Model answers sometimes arrive wrapped in a fence despite the prompt.
_CODE_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")

# In-process catalog cache: (fetched_at, models). One hour per docs/api.md § GET /v1/models.
_models_cache: tuple[float, list[dict]] | None = None


def reset_models_cache() -> None:
    global _models_cache
    _models_cache = None


async def validate_api_key(candidate: str) -> bool:
    """Check a user-supplied OpenRouter key before storing it (docs/auth.md § 3).

    Returns False for a rejected key; raises `ApiError(upstream)` when OpenRouter itself
    is unhealthy, so a provider outage is never reported as a bad key.
    """
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(KEY_URL, headers={"Authorization": f"Bearer {candidate}"})
    except httpx.HTTPError as exc:
        raise ApiError(502, "upstream", "Could not reach OpenRouter to validate the key") from exc
    if response.status_code == 200:
        return True
    if response.status_code in (401, 403):
        return False
    raise ApiError(502, "upstream", f"OpenRouter returned {response.status_code} for key check")


async def fetch_image_models(now: float | None = None) -> list[dict]:
    """The model catalog filtered to image-input models, cached in process for an hour.

    The upstream catalog endpoint needs no credentials, so this never touches a user key.
    """
    global _models_cache
    now = time.monotonic() if now is None else now
    if _models_cache is not None and now - _models_cache[0] < MODELS_CACHE_TTL_SECONDS:
        return _models_cache[1]

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(MODELS_URL)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise ApiError(502, "upstream", "Could not reach the OpenRouter model catalog") from exc

    models = [
        model
        for model in payload.get("data", [])
        if "image" in (model.get("architecture") or {}).get("input_modalities", [])
    ]
    _models_cache = (now, models)
    return models


# --- provider connections (user-defined OpenAI-compatible endpoints) ------------------


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP a host literal denotes, canonicalizing the forms the resolver accepts.

    `ipaddress` only parses canonical text, but the system resolver also takes the
    shortened/decimal/hex IPv4 spellings (`127.1`, `2130706433`, `0x7f000001`) — exactly
    what an SSRF probe would use. `inet_aton` speaks the resolver's dialect, so anything
    it accepts is judged by the address it denotes, not the spelling.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(host)))
    except OSError:
        return None


# Module-level so tests can stub it: resolving a hostname is a real network side effect,
# and the suite must never touch DNS (docs/testing.md).
_getaddrinfo = socket.getaddrinfo


async def _resolved_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` currently resolves to; 400 when it resolves to nothing.

    Resolution runs in a worker thread: a slow or black-holed DNS name must not stall the
    event loop for every other request while the resolver waits it out.
    """
    try:
        infos = await asyncio.to_thread(_getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ApiError(400, "invalid_request", "base_url's host could not be resolved") from exc
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addresses:
        raise ApiError(400, "invalid_request", "base_url's host could not be resolved")
    return addresses


async def normalize_base_url(raw: str, app_env: str) -> str:
    """Validate and canonicalize a connection's base URL (docs/auth.md § 3).

    The app fetches this URL server-side, so outside local it must be https and must not
    denote a non-public address — a hosted deployment must not be usable as a probe into
    its own network. A hostname is resolved here and every answer must be global, which
    catches `/etc/hosts` aliases and attacker domains pointing inward; a later DNS change
    (rebinding) is accepted residual risk, documented in docs/auth.md. Userinfo is refused
    everywhere: `base_url` is stored and displayed in plaintext, so credentials must never
    ride inside it.
    """
    candidate = raw.strip().rstrip("/")
    try:
        parts = urlsplit(candidate)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for a malformed/out-of-range port
    except ValueError as exc:  # a bad IPv6 bracket or port is a typo, not a 500
        raise ApiError(400, "invalid_request", "base_url must be an http(s) URL") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise ApiError(400, "invalid_request", "base_url must be an http(s) URL")
    if parts.query or parts.fragment:
        raise ApiError(400, "invalid_request", "base_url must not carry a query or fragment")
    if parts.username or parts.password:
        raise ApiError(
            400,
            "invalid_request",
            "base_url must not contain credentials — the key is stored separately",
        )
    if app_env != "local":
        if parts.scheme != "https":
            raise ApiError(400, "invalid_request", "base_url must use https")
        if host == "localhost" or host.endswith(".localhost"):
            raise ApiError(400, "invalid_request", "base_url must be a public endpoint")
        literal = _literal_ip(host)
        addresses = [literal] if literal is not None else await _resolved_addresses(host)
        if any(not address.is_global for address in addresses):
            raise ApiError(400, "invalid_request", "base_url must be a public endpoint")
    return candidate


class ResponseTooLarge(Exception):
    """An upstream body exceeded the cap (docs/parsing.md § Upstream usage)."""


async def _read_body_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """Read a streamed response body, aborting past `max_bytes` — a user-controlled
    endpoint must not be able to exhaust memory with an unbounded reply."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


async def stored_connection_models(
    base_url: str,
    secret_key: str,
    ciphertext: bytes,
    max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
) -> list[dict]:
    """`fetch_connection_models` for a stored key — decryption stays inside this module."""
    return await fetch_connection_models(
        base_url, decrypt_connection_key(secret_key, ciphertext), max_bytes=max_bytes
    )


async def fetch_connection_models(
    base_url: str, api_key: str, max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES
) -> list[dict]:
    """The model catalog of an OpenAI-compatible endpoint — also the save-time key check.

    `GET {base_url}/models` is free on every OpenAI-format server we care about, so it
    doubles as validation without spending the user's money (docs/auth.md § 3).
    """
    try:
        async with (
            httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client,
            client.stream(
                "GET", f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
            ) as response,
        ):
            if response.status_code in (401, 403):
                raise ApiError(400, "invalid_request", "The endpoint rejected this API key")
            if response.status_code >= 400:
                raise ApiError(
                    502, "upstream", f"The endpoint returned {response.status_code} for /models"
                )
            body = await _read_body_capped(response, max_bytes)
    except httpx.HTTPError as exc:
        raise ApiError(502, "upstream", "Could not reach the connection's endpoint") from exc
    except ResponseTooLarge as exc:
        raise ApiError(502, "upstream", "The endpoint's model list exceeded the size cap") from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ApiError(502, "upstream", "The endpoint returned a non-JSON model list") from exc
    # An upstream answering `[]` (or any non-object) is a broken endpoint, not our bug.
    if not isinstance(payload, dict) or not isinstance(payload.get("data") or [], list):
        raise ApiError(502, "upstream", "The endpoint returned an unreadable model list")

    models = []
    for entry in payload.get("data") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            name = entry.get("name")
            models.append({"id": entry["id"], "name": name if isinstance(name, str) else None})
    return models


# --- vision calls ---------------------------------------------------------------------


class UpstreamError(Exception):
    """An upstream call failed. `fatal` marks a failure that will repeat for every page,
    so the caller should abort the whole job instead of burning pages on it; `transient`
    marks one the same request may not repeat, so the caller retries it
    (docs/parsing.md § Upstream usage)."""

    def __init__(self, message: str, *, fatal: bool = False, transient: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal
        self.transient = transient


class RateLimited(UpstreamError):
    """429. The caller backs off and reduces that job's concurrency (docs/parsing.md)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("The upstream rate-limited this key")
        self.retry_after = retry_after


class EmptyCompletion(UpstreamError):
    """The upstream answered without an error and without any text.

    Ambiguous on its own — a blank page transcribes to nothing, and so does a hiccup — so
    it is transient; a page that stays empty through every attempt is a blank page
    (docs/parsing.md § Upstream usage)."""

    def __init__(self) -> None:
        super().__init__("The upstream returned an empty completion", transient=True)
        # What the empty answer was billed, for the caller that accepts it as a blank page.
        self.usage = Usage(
            prompt_tokens=0, completion_tokens=0, cost=None, cost_source=COST_SOURCE_UNKNOWN
        )


class PaymentRequired(UpstreamError):
    """402. The page fails with reason `payment`; a repeat means the key is dead."""

    def __init__(self) -> None:
        super().__init__("The upstream reported exhausted credits")


@dataclass(frozen=True)
class Connection:
    """A resolved upstream: where to call, and the still-encrypted key to call it with.

    Only this module ever opens the ciphertext, and the plaintext never leaves the request
    it authorises (docs/project-structure.md). The default field values are the built-in
    OpenRouter upstream; a provider connection carries its own base URL and kind, and
    whether its reported cost is recorded (`report_cost`, docs/parsing.md § Upstream usage).
    """

    ciphertext: bytes
    secret_key: str
    base_url: str = BASE_URL
    kind: str = KIND_OPENROUTER
    report_cost: bool = False

    def __repr__(self) -> str:  # never let key material reach a log line
        return "Connection(...)"

    def authorization(self) -> str:
        decrypt = decrypt_openrouter_key if self.kind == KIND_OPENROUTER else decrypt_connection_key
        return f"Bearer {decrypt(self.secret_key, self.ciphertext)}"


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    # None exactly when `cost_source` is unknown: an unpriced call is not a free one.
    cost: Decimal | None
    cost_source: str


@dataclass(frozen=True)
class PageTranscription:
    markdown: str
    usage: Usage


async def load_connection(
    db: AsyncSession, secret_key: str, user_id: int, connection_id: int | None
) -> Connection | None:
    """The upstream a job calls: OpenRouter when `connection_id` is NULL, else that
    provider connection (docs/api.md § Upstreams). None when the credential is gone."""
    if connection_id is None:
        row = (
            await db.execute(select(OpenRouterKey).where(OpenRouterKey.user_id == user_id))
        ).scalar_one_or_none()
        return (
            None if row is None else Connection(ciphertext=row.ciphertext, secret_key=secret_key)
        )
    connection = (
        await db.execute(
            select(ProviderConnection).where(
                ProviderConnection.id == connection_id, ProviderConnection.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if connection is None:
        return None
    return Connection(
        ciphertext=connection.ciphertext,
        secret_key=secret_key,
        base_url=connection.base_url,
        kind=KIND_OPENAI,
        report_cost=connection.report_cost,
    )


def image_data_url(path: Path) -> str:
    media_type = DATA_URL_MEDIA_TYPES.get(path.suffix.lower(), "image/png")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _usage(payload: dict, connection: Connection) -> Usage:
    """Token counts from any upstream, and the cost tagged with where it came from.

    OpenRouter's cost is its billing. A provider connection's cost is recorded only when
    the user opted in (`report_cost`), and then only as an estimate; otherwise, or when the
    reported value is not a usable number, the cost is unknown — never 0, and never mixed
    into billed totals (docs/parsing.md § Upstream usage).
    """
    raw = payload.get("usage") or {}
    prompt_tokens = _int(raw.get("prompt_tokens"))
    completion_tokens = _int(raw.get("completion_tokens"))

    if connection.kind == KIND_OPENROUTER:
        try:
            cost = Decimal(str(raw.get("cost", "0"))).quantize(COST_QUANTUM)
        except (InvalidOperation, ValueError):
            cost = Decimal("0")
        return Usage(prompt_tokens, completion_tokens, cost, COST_SOURCE_OPENROUTER)

    reported = raw.get("cost")
    # A JSON number only: `bool` is an `int` to Python, and a string is not what an
    # OpenRouter-compatible `usage.cost` is. `json` also parses NaN and Infinity.
    if (
        connection.report_cost
        and isinstance(reported, int | float)
        and not isinstance(reported, bool)
    ):
        cost = Decimal(str(reported))
        if cost.is_finite() and 0 <= cost <= MAX_REPORTED_COST:
            return Usage(
                prompt_tokens,
                completion_tokens,
                cost.quantize(COST_QUANTUM),
                COST_SOURCE_ESTIMATED,
            )
    return Usage(prompt_tokens, completion_tokens, None, COST_SOURCE_UNKNOWN)


def _message_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise EmptyCompletion()
    content = (choices[0].get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Some providers answer with content parts instead of a plain string.
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if content is None:
        # How some servers say "nothing": a page with nothing to transcribe. Only a choice
        # that finished on its own is that answer; one cut short is a failed call, and
        # one that does not say is ambiguous (docs/parsing.md § Upstream usage).
        finish_reason = choices[0].get("finish_reason")
        if finish_reason == "stop":
            return ""
        if finish_reason is None:
            raise EmptyCompletion()
        raise UpstreamError("The upstream ended the completion early", transient=True)
    raise UpstreamError("The upstream returned an unreadable completion")


def _is_transient(status: int) -> bool:
    """408 and 5xx may not repeat; any other 4xx is the request's own fault and will."""
    return status == 408 or status >= 500


def _raise_for_error_payload(payload: dict) -> None:
    """OpenRouter-style servers can report a provider failure inside a 200 response."""
    error = payload.get("error")
    if not isinstance(error, dict):
        return
    code = _int(error.get("code"))
    if code == 402:
        raise PaymentRequired()
    if code == 429:
        raise RateLimited()
    raise UpstreamError(
        f"The upstream reported an error ({code or 'unknown'})", transient=_is_transient(code)
    )


async def _chat_with_image(
    connection: Connection, model: str, prompt: str, image: Path, max_bytes: int
) -> tuple[str, Usage]:
    body: dict = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(image)}},
                ],
            }
        ],
    }
    if connection.kind == KIND_OPENROUTER:
        # Ask for token counts and the actual cost; never price locally (docs/parsing.md).
        # OpenRouter-only: a generic OpenAI server would reject the unknown field.
        body["usage"] = {"include": True}
    try:
        async with (
            httpx.AsyncClient(timeout=CHAT_TIMEOUT_SECONDS) as client,
            client.stream(
                "POST",
                f"{connection.base_url}/chat/completions",
                headers={"Authorization": connection.authorization()},
                json=body,
            ) as response,
        ):
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                raise RateLimited(float(retry_after) if (retry_after or "").isdigit() else None)
            if response.status_code == 402:
                raise PaymentRequired()
            if response.status_code in (401, 403):
                raise UpstreamError("The upstream rejected the stored key", fatal=True)
            if response.status_code >= 400:
                raise UpstreamError(
                    f"The upstream returned {response.status_code}",
                    transient=_is_transient(response.status_code),
                )
            raw = await _read_body_capped(response, max_bytes)
    except httpx.HTTPError as exc:
        raise UpstreamError("The upstream was unreachable", transient=True) from exc
    except ResponseTooLarge as exc:
        raise UpstreamError("The upstream response exceeded the size cap") from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise UpstreamError("The upstream returned a non-JSON body", transient=True) from exc
    if not isinstance(payload, dict):
        # A non-object body (e.g. `[]`) is the endpoint's fault — a failed page, never
        # an internal error that kills the whole job.
        raise UpstreamError("The upstream returned an unreadable body")
    _raise_for_error_payload(payload)
    try:
        text = _message_text(payload)
    except EmptyCompletion as exc:
        exc.usage = _usage(payload, connection)
        raise
    return text, _usage(payload, connection)


async def transcribe_page(
    connection: Connection,
    model: str,
    prompt_template: str,
    bbox_format: str,
    image: Path,
    page_no: int,
    max_response_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
) -> PageTranscription:
    """Transcribe one rendered page; the answer carries its own figure placeholders.

    Token substitution is plain replacement, not `str.format`: a user-supplied template
    with stray braces must never break the call (docs/parsing.md § Prompts).
    """
    prompt = prompt_template.replace("{page}", str(page_no)).replace("{bbox_format}", bbox_format)
    text, usage = await _chat_with_image(connection, model, prompt, image, max_response_bytes)
    return PageTranscription(markdown=_CODE_FENCE_RE.sub("", text.strip()), usage=usage)
