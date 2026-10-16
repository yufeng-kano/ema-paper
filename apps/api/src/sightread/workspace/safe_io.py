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
# A walk that measures keeps one descriptor per level and goes no deeper than this: a
# tree nested past it cannot be measured (docs/workspace.md § Quota). A removal has no
# such bound — it folds the deepest levels up as it goes (`_remove_contents`).
DEPTH_MAX = 512
# How many levels a removal keeps open before it folds the deepest one up to the top.
REMOVE_DEPTH_WINDOW = 64


class TreeTooDeep(OSError):
    """A directory nested past `DEPTH_MAX`: not measured, never silently undercounted."""


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


def list_dir(root: Path, path: Path, *, limit: int | None = None) -> list[tuple[str, bool]]:
    """`(name, is_dir)` for each entry of the directory at `path`, looked at without
    following anything: a link is listed as a file. Read lazily, and no further than
    `limit` entries when one is given: a directory a shell filled with millions of
    files is not held whole in memory for a listing that shows a page of it."""
    fd = _open_dir(root, path)
    try:
        entries: list[tuple[str, bool]] = []
        with os.scandir(fd) as scan:
            for entry in scan:
                if limit is not None and len(entries) >= limit:
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((entry.name, is_dir))
        return entries
    finally:
        os.close(fd)


def _remove_contents(fd: int) -> None:
    """Empty the directory `fd` through descriptors, children first, with an explicit
    stack rather than recursion (a shell can nest past the interpreter's limit) — and
    with a bounded set of open descriptors however deep the tree: at the window, the
    deepest directory is renamed up into `fd` itself under a temporary name, where the
    walk meets it again at depth one, so a chain of any depth is removed with a handful
    of descriptors."""
    # (parent fd, name, child fd): a directory is removed once its own entries are gone.
    stack: list[tuple[int | None, str | None, int]] = [(None, None, fd)]
    try:
        while stack:
            parent, name, current = stack[-1]
            if len(stack) > REMOVE_DEPTH_WINDOW and parent is not None and name is not None:
                folded = f"{TEMP_PREFIX}{secrets.token_hex(8)}"
                os.rename(name, folded, src_dir_fd=parent, dst_dir_fd=fd)
                stack.pop()
                os.close(current)
                continue
            removed_children = False
            for entry in os.listdir(current):
                mode = os.stat(entry, dir_fd=current, follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    child = os.open(entry, _OPEN_DIR_FLAGS, dir_fd=current)
                    stack.append((current, entry, child))
                    removed_children = True
                    break
                os.unlink(entry, dir_fd=current)
            if removed_children:
                continue  # descend first; this directory comes back once the child is gone
            stack.pop()
            if parent is not None and name is not None:
                os.close(current)
                os.rmdir(name, dir_fd=parent)
    except BaseException:
        for parent, _name, current in stack:
            if parent is not None:
                os.close(current)
        raise


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


def _dir_size(fd: int) -> int:
    """An explicit stack, not recursion: a shell can nest directories deeper than the
    interpreter's recursion limit. One open descriptor per level of depth, never one per
    sibling: a directory's entries are taken one at a time, and a child is opened only
    when its turn comes, so a wide tree costs no more descriptors than a deep one.
    `fd` is closed by the caller; children here."""
    total = 0
    # (directory, the names still to look at in it)
    stack: list[tuple[int, list[str]]] = [(fd, _names(fd))]
    try:
        while stack:
            current, names = stack[-1]
            if not names:
                stack.pop()
                if current != fd:
                    os.close(current)
                continue
            name = names.pop()
            try:
                info = os.stat(name, dir_fd=current, follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                total += info.st_size  # a link counts as itself, never its target
                continue
            if len(stack) >= DEPTH_MAX:
                # Bounded descriptors, and never a silent undercount past the bound.
                raise TreeTooDeep(f"a directory is nested deeper than {DEPTH_MAX} levels")
            try:
                child = os.open(name, _OPEN_DIR_FLAGS, dir_fd=current)
            except OSError:
                continue
            stack.append((child, _names(child)))
    except BaseException:
        for pending, _names_left in stack:
            if pending != fd:
                os.close(pending)
        raise
    return total


def _names(fd: int) -> list[str]:
    try:
        return os.listdir(fd)
    except OSError:
        return []


def tree_size(root: Path, path: Path) -> int:
    """Bytes under the directory at `path`, every step through a descriptor opened
    without following: a link the shell plants is one entry, never a tree elsewhere.
    Zero when there is no plain directory there."""
    try:
        fd = _open_dir(root, path)
    except (UnsafeEntry, OSError):
        return 0
    try:
        return _dir_size(fd)
    finally:
        os.close(fd)
