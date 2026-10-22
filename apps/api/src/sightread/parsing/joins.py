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
# A sentence's end: . ! ? : ; or a full-width form, with closing quotes, brackets or
# emphasis markers after one (`**the conclusion.**`).
SENTENCE_END_RE = re.compile(r"[.!?:;。！？：；][\"'”’)\]）」』*_~]*$")
STARTS_LOWER_RE = re.compile(r"^[\"'“‘(\[]*[a-z]")
HYPHEN_END_RE = re.compile(r"[A-Za-z]-$")
# The result viewer's block grammar (apps/web/app/lib/markdown.ts), read the same way here
# so the rules and the reader agree on where a block ends.
HEADING_RE = re.compile(r"^#{1,6}\s+")
LIST_ITEM_RE = re.compile(r"^([-*+]|\d{1,9}[.)])\s+")
RULE_RE = re.compile(r"^(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
CODE_FENCE_RE = re.compile(r"^(?:```|~~~)\s*[^\s`]*\s*$")
TABLE_RULE_RE = re.compile(r"^\|?[\s|:-]+\|[\s|:-]*$")
COMMENT_LINE_RE = re.compile(r"^<!--.*-->$")
FIGURE_RE = re.compile(
    r"^!\[[^\]\n]*\]\(\s*(?:sightread://p\d+/-?\d+,-?\d+,-?\d+,-?\d+"
    r"|figures/img-[0-9a-f]{16}\.(?:png|jpg))\s*\)$"
)
# What a formula holds and an escaped reference number does not.
TEX_LIKE_RE = re.compile(r"[\\^_{}=]")
# A contents page's row: a dot leader of four or more, then the page, arabic or roman.
TOC_LEADER_RE = re.compile(
    r"^.*?\S\s*(?:\.\s*){4,}(?:\*\*|__)?(?:\d+|[ivxlcdm]+)(?:\*\*|__)?$", re.IGNORECASE
)
# A paragraph that is one bold run starting with a section number: a heading set in bold.
BOLD_HEADING_RE = re.compile(r"^\*\*\d+(?:\.\d+)*\.?\s+[^*\n]+\*\*$")
# What the text key leaves out: comments, tags, the LaTeX delimiters, the markup
# characters and whitespace (docs/parsing.md § Format check).
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
LATEX_DELIMITER_RE = re.compile(r"\\[()\[\]]")
MARKUP_RE = re.compile(r"[\s#*_`~$\\|>:-]")
# The cuts a join mends, as (the last block of one page, the first of the next): a
# paragraph continuing a paragraph or a list item, and a table continuing a table.
JOINS = frozenset({("paragraph", "paragraph"), ("list", "paragraph"), ("table", "table")})
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


def _table_rule(line: str) -> bool:
    """A table's separator row, with or without the outer pipes: `| --- | :-: |`."""
    return bool(TABLE_RULE_RE.match(line)) and "-" in line


def _math_close(line: str) -> str | None:
    """The delimiter that closes the display formula `line` opens, or None. `$$` alone
    opens one, as do `$$x = 1$$` and `$$x = 1` closed lines later, but a line with a second
    `$$` before its end is prose; `\\[` opens one alone on its line, or as a whole line
    `\\[…\\]` around TeX, so a reference's `\\[87] A. Author` stays prose."""
    if line.startswith("$$"):
        again = line.find("$$", 2)
        return "$$" if again == -1 or (again == len(line) - 2 and again > 2) else None
    if line == "\\[" or (
        line.startswith("\\[")
        and line.find("\\]", 2) == len(line) - 2
        and TEX_LIKE_RE.search(line[2:-2])
    ):
        return "\\]"
    return None


def starts_block(line: str, under: str) -> bool:
    """Whether a stripped line starts a block of its own, so a paragraph ends above it.
    `under` is the line below it, which only a table's separator row needs."""
    return (
        not line
        or line.startswith((">", "#"))
        or bool(RULE_RE.match(line))
        or _math_close(line) is not None
        or bool(CODE_FENCE_RE.match(line))
        or ("|" in line and _table_rule(under))
        or bool(LIST_ITEM_RE.match(line))
        or bool(FIGURE_RE.match(line))
        or bool(COMMENT_LINE_RE.match(line))
    )


def blocks(body: str) -> list[tuple[int, int, str]]:
    """A page's blocks as the result viewer reads them (apps/web/app/lib/markdown.ts,
    docs/web.md § Result viewer): where each starts and ends in `body`, and its kind. A
    paragraph runs until a blank line or a line that starts another block, so the rules
    and the reader agree on where a block ends, and no join reaches past a paragraph."""
    raws = body.split("\n")
    count = len(raws)
    # Two blank lines past the end, so looking under the last line reads a blank one.
    lines = [raw.strip() for raw in raws] + ["", ""]
    starts = []
    offset = 0
    for raw in raws:
        starts.append(offset)
        offset += len(raw) + 1
    found: list[tuple[int, int, str]] = []
    index = 0
    while index < count:
        line = lines[index]
        if not line:
            index += 1
            continue
        last = index
        if line.startswith(">"):
            kind = "quote"
            while last + 1 < count and lines[last + 1].startswith(">"):
                last += 1
        elif COMMENT_LINE_RE.match(line):
            kind = "comment"
        elif RULE_RE.match(line):
            kind = "rule"
        elif FIGURE_RE.match(line):
            # Its caption is the next line, unless that line starts a block of its own.
            kind = "figure"
            if lines[index + 1] and not starts_block(lines[index + 1], lines[index + 2]):
                last += 1
        elif CODE_FENCE_RE.match(line):
            # To the next fence line, or to the end of the page.
            kind = "code"
            last += 1
            while last < count and not CODE_FENCE_RE.match(lines[last]):
                last += 1
            last = min(last, count - 1)
        elif (close := _math_close(line)) is not None:
            # Closed on its own line, or on the first line after it that ends with the
            # delimiter, or at the end of the page.
            kind = "math"
            if not line[2:].endswith(close):
                last += 1
                while last < count and not raws[last].rstrip().endswith(close):
                    last += 1
                last = min(last, count - 1)
        elif HEADING_RE.match(line):
            kind = "heading"
        elif item := LIST_ITEM_RE.match(line):
            # Items with one marker kind, and the indented lines that wrap one; a rule,
            # an item of the other kind or any other line ends the list.
            kind = "list"
            ordered = item[1] not in ("-", "*", "+")
            while last + 1 < count and not RULE_RE.match(lines[last + 1]):
                following = LIST_ITEM_RE.match(lines[last + 1])
                if following and (following[1] not in ("-", "*", "+")) != ordered:
                    break
                wraps = (
                    raws[last + 1][:1].isspace()
                    and bool(lines[last + 1])
                    and not starts_block(lines[last + 1], lines[last + 2])
                )
                if not following and not wraps:
                    break
                last += 1
        elif "|" in line and _table_rule(lines[index + 1]):
            # Every line still carrying a pipe.
            kind = "table"
            while last + 1 < count and "|" in lines[last + 1]:
                last += 1
        else:
            kind = "paragraph"
            while last + 1 < count and not starts_block(lines[last + 1], lines[last + 2]):
                last += 1
            # A contents page's rows keep their lines: the viewer shows them as a list of
            # entries, not as prose.
            if any(TOC_LEADER_RE.match(row) for row in lines[index : last + 1]):
                kind = "contents"
        found.append((starts[index], starts[last] + len(raws[last]), kind))
        index = last + 1
    return found


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


def _edges(pages: list[Page], index: int) -> tuple[int, str, str, str, str] | None:
    """The break before `pages[index]`: the page before it, its last block and that
    block's kind, then this page's first block and its kind — None when either side has
    no block."""
    before = _before(pages, index)
    if before is None or not pages[index].body.strip():
        return None
    upper, lower = pages[before].body, pages[index].body
    last_start, _, last_kind = blocks(upper)[-1]
    first_start, first_end, first_kind = blocks(lower)[0]
    return before, upper[last_start:], last_kind, lower[first_start:first_end], first_kind


def _join(pages: list[Page], before: int, index: int) -> None:
    """Move the first block of `pages[index]` up to continue the last block of
    `pages[before]`: a table takes the second table's rows, its separator row dropped and
    its header kept as a row; anything else continues after a space, or after nothing when
    the first part ends in a hyphen after a letter — the hyphen stays — and a list item
    takes the continuation's later lines indented, as the viewer reads a wrapped item."""
    upper, lower = pages[before], pages[index]
    last_start, _, last_kind = blocks(upper.body)[-1]
    _, first_end, first_kind = blocks(lower.body)[0]
    last = upper.body[last_start:]
    first = lower.body[:first_end]
    if last_kind == first_kind == "table":
        rows = first.split("\n")
        if len(rows) > 1 and _table_rule(rows[1].strip()):
            del rows[1]
        upper.body = upper.body + "\n" + "\n".join(rows)
    else:
        separator = "" if HYPHEN_END_RE.search(last) else " "
        rows = first.lstrip().split("\n")
        if last_kind == "list":
            # The viewer keeps only indented lines in an item: unindented, the rest of the
            # continuation would be a paragraph of its own under the list.
            rows[1:] = ["  " + row.lstrip() for row in rows[1:]]
        upper.body = upper.body + separator + "\n".join(rows)
    lower.body = lower.body[first_end:].lstrip("\n")


def sure_joins(pages: list[Page]) -> int:
    """Join every break that cut a paragraph in two for certain: a paragraph that ends
    without closing punctuation, then one that starts in lowercase. Returns how many."""
    joined = 0
    for index in range(1, len(pages)):
        edges = _edges(pages, index)
        if edges is None:
            continue
        before, last, last_kind, first, first_kind = edges
        if (
            last_kind == first_kind == "paragraph"
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
            _, last, upper, first, lower = edges
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
        for start, end, kind in blocks(page.body):
            block = page.body[start:end]
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
    before, _, last_kind, _, first_kind = edges
    if (last_kind, first_kind) not in JOINS:
        return (
            "only a paragraph continuing a paragraph or a list item, or a table continuing "
            "a table, joins across a page break"
        )
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
