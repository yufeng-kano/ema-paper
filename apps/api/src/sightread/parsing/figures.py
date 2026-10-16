"""Figure crop persistence (docs/parsing.md § Figure crops).

Crops are taken from the rendered page image at the one moment it exists — right after the
vision call, before retention deletes it — and written under the job's own directory in
`FIGURES_DIR`, named by the page and the cleaned bbox. That name is exactly what a
`sightread://` placeholder carries, so a crop is addressable without waiting for
document-wide figure ids, including from a partial result.

This is raster work on our own render (Pillow), not PDF work; the Poppler-only rule is
untouched. A crop that fails is logged and skipped — it must never fail the page.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from PIL import Image

from .markdown import BBOX_MAX, PLACEHOLDER_RE, clean_bbox

logger = logging.getLogger(__name__)

Bbox = tuple[int, int, int, int]

# Margin per side in bbox space: 0.8% of the page. Enough to forgive a slightly tight
# box; a wider margin (the ~2% docs/api.md suggests to self-cropping callers) would pull
# the caption the prompt excludes right back into the crop (docs/parsing.md).
CROP_MARGIN = 8

# A bbox as the figure routes receive it: `120,60,480,940`.
_BBOX_PATH_RE = re.compile(r"^(-?\d{1,5}),(-?\d{1,5}),(-?\d{1,5}),(-?\d{1,5})$")


def parse_bbox_path(raw: str) -> Bbox | None:
    """`"120,60,480,940"` → a cleaned bbox, or None when it is not one."""
    match = _BBOX_PATH_RE.match(raw)
    if match is None:
        return None
    return clean_bbox(tuple(int(group) for group in match.groups()))


def crop_path(figures_dir: Path, job_id: uuid.UUID, page: int, bbox: Bbox) -> Path:
    """Where one figure's crop lives. Deterministic from the placeholder alone."""
    y_min, x_min, y_max, x_max = bbox
    return figures_dir / str(job_id) / f"p{page}_{y_min}_{x_min}_{y_max}_{x_max}.png"


def find_crop(figures_dir: Path, job_id: uuid.UUID, page: int, bbox: Bbox) -> Path | None:
    """The crop where it is now: under the job's name once published, else in the newest
    attempt directory of a job still running — the same page render and box give the
    same crop, so a stale attempt beside the replacement's is no different answer
    (docs/parsing.md § Figure crops). None when no attempt has written it."""
    final = crop_path(figures_dir, job_id, page, bbox)
    if final.is_file():
        return final
    attempts: list[tuple[float, Path]] = []
    for attempt in figures_dir.glob(f"{job_id}.*"):
        try:
            attempts.append((attempt.stat().st_mtime, attempt))
        except OSError:
            continue  # discarded between the listing and the look
    for _, attempt in sorted(attempts, reverse=True):
        candidate = attempt / final.name
        if candidate.is_file():
            return candidate
    # The publication can land between the two looks — the attempt renamed to the job's
    # name after the first miss — so the job's directory is looked at once more.
    return final if final.is_file() else None


def open_crop(figures_dir: Path, job_id: uuid.UUID, page: int, bbox: Bbox) -> BinaryIO | None:
    """The crop open for reading, wherever it is now: opened where `find_crop` finds it,
    and looked up once more when the path vanished under the open — the attempt
    directory published in between. A descriptor rather than a path, since a path can
    be moved before a response opens it, and a descriptor rather than the bytes, since
    the response streams from it off the event loop (docs/parsing.md § Figure crops).
    None when no attempt has written it."""
    for _ in range(2):
        path = find_crop(figures_dir, job_id, page, bbox)
        if path is None:
            return None
        try:
            return path.open("rb")
        except FileNotFoundError:
            continue
    return None


CROP_CHUNK_BYTES = 65_536


