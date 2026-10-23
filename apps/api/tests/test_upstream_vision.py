"""Vision calls against a stubbed upstream. No test ever reaches the network.

The stubs assert on the request we send (image data URL, key, no extension fields) and on
how defensively we read what comes back (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
import respx

from sightread.upstream.client import (
    Connection,
    EmptyCompletion,
    IncompleteCompletion,
    PaymentRequired,
    RateLimited,
    UpstreamError,
    transcribe_page,
)
from tests.conftest import KANO_BASE, KANO_CHAT_URL

SECRET = "test-secret-key-not-a-real-one"
PROMPT = "Transcribe page {page} in {bbox_format}."


@pytest.fixture
def key() -> Connection:
    """Kano Proxy, as `kano_connection` resolves it on the operator's key."""
    return Connection(
        api_key="sk-kano-proxy-test",
        base_url=f"{KANO_BASE}/openai/v1",
        snapshot=KANO_BASE,
    )


def completion(content: str, cost: float | None = 0.00042) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "total_tokens": 1500,
            "cost": cost,
        },
    }


@respx.mock
async def test_transcribe_page_sends_the_image_and_records_usage(key, documents) -> None:
    route = respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(200, json=completion("# Page\n\ntext"))
    )

    result = await transcribe_page(key, "vendor/model", PROMPT, documents["png"], 7)

    assert result.markdown == "# Page\n\ntext"
    assert result.usage.prompt_tokens == 1200
    # Kano Proxy prices at list prices: an estimate, never billing.
    assert result.usage.cost_source == "estimated"
    assert result.usage.cost == Decimal("0.000420")

    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "vendor/model"
    # Plain Chat Completions: no extension fields the upstream would have to understand.
    assert "usage" not in sent
    parts = sent["messages"][0]["content"]
    assert parts[0]["text"] == "Transcribe page 7 in yxyx_norm1000."
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # The decrypted key only ever appears in the Authorization header.
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-kano-proxy-test"


@respx.mock
async def test_transcribe_page_strips_a_code_fence(key, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(200, json=completion("```markdown\n# Page\n```"))
    )
    result = await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert result.markdown == "# Page"


@pytest.mark.parametrize(
    ("answer", "markdown"),
    [
        # A wrapper around a page that carries its own code block: only the wrapper goes.
        (
            "```markdown\n# Page\n\n```python\nprint(1)\n```\n```",
            "# Page\n\n```python\nprint(1)\n```",
        ),
        # A wrapper the model forgot to close is still a wrapper.
        ("```md\nText", "Text"),
        # A page ending with a listing keeps the listing's closing fence.
        ("Text\n\n```python\nprint(1)\n```", "Text\n\n```python\nprint(1)\n```"),
        # A page opening with one keeps its opening fence.
        ("```python\nprint(1)\n```\n\nText", "```python\nprint(1)\n```\n\nText"),
        # A bare fence cannot be told from an unlabelled listing: left as it came.
        ("```\ncode\n```", "```\ncode\n```"),
    ],
)
@respx.mock
async def test_only_a_markdown_wrapper_is_unwrapped(key, documents, answer, markdown) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json=completion(answer)))
    result = await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert result.markdown == markdown


@respx.mock
async def test_a_prompt_with_stray_braces_still_substitutes_its_tokens(key, documents) -> None:
    """User-supplied templates go through plain replacement, never `str.format`."""
    route = respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json=completion("ok")))

    await transcribe_page(
        key,
        "m",
        'Emit {json} like {"a": 1} for page {page}.',
        documents["png"],
        3,
    )

    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0]["content"][0]["text"] == 'Emit {json} like {"a": 1} for page 3.'


@respx.mock
async def test_rate_limit_carries_retry_after(key, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "12"}))

    with pytest.raises(RateLimited) as raised:
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert raised.value.retry_after == 12


@respx.mock
async def test_payment_required(key, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(402, json={"error": {"code": 402}}))

    with pytest.raises(PaymentRequired):
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)


@respx.mock
async def test_a_rejected_key_is_fatal(key, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(401))

    with pytest.raises(UpstreamError) as raised:
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert raised.value.fatal is True


@respx.mock
async def test_provider_error_inside_a_200(key, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(200, json={"error": {"code": 402, "message": "no credits"}})
    )

    with pytest.raises(PaymentRequired):
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)


@respx.mock
@pytest.mark.parametrize(
    ("response", "transient"),
    [
        (httpx.Response(500), True),
        (httpx.Response(408), True),
        (httpx.Response(200, text="<html>bad gateway</html>"), True),
        (httpx.Response(200, json={"error": {"code": 502}}), True),
        (httpx.Response(400), False),
        (httpx.Response(200, json={"error": {"code": 400}}), False),
    ],
)
async def test_only_failures_that_may_not_repeat_are_transient(
    key, documents, response, transient
) -> None:
    respx.post(KANO_CHAT_URL).mock(return_value=response)

    with pytest.raises(UpstreamError) as raised:
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert raised.value.transient is transient
    assert raised.value.fatal is False


@respx.mock
async def test_an_unreachable_upstream_is_transient(key, documents) -> None:
    respx.post(KANO_CHAT_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(UpstreamError) as raised:
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert raised.value.transient is True


@respx.mock
async def test_a_finished_null_completion_is_an_empty_page(key, documents) -> None:
    body = completion("x")
    body["choices"] = [{"message": {"content": None}, "finish_reason": "stop"}]
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json=body))

    page = await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert page.markdown == ""
    assert page.usage.prompt_tokens == 1200


