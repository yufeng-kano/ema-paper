"""The summary model's instructions (docs/chat.md § Conversations, turns, events, *The
summary model*).

The summary model is the deployment's small model, `SUMMARY_MODEL`, for short writing that
needs no conversation of its own; today it writes the title `/rename` asks for. What it is
told and how its answer is read live here — no HTTP, no database. Its one call and what it
costs are `jobs/summary.py`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .memory import clean_text

# A conversation's title, whoever writes it: the first line, a rename, the summary model.
TITLE_MAX_CHARS = 80

TITLE_SYSTEM = (
    "You name conversations. A reader is reading a document in a reading app and talking "
    "about it with an AI agent. Write a title for their conversation.\n\n"
    "Name its subject: what the document is, and what the reader is doing with it or asking "
    "about it. Never name the command the reader typed (such as /translate or /compact), and "
    "never a greeting.\n\n"
    "- At most eight words; in Chinese or Japanese, at most twenty characters.\n"
    "- Write it in the language whose tag is {language}.\n"
    "- No quotation marks, no brackets around it, no emoji, no full stop at the end.\n"
    "- Answer with the title alone, on one line."
)

TITLE_TASK = "Write the title."

# What a model may wrap its title in, opening and closing: quotes, CJK corner brackets,
# double angle brackets, backticks, asterisks.
_WRAPPERS = (
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("`", "`"),
    ("**", "**"),
    ("*", "*"),
)
_FULL_STOPS = re.compile(r"[.。．]+$")


class StoredMessage(Protocol):
    """What a `messages` row offers here."""

    role: str
    content: list
    turn_id: object


@dataclass(frozen=True)
class Exchange:
    """One `message` turn as a title reads it: the reader's line as the thread shows it,
    and the agent's text of that turn."""

    reader: str
    agent: str


def _reader_line(content: list) -> str:
    """The reader's own text — the line as typed, a command's `/name` with it — and the
    names of what they attached; never the selection, a section or a context block."""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        attachment = block.get("attachment")
        if isinstance(attachment, dict) and isinstance(attachment.get("name"), str):
            parts.append(f"[attached: {attachment['name']}]")
        elif block.get("type") == "text" and not block.get("context"):
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part.strip())


def _agent_text(content: list) -> str:
    return "\n\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def title_exchanges(
    message_turns: Sequence[object], messages: Sequence[StoredMessage], max_chars: int
) -> list[Exchange]:
    """The first and the latest of `message_turns` — the ids of the branch's `message`
    turns, root first — each part cut at `max_chars`: what a title is written from. A
    turn's reader line is its first user message's; its agent text, every text block its
    assistant messages hold, in order (tool results are user messages, and say nothing
    here)."""
    if not message_turns:
        return []
    first, latest = message_turns[0], message_turns[-1]
    picked = [first] if first == latest else [first, latest]
    exchanges = []
    for turn_id in picked:
        own = [message for message in messages if message.turn_id == turn_id]
        reader = next((m for m in own if m.role == "user"), None)
        agent = "\n\n".join(
            text for m in own if m.role == "assistant" and (text := _agent_text(m.content).strip())
        )
        exchanges.append(
            Exchange(
                reader=_reader_line(reader.content if reader else [])[:max_chars].strip(),
                agent=agent[:max_chars].strip(),
            )
        )
    return exchanges


def title_request(
    model: str,
    document_name: str,
    exchanges: Sequence[Exchange],
    language: str,
    max_tokens: int,
) -> dict:
    """The one Messages request a title is asked with: no tools, no thinking asked for."""
    parts = [f"<document>{document_name}</document>"]
    for exchange in exchanges:
        parts.append(
            f"<exchange>\n<reader>{exchange.reader}</reader>\n<agent>{exchange.agent}</agent>\n</exchange>"
        )
    parts.append(TITLE_TASK)
    return {
        "model": model,
        "max_tokens": max_tokens,
        "system": TITLE_SYSTEM.format(language=language),
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def clean_title(answer: str) -> str:
    """The answer's first non-empty line as a title: cleaned as a rename's text is, the
    quotes or brackets around it and a final full stop taken off, cut at the cap. Empty
    when nothing is left."""
    line = next((line for line in answer.splitlines() if line.strip()), "")
    # A full stop may stand after the brackets (「…」。) or inside them ("….").
    title = _FULL_STOPS.sub("", clean_text(line, single_line=True)).strip()
    for opening, closing in _WRAPPERS:
        wrapped = title.startswith(opening) and title.endswith(closing)
        if wrapped and len(title) > len(opening) + len(closing):
            title = title[len(opening) : -len(closing)].strip()
    title = _FULL_STOPS.sub("", title).strip()
    return title[:TITLE_MAX_CHARS].strip()
