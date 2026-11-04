"""What the chat agent's tools do on the host (docs/chat.md § Tools).

Every tool takes the model's input as untrusted data: a path is resolved and must stay
inside the user's workspace, a page number is bounded by the document, a command goes to
the runner and nowhere else. A tool that fails answers an error result; it never raises
into the loop.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import io
import math
import os
import shlex
import stat
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from ..parsing import poppler
from ..parsing.figures import parse_bbox_path
from ..parsing.images import JPEG_QUALITY
from ..sandbox.client import SandboxClient, SandboxRejected, SandboxUnavailable
from ..workspace import git, safe_io
from ..workspace.documents import IMAGE_FORMATS, MARKDOWN_KIND, DocumentPaths, original_of
from ..workspace.layout import (
    CACHE_DIR,
    FIGURES_DIR,
    META_FILE,
    SIGHTREAD_DIR,
    SOURCE_STEM,
    inside,
)
from ..workspace.locks import workspace_lock

VM_ROOT = PurePosixPath("/workspace")
# Where the editor tool may not write: history, the workspace's own records, the cache.
READ_ONLY_NAMES = frozenset({".git", ".gitignore", SIGHTREAD_DIR, META_FILE, CACHE_DIR})
VIEW_MAX_CHARS = 60_000
# The long edge a page image is sent at: what Claude would resize to anyway.
PAGE_IMAGE_LONG_EDGE_PX = 1568
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# EXIF orientation → the transpose that turns the stored raster upright, as Pillow's
# `exif_transpose` would — applied by hand, since that call rewrites the EXIF block and
# raises on a malformed one (docs/chat.md § Tools) — and the ImageMagick option that does
# the same in the shell, so a crop the agent asks for is cut from the image it was shown.
ORIENTATIONS: dict[int, tuple[Image.Transpose, str]] = {
    2: (Image.Transpose.FLIP_LEFT_RIGHT, "-flop"),
    3: (Image.Transpose.ROTATE_180, "-rotate 180"),
    4: (Image.Transpose.FLIP_TOP_BOTTOM, "-flip"),
    5: (Image.Transpose.TRANSPOSE, "-transpose"),
    6: (Image.Transpose.ROTATE_270, "-rotate 90"),
    7: (Image.Transpose.TRANSVERSE, "-transverse"),
    8: (Image.Transpose.ROTATE_90, "-rotate 270"),
}


@dataclass(frozen=True)
class ImageHeader:
    """What an image's header says, before any pixel is decoded: its upright size, its
    EXIF orientation (1 when it has none) and Pillow's name for its format."""

    width: int
    height: int
    orientation: int
    format: str


class ImageTooLarge(Exception):
    """The file would decode to more pixels than the worker will hold."""

    def __init__(self, header: ImageHeader) -> None:
        super().__init__(f"{header.width}x{header.height} exceeds the pixel cap")
        self.header = header


@dataclass(frozen=True)
class ModelImage:
    """An image as the model is sent it, with the header of what it was made from."""

    data: bytes
    media_type: str
    source: ImageHeader
    # The size sent, after the shrink to the long edge.
    shown_width: int
    shown_height: int


@dataclass(frozen=True)
class ImageView:
    """What an editor `view` of an image may take, from the turn (docs/chat.md § Tools):
    the bytes it reads (`CHAT_IMAGE_ROUND_MAX_BYTES`), the pixels it decodes
    (`CHAT_PAGE_IMAGE_MAX_PIXELS`), the long edge it is sent at
    (`CHAT_ATTACHMENT_IMAGE_LONG_EDGE`), the turn's one decode at a time, and whether the
    caller has a shell to crop with — the commands are named only then."""

    max_bytes: int
    max_pixels: int
    long_edge: int
    decode_slot: asyncio.Semaphore
    shell: bool


@dataclass(frozen=True)
class _ImageRead:
    """A `view` of a file that is not text: its bytes, read under the workspace lock and
    decoded after it is released."""

    vm_path: str
    data: bytes
    # Over the text cap: if it is no image either, it is a file too large to view.
    over_text_cap: bool
    ranged: bool