@respx.mock
@pytest.mark.parametrize("choices", [[], [{"message": {"content": None}}], [{"message": {}}]])
async def test_an_ambiguous_empty_completion_carries_its_usage(key, documents, choices) -> None:
    body = completion("x")
    body["choices"] = choices
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(EmptyCompletion) as raised:
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert raised.value.transient is True
    assert raised.value.usage.prompt_tokens == 1200


@respx.mock
@pytest.mark.parametrize(
    ("finish_reason", "content", "reason"),
    [
        ("length", None, "cut off"),
        ("length", "# Page\n\nthe first half", "cut off"),
        ("content_filter", "", "blocked"),
        ("content_filter", "a recited line", "blocked"),
    ],
)
async def test_a_completion_the_model_stopped_fails_its_page_with_the_reason(
    key, documents, finish_reason, content, reason
) -> None:
    """Never read as blank, nor as the page: whatever text came back is a part of it. Not
    worth asking the same model again, and billed all the same (docs/parsing.md)."""
    body = completion("x")
    body["choices"] = [{"message": {"content": content}, "finish_reason": finish_reason}]
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json=body))

    with pytest.raises(IncompleteCompletion) as raised:
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)
    assert raised.value.reason == reason
    assert not raised.value.transient and not raised.value.fatal
    assert (raised.value.usage.prompt_tokens, raised.value.usage.completion_tokens) == (1200, 300)


def test_the_operator_key_never_reveals_itself_in_a_repr(key) -> None:
    assert repr(key) == "Connection(...)"
    assert "sk-kano" not in repr(key)


@respx.mock
async def test_every_call_carries_the_operator_key_and_asks_for_no_usage(key, documents) -> None:
    """A vision call posts to `{base_url}/chat/completions` as `Bearer` the operator's key
    and nothing else of it (docs/auth.md § 3)."""
    route = respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# Page"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    result = await transcribe_page(key, "vendor/vision", PROMPT, documents["png"], 2)

    assert result.markdown == "# Page"
    assert result.usage.prompt_tokens == 10
    sent = json.loads(route.calls[0].request.content)
    assert "usage" not in sent
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-kano-proxy-test"


@respx.mock
async def test_a_non_object_completion_is_a_page_failure_not_a_crash(key, documents) -> None:
    """A `[]` body must surface as UpstreamError (a failed page), never an AttributeError
    that marks the whole job as an internal error."""
    respx.post(KANO_CHAT_URL).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(UpstreamError):
        await transcribe_page(key, "m", PROMPT, documents["png"], 1)


@respx.mock
async def test_an_oversized_completion_fails_the_call(key, documents) -> None:
    """The response body cap applies to vision completions too (docs/parsing.md)."""
    respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(
            200, content=b"x" * 1024, headers={"content-type": "application/json"}
        )
    )

    with pytest.raises(UpstreamError):
        await transcribe_page(key, "m", PROMPT, documents["png"], 1, max_response_bytes=64)


# --- reported cost on a connection (docs/parsing.md § Upstream usage) ------------------

PROXY_CHAT = "https://proxy.example/openai/v1/chat/completions"


def _proxy() -> Connection:
    return Connection(
        api_key="sk-kano-proxy-test",
        base_url="https://proxy.example/openai/v1",
        snapshot="https://proxy.example",
    )


async def _usage_for(connection: Connection, usage: dict, documents):
    # Serialized by hand: `json.dumps` writes NaN the way a sloppy server would, and
    # httpx's own `json=` refuses to.
    body = json.dumps({"choices": [{"message": {"content": "# Page"}}], "usage": usage})
    respx.post(PROXY_CHAT).mock(return_value=httpx.Response(200, content=body.encode()))
    result = await transcribe_page(connection, "m", PROMPT, documents["png"], 1)
    return result.usage


@respx.mock
async def test_a_reported_cost_is_recorded_as_an_estimate(documents) -> None:
    usage = await _usage_for(
        _proxy(),
        {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0123456789},
        documents,
    )

    assert usage.cost_source == "estimated"
    assert usage.cost == Decimal("0.012346")
    assert usage.prompt_tokens == 10


@respx.mock
async def test_a_reported_zero_is_a_real_estimate(documents) -> None:
    """An endpoint that prices a call at 0 said so; that is not the same as not saying."""
    usage = await _usage_for(_proxy(), {"cost": 0}, documents)

    assert usage.cost_source == "estimated"
    assert usage.cost == Decimal("0")


@respx.mock
@pytest.mark.parametrize(
    "usage_body",
    [
        {"prompt_tokens": 10},  # no cost at all
        {"cost": None},  # kano-proxy's "cannot price this model"
        {"cost": "0.01"},  # a string is not a JSON number
        {"cost": True},
        {"cost": -0.5},
        {"cost": float("nan")},
        {"cost": 1e12},  # beyond NUMERIC(12,6)
        {"cost": {"total": 0.01}},
    ],
)
async def test_an_unusable_reported_cost_is_unknown_not_zero(documents, usage_body) -> None:
    usage = await _usage_for(_proxy(), usage_body, documents)

    assert usage.cost_source == "unknown"
    assert usage.cost is None


@respx.mock
async def test_kano_proxy_without_a_reported_cost_is_unpriced_not_free(key, documents) -> None:
    """The built-in upstream's cost is recorded whenever it says one — and when it does
    not (a model it cannot price), the call is unknown, never 0 and never billed."""
    respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}], "usage": {"prompt_tokens": 1}}
        )
    )

    result = await transcribe_page(key, "m", PROMPT, documents["png"], 1)

    assert result.usage.cost_source == "unknown"
    assert result.usage.cost is None
