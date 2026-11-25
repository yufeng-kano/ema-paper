"""The format check's run: the program rules, then the check agent when they leave spots
(docs/parsing.md § Format check).

The parse's runner calls it once every page has succeeded and the document is assembled.
It answers the reading copy, or raises `CheckFailed` with the reason the job's error
names. The agent speaks the Messages API, as the chat agent does, and each of its calls is
billed through `on_usage` before anything of it is used — a call that broke off after its
stream started with what it reported so far, as a cut-short chat call is.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..agents import check as check_agent
from ..config import Settings
from ..parsing import joins
from ..upstream.anthropic import ChatUsage, CompletedMessage, StreamProgress, stream_messages
from ..upstream.client import (
    Connection,
    PaymentRequired,
    RateLimited,
    UpstreamError,
    with_retries,
)


class CheckFailed(Exception):
    """The check could not finish, and the job fails with this reason: a fixed phrase,
    never the provider's prose (docs/parsing.md § Format check)."""


@dataclass(frozen=True)
class CheckOutcome:
    markdown: str
    joins: int
    edits: int


def _spots_message(spots: list[joins.Spot]) -> str:
    """The places the rules left, each with its excerpt, in document order."""
    places = []
    for number, spot in enumerate(spots, 1):
        if spot.kind == "join":
            body = (
                f"<end_of_previous_page>\n{spot.excerpt}\n</end_of_previous_page>\n"
                f"<start_of_page>\n{spot.after}\n</start_of_page>"
            )
        else:
            body = spot.excerpt
        opening = f'<place n="{number}" kind="{spot.kind}" page="{spot.page}">'
        places.append(f"{opening}\n{body}\n</place>")
    return f"{len(spots)} places need a decision:\n\n" + "\n\n".join(places)


def _run_tool(pages: list[joins.Page], block: dict, malformed: set[str]) -> tuple[str | None, str]:
    """One call of the agent's, made on `pages`: why it was refused (None when it was not),
    and what it answers."""
    name = block.get("name")
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    if str(block.get("id")) in malformed:
        return "The tool input was not valid JSON; send it again.", ""
    if name == "read_page":
        page = tool_input.get("page")
        text = joins.page_text(pages, page) if type(page) is int else None
        if text is None:
            return f"there is no page {page} in the document", ""
        return None, text or f"(page {page} is empty: its text was joined to the page before)"
    if name == "join_pages":
        page = tool_input.get("page")
        if type(page) is not int:
            return "page must be an integer", ""
        return joins.join_pages(pages, page), "Joined."
    if name == "str_replace":
        old, new = tool_input.get("old_str"), tool_input.get("new_str")
        if not isinstance(old, str) or not isinstance(new, str):
            return "old_str and new_str must be strings", ""
        return joins.replace_once(pages, old, new), "Replaced."
    return f"{name!r} is not one of this agent's tools", ""


async def _call(
    connection: Connection,
    settings: Settings,
    body: dict,
    on_usage: Callable[[ChatUsage], Awaitable[None]],
) -> CompletedMessage:
    """One model call, with the vision call's retry policy (docs/parsing.md § Upstream
    usage). An attempt that fails after its stream started is billed what it used before
    it is retried or raised; the completed one is the caller's to bill."""

    async def attempt() -> CompletedMessage:
        progress = StreamProgress()
        try:
            async with contextlib.aclosing(
                stream_messages(
                    connection,
                    body,
                    max_bytes=settings.upstream_response_max_bytes,
                    line_max_bytes=settings.upstream_sse_line_max_bytes,
                    blocks_max=settings.upstream_content_blocks_max,
                    progress=progress,
                )
            ) as stream:
                async for item in stream:
                    if isinstance(item, CompletedMessage):
                        return item
            raise UpstreamError("The upstream closed the stream early", transient=True)
        except BaseException:
            used = progress.usage_so_far()
            if used is not None:
                await on_usage(used)
            raise

    return await with_retries(attempt)


async def run_check(
    connection: Connection,
    settings: Settings,
    *,
    model: str,
    effort: str,
    system: str,
    markdown: str,
    on_usage: Callable[[ChatUsage], Awaitable[None]],
) -> CheckOutcome:
    """Check one assembled document. `connection` is Kano Proxy on its Anthropic-format
    route, called only when the rules leave a spot."""
    lead, pages = joins.split_pages(markdown)
    joined = joins.sure_joins(pages)
    spots = joins.find_spots(pages)[: settings.check_spots_max]
    if not spots:
        return CheckOutcome(joins.render(lead, pages), joins=joined, edits=0)

    messages: list[dict] = [{"role": "user", "content": _spots_message(spots)}]
    edits = 0
    for _ in range(settings.check_max_calls):
        body = {
            "model": model,
            "max_tokens": settings.check_max_output_tokens,
            "system": system,
            "tools": list(check_agent.TOOLS),
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
            "cache_control": {"type": "ephemeral"},
        }
        try:
            completed = await _call(connection, settings, body, on_usage)
        except PaymentRequired:
            raise CheckFailed("payment") from None
        except RateLimited:
            raise CheckFailed("rate limited") from None
        except UpstreamError as exc:
            if exc.fatal:
                raise CheckFailed("the upstream rejected the operator's key") from None
            raise CheckFailed("upstream call failed") from exc
        # Billed whatever comes of it.
        await on_usage(completed.usage)
        if completed.stop_reason == "refusal":
            raise CheckFailed("blocked")
        if completed.stop_reason in ("max_tokens", "model_context_window_exceeded"):
            raise CheckFailed("cut off")
        tool_uses = [block for block in completed.content if block.get("type") == "tool_use"]
        if not tool_uses:
            # The agent ended its turn: what the pages hold now is the reading copy.
            return CheckOutcome(joins.render(lead, pages), joins=joined, edits=edits)
        results = []
        for block in tool_uses:
            error, output = _run_tool(pages, block, completed.malformed_tool_uses)
            if error is None and block.get("name") == "join_pages":
                joined += 1
            elif error is None and block.get("name") == "str_replace":
                edits += 1
            result = {
                "type": "tool_result",
                "tool_use_id": str(block.get("id")),
                "content": output if error is None else error,
            }
            if error is not None:
                result["is_error"] = True
            results.append(result)
        messages.append({"role": "assistant", "content": completed.content})
        messages.append({"role": "user", "content": results})
    raise CheckFailed("not finished")
