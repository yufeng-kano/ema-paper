"""The Messages API client against a stubbed stream (docs/chat.md § Upstream). Nothing
here reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import json
import math
from decimal import Decimal

import httpx
import pytest
import respx

from sightread.upstream.anthropic import (
    CompletedMessage,
    StreamEvent,
    StreamProgress,
    stream_messages,
)
from sightread.upstream.client import Connection, PaymentRequired, RateLimited, UpstreamError
from tests.chat_helpers import MESSAGES_URL, sse
from tests.helpers import stub_connection

# The bounds every caller passes from the deployment's `UPSTREAM_*` settings, at their
# defaults (docs/api.md).
BOUNDS = {"max_bytes": 33_554_432, "line_max_bytes": 4 * 1024 * 1024, "blocks_max": 4096}


@pytest.fixture
def key() -> Connection:
    return stub_connection("anthropic")


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
    async for item in stream_messages(
        key, body, betas=("thinking-display-updates-2026-08-18",), **BOUNDS
    ):
        if isinstance(item, CompletedMessage):
            final = item
        else:
            events.append(item)
    assert final is not None
    return events, final


async def drain(key: Connection, body: dict, **options) -> None:
    """Read a stream to its end, on `BOUNDS` unless `options` overrides one."""
    async for _ in stream_messages(key, body, **{**BOUNDS, **options}):
        pass


@respx.mock
async def test_thinking_the_api_dropped_as_unbound_is_counted(key) -> None:
    """docs/chat.md § History is append-only: `input_transformations` on message_start says
    which thinking blocks the API dropped; only those bound to another prompt are counted —
    a move to another model drops blocks too, and that is no fault of the replay."""
    dropped = [
        {
            "type": "thinking_dropped",
            "path": "messages.1.content.0",
            "reason": "prefix_binding_mismatch",
        },
        {
            "type": "thinking_dropped",
            "path": "messages.3.content.0",
            "reason": "prefix_binding_mismatch",
        },
        {
            "type": "thinking_dropped",
            "path": "messages.5.content.0",
            "reason": "model_binding_mismatch",
        },
        {"type": "something_new", "path": "messages.7.content.0"},
    ]
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200,
            content=sse(
                {
                    "type": "message_start",
                    "message": {
                        "model": "claude-opus-5-5",
                        "usage": {"input_tokens": 10},
                        "input_transformations": dropped,
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hi."},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 2},
                },
                {"type": "message_stop"},
            ),
            headers={"content-type": "text/event-stream"},
        )
    )
    _, final = await collect(key, {"model": "claude-opus-5-5", "messages": []})
    assert final.thinking_dropped == 2
    assert final.content == [{"type": "text", "text": "Hi."}]


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
async def test_each_event_is_handed_on_as_it_arrives(key) -> None:
    """The reader sees an event the moment its bytes come, not once 64 KiB have piled up:
    otherwise a short answer reaches the panel in one piece (docs/chat.md § Upstream)."""
    first_seen = asyncio.Event()
    whole = a_turn()
    # Up to the first delta, the first event the reader is handed.
    split = whole.index(b"\n\n", whole.index(b"thinking_delta")) + 2
    head, tail = whole[:split], whole[split:]

    async def body():
        yield head
        # The rest only comes once the first event was read: a reader that waits for more
        # bytes before it parses would wait here for ever.
        await first_seen.wait()
        yield tail

    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, content=body(), headers={"content-type": "text/event-stream"}
        )
    )

    async def read() -> list:
        seen = []
        async for event in stream_messages(
            key, {"model": "claude-opus-5-5", "messages": []}, **BOUNDS
        ):
            seen.append(event)
            first_seen.set()
        return seen

    seen = await asyncio.wait_for(read(), 5)
    assert isinstance(seen[-1], CompletedMessage)


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
        await drain(key, {"messages": []})
    assert "SECRET" not in str(raised.value)
    assert "invalid_request_error" in str(raised.value)


@respx.mock
async def test_an_overlong_stream_line_is_refused_before_it_is_held(key) -> None:
    endless = b"data: " + b"x" * 5_000
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=endless))
    with pytest.raises(UpstreamError) as refused:
        await drain(key, {"messages": []}, line_max_bytes=1_000)
    assert "overlong" in str(refused.value)


@respx.mock
async def test_a_body_past_the_size_cap_is_refused(key) -> None:
    """docs/chat.md § Upstream: a body past `UPSTREAM_RESPONSE_MAX_BYTES` ends the call."""
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=a_turn()))
    with pytest.raises(UpstreamError) as refused:
        await drain(key, {"messages": []}, max_bytes=100)
    assert "exceeded the size cap" in str(refused.value)


def _start(index: int) -> dict:
    return {"type": "content_block_start", "index": index, "content_block": {"type": "text"}}


def _delta(**index: int) -> dict:
    return {"type": "content_block_delta", **index, "delta": {"type": "text_delta", "text": "x"}}


@pytest.mark.parametrize(
    ("stray", "blocks_max", "expected"),
    [
        pytest.param(_start(2), BOUNDS["blocks_max"], "out-of-order", id="skips-ahead"),
        pytest.param(_start(0), BOUNDS["blocks_max"], "out-of-order", id="repeated-start"),
        pytest.param(_start(1), 1, "out-of-order", id="past-blocks-max"),
        pytest.param(_delta(index=-1), BOUNDS["blocks_max"], "out-of-order", id="negative-index"),
        pytest.param(_delta(), BOUNDS["blocks_max"], "out-of-order", id="missing-index"),
        pytest.param(_delta(index=1), BOUNDS["blocks_max"], "has not started", id="early-delta"),
        pytest.param(
            {"type": "content_block_stop", "index": 1},
            BOUNDS["blocks_max"],
            "has not started",
            id="early-stop",
        ),
    ],
)
@respx.mock
async def test_a_broken_block_sequence_is_refused(key, stray, blocks_max, expected) -> None:
    """Blocks arrive in order, each exactly the next one: a truncated or reordered stream
    is neither allocated for nor stored as a complete answer with pieces missing
    (docs/chat.md § Upstream, § Conversations, turns, events). The stream still ends with
    its message_stop, so the refusal is not the early close."""
    wild = sse(
        {"type": "message_start", "message": {"usage": {}}},
        _start(0),
        stray,
        {"type": "message_stop"},
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=wild))
    with pytest.raises(UpstreamError) as refused:
        await drain(key, {"messages": []}, blocks_max=blocks_max)
    assert expected in str(refused.value)


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
async def test_a_stream_cut_short_still_says_what_it_used(key) -> None:
    """docs/chat.md § Conversations, turns, events: a call that ends before its
    message_stop is billed on message_start's input figures and the output that streamed —
    the count a message_delta reported, else one token per three bytes of UTF-8 — with
    its cost unknown. Before message_start there is nothing to bill."""
    body = {"model": "m", "messages": []}
    started = [
        {
            "type": "message_start",
            "message": {
                "model": "m",
                "usage": {
                    "input_tokens": 900,
                    "cache_read_input_tokens": 700,
                    "cache_creation_input_tokens": 100,
                    "cost": 0.01,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "Reading."},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "表格"},
        },
        {"type": "content_block_stop", "index": 1},
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
    ]

    async def cut_after(*events: dict) -> StreamProgress:
        respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=sse(*events)))
        progress = StreamProgress()
        with pytest.raises(UpstreamError):
            await drain(key, body, progress=progress)
        return progress

    usage = (await cut_after(*started)).usage_so_far()
    assert usage is not None
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        900,
        700,
        100,
    )
    # Eight bytes of thinking, six of text (two CJK characters), six of tool input.
    assert usage.output_tokens == math.ceil((8 + 6 + 6) / 3)
    assert (usage.cost, usage.cost_source) == (None, "unknown")

    reported = await cut_after(
        *started, {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 42}}
    )
    assert reported.usage_so_far().output_tokens == 42

    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(503, json={}))
    refused = StreamProgress()
    with pytest.raises(UpstreamError):
        await drain(key, body, progress=refused)
    assert refused.usage_so_far() is None


@respx.mock
async def test_a_silent_stream_is_not_retried(key) -> None:
    body = {"model": "m", "messages": []}
    respx.post(MESSAGES_URL).mock(side_effect=httpx.ReadTimeout("silent"))
    with pytest.raises(UpstreamError) as silent:
        await collect(key, body)
    assert not silent.value.transient


@respx.mock
async def test_a_complete_overlong_line_is_refused_before_it_is_yielded(key) -> None:
    """A line past the cap that ends within the chunk is refused as one that never ends:
    the bound holds whichever way the line arrives (docs/chat.md § Conversations, turns,
    events)."""
    ended = b"data: " + b"x" * 5_000 + b"\n\n"
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=ended))
    with pytest.raises(UpstreamError) as refused:
        await drain(key, {"messages": []}, line_max_bytes=1_000)
    assert "overlong" in str(refused.value)
