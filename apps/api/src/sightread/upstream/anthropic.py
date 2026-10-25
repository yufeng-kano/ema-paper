"""The Messages API client — the chat agent's upstream (docs/chat.md § Upstream).

Streams `POST {base}/messages` over Kano Proxy's Anthropic-format route and turns the SSE
events into content blocks the runner can act on and store verbatim. Like `client.py`, it
is one of the two modules that ever decrypt a stored user key, and the plaintext never
leaves the request it authorises. Errors map onto the same classes the vision client
raises so the runner's failure policy has one vocabulary.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from ..db.models import COST_SOURCE_ESTIMATED, COST_SOURCE_UNKNOWN
from .client import (
    COST_QUANTUM,
    DEFAULT_RESPONSE_MAX_BYTES,
    MAX_REPORTED_COST,
    Connection,
    PaymentRequired,
    RateLimited,
    ResponseTooLarge,
    UpstreamError,
    _is_transient,
)

ANTHROPIC_VERSION = "2023-06-01"
# A turn can think and call tools for a long time; the stream keeps the connection alive.
STREAM_TIMEOUT_SECONDS = 600.0
# The defaults of `UPSTREAM_SSE_LINE_MAX_BYTES` and `UPSTREAM_CONTENT_BLOCKS_MAX`: one
# SSE line past the first is a broken upstream, not a long answer, and a block index past
# the second is refused outright. The runner passes the deployment's values.
DEFAULT_SSE_LINE_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_CONTENT_BLOCKS_MAX = 4096
# The output of a call cut short is estimated from what streamed, one token per this many
# bytes of UTF-8 (docs/chat.md § Conversations, turns, events).
ESTIMATED_BYTES_PER_TOKEN = 3
# The error types the Messages API documents; anything else is reported as "error".
KNOWN_ERROR_TYPES = frozenset(
    {
        "invalid_request_error",
        "authentication_error",
        "permission_error",
        "not_found_error",
        "request_too_large",
        "rate_limit_error",
        "api_error",
        "overloaded_error",
        "billing_error",
    }
)


@dataclass(frozen=True)
class ChatUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    # None exactly when `cost_source` is unknown (docs/parsing.md § Upstream usage).
    cost: Decimal | None
    cost_source: str


@dataclass(frozen=True)
class StreamEvent:
    """One incremental step of the stream the runner relays to the conversation."""

    kind: str  # text_delta | thinking_delta | block_start | block_stop
    index: int
    text: str = ""
    block: dict | None = None


@dataclass
class CompletedMessage:
    """The assistant turn as the API finished it: blocks to replay, and what to bill."""

    content: list[dict]
    stop_reason: str | None
    stop_details: dict | None
    usage: ChatUsage
    model: str | None
    # Tool-use blocks whose input never parsed: the runner answers them with an error
    # result rather than running something half-formed.
    malformed_tool_uses: set[str] = field(default_factory=set)


@dataclass
class StreamProgress:
    """What a stream has reported and sent so far, kept as it arrives: the bill of a call
    that ends before its `message_stop` — a Stop, the turn's timeout, a stream that broke
    (docs/chat.md § Conversations, turns, events)."""

    started: bool = False
    # message_start's usage, with the input figures a message_delta reported since.
    usage: dict = field(default_factory=dict)
    # The output tokens a message_delta reported; 0 until one does.
    output_tokens: int = 0
    # UTF-8 bytes of the text, thinking and tool input streamed so far.
    output_bytes: int = 0

    def usage_so_far(self) -> ChatUsage | None:
        """None before message_start: nothing was generated the runner can know of. The
        output is the reported count, else the estimate; the cost is unknown, since no
        figure covers the call as it was cut."""
        if not self.started:
            return None
        return ChatUsage(
            input_tokens=_int(self.usage.get("input_tokens")),
            output_tokens=self.output_tokens
            or math.ceil(self.output_bytes / ESTIMATED_BYTES_PER_TOKEN),
            cache_read_tokens=_int(self.usage.get("cache_read_input_tokens")),
            cache_write_tokens=_int(self.usage.get("cache_creation_input_tokens")),
            cost=None,
            cost_source=COST_SOURCE_UNKNOWN,
        )


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _index(data: dict) -> int:
    """A content block event's index: a genuine integer, or the stream is broken."""
    value = data.get("index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UpstreamError("The upstream sent an out-of-order content block")
    return value


def _usage(raw: dict, *, output_tokens: int) -> ChatUsage:
    reported = raw.get("cost")
    cost: Decimal | None = None
    source = COST_SOURCE_UNKNOWN
    if isinstance(reported, int | float) and not isinstance(reported, bool):
        candidate = Decimal(str(reported))
        if candidate.is_finite() and 0 <= candidate <= MAX_REPORTED_COST:
            cost, source = candidate.quantize(COST_QUANTUM), COST_SOURCE_ESTIMATED
    return ChatUsage(
        input_tokens=_int(raw.get("input_tokens")),
        output_tokens=output_tokens,
        cache_read_tokens=_int(raw.get("cache_read_input_tokens")),
        cache_write_tokens=_int(raw.get("cache_creation_input_tokens")),
        cost=cost,
        cost_source=source,
    )


def _raise_for_status(response: httpx.Response) -> None:
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


async def _bounded_lines(
    response: httpx.Response, max_bytes: int, line_max_bytes: int
) -> AsyncIterator[str]:
    """The body's lines, decoded, from the chunks as the transport hands them on; a line
    that outgrows `line_max_bytes` or a body that outgrows `max_bytes` ends the read. No
    chunk size is asked for: httpx would then hold the bytes until that many had come, and
    an answer shorter than that would reach the reader in one piece at its end."""
    pending = bytearray()
    seen = 0
    async for chunk in response.aiter_bytes():
        seen += len(chunk)
        if seen > max_bytes:
            raise ResponseTooLarge()
        pending += chunk
        while True:
            newline = pending.find(b"\n")
            if newline == -1:
                break
            if newline > line_max_bytes:
                # Complete, but past the cap: refused before it is built or handed on,
                # like a line that outgrows the cap with no end in sight.
                raise UpstreamError("The upstream sent an overlong stream line")
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            yield line.decode("utf-8", errors="replace").rstrip("\r")
        if len(pending) > line_max_bytes:
            raise UpstreamError("The upstream sent an overlong stream line")
    if pending:
        yield bytes(pending).decode("utf-8", errors="replace").rstrip("\r")


async def _sse_events(
    response: httpx.Response, max_bytes: int, line_max_bytes: int
) -> AsyncIterator[tuple[str, dict]]:
    """`(event, data)` pairs from an SSE body, bounded in total size and per line."""
    total = 0
    event_name = ""
    data_lines: list[str] = []
    # Bytes, not `aiter_lines()`: that would hold a whole line before any check could
    # run. Lines are split as the chunks come, and a line past `line_max_bytes` or a
    # body past `max_bytes` is refused before it is held.
    async for line in _bounded_lines(response, max_bytes, line_max_bytes):
        total += len(line) + 1
        if total > max_bytes:
            raise ResponseTooLarge()
        if not line:
            if data_lines:
                try:
                    payload = json.loads("\n".join(data_lines))
                except ValueError as exc:
                    raise UpstreamError(
                        "The upstream sent an unreadable stream event", transient=True
                    ) from exc
                if isinstance(payload, dict):
                    yield event_name or str(payload.get("type") or ""), payload
            event_name, data_lines = "", []
            continue
        if line.startswith(":"):
            continue
        key, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if key == "event":
            event_name = value
        elif key == "data":
            if len(value) > line_max_bytes:
                raise ResponseTooLarge()
            data_lines.append(value)


async def stream_messages(
    connection: Connection,
    body: dict,
    *,
    betas: tuple[str, ...] = (),
    max_bytes: int = DEFAULT_RESPONSE_MAX_BYTES,
    line_max_bytes: int = DEFAULT_SSE_LINE_MAX_BYTES,
    blocks_max: int = DEFAULT_CONTENT_BLOCKS_MAX,
    timeout: float = STREAM_TIMEOUT_SECONDS,
    progress: StreamProgress | None = None,
) -> AsyncIterator[StreamEvent | CompletedMessage]:
    """Stream one Messages request. Yields `StreamEvent`s, then exactly one
    `CompletedMessage` last — unless the upstream fails, in which case it raises. The
    bounds are the deployment's (`UPSTREAM_*`, docs/chat.md § Upstream). `progress`, when
    given, follows the stream as it arrives, so a caller that drops it still knows what
    the call used."""
    if progress is None:
        progress = StreamProgress()
    headers = {
        "Authorization": f"Bearer {connection.api_key}",
        "x-api-key": connection.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    request_body = {**body, "stream": True}

    blocks: list[dict] = []
    partial_json: dict[int, list[str]] = {}
    malformed: set[str] = set()
    stop_reason: str | None = None
    stop_details: dict | None = None
    model: str | None = None
    finished = False

    try:
        async with (
            httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20.0)) as client,
            client.stream(
                "POST", f"{connection.base_url}/messages", headers=headers, json=request_body
            ) as response,
        ):
            if response.status_code >= 400:
                # Drain nothing: the status alone decides, and a body may carry content.
                _raise_for_status(response)

            async for event, data in _sse_events(response, max_bytes, line_max_bytes):
                kind = data.get("type") or event
                if kind == "message_start":
                    message = data.get("message") or {}
                    model = message.get("model") if isinstance(message.get("model"), str) else None
                    progress.started = True
                    progress.usage = message.get("usage") or {}
                elif kind == "content_block_start":
                    index = _index(data)
                    # Blocks arrive in order, each exactly the next one; an index that
                    # repeats, skips ahead or exceeds any sane count is a broken upstream,
                    # not a reason to allocate or to overwrite.
                    if index != len(blocks) or index >= blocks_max:
                        raise UpstreamError("The upstream sent an out-of-order content block")
                    block = dict(data.get("content_block") or {})
                    if block.get("type") == "tool_use":
                        partial_json[index] = []
                        block.setdefault("input", {})
                    elif block.get("type") == "text":
                        block.setdefault("text", "")
                    elif block.get("type") == "thinking":
                        block.setdefault("thinking", "")
                    while len(blocks) <= index:
                        blocks.append({})
                    blocks[index] = block
                    yield StreamEvent("block_start", index, block=block)
                elif kind == "content_block_delta":
                    index = _index(data)
                    if index >= len(blocks):
                        # A delta for a block that never started: a truncated or reordered
                        # stream, whose answer would be stored with pieces missing — or a
                        # tool call with no input — were the delta dropped and the stop
                        # accepted (docs/chat.md § Conversations, turns, events).
                        raise UpstreamError(
                            "The upstream sent a delta for a content block that has not started"
                        )
                    delta = data.get("delta") or {}
                    delta_type = delta.get("type")
                    block = blocks[index]
                    if delta_type == "text_delta":
                        text = str(delta.get("text") or "")
                        block["text"] = block.get("text", "") + text
                        progress.output_bytes += len(text.encode())
                        yield StreamEvent("text_delta", index, text=text)
                    elif delta_type == "thinking_delta":
                        text = str(delta.get("thinking") or "")
                        block["thinking"] = block.get("thinking", "") + text
                        progress.output_bytes += len(text.encode())
                        if text:
                            yield StreamEvent("thinking_delta", index, text=text)
                    elif delta_type == "signature_delta":
                        block["signature"] = block.get("signature", "") + str(
                            delta.get("signature") or ""
                        )
                    elif delta_type == "input_json_delta":
                        partial = str(delta.get("partial_json") or "")
                        partial_json.setdefault(index, []).append(partial)
                        progress.output_bytes += len(partial.encode())
                elif kind == "content_block_stop":
                    index = _index(data)
                    if index >= len(blocks):
                        raise UpstreamError(
                            "The upstream sent a stop for a content block that has not started"
                        )
                    block = blocks[index]
                    if block.get("type") == "tool_use":
                        raw = "".join(partial_json.pop(index, []))
                        try:
                            parsed = json.loads(raw) if raw.strip() else {}
                        except ValueError:
                            parsed = {}
                            malformed.add(str(block.get("id")))
                        if not isinstance(parsed, dict):
                            parsed = {}
                            malformed.add(str(block.get("id")))
                        block["input"] = parsed
                    yield StreamEvent("block_stop", index, block=block)
                elif kind == "message_delta":
                    delta = data.get("delta") or {}
                    if delta.get("stop_reason"):
                        stop_reason = str(delta["stop_reason"])
                    if isinstance(delta.get("stop_details"), dict):
                        stop_details = delta["stop_details"]
                    usage = data.get("usage") or {}
                    progress.output_tokens = (
                        _int(usage.get("output_tokens")) or progress.output_tokens
                    )
                    # Some servers report the final input figures here as well.
                    for key in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "cost",
                    ):
                        if key in usage:
                            progress.usage[key] = usage[key]
                elif kind == "message_stop":
                    finished = True
                elif kind == "error":
                    error = data.get("error") or {}
                    kind_name = str(error.get("type") or "")
                    if kind_name == "overloaded_error":
                        raise UpstreamError("The upstream is overloaded", transient=True)
                    # The type is a fixed token; the message is the provider's prose and
                    # is never stored or shown (docs/chat.md).
                    label = kind_name if kind_name in KNOWN_ERROR_TYPES else "error"
                    raise UpstreamError(f"The upstream reported an error ({label})")
    except httpx.ReadTimeout as exc:
        # Silence past the stream timeout is the turn's end, not a retry: the attempt
        # may have generated — and billed — tokens that never arrived (docs/chat.md).
        raise UpstreamError("The upstream went silent for too long", transient=False) from exc
    except httpx.HTTPError as exc:
        raise UpstreamError("The upstream was unreachable", transient=True) from exc
    except ResponseTooLarge as exc:
        raise UpstreamError("The upstream response exceeded the size cap") from exc

    if not finished:
        # A stop reason without the message_stop that completes the message is a stream
        # cut short, not an answer: never stored, never acted on (docs/chat.md).
        raise UpstreamError("The upstream closed the stream early", transient=True)

    yield CompletedMessage(
        content=[block for block in blocks if block],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=_usage(progress.usage, output_tokens=progress.output_tokens),
        model=model,
        malformed_tool_uses=malformed,
    )
