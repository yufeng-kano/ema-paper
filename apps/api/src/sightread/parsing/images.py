"""Image input normalization (docs/parsing.md § Image input).

Order is fixed: HEIC decode, EXIF orientation, downscale to a long edge of 2000 px. The
reported dimensions are those of the oriented original, because the coordinate contract
denormalizes against the page the caller holds, not against our downscaled copy.
"""

from __future__ import annotations

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


class ImageError(Exception):
    """An image could not be read. The message never carries image content."""


@dataclass(frozen=True)
class NormalizedImage:
    path: Path
    media_type: str
    # Pixel dimensions of the oriented original — the page space bboxes normalize against.
    width_px: int
    height_px: int


def probe_image(source: Path) -> tuple[int, int]:
    """Oriented pixel dimensions, used to reject unreadable uploads before a job exists."""
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image) or image
            return image.width, image.height
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageError("the image could not be decoded") from exc


def image_size(path: Path) -> tuple[int, int]:
    """Pixel dimensions of an image we rendered or normalized ourselves (already oriented)."""
    with Image.open(path) as image:
        return image.width, image.height


def normalize_image(source: Path, out_dir: Path) -> NormalizedImage:
    """Write a model-ready copy of `source` into `out_dir`."""
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened) or opened
            width, height = image.width, image.height
            # PNG sources are usually screenshots and diagrams, where JPEG ringing costs
            # transcription accuracy; everything else is stored as JPEG to keep the data
            # URL small.
            keep_png = (opened.format or "").upper() == "PNG"
            # Transparency renders as black in most model pipelines, so flatten onto white.
            if image.mode in ("RGBA", "LA", "P"):
                image = image.convert("RGBA")
                flattened = Image.new("RGB", image.size, (255, 255, 255))
                flattened.paste(image, mask=image.split()[-1])
                image = flattened
            else:
                image = image.convert("RGB")
            image.thumbnail((MAX_LONG_EDGE_PX, MAX_LONG_EDGE_PX), Image.LANCZOS)

            target = out_dir / ("source.png" if keep_png else "source.jpg")
            if keep_png:
                image.save(target, format="PNG", optimize=True)
            else:
                image.save(target, format="JPEG", quality=JPEG_QUALITY)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageError("the image could not be decoded") from exc

    return NormalizedImage(
        path=target,
        media_type="image/png" if keep_png else "image/jpeg",
        width_px=width,
        height_px=height,
    )
