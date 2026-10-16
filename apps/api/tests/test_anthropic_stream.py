"""The Messages API client against a stubbed stream (docs/chat.md § Upstream). Nothing
here reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx

from sightread.auth.crypto import encrypt_kano_proxy_key
from sightread.upstream.anthropic import CompletedMessage, StreamEvent, stream_messages
from sightread.upstream.client import Connection, PaymentRequired, RateLimited, UpstreamError
from tests.conftest import KANO_BASE

SECRET = "test-secret-key-not-a-real-one"
MESSAGES_URL = f"{KANO_BASE}/anthropic/v1/messages"


@pytest.fixture
def key() -> Connection:
    return Connection(
        ciphertext=encrypt_kano_proxy_key(SECRET, "sk-kano-proxy-test"),
        secret_key=SECRET,
        base_url=f"{KANO_BASE}/anthropic/v1",
        snapshot=KANO_BASE,
        builtin=True,
        report_cost=True,
    )


def sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def a_turn(*, with_tool: bool = True, cost: float | None = 0.0123) -> bytes:
    usage = {
        "input_tokens": 900,
        "cache_read_input_tokens": 700,
        "cache_creation_input_tokens": 100,
    }
    if cost is not None:
        usage["cost"] = cost
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "model": "claude-opus-5-5", "usage": usage},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Reading page 3."},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "sig"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Checking "},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "the table."},
        },
        {"type": "content_block_stop", "index": 1},
    ]
    if with_tool:
        events += [
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"comm'},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": 'and": "ls"}'},
            },
            {"type": "content_block_stop", "index": 2},
        ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if with_tool else "end_turn"},
            "usage": {"output_tokens": 42},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


async def collect(key: Connection, body: dict) -> tuple[list[StreamEvent], CompletedMessage]:
    events: list[StreamEvent] = []
    final: CompletedMessage | None = None
    async for item in stream_messages(key, body, betas=("thinking-display-updates-2026-08-18",)):
        if isinstance(item, CompletedMessage):
            final = item
        else:
            events.append(item)
    assert final is not None
    return events, final


@respx.mock
async def test_the_stream_becomes_blocks_the_runner_replays(key) -> None:
    route = respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, content=a_turn(), headers={"content-type": "text/event-stream"}
        )
    )
    events, final = await collect(key, {"model": "claude-opus-5-5", "messages": []})

    sent = json.loads(route.calls[0].request.content)
    assert sent["stream"] is True
    headers = route.calls[0].request.headers
    assert headers["authorization"] == "Bearer sk-kano-proxy-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "thinking-display-updates-2026-08-18"

    assert [event.kind for event in events if event.kind.endswith("delta")] == [
        "thinking_delta",
        "text_delta",
        "text_delta",
    ]
    assert final.stop_reason == "tool_use"
    assert final.model == "claude-opus-5-5"
    assert final.content == [
        {"type": "thinking", "thinking": "Reading page 3.", "signature": "sig"},
        {"type": "text", "text": "Checking the table."},
        {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": "ls"}},
    ]
    assert final.malformed_tool_uses == set()
    assert final.usage.input_tokens == 900
    assert final.usage.cache_read_tokens == 700
    assert final.usage.cache_write_tokens == 100
    assert final.usage.output_tokens == 42
    assert final.usage.cost == Decimal("0.012300")
    assert final.usage.cost_source == "estimated"


@respx.mock
async def test_an_unpriced_turn_stays_unknown_and_a_bad_tool_input_is_flagged(key) -> None:
    broken = sse(
        {"type": "message_start", "message": {"model": "m", "usage": {"input_tokens": 5}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_x", "name": "bash", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"command": '},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=broken))
    _, final = await collect(key, {"model": "m", "messages": []})
    assert final.usage.cost is None
    assert final.usage.cost_source == "unknown"
    assert final.malformed_tool_uses == {"toolu_x"}
    assert final.content[0]["input"] == {}


@respx.mock
async def test_upstream_failures_map_onto_the_shared_error_classes(key) -> None:
    body = {"model": "m", "messages": []}
    for status, expected in ((429, RateLimited), (402, PaymentRequired), (503, UpstreamError)):
        respx.post(MESSAGES_URL).mock(return_value=httpx.Response(status, json={"error": {}}))
        with pytest.raises(expected):
            await collect(key, body)

    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(UpstreamError) as rejected:
        await collect(key, body)
    assert rejected.value.fatal

    cut = sse({"type": "message_start", "message": {"usage": {}}})
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=cut))
    with pytest.raises(UpstreamError) as early:
        await collect(key, body)
    assert early.value.transient

    failed = sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=failed))
    with pytest.raises(UpstreamError) as overloaded:
        await collect(key, body)
    assert overloaded.value.transient


@respx.mock
async def test_an_upstream_error_is_stored_by_type_never_by_its_text(key) -> None:
    leaky = sse(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "key sk-ant-SECRET rejected"},
        }
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=leaky))
    with pytest.raises(UpstreamError) as raised:
        async for _ in stream_messages(key, {"messages": []}, betas=(), max_bytes=1_000_000):
            pass
    assert "SECRET" not in str(raised.value)
    assert "invalid_request_error" in str(raised.value)


@respx.mock
async def test_an_overlong_stream_line_is_refused_before_it_is_held(key) -> None:
    from sightread.upstream import anthropic

    # The bound is the deployment's (`UPSTREAM_SSE_LINE_MAX_BYTES`), passed per call.
    assert anthropic.DEFAULT_SSE_LINE_MAX_BYTES == 4 * 1024 * 1024
    endless = b"data: " + b"x" * 5_000
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=endless))
    with pytest.raises(UpstreamError) as refused:
        async for _ in stream_messages(
            key, {"messages": []}, betas=(), max_bytes=10_000_000, line_max_bytes=1_000
        ):
            pass
    assert "overlong" in str(refused.value)


@respx.mock
async def test_a_wild_content_block_index_is_refused(key) -> None:
    wild = sse(
        {"type": "message_start", "message": {"usage": {}}},
        {
            "type": "content_block_start",
            "index": 1_000_000_000,
            "content_block": {"type": "text", "text": ""},
        },
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=wild))
    with pytest.raises(UpstreamError) as refused:
        async for _ in stream_messages(key, {"messages": []}, betas=(), max_bytes=10_000_000):
            pass
    assert "out-of-order" in str(refused.value)


@respx.mock
async def test_a_negative_delta_index_is_refused(key) -> None:
    wild = sse(
        {"type": "message_start", "message": {"usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": -1, "delta": {"type": "text_delta", "text": "x"}},
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=wild))
    with pytest.raises(UpstreamError) as refused:
        async for _ in stream_messages(key, {"messages": []}, betas=(), max_bytes=10_000_000):
            pass
    assert "out-of-order" in str(refused.value)


@respx.mock
async def test_a_repeated_or_missing_block_index_is_refused(key) -> None:
    for second in (
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}},
    ):
        wild = sse(
            {"type": "message_start", "message": {"usage": {}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            second,
        )
        respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=wild))
        with pytest.raises(UpstreamError) as refused:
            async for _ in stream_messages(key, {"messages": []}, betas=(), max_bytes=10_000_000):
                pass
        assert "out-of-order" in str(refused.value)


@respx.mock
async def test_a_stop_reason_without_message_stop_is_an_early_close(key) -> None:
    body = {"model": "m", "messages": []}
    cut = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=cut))
    with pytest.raises(UpstreamError) as early:
        await collect(key, body)
    assert early.value.transient


@respx.mock
async def test_a_silent_stream_is_not_retried(key) -> None:
    body = {"model": "m", "messages": []}
    respx.post(MESSAGES_URL).mock(side_effect=httpx.ReadTimeout("silent"))
    with pytest.raises(UpstreamError) as silent:
        await collect(key, body)
    assert not silent.value.transient


@respx.mock
async def test_a_delta_or_stop_for_a_block_that_never_started_is_refused(key) -> None:
    """A truncated or reordered stream must not be stored as a complete answer with pieces
    missing (docs/chat.md § Conversations, turns, events)."""
    for stray in (
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "x"}},
        {"type": "content_block_stop", "index": 1},
    ):
        wild = sse(
            {"type": "message_start", "message": {"usage": {}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            stray,
            {"type": "message_stop"},
        )
        respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=wild))
        with pytest.raises(UpstreamError) as refused:
            async for _ in stream_messages(key, {"messages": []}, betas=(), max_bytes=10_000_000):
                pass
        assert "has not started" in str(refused.value)


@respx.mock
async def test_a_complete_overlong_line_is_refused_before_it_is_yielded(key) -> None:
    """A line past the cap that ends within the chunk is refused as one that never ends:
    the bound holds whichever way the line arrives (docs/chat.md § Conversations, turns,
    events)."""
    ended = b"data: " + b"x" * 5_000 + b"\n\n"
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=ended))
    with pytest.raises(UpstreamError) as refused:
        async for _ in stream_messages(
            key, {"messages": []}, betas=(), max_bytes=10_000_000, line_max_bytes=1_000
        ):
            pass
    assert "overlong" in str(refused.value)
