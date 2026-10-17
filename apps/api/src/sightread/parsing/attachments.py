"""The chat composer's attachments, prepared for the model (docs/chat.md § What a user
message carries).

An upload arrives here as a file on disk; what leaves is a file the send embeds as it is:
an image normalized the way Claude would resize it, or the text of a PDF or a text file,
kept to a bound, head and tail. Nothing here touches the database or the request.
"""

from __future__ import annotations

import asyncio
import codecs
import tempfile
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path, PurePath

from PIL import Image, UnidentifiedImageError

from . import poppler
from .images import ImageError, normalize_image

# By extension first: a browser reports `application/octet-stream` for whatever it has no
# association for, which is most of what a reader drags in.
IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
PDF_EXTENSIONS = frozenset({".pdf"})
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".tex",
        ".bib",
        ".html",
        ".htm",
        ".rst",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".r",
        ".sql",
    }
)
TEXT_MEDIA_TYPES = frozenset({"application/json", "application/xml", "application/x-yaml"})
# What `GET /api/me` tells the composer it may offer (docs/api.md).
ATTACHMENT_EXTENSIONS: tuple[str, ...] = tuple(
    sorted({*IMAGE_TYPES, *PDF_EXTENSIONS, *TEXT_EXTENSIONS})
)
# Every attachment file starts so, in the upload directory the sweeper empties of anything
# older than `UPLOAD_MAX_AGE_SECONDS` (docs/jobs.md § Retention).
FILE_PREFIX = "chat-attachment-"
READ_CHUNK_BYTES = 64 * 1024
# What follows a PDF's text where `pdftotext` was stopped (docs/chat.md § What a user
# message carries).
PDF_TEXT_CUT = "\n\n[... the PDF's text goes on past this point ...]"


class AttachmentRefused(Exception):
    """A file that cannot be attached; the message is the reader's to see and never
    carries the file's content."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class PreparedAttachment:
    path: Path
    kind: str  # image | text
    media_type: str
    size_bytes: int
    # The prepared text's length, for a text attachment.
    chars: int | None


def classify(name: str, media_type: str) -> str | None:
    """`image`, `pdf` or `text`, or None for anything else."""
    suffix = PurePath(name).suffix.lower()
    if suffix in IMAGE_TYPES:
        return "image"
    if suffix in PDF_EXTENSIONS:
        return "pdf"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    base = media_type.split(";")[0].strip().lower()
    if base in IMAGE_TYPES.values():
        return "image"
    if base == "application/pdf":
        return "pdf"
    if base.startswith("text/") or base in TEXT_MEDIA_TYPES:
        return "text"
    return None


def prepared_path(upload_dir: Path, attachment_id: uuid.UUID, media_type: str) -> Path:
    """Where a prepared attachment waits for its message, by what it was prepared into."""
    suffix = {"image/png": ".png", "image/jpeg": ".jpg"}.get(media_type, ".txt")
    return upload_dir / f"{FILE_PREFIX}{attachment_id}{suffix}"


def _read_bounded(path: Path, max_chars: int, *, strict: bool) -> str:
    """The file's UTF-8 text kept to `max_chars`: the head and the tail with what went
    missing between them said, read in slices so a long file is never held whole. With
    `strict`, text that is not UTF-8 raises; otherwise it is replaced. NUL characters are
    dropped: a JSONB column refuses them."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict" if strict else "replace")
    keep = max(max_chars // 2 - 40, 0)
    head = ""
    tail = ""
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            piece = decoder.decode(chunk, final=not chunk).replace("\x00", "")
            total += len(piece)
            if len(head) < max_chars:
                head += piece[: max_chars - len(head)]
            if keep:
                tail = (tail + piece)[-keep:]
            if not chunk:
                break
    if total <= max_chars:
        return head
    if not keep:
        return head[:max_chars]
    omitted = total - 2 * keep
    return f"{head[:keep]}\n\n[... {omitted} characters omitted ...]\n\n{tail}"


def _prepare_image(
    source: Path, out_dir: Path, stem: str, max_pixels: int, long_edge: int
) -> PreparedAttachment:
    # The size from the header first: a small file that would decode past the cap is
    # refused before a raster exists, and Pillow's own bomb check is an error here.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as opened:
                too_large = opened.width * opened.height > max_pixels
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        ValueError,
    ) as exc:
        raise AttachmentRefused(400, "The image could not be read") from exc
    if too_large:
        raise AttachmentRefused(400, "The image is too large to attach")
    try:
        normalized = normalize_image(source, out_dir, max_long_edge=long_edge, stem=stem)
    except ImageError as exc:
        raise AttachmentRefused(400, "The image could not be read") from exc
    return PreparedAttachment(
        path=normalized.path,
        kind="image",
        media_type=normalized.media_type,
        size_bytes=normalized.path.stat().st_size,
        chars=None,
    )


def _write_text(path: Path, text: str) -> PreparedAttachment:
    path.write_text(text, encoding="utf-8")
    return PreparedAttachment(
        path=path,
        kind="text",
        media_type="text/plain",
        size_bytes=path.stat().st_size,
        chars=len(text),
    )


async def prepare_attachment(
    source: Path,
    *,
    name: str,
    media_type: str,
    out_dir: Path,
    attachment_id: uuid.UUID,
    max_pixels: int,
    long_edge: int,
    text_max_chars: int,
    pdf_timeout: float,
) -> PreparedAttachment:
    """Prepare the upload at `source` into `out_dir` under the attachment's own name. An
    image is decoded, turned upright, flattened onto white and downscaled (PNG stays PNG,
    anything else becomes JPEG); a PDF is its text layer's head, through Poppler; a text
    file is its UTF-8 text. Raises `AttachmentRefused` for anything that cannot be attached."""
    kind = classify(name, media_type)
    if kind is None:
        raise AttachmentRefused(415, "Only images, PDFs and text files can be attached")
    stem = f"{FILE_PREFIX}{attachment_id}"
    if kind == "image":
        return await asyncio.to_thread(_prepare_image, source, out_dir, stem, max_pixels, long_edge)
    if kind == "pdf":
        with tempfile.TemporaryDirectory(prefix="sr-attach-", dir=out_dir) as work:
            try:
                # Four bytes a character at most: the cap always holds `text_max_chars`.
                data, goes_on = await poppler.pdf_text(
                    source, cwd=Path(work), timeout=pdf_timeout, max_bytes=4 * text_max_chars
                )
            except poppler.PopplerError as exc:
                raise AttachmentRefused(400, "The PDF could not be read") from exc
        # Not final: a character the cap split is dropped rather than replaced.
        text = codecs.getincrementaldecoder("utf-8-sig")("replace").decode(data)
        text = text.replace("\x00", "")
        if not text.strip():
            raise AttachmentRefused(
                400, "The PDF has no text layer to read; attach its pages as images instead"
            )
        if goes_on or len(text) > text_max_chars:
            text = text[:text_max_chars] + PDF_TEXT_CUT
    else:
        try:
            text = await asyncio.to_thread(_read_bounded, source, text_max_chars, strict=True)
        except UnicodeDecodeError as exc:
            raise AttachmentRefused(400, "The file is not UTF-8 text") from exc
        if not text.strip():
            raise AttachmentRefused(400, "The file is empty")
    return await asyncio.to_thread(_write_text, out_dir / f"{stem}.txt", text)
