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
import os
import stat
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

from ..parsing import poppler
from ..parsing.figures import parse_bbox_path
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..workspace import git, safe_io
from ..workspace.documents import DocumentPaths, original_of
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
# The most an image document may decode to: read from the header, before any decoding.
PAGE_IMAGE_MAX_PIXELS = 40_000_000


class ImageTooLarge(Exception):
    """The file would decode to more pixels than the worker will hold."""


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
    if any(part in READ_ONLY_NAMES or part == FIGURES_DIR for part in relative.parts):
        return False
    name = relative.name
    return not (name.startswith((f"{SOURCE_STEM}.", safe_io.TEMP_PREFIX)))


def _listing(root: Path, path: Path) -> str:
    """The directory's entries, read through a descriptor opened beneath the root with
    nothing followed — a path listing could be swapped for a link in between."""
    entries = safe_io.list_dir(root, path)
    rows = [
        f"{name}/" if is_dir else name
        for name, is_dir in sorted(entries, key=lambda item: item[0])
        if name != ".git"
    ]
    return "\n".join(rows) or "(empty directory)"


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
    root: Path, tool_input: dict, author: str, *, max_chars: int, max_bytes: int
) -> ToolOutcome:
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
                return ToolOutcome(_listing(root, path))
            except (safe_io.UnsafeEntry, OSError):
                return ToolOutcome(f"{vm_path} is not a plain directory", is_error=True)
        if kind == "none":
            return ToolOutcome(f"{vm_path} does not exist", is_error=True)
        try:
            text = safe_io.read_text(root, path, max_bytes)
        except safe_io.EntryTooLarge:
            return ToolOutcome(f"{vm_path} is too large to view; use bash", is_error=True)
        except (safe_io.UnsafeEntry, OSError):
            return ToolOutcome(f"{vm_path} is not a plain file", is_error=True)
        except UnicodeDecodeError:
            return ToolOutcome(f"{vm_path} is not a text file", is_error=True)
        if text is None:
            return ToolOutcome(f"{vm_path} does not exist", is_error=True)
        return ToolOutcome(truncate(_numbered(text, tool_input.get("view_range")), max_chars))

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

    if len(after.encode()) > max_bytes:
        return ToolOutcome(f"the result would make {vm_path} too large; use bash", is_error=True)
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
    root: Path, tool_input: dict, author: str, *, max_chars: int, max_bytes: int
) -> ToolOutcome:
    """A path that is well-formed but unusable — a directory where a file was expected, a
    parent that is a file, a vanished file — is the model's mistake to correct, not the
    turn's end."""
    try:
        return _edit_sync(root, tool_input, author, max_chars=max_chars, max_bytes=max_bytes)
    except (OSError, git.GitError) as exc:
        return ToolOutcome(f"The file operation failed: {type(exc).__name__}", is_error=True)


async def run_editor(
    root: Path, tool_input: dict, author: str, *, max_chars: int, max_bytes: int
) -> ToolOutcome:
    # A view under the lock too: a shell command of another conversation may be
    # rewriting the file, and a read of it half-written would feed the model a mix.
    async with workspace_lock(root):
        return await asyncio.to_thread(
            _edit_guarded, root, tool_input, author, max_chars=max_chars, max_bytes=max_bytes
        )


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


def _png_block(data: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _as_png(source: Path | bytes) -> bytes:
    """A PNG no larger than the model would keep, oriented, flattened onto white. The
    size comes from the header: a file that would decode past the pixel cap is refused
    before a raster exists, and Pillow's own bomb check is an error, never a warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        opened = Image.open(io.BytesIO(source) if isinstance(source, bytes) else source)
    with opened:
        if opened.width * opened.height > PAGE_IMAGE_MAX_PIXELS:
            raise ImageTooLarge(f"{opened.width}x{opened.height} exceeds the pixel cap")
        image = ImageOps.exif_transpose(opened) or opened
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image).convert("RGB")
        elif image.mode != "RGB":
            image = image.convert("RGB")
        long_edge = max(image.width, image.height)
        if long_edge > PAGE_IMAGE_LONG_EDGE_PX:
            scale = PAGE_IMAGE_LONG_EDGE_PX / long_edge
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


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
    kind: str,
) -> ToolOutcome:
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

    # Poppler gets the file as a descriptor opened without following anything, not as a
    # path it would resolve again (docs/workspace.md § Revisions).
    try:
        fd = _open_source(paths.root, source, max_bytes)
    except (safe_io.UnsafeEntry, safe_io.EntryTooLarge, OSError):
        return ToolOutcome("the original file could not be opened", is_error=True)
    if fd is None:
        return ToolOutcome("the original file is gone", is_error=True)
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
                        data = await _settled(asyncio.to_thread(_as_png, rendered))
                except poppler.PopplerError:
                    return ToolOutcome(f"page {page} could not be rendered", is_error=True)
        else:
            try:
                with os.fdopen(os.dup(fd), "rb") as reader:
                    raw = await asyncio.to_thread(reader.read, max_bytes + 1)
                async with decode_slot:
                    data = await _settled(asyncio.to_thread(_as_png, raw))
            except OSError:
                return ToolOutcome("the original image could not be read", is_error=True)
            except (ImageTooLarge, Image.DecompressionBombError, Image.DecompressionBombWarning):
                return ToolOutcome("the original image is too large to render", is_error=True)
    finally:
        os.close(fd)
    return ToolOutcome([_png_block(data), {"type": "text", "text": f"Page {page}."}])


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
    return ToolOutcome([_png_block(data), {"type": "text", "text": f"Figure at p{page}."}])
