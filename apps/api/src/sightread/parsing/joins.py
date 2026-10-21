"""The format check's program rules (docs/parsing.md § Format check).

A parsed document as its pages; the joins at page breaks the rules are sure of; the spots
they leave for the check agent; and the two changes the agent may make, a join and a
markup-only replacement, each of which keeps the document's text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .markdown import page_marker

MARKER_LINE_RE = re.compile(r"^<!-- page: (\d+) -->$", re.MULTILINE)
# A sentence's end: . ! ? : ; or a full-width form, a closing quote or bracket after one.
SENTENCE_END_RE = re.compile(r"[.!?:;。！？：；][\"'”’)\]）」』]*$")
STARTS_LOWER_RE = re.compile(r"^[\"'“‘(\[]*[a-z]")
HYPHEN_END_RE = re.compile(r"[A-Za-z]-$")
LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
RULE_RE = re.compile(r"^(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
# A paragraph that is one bold run starting with a section number: a heading set in bold.
BOLD_HEADING_RE = re.compile(r"^\*\*\d+(?:\.\d+)*\.?\s+[^*\n]+\*\*$")
# What the text key leaves out: comments, tags, the LaTeX delimiters, the markup
# characters and whitespace (docs/parsing.md § Format check).
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
LATEX_DELIMITER_RE = re.compile(r"\\[()\[\]]")
MARKUP_RE = re.compile(r"[\s#*_`~$\\|>:-]")
# The blocks a page break can cut in two, and so the ones a join may move.
JOINABLE = frozenset({"paragraph", "list", "table"})
# How much of a block a spot shows the agent; `read_page` has the rest.
EXCERPT_CHARS = 600


@dataclass
class Page:
    number: int
    body: str


@dataclass(frozen=True)
class Spot:
    """A place the rules could not settle: a page break (`join`, at the break before
    `page`), or a block whose markup is left open (`emphasis`, `math`) or looks like a
    heading set in bold (`heading`)."""

    kind: str
    page: int
    excerpt: str
    # The start of the second part, for a join; `excerpt` is then the end of the first.
    after: str = ""


def split_pages(markdown: str) -> tuple[str, list[Page]]:
    """The document as `assemble` writes it: each page's marker line, then its body. Any
    text before the first marker comes back as the lead, which nothing changes."""
    parts = MARKER_LINE_RE.split(markdown)
    pages = [
        Page(number=int(parts[index]), body=parts[index + 1].strip("\n"))
        for index in range(1, len(parts), 2)
    ]
    return parts[0], pages


def render(lead: str, pages: list[Page]) -> str:
    """The pages written back the way `assemble` writes them; a page a join emptied keeps
    its marker."""
    marked = "\n\n".join(
        f"{page_marker(page.number)}\n{page.body}" if page.body else page_marker(page.number)
        for page in pages
    )
    return f"{lead}{marked}"


def block_spans(body: str) -> list[tuple[int, int]]:
    """Where a page's blocks are: runs of lines between blank lines, with a fenced code
    block or a display formula whole whatever blank lines it holds."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    end = 0
    closer: str | None = None
    offset = 0
    for line in body.split("\n"):
        line_start, line_end = offset, offset + len(line)
        offset = line_end + 1
        stripped = line.strip()
        if closer is not None:
            end = line_end
            # A formula closes where a line ends with its delimiter, a fence where one
            # starts with it.
            math = closer in ("$$", "\\]")
            if stripped.endswith(closer) if math else stripped.startswith(closer):
                closer = None
            continue
        if not stripped:
            if start is not None:
                spans.append((start, end))
                start = None
            continue
        if start is None:
            start = line_start
        end = line_end
        if stripped.startswith(("```", "~~~")):
            closer = stripped[:3]
        elif stripped.startswith("$$") and not (len(stripped) >= 4 and stripped.endswith("$$")):
            closer = "$$"
        elif stripped == "\\[":
            closer = "\\]"
    if start is not None:
        spans.append((start, end))
    return spans


def block_kind(block: str) -> str:
    first = block.lstrip()
    if first.startswith(("```", "~~~")):
        return "code"
    if first.startswith(("$$", "\\[")):
        return "math"
    if first.startswith("#"):
        return "heading"
    if first.startswith("<!--"):
        return "comment"
    if first.startswith("!["):
        return "figure"
    if first.startswith(">"):
        return "quote"
    if first.startswith("|"):
        return "table"
    if RULE_RE.match(first.split("\n", 1)[0].strip()):
        return "rule"
    if LIST_ITEM_RE.match(first):
        return "list"
    return "paragraph"


def text_key(text: str) -> str:
    """The text with markup and whitespace taken out: what no change may alter."""
    text = COMMENT_RE.sub("", text)
    text = TAG_RE.sub("", text)
    text = LATEX_DELIMITER_RE.sub("", text)
    return MARKUP_RE.sub("", text)


def _before(pages: list[Page], index: int) -> int | None:
    """The page a break before `pages[index]` continues: the nearest one before it with a
    block, since a join can empty a page."""
    for candidate in range(index - 1, -1, -1):
        if pages[candidate].body.strip():
            return candidate
    return None


