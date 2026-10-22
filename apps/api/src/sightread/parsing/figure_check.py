"""The figure check's raster work, and what its model call shows and reads back
(docs/parsing.md § Figure check).

The rules read every box as its crop would be and push a side that cuts through ink out to
the white space past it. For a page they flag, the model is sent the page with every box's
crop drawn as a numbered rectangle and listed in its own convention, so it judges what will
be cut out, and its answer is read back into verdicts. The loop and its upstream calls are
the runner's (jobs/runner.py). This is raster work on our own render (Pillow), like the
crops: no PDF work.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..agents.bbox import BboxConvention, from_canonical, to_canonical
from ..agents.parse import FIGURE_CHECK_PROMPT
from .figures import CROP_MARGIN, Bbox, crop_rect
from .markdown import BBOX_MAX, PLACEHOLDER_RE, clean_bbox

# Ink is a pixel whose luminance differs from the page's background, its most common
# luminance, by more than this; a line with fewer ink pixels than INK_MIN_PIXELS is blank.
INK_CONTRAST = 35
INK_MIN_PIXELS = 2
# Fractions of the page's long edge: the outer lines of a crop read for ink, the blank run
# that ends a figure, and the most a side moves. On a 2000 px render: 2, 12 and 80 px.
EDGE_LINES = 0.001
BLANK_RUN = 0.006
MAX_GROWTH = 0.04

# The rectangles drawn for the model, in a colour a document rarely uses.
MARK = (255, 0, 255)


class PageInk:
    """One page render, read for ink: the rules of the figure check."""

    def __init__(self, image: Path) -> None:
        with Image.open(image) as opened:
            self._gray = opened.convert("L")
        self.width, self.height = self._gray.size
        histogram = self._gray.histogram()
        self._background = max(range(256), key=histogram.__getitem__)
        self._ink = [value for value in range(256) if abs(value - self._background) > INK_CONTRAST]
        long_edge = max(self.width, self.height)
        self._edge = max(1, round(long_edge * EDGE_LINES))
        self._run = max(2, round(long_edge * BLANK_RUN))
        self._reach = max(1, round(long_edge * MAX_GROWTH))

    def push_out(self, boxes: list[Bbox], which: Iterable[int]) -> tuple[list[Bbox], set[int]]:
        """`boxes` with each box of `which` pushed out where its crop cut through ink, and the
        boxes of `which` that cut through any. Boxes are taken in order, so a later one
        stops at an earlier one where it now is."""
        boxes = list(boxes)
        cut: set[int] = set()
        for index in which:
            boxes[index], cuts = self._push_out(boxes, index)
            if cuts:
                cut.add(index)
        return boxes, cut

    def _push_out(self, boxes: list[Bbox], index: int) -> tuple[Bbox, bool]:
        # The other boxes' own pixels, without a crop's margin: (left, top, right, bottom),
        # right and bottom exclusive.
        others = [
            (
                round(o_x_min / BBOX_MAX * self.width),
                round(o_y_min / BBOX_MAX * self.height),
                round(o_x_max / BBOX_MAX * self.width),
                round(o_y_max / BBOX_MAX * self.height),
            )
            for n, (o_y_min, o_x_min, o_y_max, o_x_max) in enumerate(boxes)
            if n != index
        ]
        # Another figure inside this crop's margin is not this figure cut: its pixels are
        # read as background.
        page = self._gray.copy()
        draw = ImageDraw.Draw(page)
        for left, top, right, bottom in others:
            if right > left and bottom > top:
                draw.rectangle((left, top, right - 1, bottom - 1), fill=self._background)

        left, top, right, bottom = crop_rect(boxes[index], self.width, self.height)
        if right <= left or bottom <= top:
            return boxes[index], False
        y_min, x_min, y_max, x_max = boxes[index]
        cut = False
        # Each side: its outermost line inside the crop, the way out, the page's extent on
        # that axis, and the span of lines across it.
        sides = (
            ("left", left, -1, self.width, top, bottom),
            ("right", right - 1, 1, self.width, top, bottom),
            ("top", top, -1, self.height, left, right),
            ("bottom", bottom - 1, 1, self.height, left, right),
        )
        for side, outer, step, extent, low, high in sides:
            columns = side in ("left", "right")
            if outer + step < 0 or outer + step >= extent:
                continue  # on the page's edge: nothing lies past it
            if not any(
                self._ink_on(page, columns, outer - step * line, low, high) >= INK_MIN_PIXELS
                for line in range(self._edge)
            ):
                continue
            cut = True
            edge = self._way_out(page, columns, outer, step, extent, low, high, others)
            if edge is None:
                continue  # no white space within reach: the side stays for the model
            if side == "left":
                x_min = min(x_min, edge * BBOX_MAX // self.width + CROP_MARGIN)
            elif side == "right":
                x_max = max(
                    x_max, min(BBOX_MAX, -(-(edge + 1) * BBOX_MAX // self.width) - CROP_MARGIN)
                )
            elif side == "top":
                y_min = min(y_min, edge * BBOX_MAX // self.height + CROP_MARGIN)
            else:
                y_max = max(
                    y_max, min(BBOX_MAX, -(-(edge + 1) * BBOX_MAX // self.height) - CROP_MARGIN)
                )
        return (y_min, x_min, y_max, x_max), cut

    def _way_out(
        self,
        page: Image.Image,
        columns: bool,
        outer: int,
        step: int,
        extent: int,
        low: int,
        high: int,
        others: list[tuple[int, int, int, int]],
    ) -> int | None:
        """The crop's new outermost line on a side that cuts through ink: a third of a blank
        run past the last ink, or the page's last line. None when another box or the reach
        comes first."""
        last_ink, blank = outer, 0
        for moved in range(1, self._reach + 1):
            at = outer + step * moved
            if not 0 <= at < extent:
                return at - step
            if any(
                (left <= at < right and top < high and low < bottom)
                if columns
                else (top <= at < bottom and left < high and low < right)
                for left, top, right, bottom in others
            ):
                # Ink running into another figure's box may be that figure's own: whose
                # it is, the rules cannot tell.
                return None
            if self._ink_on(page, columns, at, low, high) >= INK_MIN_PIXELS:
                last_ink, blank = at, 0
                continue
            blank += 1
            if blank >= self._run:
                return last_ink + step * (self._run // 3)
        return None

    def _ink_on(self, page: Image.Image, columns: bool, at: int, low: int, high: int) -> int:
        line = (at, low, at + 1, high) if columns else (low, at, high, at + 1)
        histogram = page.crop(line).histogram()
        return sum(histogram[value] for value in self._ink)


def mark_boxes(image: Path, boxes: list[Bbox], target: Path) -> None:
    """The page as the model was sent it, with box n's crop drawn as rectangle n + 1 and its
    number beside it, written to `target` as a PNG. The outline runs just outside the crop,
    so nothing the crop takes in is covered."""
    with Image.open(image) as opened:
        marked = opened.convert("RGB")
    width, height = marked.size
    long_edge = max(width, height)
    line = max(2, round(long_edge * 0.0015))
    pad = line
    font = ImageFont.load_default(size=max(12, round(long_edge * 0.014)))
    draw = ImageDraw.Draw(marked)
    for number, box in enumerate(boxes, start=1):
        left, top, right, bottom = crop_rect(box, width, height)
        draw.rectangle(
            (left - line, top - line, right + line - 1, bottom + line - 1), outline=MARK, width=line
        )
        label = str(number)
        text_left, text_top, text_right, text_bottom = draw.textbbox((0, 0), label, font=font)
        tag_width = text_right - text_left + 2 * pad
        tag_height = text_bottom - text_top + 2 * pad
        # Above the rectangle's top-left corner, or inside it when the page ends there.
        tag_top = top - line - tag_height if top - line - tag_height >= 0 else top
        tag_left = max(0, left - line)
        draw.rectangle((tag_left, tag_top, tag_left + tag_width, tag_top + tag_height), fill=MARK)
        draw.text(
            (tag_left + pad - text_left, tag_top + pad - text_top), label, fill="white", font=font
        )
    marked.save(target, format="PNG")


def check_prompt(
    boxes: list[Bbox], convention: BboxConvention, width_px: int, height_px: int
) -> str:
    """The check prompt for these rectangles: every box's crop, as `mark_boxes` draws it, in
    the model's own convention on the image it is sent (`width_px` x `height_px`). Plain
    replacement, as for a transcription."""
    listed = []
    for number, (y_min, x_min, y_max, x_max) in enumerate(boxes, start=1):
        crop = (
            max(0, y_min - CROP_MARGIN),
            max(0, x_min - CROP_MARGIN),
            min(BBOX_MAX, y_max + CROP_MARGIN),
            min(BBOX_MAX, x_max + CROP_MARGIN),
        )
        listed.append(f"{number}: {list(from_canonical(crop, convention, width_px, height_px))}")
    return FIGURE_CHECK_PROMPT.replace(
        "{box_format}", convention.box_format(width_px, height_px)
    ).replace("{boxes}", "\n".join(listed))


def read_verdicts(
    answer: str, count: int, convention: BboxConvention, width_px: int, height_px: int
) -> dict[int, Bbox | None]:
    """The model's verdict on each rectangle it answered for, by box index: None for a box it
    confirmed, and for one it corrected the box whose crop is the corrected rectangle, in the
    stored contract and cleaned. A rectangle without a usable verdict is absent
    (docs/parsing.md § Figure check).
    The JSON object is read from the first `{` to the last `}`, so a fence or a sentence
    around it costs nothing."""
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        payload = json.loads(answer[start : end + 1])
    except (ValueError, RecursionError):
        return {}
    entries = payload.get("figures") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    verdicts: dict[int, Bbox | None] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        number, ok = entry.get("n"), entry.get("ok")
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 1 <= number <= count
            or number - 1 in verdicts
            or not isinstance(ok, bool)
        ):
            continue
        if ok:
            verdicts[number - 1] = None
            continue
        values = entry.get("box")
        if not isinstance(values, list) or len(values) != 4:
            continue
        try:
            # JSON numbers only; an integer too large for a float, an infinity or a NaN
            # raises here and gives no verdict.
            numbers = tuple(
                round(float(value))
                for value in values
                if isinstance(value, int | float) and not isinstance(value, bool)
            )
        except (OverflowError, ValueError):
            continue
        if len(numbers) != 4:
            continue
        # The rectangle is a crop: the box is it less the crop's margin on each side.
        y_min, x_min, y_max, x_max = to_canonical(numbers, convention, width_px, height_px)
        box = clean_bbox(
            (y_min + CROP_MARGIN, x_min + CROP_MARGIN, y_max - CROP_MARGIN, x_max - CROP_MARGIN)
        )
        if box is not None:
            verdicts[number - 1] = box
    return verdicts


def lands_on_another(box: Bbox, others: Iterable[Bbox]) -> bool:
    """Whether a corrected box would take in another figure: it shares more than half of
    the smaller of the two with another box. Two figures are never merged, and a split
    figure stays split."""
    y_min, x_min, y_max, x_max = box
    area = (y_max - y_min) * (x_max - x_min)
    for o_y_min, o_x_min, o_y_max, o_x_max in others:
        shared = max(0, min(y_max, o_y_max) - max(y_min, o_y_min)) * max(
            0, min(x_max, o_x_max) - max(x_min, o_x_min)
        )
        if shared * 2 > min(area, (o_y_max - o_y_min) * (o_x_max - o_x_min)):
            return True
    return False


def move_boxes(markdown: str, moves: dict[Bbox, Bbox]) -> str:
    """The page's markdown with each placeholder of a moved box carrying its new box. Nothing
    else changes: the text, the alt text and the page the model wrote stay as they were."""

    def rewrite(match) -> str:
        bbox = clean_bbox(
            (
                int(match.group("ymin")),
                int(match.group("xmin")),
                int(match.group("ymax")),
                int(match.group("xmax")),
            )
        )
        moved = moves.get(bbox) if bbox is not None else None
        if moved is None or moved == bbox:
            return match.group(0)
        y_min, x_min, y_max, x_max = moved
        return f"![{match.group('alt')}](sightread://p{match.group('page')}/{y_min},{x_min},{y_max},{x_max})"

    return PLACEHOLDER_RE.sub(rewrite, markdown)
