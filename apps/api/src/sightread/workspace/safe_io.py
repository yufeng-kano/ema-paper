"""Opening workspace files without trusting the path (docs/workspace.md § Revisions).

The tree is shared with a shell that can rewrite any entry between one syscall and the
next, so a check followed by an open is not a check. Every open here walks from the user
root one component at a time with `O_NOFOLLOW`, so no symlink is ever followed — not at
the end, not in the middle — and what was opened is inspected through its descriptor
before it is read or written: a plain file of a bounded size, or nothing.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

_DIR_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_CHUNK = 1 << 20
# A write in progress, beside its destination; the git exclude rules keep it out of history.
TEMP_PREFIX = ".sr-tmp-"


class UnsafeEntry(Exception):
    """The path is not a plain file (or directory) reachable without following a link."""


class EntryTooLarge(Exception):
    """The file exists but is larger than the caller will hold in memory."""


def _descend(root: Path, path: Path, *, create: bool = False) -> tuple[int, str]:
    """Open `path`'s parent directory beneath `root`; returns (dir fd, final name). With
    `create`, a directory missing on the way is made relative to the one above it and
    then opened without following — a link that lands under the name in between is
    refused, never entered."""
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
                next_fd = _open_child_dir(fd, part, create=create)
            except OSError as exc:
                if isinstance(exc, FileNotFoundError):
                    raise
                raise UnsafeEntry(f"{path}: {part} is not a plain directory") from exc
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def _open_child_dir(fd: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=fd)
    except FileNotFoundError:
        if not create:
            raise
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o755, dir_fd=fd)
    return os.open(name, _DIR_FLAGS, dir_fd=fd)


def ensure_dir(root: Path, path: Path) -> None:
    """Make the directory at `path` and whatever is missing above it, each component
    through the descriptor of the one above; a link anywhere on the way is a refusal."""
    if path == root:
        return
    fd, name = _descend(root, path, create=True)
    try:
        os.close(_open_child_dir(fd, name, create=True))
    except OSError as exc:
        raise UnsafeEntry(f"{path} is not a plain directory") from exc
    finally:
        os.close(fd)


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


def _write_atomic(root: Path, path: Path, fill, *, exist_ok: bool) -> None:
    """Fill a temporary file beside `path` and rename it into place once every byte is
    there, so a write that fails halfway — the disk full, the process gone — never leaves
    the destination empty or cut short (docs/workspace.md § Revisions). `fill(fd)` writes
    the content. Without `exist_ok` the file is linked into place instead, which fails
    when anything already sits at the destination."""
    fd, name = _descend(root, path, create=True)
    try:
        temp = f"{TEMP_PREFIX}{secrets.token_hex(8)}"
        entry = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=fd,
        )
        try:
            try:
                fill(entry)
            finally:
                os.close(entry)
            if exist_ok:
                try:
                    os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                except IsADirectoryError as exc:
                    raise UnsafeEntry(f"{path} is a directory") from exc
                return
            try:
                os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            except FileExistsError:
                raise
            except OSError as exc:
                raise UnsafeEntry(f"{path} could not be created as a plain file") from exc
        finally:
            # After a rename the temporary name is gone; after a link, or a failure, it
            # is still there and must not stay.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=fd)
    finally:
        os.close(fd)


def write_bytes(root: Path, path: Path, data: bytes, *, exist_ok: bool = True) -> None:
    """Write `data` to the plain file at `path`, created if missing (parents too)."""

    def fill(entry: int) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(entry, view) :]

    _write_atomic(root, path, fill, exist_ok=exist_ok)


def write_text(root: Path, path: Path, text: str, *, exist_ok: bool = True) -> None:
    write_bytes(root, path, text.encode(), exist_ok=exist_ok)


def write_from(root: Path, path: Path, reader, *, chunk: int = READ_CHUNK) -> None:
    """Stream `reader` (any object with `read(n)`) into the plain file at `path`."""

    def fill(entry: int) -> None:
        while True:
            data = reader.read(chunk)
            if not data:
                return
            view = memoryview(data)
            while view:
                view = view[os.write(entry, view) :]

    _write_atomic(root, path, fill, exist_ok=True)


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


def _open_dir(root: Path, path: Path) -> int:
    """A descriptor on the plain directory at `path`, opened beneath `root`."""
    if path == root:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    fd, name = _descend(root, path)
    try:
        return os.open(name, _OPEN_DIR_FLAGS, dir_fd=fd)
    except OSError as exc:
        raise UnsafeEntry(f"{path} is not a plain directory") from exc
    finally:
        os.close(fd)


def list_dir(root: Path, path: Path) -> list[tuple[str, bool]]:
    """`(name, is_dir)` for each entry of the directory at `path`, looked at without
    following anything: a link is listed as a file."""
    fd = _open_dir(root, path)
    try:
        entries: list[tuple[str, bool]] = []
        for name in os.listdir(fd):
            try:
                mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
            except OSError:
                continue
            entries.append((name, stat.S_ISDIR(mode)))
        return entries
    finally:
        os.close(fd)


def _remove_contents(fd: int) -> None:
    for name in os.listdir(fd):
        mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
        if stat.S_ISDIR(mode):
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
            )
            try:
                _remove_contents(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)


def remove_tree(root: Path, path: Path) -> bool:
    """Delete the directory at `path` and everything under it, every step through a
    descriptor opened without following: a link is unlinked, never descended into.
    False when there was nothing there — a parent included."""
    try:
        fd, name = _descend(root, path)
    except FileNotFoundError:
        return False
    try:
        try:
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode):
            os.unlink(name, dir_fd=fd)
            return True
        child = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd
        )
        try:
            _remove_contents(child)
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=fd)
        return True
    finally:
        os.close(fd)


def rename(root: Path, source: Path, target: Path) -> None:
    """Move `source` to `target`, both beneath `root`, resolving neither through a link."""
    src_fd, src_name = _descend(root, source)
    try:
        dst_fd, dst_name = _descend(root, target, create=True)
        try:
            os.rename(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def remove_empty_dir(root: Path, path: Path) -> bool:
    """`rmdir` the directory at `path` through its parent's descriptor; False when it is
    not empty or not there."""
    fd, name = _descend(root, path)
    try:
        os.rmdir(name, dir_fd=fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
