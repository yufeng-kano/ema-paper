"""Upstream client — the module that calls the model upstream, Kano Proxy, on the
operator's own key (docs/auth.md § 3, docs/project-structure.md).

The key is never logged and never appears in raised messages or responses.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx

from ..agents import CHAT, CHECK, PARSE, bbox
from ..agents.bbox import BboxConvention
from ..config import Settings
from ..db.models import COST_SOURCE_ESTIMATED, COST_SOURCE_UNKNOWN
from ..errors import ApiError
from ..parsing.images import image_size

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
_FENCE_LINE_RE = re.compile(r"^\s*(```|~~~)\s*([^\s`]*)\s*$")
_WRAPPER_INFO = frozenset({"markdown", "md"})


def _unwrap_answer(text: str) -> str:
    """The answer without the fence a model wrapped it in, and nothing else removed.

    Only a first line opening a `markdown`/`md` fence is a wrapper; a bare closing fence on
    the last line goes with it. A page that opens or ends with a code block of its own keeps
    its fences, and a bare ``` opener is left alone: it cannot be told from a page that is
    one unlabelled listing (docs/parsing.md § Vision-only conversion).
    """
    lines = text.strip().split("\n")
    opener = _FENCE_LINE_RE.match(lines[0])
    if not opener or opener.group(2).lower() not in _WRAPPER_INFO:
        return text.strip()
    body = lines[1:]
    closer = _FENCE_LINE_RE.match(body[-1]) if body else None
    if closer and closer.group(1) == opener.group(1) and not closer.group(2):
        body = body[:-1]
    return "\n".join(body).strip()


# --- the model catalog ---------------------------------------------------------------


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


def offered_models(settings: Settings, agent: str) -> list[str]:
    """The ids an agent may run, in the operator's order (docs/billing.md § Models): the
    parse agent its fixed model and the fallback, the check agent its own or the parse's,
    the chat agent every priced model, before the live catalog narrows it. Empty when the
    deployment names none."""
    if agent == PARSE.name:
        return list(
            dict.fromkeys(m for m in (settings.parse_model, settings.parse_fallback_model) if m)
        )
    if agent == CHECK.name:
        return [settings.check_model or settings.parse_model] if settings.parse_model else []
    if agent == CHAT.name:
        return list(settings.model_prices)
    return []


async def agent_models(settings: Settings, agent: str) -> list[dict]:
    """`GET /api/agents/{agent}/models` (docs/api.md): the parse and check agents' models
    are configuration, answered as they are with no name; the chat agent's are the priced
    models Kano Proxy's live catalog lists, in the price table's order, each with its name."""
    offered = offered_models(settings, agent)
    if agent != CHAT.name:
        if not offered:
            raise ApiError(503, "upstream", "No parse model is configured on this deployment")
        return [{"id": model, "name": None} for model in offered]
    connection = kano_connection(settings)
    if connection is None:
        raise ApiError(503, "upstream", "Kano Proxy is not configured on this deployment")
    catalog = await fetch_models(
        connection.base_url, connection.api_key, max_bytes=settings.upstream_response_max_bytes
    )
    names = {entry["id"]: entry["name"] for entry in catalog}
    return [{"id": model, "name": names[model]} for model in offered if model in names]


async def fetch_models(
    base_url: str, api_key: str, max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES
) -> list[dict]:
    """The model catalog of an OpenAI-compatible endpoint, `GET {base_url}/models` —
    free on Kano Proxy's `/openai/v1`. Every failure is the upstream's, 502."""
    try:
        async with (
            httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client,
            client.stream(
                "GET", f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
            ) as response,
        ):
            if response.status_code in (401, 403):
                raise ApiError(502, "upstream", "Kano Proxy rejected the operator's key")
            if response.status_code >= 400:
                raise ApiError(
                    502, "upstream", f"The upstream returned {response.status_code} for /models"
                )
            body = await _read_body_capped(response, max_bytes)
    except httpx.HTTPError as exc:
        raise ApiError(502, "upstream", "Could not reach Kano Proxy") from exc
    except ResponseTooLarge as exc:
        raise ApiError(502, "upstream", "The upstream's model list exceeded the size cap") from exc

    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ApiError(502, "upstream", "The upstream returned a non-JSON model list") from exc
    # An upstream answering `[]` (or any non-object) is a broken endpoint, not our bug.
    if not isinstance(payload, dict) or not isinstance(payload.get("data") or [], list):
        raise ApiError(502, "upstream", "The upstream returned an unreadable model list")

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


class IncompleteCompletion(UpstreamError):
    """The model stopped its own answer: `content_filter` (`blocked`) or `length` (`cut
    off`). The page fails at once, whatever text came with it — the same page asks the
    same thing again, so it is not retried on the same model (docs/parsing.md § Upstream
    usage). `reason` is the page's failure reason; `usage` what the answer was billed."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"The model stopped the completion ({reason})")
        self.reason = reason
        self.usage = Usage(
            prompt_tokens=0, completion_tokens=0, cost=None, cost_source=COST_SOURCE_UNKNOWN
        )


# A choice's `finish_reason` that ends its page (docs/parsing.md § Upstream usage).
STOPPED_REASONS = {"content_filter": "blocked", "length": "cut off"}


class PaymentRequired(UpstreamError):
    """402. The page fails with reason `payment`; a repeat means the key is dead."""

    def __init__(self) -> None:
        super().__init__("The upstream reported exhausted credits")


@dataclass(frozen=True)
class Connection:
    """The upstream to call: where, and the operator's key (docs/auth.md § 3). The key
    lives in this value for the request it authorises and in no log line; `snapshot` is
    what the job's dedup key stores for the upstream (docs/jobs.md § Dedup)."""

    api_key: str
    base_url: str
    snapshot: str

    def __repr__(self) -> str:  # never let key material reach a log line
        return "Connection(...)"


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


def kano_connection(settings: Settings, *, anthropic: bool = False) -> Connection | None:
    """Kano Proxy on the operator's key — its OpenAI-format base for vision calls and the
    catalog, its Anthropic-format base (`anthropic=True`) for the chat and check agents
    (docs/chat.md § Upstream). None when the deployment configures no upstream."""
    if not settings.kano_proxy_configured:
        return None
    return Connection(
        api_key=settings.kano_proxy_api_key,
        base_url=settings.kano_proxy_anthropic_base if anthropic else settings.kano_proxy_api_base,
        snapshot=settings.kano_proxy_base_url,
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


def _usage(payload: dict) -> Usage:
    """Token counts, and the cost tagged with where it came from.

    Kano Proxy's reported cost is recorded as an estimate (list prices). When it reports
    none, or the value is not a usable number, the cost is unknown — never 0
    (docs/parsing.md § Upstream usage).
    """
    raw = payload.get("usage") or {}
    prompt_tokens = _int(raw.get("prompt_tokens"))
    completion_tokens = _int(raw.get("completion_tokens"))

    reported = raw.get("cost")
    # A JSON number only: `bool` is an `int` to Python, and a string is not what an
    # OpenAI-compatible `usage.cost` is. `json` also parses NaN and Infinity.
    if isinstance(reported, int | float) and not isinstance(reported, bool):
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
    finish_reason = choices[0].get("finish_reason")
    # A stopped answer fails its page with whatever text came with it: a page cut short is
    # never a shorter page (docs/parsing.md § Upstream usage).
    if isinstance(finish_reason, str) and finish_reason in STOPPED_REASONS:
        raise IncompleteCompletion(STOPPED_REASONS[finish_reason])
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
    """Some OpenAI-compatible servers report a provider failure inside a 200 response."""
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
    try:
        async with (
            httpx.AsyncClient(timeout=CHAT_TIMEOUT_SECONDS) as client,
            client.stream(
                "POST",
                f"{connection.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {connection.api_key}"},
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
    except (EmptyCompletion, IncompleteCompletion) as exc:
        exc.usage = _usage(payload)
        raise
    return text, _usage(payload)


async def transcribe_page(
    connection: Connection,
    model: str,
    prompt_template: str,
    image: Path,
    page_no: int,
    *,
    convention: BboxConvention = bbox.DEFAULT,
    max_response_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
) -> PageTranscription:
    """Transcribe one rendered page. The answer's figure placeholders come back in the
    stored contract, whatever convention the model was asked for (agents/bbox.py).

    Token substitution is plain replacement, not `str.format`: a user-supplied template
    with stray braces must never break the call (docs/parsing.md § Prompts). The
    instructions go in first, because they carry a `{page}` of their own.
    """
    width_px, height_px = image_size(image)
    prompt = (
        prompt_template.replace("{bbox_instructions}", convention.instructions(width_px, height_px))
        .replace("{page}", str(page_no))
        .replace("{bbox_format}", convention.format)
    )
    text, usage = await _chat_with_image(connection, model, prompt, image, max_response_bytes)
    markdown = bbox.canonicalize(_unwrap_answer(text), convention, width_px, height_px)
    return PageTranscription(markdown=markdown, usage=usage)


async def ask_about_page(
    connection: Connection,
    model: str,
    prompt: str,
    image: Path,
    *,
    max_response_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
) -> PageTranscription:
    """One more question about a page image, the figure check's (docs/parsing.md § Figure
    check), on the same wire as its transcription. The answer's text comes back as it is,
    in the shape a transcription has, so the runner's failure policy reads both alike."""
    text, usage = await _chat_with_image(connection, model, prompt, image, max_response_bytes)
    return PageTranscription(markdown=text, usage=usage)