@dataclass
class ToolOutcome:
    content: str | list[dict]
    is_error: bool = False
    # An edit the tool made: what the `edit` event carries.
    edit: dict | None = field(default=None)
    # False for a shell command whose end the runner could not vouch for (it was
    # unreachable, or the VM could not be stopped): a process may still be writing, so
    # the tree is not committed now — the next command's revision carries it, once the
    # runner has confirmed the VM gone (docs/chat.md § Tools).
    settled: bool = True
    # What a `subagent` call did, stored with its result for the panel and stripped before
    # the result goes upstream (docs/chat.md § Subagents).
    subagent: dict | None = field(default=None)
    # What a `memory` call changed, for its `memory` event: `{action, memory, previous}`
    # (docs/chat.md § Memory). Set on success only.
    memory: dict | None = field(default=None)
    # What a library tool changed, as the Files page's undo keeps its steps: carried on the
    # `tool_result` event and the stored block, stripped before the history goes upstream
    # (docs/chat.md § Library conversations, *The library tools*). None when nothing changed.
    library: dict | None = field(default=None)


def truncate(text: str, max_chars: int) -> str:
    """Keep the head and the tail; say how much went missing between them."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2 - 40
    if keep <= 0:
        # No room for the marker: a plain cut, never `text[-0:]`, which is the whole text.
        return text[: max(max_chars, 0)]
    omitted = len(text) - 2 * keep
    return f"{text[:keep]}\n\n[... {omitted} characters omitted ...]\n\n{text[-keep:]}"


# --- bash ---------------------------------------------------------------------------------


async def run_bash(
    client: SandboxClient,
    user_id: int,
    session: str,
    tool_input: dict,
    *,
    max_chars: int,
    timeout_seconds: int,
) -> ToolOutcome:
    if tool_input.get("restart"):
        try:
            await client.restart(user_id, session)
        except SandboxUnavailable as exc:
            return ToolOutcome(f"The sandbox is unavailable: {exc}", is_error=True)
        return ToolOutcome("The shell was restarted.")
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolOutcome("bash needs a `command`", is_error=True)
    try:
        result = await client.exec(user_id, session, command, timeout_seconds=timeout_seconds)
    except SandboxRejected as exc:
        # Definitive: the runner refused the request before anything ran (the token, a
        # command past its validation cap), so nothing is unconfirmed about it.
        return ToolOutcome(f"The sandbox runner rejected the command: {exc}", is_error=True)
    except SandboxUnavailable as exc:
        return ToolOutcome(
            f"The sandbox is unavailable: {exc}\n"
            "[Whether the command ran is unknown; what it changed is recorded with the "
            "next command.]",
            is_error=True,
            settled=False,
        )
    output = truncate(result.output, max_chars)
    notes = []
    if result.timed_out:
        notes.append(f"[command timed out after {timeout_seconds}s and was killed]")
    if result.truncated:
        notes.append("[output was cut by the sandbox]")
    if result.exit_code not in (None, 0):
        notes.append(f"[exit code {result.exit_code}]")
    elif result.exit_code is None and not result.timed_out:
        notes.append("[the shell ended without reporting a status]")
    if notes:
        output = (output + "\n" if output else "") + "\n".join(notes)
    # Only a status of zero is success: a lost status (the shell died, `exit` was
    # called) is not a command that completed (docs/chat.md § Tools).
    failed = result.timed_out or result.exit_code != 0
    return ToolOutcome(output or "(no output)", is_error=failed)


# --- the editor ---------------------------------------------------------------------------


class PathRefused(Exception):
    pass


def resolve_workspace_path(root: Path, raw: object) -> Path:
    """`/workspace/files/x/doc.md` (or `files/x/doc.md`) → the host path, inside `root`."""
    if not isinstance(raw, str) or not raw.strip():
        raise PathRefused("a `path` is required")
    posix = PurePosixPath(raw.strip())
    if posix.is_absolute():
        try:
            relative = posix.relative_to(VM_ROOT)
        except ValueError as exc:
            raise PathRefused("paths must be under /workspace") from exc
    else:
        relative = posix
    # Lexically, before anything is opened: a `create` makes missing parents one by one,
    # and `..` would have it make directories wherever the path led before it came back
    # inside (docs/chat.md § Tools).
    if any(part in (".", "..") for part in relative.parts):
        raise PathRefused("paths must not contain . or .. components")
    candidate = root / Path(*relative.parts) if relative.parts else root
    if not inside(root, candidate):
        raise PathRefused("paths must stay inside /workspace")
    return candidate


def _writable(root: Path, path: Path) -> bool:
    """Not the workspace's own files, and nothing history excludes — a write there would
    have no revision to show or revert (docs/chat.md § Tools)."""
    relative = path.relative_to(root)
    # Every component, not the last alone: git ignores `source.*` and `.sr-tmp-*` at any
    # depth, so a file beneath one would be written and never recorded.
    return not any(
        part in READ_ONLY_NAMES
        or part == FIGURES_DIR
        or part.startswith((f"{SOURCE_STEM}.", safe_io.TEMP_PREFIX))
        for part in relative.parts
    )


def _listing(root: Path, path: Path, max_chars: int) -> str:
    """The directory's entries, read through a descriptor opened beneath the root with
    nothing followed — a path listing could be swapped for a link in between. Bounded
    like every tool result: every entry takes at least two characters of the result, so
    `max_chars` entries fill it, and no more are read (docs/chat.md § Tools)."""
    entries = safe_io.list_dir(root, path, limit=max_chars + 1)
    more = len(entries) > max_chars
    rows = [
        f"{name}/" if is_dir else name
        for name, is_dir in sorted(entries[:max_chars], key=lambda item: item[0])
        if name != ".git"
    ]
    if more:
        rows.append("[... more entries than this view can list; use bash ...]")
    return truncate("\n".join(rows) or "(empty directory)", max_chars)


def _numbered(text: str, view_range: object) -> str:
    lines = text.split("\n")
    start, end = 1, len(lines)
    if isinstance(view_range, list) and len(view_range) == 2:
        try:
            start = max(1, int(view_range[0]))
            end = len(lines) if int(view_range[1]) == -1 else min(len(lines), int(view_range[1]))
        except (TypeError, ValueError):
            pass
    return "\n".join(f"{number:6d}\t{lines[number - 1]}" for number in range(start, end + 1))


def _diff(before: str, after: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )


def _edit_sync(
    root: Path,
    tool_input: dict,
    author: str,
    *,
    max_chars: int,
    max_bytes: int,
    images: ImageView | None = None,
) -> ToolOutcome | _ImageRead:
    command = tool_input.get("command")
    try:
        path = resolve_workspace_path(root, tool_input.get("path"))
    except PathRefused as exc:
        return ToolOutcome(str(exc), is_error=True)
    vm_path = str(VM_ROOT / path.relative_to(root).as_posix())

    # Every open goes through `safe_io`: nothing is followed, and what was opened is
    # checked through its descriptor (docs/workspace.md § Revisions).
    if command == "view":
        kind = safe_io.entry_kind(root, path)
        if kind == "dir":
            try:
                return ToolOutcome(_listing(root, path, max_chars))
            except (safe_io.UnsafeEntry, OSError):
                return ToolOutcome(f"{vm_path} is not a plain directory", is_error=True)
        if kind == "none":
            return ToolOutcome(f"{vm_path} does not exist", is_error=True)
        # A file that is not text may be an image, read up to the image cap rather than
        # the text one (docs/chat.md § Tools); it is decoded once the lock is released.
        try:
            data = safe_io.read_bytes(root, path, max(max_bytes, images.max_bytes if images else 0))
        except safe_io.EntryTooLarge:
            return ToolOutcome(_too_large_to_view(vm_path, images), is_error=True)
        except (safe_io.UnsafeEntry, OSError):
            return ToolOutcome(f"{vm_path} is not a plain file", is_error=True)
        if data is None:
            return ToolOutcome(f"{vm_path} does not exist", is_error=True)
        text = None
        if len(data) <= max_bytes:
            try:
                text = data.decode()
            except UnicodeDecodeError:
                text = None
        if text is not None:
            return ToolOutcome(truncate(_numbered(text, tool_input.get("view_range")), max_chars))
        if images is None:
            return ToolOutcome(f"{vm_path} is not a text file", is_error=True)
        return _ImageRead(
            vm_path,
            data,
            over_text_cap=len(data) > max_bytes,
            ranged=tool_input.get("view_range") is not None,
        )

    if command not in ("str_replace", "insert", "create"):
        return ToolOutcome(f"unknown editor command {command!r}", is_error=True)
    if not _writable(root, path):
        return ToolOutcome(f"{vm_path} is read-only", is_error=True)

    try:
        before = safe_io.read_text(root, path, max_bytes)
    except safe_io.EntryTooLarge:
        return ToolOutcome(f"{vm_path} is too large to edit here; use bash", is_error=True)
    except (safe_io.UnsafeEntry, OSError):
        return ToolOutcome(f"{vm_path} is not a plain file", is_error=True)
    except UnicodeDecodeError:
        return ToolOutcome(f"{vm_path} is not a text file", is_error=True)
    if before is not None and path.name not in git.RECORDED_NAMES:
        held = before.encode()
        if len(held) > git.revision_max_file_bytes() or not git.is_text(held):
            # A file history never recorded — over the cap when it arrived, or binary by
            # git's test (a NUL byte, which decodes as text all the same) — is not edited
            # here either, whatever the edit would leave: that revision would read as its
            # creation, and the card's Revert would delete it rather than put it back
            # (docs/workspace.md § Revisions, docs/chat.md § Tools).
            return ToolOutcome(
                f"{vm_path} is binary or too large for a revision to record, "
                "so it is not edited here; use bash",
                is_error=True,
            )
    if command == "create":
        new_text = tool_input.get("file_text")
        if not isinstance(new_text, str):
            return ToolOutcome("create needs `file_text`", is_error=True)
        if before is not None:
            return ToolOutcome(f"{vm_path} already exists; use str_replace", is_error=True)
        after = new_text
    elif command == "str_replace":
        if before is None:
            return ToolOutcome(f"{vm_path} does not exist", is_error=True)
        old, new = tool_input.get("old_str"), tool_input.get("new_str", "")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            return ToolOutcome("str_replace needs `old_str` and `new_str`", is_error=True)
        count = before.count(old)
        if count == 0:
            return ToolOutcome("old_str was not found in the file", is_error=True)
        if count > 1:
            return ToolOutcome(
                f"old_str occurs {count} times; include more context so it is unique",
                is_error=True,
            )
        after = before.replace(old, new, 1)
    else:  # insert
        if before is None:
            return ToolOutcome(f"{vm_path} does not exist", is_error=True)
        text_to_insert = tool_input.get("insert_text")
        try:
            line = int(tool_input.get("insert_line"))
        except (TypeError, ValueError):
            return ToolOutcome("insert needs an integer `insert_line`", is_error=True)
        if not isinstance(text_to_insert, str):
            return ToolOutcome("insert needs `insert_text`", is_error=True)
        lines = before.split("\n")
        line = max(0, min(line, len(lines)))
        inserted = text_to_insert.split("\n")
        after = "\n".join(lines[:line] + inserted + lines[line:])

    result = after.encode()
    if len(result) > max_bytes:
        return ToolOutcome(f"the result would make {vm_path} too large; use bash", is_error=True)
    if path.name not in git.RECORDED_NAMES and (
        len(result) > git.revision_max_file_bytes() or not git.is_text(result)
    ):
        # A file history would not record — this large, or binary — is not written here:
        # an edit with no revision has no undo (docs/chat.md § Tools, docs/workspace.md
        # § Revisions).
        return ToolOutcome(
            f"{vm_path} would be binary or too large for a revision to record; use bash",
            is_error=True,
        )
    # The directories a create makes for its file, deepest first: removed with the file
    # when its revision cannot be recorded, so a failed create leaves no empty folder
    # for the next reconciliation to record as a row (docs/workspace.md § Revisions).
    made: list[Path] = []
    if before is None:
        parent = path.parent
        while parent != root and safe_io.entry_kind(root, parent) == "none":
            made.append(parent)
            parent = parent.parent
    try:
        safe_io.write_text(root, path, after, exist_ok=command != "create")
    except FileExistsError:
        return ToolOutcome(f"{vm_path} already exists; use str_replace", is_error=True)
    except safe_io.UnsafeEntry:
        return ToolOutcome(f"{vm_path} is not a plain file", is_error=True)
    git.ensure_repo(root)
    try:
        commit = git.commit_paths(root, author, f"{command} {vm_path}", [path.relative_to(root)])
    except git.GitError:
        # The file as it was: an edit with no revision would ride into a shell's next
        # commit as someone else's change (docs/workspace.md § Revisions).
        if before is None:
            safe_io.remove_file(root, path)
            for directory in made:
                safe_io.remove_empty_dir(root, directory)
        else:
            safe_io.write_text(root, path, before)
        raise
    diff = _diff(before or "", after, vm_path)
    return ToolOutcome(
        f"{command} applied to {vm_path}",
        edit={
            "path": vm_path,
            "command": command,
            "commit": commit,
            "diff": truncate(diff, max_chars),
        },
    )


def _edit_guarded(
    root: Path,
    tool_input: dict,
    author: str,
    *,
    max_chars: int,
    max_bytes: int,
    images: ImageView | None = None,
) -> ToolOutcome | _ImageRead:
    """A path that is well-formed but unusable — a directory where a file was expected, a
    parent that is a file, a vanished file — is the model's mistake to correct, not the
    turn's end."""
    try:
        return _edit_sync(
            root, tool_input, author, max_chars=max_chars, max_bytes=max_bytes, images=images
        )
    except (OSError, git.GitError) as exc:
        return ToolOutcome(f"The file operation failed: {type(exc).__name__}", is_error=True)


