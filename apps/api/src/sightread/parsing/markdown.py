"""Markdown assembly and the `sightread://` figure placeholder contract (docs/api.md).

Vision pages arrive with the model's markdown already carrying placeholders. Assembly
gives them document-wide figure ids, page numbers we trust (ours, not the model's),
bounding boxes validated against the 0-1000 coordinate space, and a `<!-- page: N -->`
marker in front of every page so any passage maps back to its page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BBOX_MAX = 1000

# What the figure check made of a figure's box: a figure's `check` in a result
# (docs/parsing.md § Figure check).
VERIFIED = "verified"
ADJUSTED = "adjusted"
UNVERIFIED = "unverified"

# A placeholder as emitted by the model or by us: ![anything](sightread://p3/10,20,30,40).
PLACEHOLDER_RE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\(\s*sightread://p(?P<page>\d+)/"
    r"(?P<ymin>-?\d+)\s*,\s*(?P<xmin>-?\d+)\s*,\s*(?P<ymax>-?\d+)\s*,\s*(?P<xmax>-?\d+)\s*\)"
)


@dataclass
class PageMarkdown:
    page: int
    markdown: str
    # The figure check's status of each box, keyed "ymin,xmin,ymax,xmax"; None for a page
    # parsed without the check, whose figures then carry no `check`.
    figures: dict[str, str] | None = None


@dataclass
class AssembledDocument:
    markdown: str
    figures: list[dict]
    # Placeholders the model emitted with an unusable box; counted, never guessed at.
    dropped_figures: int


def clean_bbox(values: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """Clamp a `[ymin, xmin, ymax, xmax]` box to 0-1000, or reject it as unusable."""
    y_min, x_min, y_max, x_max = (max(0, min(BBOX_MAX, int(value))) for value in values)
    if y_max <= y_min or x_max <= x_min:
        return None
    return y_min, x_min, y_max, x_max


def placeholder(figure_id: str, page: int, bbox: tuple[int, int, int, int]) -> str:
    y_min, x_min, y_max, x_max = bbox
    return f"![{figure_id}](sightread://p{page}/{y_min},{x_min},{y_max},{x_max})"


def page_marker(page: int) -> str:
    """The marker line callers use to map content back to a page (docs/parsing.md)."""
    return f"<!-- page: {page} -->"


def _caption_after(text: str, end: int) -> str:
    """The caption a placeholder claims, read as the result viewer reads it: the line right
    after its own, unless that line is blank or starts a block of its own. A figure without
    a caption is followed by an empty line and claims nothing after it (docs/parsing.md
    § Coordinate contract)."""
    # joins reads pages through this module, so its grammar is imported where it is used.
    from .joins import starts_block

    lines = text[end:].split("\n")
    caption = lines[1].strip() if len(lines) > 1 else ""
    under = lines[2].strip() if len(lines) > 2 else ""
    # A page's placeholders are not normalized yet, so one with spaces between its numbers
    # is a figure that joins' stricter pattern would not see.
    if PLACEHOLDER_RE.search(caption) or starts_block(caption, under):
        return ""
    return caption


def assemble(pages: list[PageMarkdown]) -> AssembledDocument:
    """Join pages into one marked document with document-wide figure ids."""
    figures: list[dict] = []
    dropped = 0
    bodies: list[str] = []

    for page in pages:
        parts: list[str] = []
        cursor = 0
        for match in PLACEHOLDER_RE.finditer(page.markdown):
            parts.append(page.markdown[cursor : match.start()])
            cursor = match.end()
            bbox = clean_bbox(
                (
                    int(match.group("ymin")),
                    int(match.group("xmin")),
                    int(match.group("ymax")),
                    int(match.group("xmax")),
                )
            )
            if bbox is None:
                dropped += 1
                continue
            figure_id = f"fig{len(figures) + 1}"
            # The page number is ours: a model that mislabels the page must not corrupt
            # the coordinate contract.
            figure = {
                "id": figure_id,
                "page": page.page,
                "bbox": list(bbox),
                "caption": _caption_after(page.markdown, match.end()),
            }
            if page.figures is not None:
                # A box the check holds no status for is past the per-page cap: it has no
                # crop, and nothing confirmed it.
                key = ",".join(str(value) for value in bbox)
                figure["check"] = page.figures.get(key, UNVERIFIED)
            figures.append(figure)
            parts.append(placeholder(figure_id, page.page, bbox))
        parts.append(page.markdown[cursor:])

        body = "".join(parts).strip()
        if body:
            bodies.append(f"{page_marker(page.page)}\n{body}")

    return AssembledDocument(markdown="\n\n".join(bodies), figures=figures, dropped_figures=dropped)