def crop_chunks(handle: BinaryIO) -> Iterator[bytes]:
    """The open crop in chunks, closed at the end or when the reader stops."""
    with handle:
        while chunk := handle.read(CROP_CHUNK_BYTES):
            yield chunk


def discard_job_figures(figures_dir: Path, job_id: uuid.UUID) -> None:
    """Remove one job's crop directory, and every attempt directory of it still standing.

    Crops live exactly as long as something references them (docs/jobs.md § Retention): a
    job that fails wrote no result, and a requeued job reparses from scratch — either way
    the crops of the abandoned attempt must go with it, or a reparse whose transcription
    emits different boxes would leave the first attempt's files answering figure routes.
    """
    shutil.rmtree(figures_dir / str(job_id), ignore_errors=True)
    for attempt in figures_dir.glob(f"{job_id}.*"):
        shutil.rmtree(attempt, ignore_errors=True)


def publish_job_figures(figures_dir: Path, job_id: uuid.UUID, attempt: Path) -> None:
    """The attempt's crops under the job's name: what the figure routes and the document
    copies read, once the result is committed as this attempt's (docs/jobs.md § Queue).
    Whatever stood there before — nothing, or a dead attempt's leftovers — goes first."""
    final = figures_dir / str(job_id)
    shutil.rmtree(final, ignore_errors=True)
    if attempt.is_dir():
        attempt.rename(final)


DEFAULT_FIGURES_PER_PAGE = 40


def save_page_figures(
    markdown: str,
    image_path: Path,
    page: int,
    job_dir: Path,
    limit: int = DEFAULT_FIGURES_PER_PAGE,
) -> int:
    """Crop every placeholder on one transcribed page from its rendered image.

    `page` is our page number, not the model's — the same renumbering `assemble` applies,
    so the stored name matches the placeholder the result will carry. Returns how many
    crops were written; failures are logged and never raised.

    At most `limit` distinct boxes, in reading order (FIGURES_PER_PAGE_MAX): the upstream
    response body is capped, but a malicious user-defined endpoint could otherwise turn
    one response into unbounded PNG encodes and durable disk.
    """
    boxes: list[Bbox] = []
    seen: set[Bbox] = set()
    truncated = False
    for match in PLACEHOLDER_RE.finditer(markdown):
        bbox = clean_bbox(
            (
                int(match.group("ymin")),
                int(match.group("xmin")),
                int(match.group("ymax")),
                int(match.group("xmax")),
            )
        )
        if bbox is None or bbox in seen:
            continue
        if len(boxes) >= max(0, limit):
            truncated = True
            break
        seen.add(bbox)
        boxes.append(bbox)
    if truncated:
        logger.warning("page %d exceeded the per-page figure crop cap (%d)", page, limit)
    if not boxes:
        return 0

    saved = 0
    try:
        with Image.open(image_path) as image:
            width, height = image.size
            job_dir.mkdir(parents=True, exist_ok=True)
            for bbox in boxes:
                y_min, x_min, y_max, x_max = bbox
                left = max(0, round((x_min - CROP_MARGIN) / BBOX_MAX * width))
                top = max(0, round((y_min - CROP_MARGIN) / BBOX_MAX * height))
                right = min(width, round((x_max + CROP_MARGIN) / BBOX_MAX * width))
                bottom = min(height, round((y_max + CROP_MARGIN) / BBOX_MAX * height))
                if right <= left or bottom <= top:
                    continue
                destination = job_dir / f"p{page}_{y_min}_{x_min}_{y_max}_{x_max}.png"
                # Encode to a temporary name and rename: a partial result can point the
                # viewer at this crop while it is still being written, and a name that
                # exists must never stream as a truncated PNG.
                staging = destination.with_name(destination.name + ".writing")
                image.crop((left, top, right, bottom)).save(staging, format="PNG")
                staging.replace(destination)
                saved += 1
    except OSError:
        # Which figure failed is diagnostic; what the page shows is not logged.
        logger.warning("figure crops for page %d could not all be saved", page)
    return saved