async def run_editor(
    root: Path,
    tool_input: dict,
    author: str,
    *,
    max_chars: int,
    max_bytes: int,
    images: ImageView | None = None,
) -> ToolOutcome:
    """The editor. With `images`, a `view` of an image file answers the image; without,
    such a file is not text and is refused (docs/chat.md § Tools)."""
    # A view under the lock too: a shell command of another conversation may be
    # rewriting the file, and a read of it half-written would feed the model a mix.
    async with workspace_lock(root):
        outcome = await asyncio.to_thread(
            _edit_guarded,
            root,
            tool_input,
            author,
            max_chars=max_chars,
            max_bytes=max_bytes,
            images=images,
        )
    # The bytes were read under the lock; the decode waits for nobody's write.
    if isinstance(outcome, _ImageRead) and images is not None:
        return await _view_image(outcome, images)
    return outcome


def _convert(vm_path: str, header: ImageHeader | None, operation: str, output: str) -> str:
    """An ImageMagick command on the image as this tool shows it (docs/chat.md § Tools).
    The file comes in on stdin, so ImageMagick never parses its name: a `*`, `?`, `[1]` or
    `%d` in a library name would pick other files or none. Its first frame only (an
    animated GIF or WebP would otherwise give a file per frame), turned upright as Pillow
    turned it — by the orientation the header named, since ImageMagick 6 reads none from
    a PNG or a WebP; `-auto-orient` when the header was never read — then its page reset,
    since a transpose and a PNG's stored offset leave a canvas the crop would tile against,
    flattened onto white as the tool flattens it, and its EXIF stripped, so the piece is
    not turned a second time when it is viewed."""
    if header is None:
        turn = " -auto-orient"
    elif header.orientation != 1:
        turn = " " + ORIENTATIONS[header.orientation][1]
    else:
        turn = ""
    return (
        f"convert '-[0]'{turn} +repage -background white -alpha remove -alpha off -strip "
        f"{operation} {output} < {shlex.quote(vm_path)}"
    )


