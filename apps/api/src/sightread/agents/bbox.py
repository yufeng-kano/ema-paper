"""Coordinate conventions per model family (docs/parsing.md § Coordinate conventions).

Every stored result declares `yxyx_norm1000`; that contract never moves. What moves is the
convention a model is *asked* for, because vision models are trained on different ones and
asking for the wrong one costs box accuracy. This module picks the convention from the
model id, writes the prompt paragraph that asks for it, prepares the image the convention
needs, and converts the model's answer back into the stored contract before anything
downstream sees it. Nothing here is a user setting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from ..parsing.markdown import BBOX_MAX, PLACEHOLDER_RE

CANONICAL_FORMAT = "yxyx_norm1000"
PIXEL_FORMAT = "xyxy_px"

_FIGURE_LEAD = (
    "Where a figure, chart, photograph, diagram or map appears, emit a line of the form\n"
)
_CAPTION_RULE = (
    "at that position, with the figure's caption verbatim on the very next line (an empty line\n"
    "when it has none). "
)


@dataclass(frozen=True)
class BboxConvention:
    family: str
    format: str

    @property
    def pixel(self) -> bool:
        return self.format == PIXEL_FORMAT

    def instructions(self, width_px: int, height_px: int) -> str:
        """What `{bbox_instructions}` expands to: the placeholder to emit and what its four
        integers mean. `{page}` is left for the caller's substitution."""
        if self.pixel:
            return (
                f"{_FIGURE_LEAD}![fig](sightread://p{{page}}/X1,Y1,X2,Y2)\n{_CAPTION_RULE}"
                f"X1, Y1, X2 and Y2 are integer pixel coordinates in this page image, which is "
                f"{width_px} pixels wide and {height_px} pixels tall: [x1, y1, x2, y2], the "
                f"top-left and bottom-right corners of the box, origin at the top-left corner "
                f"of the image."
            )
        return (
            f"{_FIGURE_LEAD}![fig](sightread://p{{page}}/YMIN,XMIN,YMAX,XMAX)\n{_CAPTION_RULE}"
            f"YMIN, XMIN, YMAX and XMAX are integers in the {CANONICAL_FORMAT} coordinate "
            f"space: [ymin, xmin, ymax, xmax] normalized to 0-{BBOX_MAX}, origin at the "
            f"top-left corner of this page image."
        )


GEMINI = BboxConvention(family="gemini", format=CANONICAL_FORMAT)
CLAUDE = BboxConvention(family="claude", format=PIXEL_FORMAT)
GPT = BboxConvention(family="gpt", format=PIXEL_FORMAT)
DEFAULT = BboxConvention(family="default", format=CANONICAL_FORMAT)

# Kano Proxy lists models as `vendor/model`; the bare names cover a connection that does not.
_FAMILIES: tuple[tuple[str, str, BboxConvention], ...] = (
    ("google/", "gemini-", GEMINI),
    ("anthropic/", "claude-", CLAUDE),
    ("openai/", "gpt-", GPT),
)


def convention_for_model(model: str | None) -> BboxConvention:
    name = (model or "").strip().lower()
    for vendor, bare, convention in _FAMILIES:
        if name.startswith(vendor) or name.startswith(bare):
            return convention
    return DEFAULT


# --- the image the convention needs ------------------------------------------------------

# Claude's standard resolution tier (its vision-coordinates docs): no edge over this many
# pixels, and no more than this many 28x28 visual tokens. Sending the image at exactly the
# size Claude would resize it to makes its pixel answer map one-to-one onto what we hold.
CLAUDE_MAX_EDGE_PX = 1568
CLAUDE_MAX_TOKENS = 1568
_PATCH = 28


def claude_input_size(
    width: int,
    height: int,
    max_edge: int = CLAUDE_MAX_EDGE_PX,
    max_tokens: int = CLAUDE_MAX_TOKENS,
) -> tuple[int, int]:
    """The size Claude resizes a `width x height` image to before padding (the published
    reference implementation): unchanged when it already fits."""

    def fits(w: int, h: int) -> bool:
        return (
            math.ceil(w / _PATCH) * _PATCH <= max_edge
            and math.ceil(h / _PATCH) * _PATCH <= max_edge
            and math.ceil(w / _PATCH) * math.ceil(h / _PATCH) <= max_tokens
        )

    if fits(width, height):
        return width, height
    if height > width:
        resized_h, resized_w = claude_input_size(height, width, max_edge, max_tokens)
        return resized_w, resized_h
    aspect = width / height
    low, high = 1, width  # low always fits; high never does
    while low + 1 < high:
        mid = (low + high) // 2
        if fits(mid, max(round(mid / aspect), 1)):
            low = mid
        else:
            high = mid
    return low, max(round(low / aspect), 1)


@dataclass(frozen=True)
class PreparedImage:
    """The image a page is sent as, with the dimensions its pixel coordinates refer to."""

    path: Path
    width_px: int
    height_px: int
    # True when `path` is a resized copy the caller must delete; False when it is the render.
    derived: bool

    def cleanup(self) -> None:
        if self.derived:
            self.path.unlink(missing_ok=True)


def prepare_image(image: Path, convention: BboxConvention) -> PreparedImage:
    """Size the render for the model. Claude gets the exact size it would resize to; every
    other family gets the render as it is."""
    with Image.open(image) as opened:
        width, height = opened.width, opened.height
        if convention is not CLAUDE:
            return PreparedImage(image, width, height, derived=False)
        target_w, target_h = claude_input_size(width, height)
        if (target_w, target_h) == (width, height):
            return PreparedImage(image, width, height, derived=False)
        resized = opened.convert("RGB").resize((target_w, target_h), Image.LANCZOS)
        target = image.with_name(f"{image.stem}-sent{image.suffix}")
        resized.save(target, format="PNG" if image.suffix.lower() == ".png" else "JPEG")
    return PreparedImage(target, target_w, target_h, derived=True)


# --- back into the stored contract -----------------------------------------------------------


def canonicalize(markdown: str, convention: BboxConvention, width_px: int, height_px: int) -> str:
    """Rewrite every placeholder from the convention the model was asked for into
    `yxyx_norm1000`. Clamping and rejection stay with `clean_bbox`, downstream."""
    if not convention.pixel or width_px <= 0 or height_px <= 0:
        return markdown

    def rewrite(match) -> str:
        # The pattern names its groups for the stored order; a pixel answer fills them in
        # the order it was asked for: x1, y1, x2, y2.
        x1, y1, x2, y2 = (int(match.group(name)) for name in ("ymin", "xmin", "ymax", "xmax"))
        y_min = round(y1 / height_px * BBOX_MAX)
        x_min = round(x1 / width_px * BBOX_MAX)
        y_max = round(y2 / height_px * BBOX_MAX)
        x_max = round(x2 / width_px * BBOX_MAX)
        return (
            f"![{match.group('alt')}](sightread://p{match.group('page')}/"
            f"{y_min},{x_min},{y_max},{x_max})"
        )

    return PLACEHOLDER_RE.sub(rewrite, markdown)
