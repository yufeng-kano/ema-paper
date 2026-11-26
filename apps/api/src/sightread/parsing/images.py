"""Image input normalization (docs/parsing.md § Image input).

Order is fixed: HEIC decode, EXIF orientation, 8-bit RGB on white, downscale to a long
edge of 2000 px. The reported dimensions are those of the oriented original, because the
coordinate contract denormalizes against the page the caller holds, not against our
downscaled copy.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

# Teaches Pillow to open HEIC/HEIF; the rest of this module then treats them as any image.
pillow_heif.register_heif_opener()

MAX_LONG_EDGE_PX = 2000
JPEG_QUALITY = 92

# Accepted image inputs (docs/parsing.md) mapped to the extension we store them under.
ACCEPTED_IMAGE_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heic",
}
# The decoders a parse's image is opened with, by Pillow's format names: the accepted
# types' own, whatever the upload is named or declared as.
PARSE_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "HEIF")

# What a decode that failed raises. Pillow's bomb check raises the first past twice its
# MAX_IMAGE_PIXELS, from the header, before a raster exists.
_UNDECODABLE = (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError)


class ImageError(Exception):
    """An image could not be read. The message never carries image content. `too_large`:
    its header gave more pixels than the caller's cap."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


@dataclass(frozen=True)
class NormalizedImage:
    path: Path
    media_type: str
    # Pixel dimensions of the oriented original — the page space bboxes normalize against.
    width_px: int
    height_px: int


def probe_image(source: Path) -> tuple[int, int]:
    """Oriented pixel dimensions, used to reject unreadable uploads before a job exists:
    decoded only as one of `PARSE_IMAGE_FORMATS`."""
    try:
        with Image.open(source, formats=PARSE_IMAGE_FORMATS) as image:
            image = ImageOps.exif_transpose(image) or image
            return image.width, image.height
    except _UNDECODABLE as exc:
        raise ImageError("the image could not be decoded") from exc


def check_pixels(source: Path, max_pixels: int, formats: tuple[str, ...] | None = None) -> None:
    """Refuse an image whose header gives more than `max_pixels`, before a raster exists;
    Pillow's own bomb warning is an error here. `formats` limits the decoders tried."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=formats) as opened:
                too_large = opened.width * opened.height > max_pixels
    except (*_UNDECODABLE, Image.DecompressionBombWarning) as exc:
        raise ImageError("the image could not be decoded") from exc
    if too_large:
        raise ImageError("the image has too many pixels", too_large=True)


def image_size(path: Path) -> tuple[int, int]:
    """Pixel dimensions of an image we rendered or normalized ourselves (already oriented)."""
    with Image.open(path) as image:
        return image.width, image.height


def flatten_rgb(image: Image.Image) -> Image.Image:
    """The image as 8-bit RGB on white. 16-bit grey (a microscope's, a depth map's) goes to
    8 bits by its top byte: a plain conversion to RGB clips everything past 255 to white.
    Transparency renders as black in most model pipelines, so it is flattened onto white —
    a grey or RGB PNG's too, which it keeps as one key colour rather than a channel."""
    if image.mode in ("I;16", "I;16B", "I;16L", "I"):
        image = image.convert("I").point(lambda value: value / 256).convert("L")
    if image.mode in ("RGBA", "LA", "P") or "transparency" in image.info:
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, image).convert("RGB")
    return image if image.mode == "RGB" else image.convert("RGB")


def normalize_image(
    source: Path,
    out_dir: Path,
    *,
    max_long_edge: int = MAX_LONG_EDGE_PX,
    stem: str = "source",
    formats: tuple[str, ...] | None = None,
) -> NormalizedImage:
    """Write a model-ready copy of `source` into `out_dir` as `<stem>.png` or `.jpg`. A
    parse keeps the defaults; the chat composer's attachments ask for Claude's own long
    edge and a name of their own (docs/chat.md § What a user message carries). `formats`
    limits the decoders Pillow may try, by its format names."""
    try:
        with Image.open(source, formats=formats) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            width, height = image.width, image.height
            # PNG sources are usually screenshots and diagrams, where JPEG ringing costs
            # transcription accuracy; everything else is stored as JPEG to keep the data
            # URL small.
            keep_png = (opened.format or "").upper() == "PNG"
            image = flatten_rgb(image)
            image.thumbnail((max_long_edge, max_long_edge), Image.LANCZOS)

            target = out_dir / (f"{stem}.png" if keep_png else f"{stem}.jpg")
            if keep_png:
                image.save(target, format="PNG", optimize=True)
            else:
                image.save(target, format="JPEG", quality=JPEG_QUALITY)
    except _UNDECODABLE as exc:
        raise ImageError("the image could not be decoded") from exc

    return NormalizedImage(
        path=target,
        media_type="image/png" if keep_png else "image/jpeg",
        width_px=width,
        height_px=height,
    )