def _piece_commands(vm_path: str, long_edge: int, header: ImageHeader | None, png: bool) -> str:
    """The shell commands that cut an image into pieces this tool shows at full size, in
    the frame the tool shows it in, so the coordinates agree. A PNG's pieces are PNGs, any
    other's JPEGs, as this tool would send them (docs/chat.md § Tools)."""
    suffix = "png" if png else "jpg"
    count = ""
    if header is not None:
        pieces = math.ceil(header.width / long_edge) * math.ceil(header.height / long_edge)
        count = f" ({pieces} pieces)"
    every = _convert(
        vm_path,
        header,
        f"-crop {long_edge}x{long_edge} +repage",
        f"/workspace/.cache/piece-%d.{suffix}",
    )
    region = _convert(
        vm_path, header, "-crop WxH+X+Y +repage", f"/workspace/.cache/region.{suffix}"
    )
    return (
        f"Every piece, left to right then top to bottom{count}: `{every}`. "
        f"One region, X,Y its top-left corner in the full image: `{region}`."
    )


def _in_steps(vm_path: str, long_edge: int, header: ImageHeader | None, png: bool) -> str:
    """How to look at an image this tool will not open: a smaller copy for the whole, then
    pieces for the detail."""
    small = _convert(
        vm_path, header, f"-resize {long_edge}x{long_edge}", "/workspace/.cache/small.jpg"
    )
    return (
        f"Look at it in steps with bash: make a smaller copy to see the whole, `{small}`, "
        "view that, then crop the parts you need under /workspace/.cache/ and view them one "
        "by one. " + _piece_commands(vm_path, long_edge, header, png)
    )


