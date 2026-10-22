"""The format check's run: the program rules, then the check agent when they leave spots
(docs/parsing.md § Format check).

The parse's runner calls it once every page has succeeded and the document is assembled.
It answers the reading copy, or raises `CheckFailed` with the reason the job's error
names. The agent speaks the Messages API, as the chat agent does, and each of its calls is
handed to `on_usage` to be billed before anything of it is used.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..agents import check as check_agent
from ..config import Settings
from ..parsing import joins
from ..upstream.anthropic import CompletedMessage, stream_messages
from ..upstream.client import Connection, PaymentRequired, RateLimited, UpstreamError

# The vision call's retry policy, for each of the agent's calls (docs/parsing.md
# § Upstream usage).
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.0


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


async def _call(connection: Connection, settings: Settings, body: dict) -> CompletedMessage:
    """One model call, backing off on 429 and on transient failures."""
    for attempt in range(MAX_ATTEMPTS):
        last = attempt == MAX_ATTEMPTS - 1
        try:
            async with contextlib.aclosing(
                stream_messages(
                    connection,
                    body,
                    max_bytes=settings.upstream_response_max_bytes,
                    line_max_bytes=settings.upstream_sse_line_max_bytes,
                    blocks_max=settings.upstream_content_blocks_max,
                )
            ) as stream:
                async for item in stream:
                    if isinstance(item, CompletedMessage):
                        return item
            raise UpstreamError("The upstream closed the stream early", transient=True)
        except RateLimited as exc:
            if last:
                raise
            delay = exc.retry_after or BACKOFF_BASE_SECONDS * (2**attempt)
        except UpstreamError as exc:
            if last or not exc.transient:
                raise
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
        await asyncio.sleep(delay)
    raise RateLimited()


async def run_check(
    connection: Connection | None,
    settings: Settings,
    *,
    model: str,
    effort: str,
    system: str,
    markdown: str,
    on_usage: Callable[[CompletedMessage], Awaitable[None]],
) -> CheckOutcome:
    """Check one assembled document. `connection` is the user's Kano Proxy on its
    Anthropic-format route, None when it is not connected: needed only when the rules
    leave a spot."""
    lead, pages = joins.split_pages(markdown)
    joined = joins.sure_joins(pages)
    spots = joins.find_spots(pages)[: settings.check_spots_max]
    if not spots:
        return CheckOutcome(joins.render(lead, pages), joins=joined, edits=0)
    if connection is None:
        raise CheckFailed("Kano Proxy is not connected")

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
            completed = await _call(connection, settings, body)
        except PaymentRequired:
            raise CheckFailed("payment") from None
        except RateLimited:
            raise CheckFailed("rate limited") from None
        except UpstreamError as exc:
            if exc.fatal:
                raise CheckFailed("the upstream rejected the stored key") from None
            raise CheckFailed("upstream call failed") from exc
        # Billed whatever comes of it.
        await on_usage(completed)
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