def _edges(pages: list[Page], index: int) -> tuple[int, str, str] | None:
    """The break before `pages[index]`: the page before it, its last block and this
    page's first block — None when either side has no block."""
    before = _before(pages, index)
    if before is None or not pages[index].body.strip():
        return None
    upper = pages[before].body
    last = upper[block_spans(upper)[-1][0] :]
    first_start, first_end = block_spans(pages[index].body)[0]
    return before, last, pages[index].body[first_start:first_end]


def _join(pages: list[Page], before: int, index: int) -> None:
    """Move the first block of `pages[index]` up to continue the last block of
    `pages[before]`: a table takes the second table's rows, its separator row dropped and
    its header kept as a row; anything else continues after a space, or after nothing when
    the first part ends in a hyphen after a letter — the hyphen stays."""
    upper, lower = pages[before], pages[index]
    last = upper.body[block_spans(upper.body)[-1][0] :]
    first_end = block_spans(lower.body)[0][1]
    first = lower.body[:first_end]
    if block_kind(last) == "table" and block_kind(first) == "table":
        rows = first.split("\n")
        if len(rows) > 1 and TABLE_SEPARATOR_RE.match(rows[1]):
            del rows[1]
        upper.body = upper.body + "\n" + "\n".join(rows)
    else:
        separator = "" if HYPHEN_END_RE.search(last) else " "
        upper.body = upper.body + separator + first.lstrip()
    lower.body = lower.body[first_end:].lstrip("\n")


def sure_joins(pages: list[Page]) -> int:
    """Join every break that cut a paragraph in two for certain: a paragraph that ends
    without closing punctuation, then one that starts in lowercase. Returns how many."""
    joined = 0
    for index in range(1, len(pages)):
        edges = _edges(pages, index)
        if edges is None:
            continue
        before, last, first = edges
        if (
            block_kind(last) == "paragraph"
            and block_kind(first) == "paragraph"
            and not SENTENCE_END_RE.search(last)
            and STARTS_LOWER_RE.match(first)
        ):
            _join(pages, before, index)
            joined += 1
    return joined


def _odd_math(block: str) -> bool:
    """An odd number of `$` once escaped dollars and `$$` pairs are left out: a formula
    left open, or a dollar sign that is no formula."""
    return block.replace("\\$", "").replace("$$", "").count("$") % 2 == 1


def find_spots(pages: list[Page]) -> list[Spot]:
    """What the rules cannot settle, in document order (docs/parsing.md § Format check)."""
    spots: list[Spot] = []
    for index, page in enumerate(pages):
        edges = _edges(pages, index) if index else None
        if edges is not None:
            _, last, first = edges
            upper, lower = block_kind(last), block_kind(first)
            cut = (
                (
                    upper == lower == "paragraph"
                    and not SENTENCE_END_RE.search(last)
                    and not STARTS_LOWER_RE.match(first)
                )
                or (upper == "list" and lower == "paragraph" and STARTS_LOWER_RE.match(first))
                or upper == lower == "table"
            )
            if cut:
                spots.append(
                    Spot("join", page.number, last[-EXCERPT_CHARS:], after=first[:EXCERPT_CHARS])
                )
        for start, end in block_spans(page.body):
            block = page.body[start:end]
            kind = block_kind(block)
            if kind in ("code", "math", "comment", "figure", "rule"):
                continue
            excerpt = block[: EXCERPT_CHARS * 2]
            if block.count("**") % 2 == 1:
                spots.append(Spot("emphasis", page.number, excerpt))
            if _odd_math(block):
                spots.append(Spot("math", page.number, excerpt))
            if kind == "paragraph" and BOLD_HEADING_RE.match(block.strip()):
                spots.append(Spot("heading", page.number, excerpt))
    return spots


def page_text(pages: list[Page], number: int) -> str | None:
    for page in pages:
        if page.number == number:
            return page.body
    return None


def join_pages(pages: list[Page], number: int) -> str | None:
    """The agent's join at the break before page `number`. None when it was made, else
    why not."""
    index = next((i for i, page in enumerate(pages) if page.number == number), None)
    if index is None:
        return f"there is no page {number}"
    edges = _edges(pages, index)
    if edges is None:
        return f"page {number} has nothing to join"
    before, last, first = edges
    if block_kind(last) not in JOINABLE or block_kind(first) not in JOINABLE:
        return "only paragraphs, list items and tables join across a page break"
    _join(pages, before, index)
    return None


def replace_once(pages: list[Page], old: str, new: str) -> str | None:
    """The agent's replacement. None when it was made, else why not: `old` must occur
    exactly once, neither side may hold a page marker, and the text must stay the same."""
    if not old:
        return "old_str is empty"
    if "<!-- page:" in old or "<!-- page:" in new:
        return "an edit may not hold a page marker"
    occurrences = sum(page.body.count(old) for page in pages)
    if occurrences != 1:
        return f"old_str occurs {occurrences} times; it must occur exactly once"
    if text_key(old) != text_key(new):
        return "the edit changes the text; only markup may change"
    for page in pages:
        if old in page.body:
            page.body = page.body.replace(old, new, 1)
    return None
