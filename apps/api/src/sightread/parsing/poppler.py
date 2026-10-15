"""Poppler subprocess wrappers — the only module that spawns Poppler (docs/parsing.md).

Every call is a short-lived child process with a timeout, no shell, and a working
directory pinned to the job's own directory, so a hostile document can at worst kill its
own child. Callers get typed results and scope failures to the page that produced them.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

PDFINFO = "pdfinfo"
PDFTOPPM = "pdftoppm"

# Per-call ceiling (docs/parsing.md: default 60 s/page).
DEFAULT_TIMEOUT_SECONDS = 60.0

# Rendered pages stay under this long edge; larger images destabilise VLM detection.
MAX_RENDER_LONG_EDGE_PX = 2000
MIN_RENDER_DPI = 36
MAX_RENDER_DPI = 300

# `pdfinfo -f 1 -l N` prints one size line per page. The upper bound only caps how many
# size lines we ask for; `page_count` still comes from the document's own "Pages:" line.
PAGE_SIZE_LIST_LIMIT = 100_000

_PAGES_RE = re.compile(r"^Pages:\s+(\d+)$", re.MULTILINE)
_PAGE_SIZE_RE = re.compile(r"^Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+) pts", re.MULTILINE)


class PopplerError(Exception):
    """A Poppler child failed. The message never carries document content."""


class PopplerTimeout(PopplerError):
    pass


@dataclass(frozen=True)
class PageSize:
    width_pt: float
    height_pt: float


@dataclass(frozen=True)
class PdfInfo:
    page_count: int
    page_sizes: tuple[PageSize, ...]

    def page_size(self, page_no: int) -> PageSize:
        """Size of a 1-based page, falling back to the first page for oversized documents."""
        if 1 <= page_no <= len(self.page_sizes):
            return self.page_sizes[page_no - 1]
        return self.page_sizes[0]


async def _run(
    args: list[str], *, cwd: Path, timeout: float, pass_fds: tuple[int, ...] = ()
) -> bytes:
    """Run a Poppler tool and return stdout; kill the child if it overruns the timeout.
    `pass_fds` lets a caller hand the tool an already-open file as `/proc/self/fd/N`."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=pass_fds,
        )
    except OSError as exc:  # Poppler not installed on this host.
        raise PopplerError(f"could not start {args[0]}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise PopplerTimeout(f"{args[0]} timed out after {timeout:.0f}s") from exc
    finally:
        # Close the child's pipes here rather than leaving them to garbage collection,
        # which can run after the event loop that owns them is already gone.
        process._transport.close()

    if process.returncode != 0:
        # Poppler's stderr is diagnostic ("Syntax Error: ..."), never document text.
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        last = detail[-1] if detail else f"exit {process.returncode}"
        raise PopplerError(f"{args[0]} failed: {last}")
    return stdout


async def pdf_info(
    pdf_path: Path,
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    pass_fds: tuple[int, ...] = (),
) -> PdfInfo:
    """Page count and per-page dimensions. Raises `PopplerError` for unreadable files."""
    stdout = await _run(
        [PDFINFO, "-f", "1", "-l", str(PAGE_SIZE_LIST_LIMIT), str(pdf_path)],
        cwd=cwd,
        timeout=timeout,
        pass_fds=pass_fds,
    )
    text = stdout.decode("utf-8", "replace")

    pages_match = _PAGES_RE.search(text)
    if pages_match is None:
        raise PopplerError("pdfinfo reported no page count")
    page_count = int(pages_match.group(1))

    sizes = [
        PageSize(width_pt=float(width), height_pt=float(height))
        for _, width, height in _PAGE_SIZE_RE.findall(text)
    ]
    if not sizes:
        raise PopplerError("pdfinfo reported no page dimensions")
    return PdfInfo(page_count=page_count, page_sizes=tuple(sizes))


def render_dpi(size: PageSize) -> int:
    """DPI that keeps the rendered long edge at or under `MAX_RENDER_LONG_EDGE_PX`."""
    long_edge_pt = max(size.width_pt, size.height_pt)
    if long_edge_pt <= 0:
        return MIN_RENDER_DPI
    dpi = int(MAX_RENDER_LONG_EDGE_PX * 72 / long_edge_pt)
    return max(MIN_RENDER_DPI, min(MAX_RENDER_DPI, dpi))


async def render_page(
    pdf_path: Path,
    page_no: int,
    size: PageSize,
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    pass_fds: tuple[int, ...] = (),
) -> Path:
    """Render one page to PNG inside `cwd` and return the file path."""
    target = cwd / f"page-{page_no}"
    await _run(
        [
            PDFTOPPM,
            "-q",
            "-png",
            "-r",
            str(render_dpi(size)),
            "-f",
            str(page_no),
            "-l",
            str(page_no),
            "-singlefile",
            str(pdf_path),
            str(target),
        ],
        cwd=cwd,
        timeout=timeout,
        pass_fds=pass_fds,
    )
    rendered = target.with_suffix(".png")
    if not rendered.exists():
        raise PopplerError(f"pdftoppm wrote no image for page {page_no}")
    return rendered
