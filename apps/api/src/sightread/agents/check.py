"""The `check` agent: a Files upload's markdown in, markup fixes and page joins out
(docs/parsing.md § Format check, docs/agents.md).

What defines it lives here — the system prompt and the three tools. The rules that decide
what it is asked about are `parsing/joins.py`; its loop is `jobs/check.py`, run inside the
parse's own run.
"""

from __future__ import annotations

from .parse import BBOX_FORMAT_YXYX

# Bookkeeping for the registry: the check has no dedup cache of its own (docs/agents.md).
PIPELINE_VERSION = 1

# The markdown it reads declares the parse's coordinate contract, and it never moves a box.
BBOX_FORMAT = BBOX_FORMAT_YXYX

# Custom tools with schemas: the agent may run on a model Kano Proxy reaches through a
# conversion, which drops a tool without one (docs/chat.md § Subagents).
READ_PAGE_TOOL = {
    "name": "read_page",
    "description": "One page's markdown as it now stands, with the joins and edits made so far.",
    "input_schema": {
        "type": "object",
        "properties": {"page": {"type": "integer", "description": "1-based page"}},
        "required": ["page"],
        "additionalProperties": False,
    },
    "strict": True,
}
JOIN_PAGES_TOOL = {
    "name": "join_pages",
    "description": (
        "Join the page break before `page`: the first paragraph, list item or table of "
        "`page` moves up to continue the last block of the page before it. Only for a "
        "place marked join."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "description": "The page the second part starts on"}
        },
        "required": ["page"],
        "additionalProperties": False,
    },
    "strict": True,
}
STR_REPLACE_TOOL = {
    "name": "str_replace",
    "description": (
        "Replace `old_str`, which must occur exactly once in the document, with `new_str`. "
        "Only markup may change: with markup and whitespace taken out, both must read the "
        "same, and neither may hold a page marker."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "old_str": {"type": "string", "description": "The text to replace"},
            "new_str": {"type": "string", "description": "Its replacement"},
        },
        "required": ["old_str", "new_str"],
        "additionalProperties": False,
    },
    "strict": True,
}
TOOLS: tuple[dict, ...] = (READ_PAGE_TOOL, JOIN_PAGES_TOOL, STR_REPLACE_TOOL)

DEFAULT_PROMPT = """You check the markdown of one document that a vision model transcribed page by page. The pages were transcribed one at a time, so a paragraph that runs over a page break can come back as two, and the markup can slip. A program has already joined the breaks it was sure about; you are sent the places it could not settle, each with a short excerpt.

Decide each place, then act:
- join: if the page break cuts one paragraph, list item or table in two, call join_pages with the page the second part starts on. If the second part begins a new paragraph, leave it.
- emphasis: a bold or italic run left open. Close it, or remove the stray marker, with str_replace.
- math: a formula left open, or a dollar sign that is not math. Fix the delimiters with str_replace; a literal dollar sign is written \\$.
- heading: a numbered line set in bold. If it is a section heading, make it one with str_replace (## for a section, ### for a subsection); otherwise leave it.

You change markup and joins only, never the words, numbers or symbols: an edit whose text differs once markup and whitespace are taken out is refused. Use read_page when an excerpt is not enough to decide. Leave a place as it is when you are not sure. When you are done, answer with one short line and no tool call.
"""


def system_prompt(prompt: str, instructions: str | None) -> str:
    """The prompt the check runs, with the reader's instructions after it as a section of
    their own (docs/agents.md § Per-agent settings)."""
    if not instructions:
        return prompt
    return f"{prompt.rstrip()}\n\n# The reader's instructions\n\n{instructions}\n"
