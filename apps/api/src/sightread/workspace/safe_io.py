"""Opening workspace files without trusting the path (docs/workspace.md § Revisions).

The tree is shared with a shell that can rewrite any entry between one syscall and the
next, so a check followed by an open is not a check. Every open here walks from the user
root one component at a time with `O_NOFOLLOW`, so no symlink is ever followed — not at
the end, not in the middle — and what was opened is inspected through its descriptor
before it is read or written: a plain file of a bounded size, or nothing.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_CHUNK = 1 << 20


class UnsafeEntry(Exception):
    """The path is not a plain file (or directory) reachable without following a link."""


class EntryTooLarge(Exception):
    """The file exists but is larger than the caller will hold in memory."""


def _descend(root: Path, path: Path) -> tuple[int, str]:
    """Open `path`'s parent directory beneath `root`; returns (dir fd, final name)."""
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise UnsafeEntry(f"{path} is outside the workspace") from exc
    if not parts:
        raise UnsafeEntry("the workspace root is not a file")
    fd = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError as exc:
                raise UnsafeEntry(f"{path}: {part} is not a plain directory") from exc
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def entry_kind(root: Path, path: Path) -> str:
    """`file`, `dir`, `none` or `other` — what sits at `path`, without following links.
    A component that is not a plain directory makes the whole path `other`."""
    try:
        fd, name = _descend(root, path)
    except UnsafeEntry:
        return "other"
    except FileNotFoundError:
        return "none"  # a parent (the user root itself, before the first write) is not there
    try:
        try:
            entry = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            return "none"
        except OSError:
            return "other"
        try:
            mode = os.fstat(entry).st_mode
        finally:
            os.close(entry)
    finally:
        os.close(fd)
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    return "other"


def _open_file(root: Path, path: Path, flags: int, mode: int = 0o644) -> int:
    """A descriptor on the plain file at `path`, opened beneath `root` without following
    anything. `O_NONBLOCK` keeps a FIFO from blocking the open; the fstat after it is
    what refuses the FIFO."""
    fd, name = _descend(root, path)
    try:
        entry = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, mode, dir_fd=fd)
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        raise UnsafeEntry(f"{path} could not be opened as a plain file") from exc
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(entry).st_mode):
            raise UnsafeEntry(f"{path} is not a plain file")
        os.set_blocking(entry, True)
    except BaseException:
        os.close(entry)
        raise
    return entry


def open_for_read(root: Path, path: Path, max_bytes: int) -> int | None:
    """A descriptor on `path` for reading, or None when there is nothing there."""
    try:
        entry = _open_file(root, path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        if os.fstat(entry).st_size > max_bytes:
            raise EntryTooLarge(f"{path} exceeds {max_bytes} bytes")
    except BaseException:
        os.close(entry)
        raise
    return entry


def read_bytes(root: Path, path: Path, max_bytes: int) -> bytes | None:
    entry = open_for_read(root, path, max_bytes)
    if entry is None:
        return None
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(entry, READ_CHUNK)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > max_bytes:
                # The file grew after the size check: the bound still holds.
                raise EntryTooLarge(f"{path} exceeds {max_bytes} bytes")
            chunks.append(chunk)
    finally:
        os.close(entry)


def read_text(root: Path, path: Path, max_bytes: int) -> str | None:
    data = read_bytes(root, path, max_bytes)
    return None if data is None else data.decode()


def write_bytes(root: Path, path: Path, data: bytes, *, exist_ok: bool = True) -> None:
    """Write `data` to the plain file at `path`, created if missing (parents too)."""
    kind = entry_kind(root, path.parent)
    if kind == "none":
        path.parent.mkdir(parents=True, exist_ok=True)
    elif kind != "dir":
        raise UnsafeEntry(f"{path.parent} is not a plain directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if not exist_ok:
        flags |= os.O_EXCL
    entry = _open_file(root, path, flags)
    try:
        view = memoryview(data)
        while view:
            written = os.write(entry, view)
            view = view[written:]
    finally:
        os.close(entry)


def write_text(root: Path, path: Path, text: str, *, exist_ok: bool = True) -> None:
    write_bytes(root, path, text.encode(), exist_ok=exist_ok)


def write_from(root: Path, path: Path, reader, *, chunk: int = READ_CHUNK) -> None:
    """Stream `reader` (any object with `read(n)`) into the plain file at `path`."""
    kind = entry_kind(root, path.parent)
    if kind == "none":
        path.parent.mkdir(parents=True, exist_ok=True)
    elif kind != "dir":
        raise UnsafeEntry(f"{path.parent} is not a plain directory")
    entry = _open_file(root, path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        while True:
            data = reader.read(chunk)
            if not data:
                return
            view = memoryview(data)
            while view:
                view = view[os.write(entry, view) :]
    finally:
        os.close(entry)


def remove_file(root: Path, path: Path) -> bool:
    """Unlink the plain file at `path`; False when there was nothing. A link is unlinked
    as itself, never its target; a directory is left alone."""
    fd, name = _descend(root, path)
    try:
        try:
            os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            return False
        except IsADirectoryError as exc:
            raise UnsafeEntry(f"{path} is a directory") from exc
        return True
    finally:
        os.close(fd)
