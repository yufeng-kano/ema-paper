"""What the format check's tests share: a three-page paper as a vision model sends it back,
its transcription, the reading the check agent makes of it, and the agent's answers on the
stubbed Messages stream (docs/parsing.md § Format check). No helper reaches the network."""

from __future__ import annotations

import httpx

from tests.chat_helpers import assistant_turn
from tests.conftest import PARSE_MODEL
from tests.helpers import Upstream, completion

# Three pages a vision model might send back for a paper: a sentence the first page break
# cuts before a capital, a bold run left open, and a word the second break hyphenates.
PAGES = {
    1: "## Introduction\n\nWe evaluate on the",
    2: "Atari benchmark. The **key idea is simple.\n\nOur method is experi-",
    3: "mental and works well.",
}
TRANSCRIPTION = (
    "<!-- page: 1 -->\n## Introduction\n\nWe evaluate on the\n\n"
    "<!-- page: 2 -->\nAtari benchmark. The **key idea is simple.\n\nOur method is experi-\n\n"
    "<!-- page: 3 -->\nmental and works well."
)
# The agent's join and its fix, over the rules' own join of the hyphenated word.
READING = (
    "<!-- page: 1 -->\n## Introduction\n\nWe evaluate on the Atari benchmark. "
    "The **key idea** is simple.\n\n"
    "<!-- page: 2 -->\nOur method is experi-mental and works well.\n\n"
    "<!-- page: 3 -->"
)


def paper_upstream() -> Upstream:
    """The vision upstream answering each of `PAGES` on `PARSE_MODEL`."""
    return Upstream(
        {(PARSE_MODEL, page): (lambda text=text: completion(text)) for page, text in PAGES.items()}
    )


def agent_turn(blocks: list[dict], stop_reason: str = "tool_use") -> httpx.Response:
    """One answer of the check agent: `blocks`, ending on `stop_reason`."""
    return httpx.Response(
        200,
        content=assistant_turn(blocks, stop_reason, model="claude-opus-5-5"),
        headers={"content-type": "text/event-stream"},
    )


def tool_use(call_id: str, name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


def agent_done() -> httpx.Response:
    """The check agent's last answer: it is done."""
    return agent_turn([{"type": "text", "text": "Done."}], "end_turn")