def _too_large_to_view(vm_path: str, images: ImageView | None) -> str:
    if images is None:
        return f"{vm_path} is too large to view; use bash"
    message = (
        f"{vm_path} is larger than this tool reads ({images.max_bytes // (1024 * 1024)} MiB); "
        "use bash."
    )
    if not images.shell:
        return message
    # Nothing of it was read: whether its pieces are PNGs goes by its name.
    png = vm_path.lower().endswith(".png")
    return f"{message} If it is an image: " + _in_steps(vm_path, images.long_edge, None, png)


async def _view_image(read: _ImageRead, images: ImageView) -> ToolOutcome:
    """An image a `view` read: decoded one at a time per turn with the pages, by the
    decoders an inserted image is offered and no other, and shrunk to the long edge an
    attached image is kept at; one too large to read in one look says how to read it in
    pieces (docs/chat.md § Tools)."""
    vm_path, long_edge = read.vm_path, images.long_edge
    try:
        async with images.decode_slot:
            image = await _settled(
                asyncio.to_thread(
                    _model_image,
                    read.data,
                    images.max_pixels,
                    long_edge,
                    formats=IMAGE_FORMATS,
                    png_only=False,
                )
            )
    except (ImageTooLarge, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        header = exc.header if isinstance(exc, ImageTooLarge) else None
        stated = f"{header.width}x{header.height} pixels" if header else "too many pixels"
        message = f"{vm_path} is {stated}, more than this tool opens ({images.max_pixels:,})."
        if images.shell:
            # Pillow's own bomb check stops before a header exists: the bytes say PNG.
            png = header.format == "PNG" if header else read.data.startswith(PNG_SIGNATURE)
            message += " " + _in_steps(vm_path, long_edge, header, png)
        return ToolOutcome(message, is_error=True)
    except UnidentifiedImageError:
        if read.over_text_cap:
            return ToolOutcome(f"{vm_path} is too large to view; use bash", is_error=True)
        return ToolOutcome(
            f"{vm_path} is not a text file or an image this tool shows (PNG, JPEG, WebP, GIF, "
            "HEIC); use bash",
            is_error=True,
        )
    except (OSError, ValueError, SyntaxError, EOFError, RuntimeError):
        # A known format by its header whose data then failed: truncated or damaged, or a
        # HEIC whose coded size is not the one its header gave (pillow_heif's RuntimeError).
        return ToolOutcome(f"{vm_path} could not be decoded as an image", is_error=True)
    source = image.source
    note = f"The image {vm_path}, {source.width}x{source.height} pixels."
    if (image.shown_width, image.shown_height) != (source.width, source.height):
        note = (
            f"The image {vm_path}, {source.width}x{source.height} pixels, shown shrunk to "
            f"{image.shown_width}x{image.shown_height}: small text and fine detail may be lost."
        )
        if images.shell:
            note += (
                f" To read it at full size, crop it with bash into pieces of at most "
                f"{long_edge}x{long_edge} under /workspace/.cache/ and view them one by one "
                "with this tool. "
                + _piece_commands(vm_path, long_edge, source, source.format == "PNG")
            )
    if read.ranged:
        note += " view_range does not apply to an image; this is all of it."
    return ToolOutcome([_image_block(image.data, image.media_type), {"type": "text", "text": note}])


def _commit_shell_sync(root: Path, command: str, author: str, *, max_chars: int) -> dict | None:
    git.ensure_repo(root)
    label = command.strip().splitlines()[0][:72] if command.strip() else "bash"
    commit = git.commit_all(root, author, f"bash: {label}")
    if commit is None:
        return None
    diff = git.show_commit(root, commit, max_chars * 4)
    return {"path": "", "command": "bash", "commit": commit, "diff": truncate(diff, max_chars)}


async def commit_shell_changes(
    root: Path,
    outcome: ToolOutcome,
    tool_input: dict,
    author: str,
    *,
    max_chars: int,
    locked: bool = False,
) -> ToolOutcome:
    """After a shell command: whatever it changed under the workspace becomes one
    revision, under the user's lock like every other write (docs/workspace.md) — a lock
    the caller may already hold across the command itself (`locked`)."""
    command = tool_input.get("command")
    if not isinstance(command, str):
        return outcome
    async with contextlib.nullcontext() if locked else workspace_lock(root):
        try:
            edit = await asyncio.to_thread(
                _commit_shell_sync, root, command, author, max_chars=max_chars
            )
        except git.GitError as exc:
            # Not a success: the changes are on disk and unrecorded. The index is reset
            # so nothing half-staged rides into a later path-scoped commit, and the
            # model is told (docs/chat.md § Tools).
            await asyncio.to_thread(git.reset_index, root)
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            return ToolOutcome(
                f"{outcome.content}\n\n"
                f"[The changes this command made could not be recorded as a revision: "
                f"{reason}]",
                is_error=True,
            )
    if edit is None:
        return outcome
    return ToolOutcome(outcome.content, is_error=outcome.is_error, edit=edit)


# --- pages and figures --------------------------------------------------------------------


def _image_block(data: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _model_image(
    source: Path | bytes,
    max_pixels: int,
    long_edge_px: int = PAGE_IMAGE_LONG_EDGE_PX,
    *,
    formats: tuple[str, ...] | None = None,
    png_only: bool = True,
) -> ModelImage:
    """An image no longer than `long_edge_px` — what the model would keep, or what a
    preview shows — oriented, flattened onto white: a PNG, or with `png_only` off a PNG
    only when the source was one and a JPEG otherwise, so a photo is not sent at a PNG's
    size. `formats` limits the decoders Pillow may try. The size comes from the header: a
    file that would decode past `max_pixels` (`CHAT_PAGE_IMAGE_MAX_PIXELS`) is refused
    before a raster exists, and Pillow's own bomb check is an error, never a warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        opened = Image.open(
            io.BytesIO(source) if isinstance(source, bytes) else source, formats=formats
        )
    with opened:
        # The cap is checked before anything decodes: a PNG's own `getexif` decodes the
        # whole raster to look for an EXIF chunk after it, so one past the cap is described
        # by what its header gave, which the base class's `getexif` parses and nothing more.
        over_cap = opened.width * opened.height > max_pixels
        try:
            exif = Image.Image.getexif(opened) if over_cap else opened.getexif()
            orientation = int(exif.get(0x0112, 1))
        except Exception:  # noqa: BLE001 - an unreadable EXIF block leaves the image as stored
            orientation = 1
        turned = orientation in (5, 6, 7, 8)
        header = ImageHeader(
            width=opened.height if turned else opened.width,
            height=opened.width if turned else opened.height,
            orientation=orientation if orientation in ORIENTATIONS else 1,
            format=opened.format or "",
        )
        if over_cap:
            raise ImageTooLarge(header)
        keep_png = png_only or header.format == "PNG"
        image: Image.Image = opened
        if header.orientation != 1:
            image = opened.transpose(ORIENTATIONS[header.orientation][0])
        if image.mode in ("I;16", "I;16B", "I;16L", "I"):
            # 16-bit grey (a microscope's, a depth map's): to 8 bits by its top byte. A
            # plain conversion to RGB clips everything past 255 to white.
            image = image.convert("I").point(lambda value: value / 256).convert("L")
        # A grey or RGB PNG keeps its transparency as one key colour, not a channel.
        if image.mode in ("RGBA", "LA", "P") or "transparency" in image.info:
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        long_edge = max(image.width, image.height)
        if long_edge > long_edge_px:
            scale = long_edge_px / long_edge
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        if keep_png:
            image.save(buffer, format="PNG", optimize=True)
        else:
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return ModelImage(
            data=buffer.getvalue(),
            media_type="image/png" if keep_png else "image/jpeg",
            source=header,
            shown_width=image.width,
            shown_height=image.height,
        )


async def _settled(coro):
    """Await `coro` so that a cancel waits for it rather than leaving it running: a
    decode in a thread is not stopped by cancelling its await, and the slot it holds
    would be released under a raster still in memory (docs/chat.md § Tools)."""
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: a decode that failed under a cancel is the cancel's
        raise


def _open_source(root: Path, source: Path, max_bytes: int) -> int | None:
    """A descriptor on the original: through the workspace's own open when the file is
    the shell's copy in the tree, a plain open that follows nothing when it is the
    original kept outside the mount — the service's own directory, which no command
    reaches (docs/workspace.md § Layout). None when it is gone."""
    if source.is_relative_to(root):
        return safe_io.open_for_read(root, source, max_bytes)
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise safe_io.UnsafeEntry(f"{source} is not a plain file")
        if info.st_size > max_bytes:
            raise safe_io.EntryTooLarge(f"{source} is larger than {max_bytes} bytes")
    except BaseException:
        os.close(fd)
        raise
    return fd


async def view_page(
    paths: DocumentPaths,
    tool_input: dict,
    *,
    document_id: int,
    page_count: int | None,
    render_slots: asyncio.Semaphore,
    decode_slot: asyncio.Semaphore,
    max_bytes: int,
    max_pixels: int,
    kind: str,
) -> ToolOutcome:
    if kind == MARKDOWN_KIND:
        # Its original is the text doc.md started as (docs/workspace.md § Markdown documents).
        return ToolOutcome(
            "This is a markdown document: it has no pages, and doc.md is the text itself.",
            is_error=True,
        )
    # The original kept outside the mount first; the shell's copy only when there is
    # none (a document filed before originals were kept there).
    source = original_of(paths.root, document_id) or paths.source()
    if source is None:
        return ToolOutcome(
            "This document kept no original file, so its pages cannot be rendered.",
            is_error=True,
        )
    try:
        page = int(tool_input.get("page"))
    except (TypeError, ValueError):
        return ToolOutcome("view_page needs an integer `page`", is_error=True)
    if page < 1 or (page_count and page > page_count):
        return ToolOutcome(f"page must be between 1 and {page_count or 1}", is_error=True)
    if kind != "pdf" and page != 1:
        return ToolOutcome("an image document has one page", is_error=True)
    try:
        data = await original_page_png(
            paths.root,
            source,
            kind=kind,
            page=page,
            render_slots=render_slots,
            decode_slot=decode_slot,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
        )
    except PageUnavailable as exc:
        return ToolOutcome(str(exc), is_error=True)
    return ToolOutcome([_image_block(data, "image/png"), {"type": "text", "text": f"Page {page}."}])


class PageUnavailable(Exception):
    """Why a page of an original cannot be drawn: what `view_page` tells the model, and why
    a page preview answers 404."""


async def original_page_png(
    root: Path,
    source: Path,
    *,
    kind: str,
    page: int,
    render_slots: asyncio.Semaphore,
    decode_slot: asyncio.Semaphore,
    max_bytes: int,
    max_pixels: int,
    long_edge_px: int = PAGE_IMAGE_LONG_EDGE_PX,
) -> bytes:
    """One page of a document's original as a PNG of at most `long_edge_px`: Poppler's
    render of a PDF page, or the image itself. Shared by the chat agent's `view_page` and
    the library's page preview (docs/api.md). Raises `PageUnavailable`."""
    # Poppler gets the file as a descriptor opened without following anything, not as a
    # path it would resolve again (docs/workspace.md § Revisions).
    try:
        fd = _open_source(root, source, max_bytes)
    except (safe_io.UnsafeEntry, safe_io.EntryTooLarge, OSError):
        raise PageUnavailable("the original file could not be opened") from None
    if fd is None:
        raise PageUnavailable("the original file is gone")
    try:
        if kind == "pdf":
            handle = Path(f"/proc/self/fd/{fd}")
            with tempfile.TemporaryDirectory(prefix="sr-view-") as work:
                work_dir = Path(work)
                try:
                    async with render_slots:
                        info = await poppler.pdf_info(handle, cwd=work_dir, pass_fds=(fd,))
                        rendered = await poppler.render_page(
                            handle, page, info.page_size(page), cwd=work_dir, pass_fds=(fd,)
                        )
                    # The raster, one at a time per turn: a round of four reads must
                    # not hold four decoded pages before the budget sees any of them.
                    async with decode_slot:
                        page_image = await _settled(
                            asyncio.to_thread(_model_image, rendered, max_pixels, long_edge_px)
                        )
                    return page_image.data
                except poppler.PopplerError:
                    raise PageUnavailable(f"page {page} could not be rendered") from None
        try:
            with os.fdopen(os.dup(fd), "rb") as reader:
                raw = await asyncio.to_thread(reader.read, max_bytes + 1)
            async with decode_slot:
                page_image = await _settled(
                    asyncio.to_thread(_model_image, raw, max_pixels, long_edge_px)
                )
            return page_image.data
        except OSError:
            raise PageUnavailable("the original image could not be read") from None
        except (ImageTooLarge, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise PageUnavailable("the original image is too large to render") from None
    finally:
        os.close(fd)


async def view_figure(paths: DocumentPaths, tool_input: dict, *, max_bytes: int) -> ToolOutcome:
    try:
        page = int(tool_input.get("page"))
        raw_bbox = tool_input.get("bbox")
        bbox = parse_bbox_path(",".join(str(int(value)) for value in raw_bbox))
    except (TypeError, ValueError):
        return ToolOutcome("view_figure needs `page` and a 4-integer `bbox`", is_error=True)
    if bbox is None or page < 1:
        return ToolOutcome("that bbox is not a usable box", is_error=True)
    # The crops live in the document directory under the same name the figures dir uses
    # (docs/parsing.md § Figure crops): the page and the cleaned box.
    y_min, x_min, y_max, x_max = bbox
    crop = paths.figures / f"p{page}_{y_min}_{x_min}_{y_max}_{x_max}.png"
    try:
        data = await asyncio.to_thread(safe_io.read_bytes, paths.root, crop, max_bytes)
    except (safe_io.UnsafeEntry, safe_io.EntryTooLarge, OSError):
        data = None
    if data is None:
        return ToolOutcome("no crop is stored for that figure", is_error=True)
    return ToolOutcome(
        [_image_block(data, "image/png"), {"type": "text", "text": f"Figure at p{page}."}]
    )
