"""Git, as the workspace's revision store (docs/workspace.md § Revisions).

Every function is synchronous and runs `git` as a child process with the repository named
explicitly — never the caller's cwd, never the user's global configuration — so the
worker and the API behave identically wherever they run. Async callers wrap these in
`asyncio.to_thread` under the user's lock (`locks.py`).
"""

from __future__ import annotations

import contextlib
import os
import select
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import get_settings
from . import safe_io
from .layout import DOC_FILE, EXCLUDE_RULES, META_FILE, NOTES_FILE, git_dir

GIT = "git"

# The workspace's exclusions as `ls-files` patterns: the lines of `info/exclude`.
_EXCLUDES = tuple(
    f"--exclude={line}" for line in EXCLUDE_RULES.splitlines() if line and not line.startswith("#")
)


def _timeout() -> float:
    """How long any one git call may take (`WORKSPACE_GIT_TIMEOUT_SECONDS`)."""
    return float(get_settings().workspace_git_timeout_seconds)


def _log_max_bytes() -> int:
    """The most of a log, a rename list or a path list read into memory
    (`WORKSPACE_LOG_MAX_BYTES`)."""
    return int(get_settings().workspace_log_max_bytes)


# The identities commits carry. An agent commit names its conversation in the author.
USER_AUTHOR = "user"
AGENT_AUTHOR_PREFIX = "agent:"
# A save through the data plane — a script's, or Claude's through the connector (docs/api.md
# § The library on the data plane).
API_AUTHOR = "api"

# What a revision records of a file that is not a document's own: text, and no more than
# `REVISION_MAX_FILE_BYTES` of it (docs/workspace.md § Revisions). A document's own files
# are recorded whatever they hold.
_RECORDED_NAMES = frozenset({DOC_FILE, NOTES_FILE, META_FILE})
_TEXT_PROBE_BYTES = 8192


def _is_text(data: bytes) -> bool:
    """Git's own test of a text file, on bytes in hand: no NUL byte in the first 8 KiB."""
    return b"\0" not in data[:_TEXT_PROBE_BYTES]


def revision_max_file_bytes() -> int:
    """The largest file a revision records (`REVISION_MAX_FILE_BYTES`)."""
    return int(get_settings().revision_max_file_bytes)


def records(name: str, data: bytes) -> bool:
    """Whether history would take a file called `name` holding `data`: `recordable`'s
    rule on bytes in hand rather than a file on disk."""
    return name in _RECORDED_NAMES or (len(data) <= revision_max_file_bytes() and _is_text(data))


def recordable(root: Path, relative: Path, max_bytes: int) -> bool:
    """Whether history takes the file at `relative`: a document's own file always; any
    other only when it is text — no NUL byte in its first 8 KiB — and no larger than
    `REVISION_MAX_FILE_BYTES`, so a video or an archive kept in the library never lands in
    the object store, and a change to one has no revision (docs/workspace.md § Revisions,
    § Files). A path with nothing plain at it — a deletion to record, a link, a gitlink —
    is git's to judge."""
    if relative.name in _RECORDED_NAMES:
        return True
    try:
        entry = safe_io.open_for_read(root, root / relative, max_bytes)
    except safe_io.EntryTooLarge:
        return False
    except (safe_io.UnsafeEntry, OSError):
        return True
    if entry is None:
        return True
    try:
        return _is_text(os.read(entry, _TEXT_PROBE_BYTES))
    finally:
        os.close(entry)


class GitError(Exception):
    """A git command failed. The message is git's stderr: paths, never file content."""


@dataclass(frozen=True)
class Revision:
    commit: str
    author: str
    message: str
    created_at: datetime
    files: tuple[str, ...]


def _env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    }


def _argv(root: Path, args: tuple[str, ...]) -> list[str]:
    # The git directory is named on every call: a `.git` the shell plants in the work tree
    # is never consulted, and no hook or config of the shell's runs here. Git's own
    # automatic gc is off: it would run detached after a commit, outside the workspace
    # and backup locks the worker packs under (`pack`, docs/workspace.md § Revisions).
    return [
        GIT,
        "-C",
        str(root),
        "--git-dir",
        str(git_dir(root)),
        "--work-tree",
        str(root),
        "-c",
        "gc.auto=0",
        "-c",
        "maintenance.auto=false",
        *args,
    ]


def _run(
    root: Path, args: tuple[str, ...], *, text: bool = True, stdin: bytes | None = None
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            _argv(root, args),
            env=_env(),
            input=stdin,
            capture_output=True,
            text=text,
            timeout=_timeout(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # `run` has killed the child. The error is the module's own, so every caller's
        # rollback runs (a written file put back, the index reset); and the lock the
        # killed git may have left would refuse every command after it, so it goes.
        with contextlib.suppress(OSError):
            (git_dir(root) / "index.lock").unlink()
        raise GitError(f"git {args[0]} timed out") from exc


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = _run(root, args)
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc


def _git_raw(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """stdout as bytes, for paths: a shell can name a file anything, UTF-8 or not."""
    proc = _run(root, args, text=False, stdin=stdin)
    if proc.returncode != 0:
        raise GitError(proc.stderr.decode(errors="replace").strip() or f"git {args[0]} failed")
    return proc.stdout


def ensure_repo(root: Path) -> None:
    """Make `root` a repository with the workspace's ignore rules. Idempotent."""
    root.mkdir(parents=True, exist_ok=True)
    repo = git_dir(root)
    if not (repo / "HEAD").is_file():
        repo.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.name", "sightread")
        _git(root, "config", "user.email", "sightread@localhost")
        _git(root, "config", "core.quotePath", "false")
    exclude = repo / "info" / "exclude"
    if not exclude.exists() or exclude.read_text() != EXCLUDE_RULES:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(EXCLUDE_RULES)


def _commit_staged(root: Path, author: str, message: str) -> str | None:
    staged = _git(root, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return None
    _git(
        root,
        "-c",
        f"user.name={author}",
        "-c",
        "user.email=sightread@localhost",
        "commit",
        "-q",
        "-m",
        message,
    )
    return head(root)


# The most argv bytes one `ls-files` call carries of pathspecs, well under any ARG_MAX: a
# path the shell made can be as long as PATH_MAX, so a count of them bounds nothing.
_PATHSPEC_CHUNK_BYTES = 128 * 1024


def _chunks(pathspecs: list[str]) -> list[list[str]]:
    """`pathspecs` in runs whose argv bytes stay within `_PATHSPEC_CHUNK_BYTES`."""
    chunks: list[list[str]] = []
    chunk: list[str] = []
    size = 0
    for pathspec in pathspecs:
        length = len(os.fsencode(pathspec)) + 1
        if chunk and size + length > _PATHSPEC_CHUNK_BYTES:
            chunks.append(chunk)
            chunk, size = [], 0
        chunk.append(pathspec)
        size += length
    if chunk:
        chunks.append(chunk)
    return chunks


def _listed(root: Path, chunks: list[list[str]], *flags: str) -> list[bytes]:
    """What `ls-files -z <flags>` lists for every chunk of pathspecs, in order."""
    return [
        name
        for chunk in chunks
        for name in _git_raw(root, "ls-files", "-z", *flags, "--", *chunk).split(b"\0")
        if name
    ]


def _stage(root: Path, pathspecs: list[str]) -> None:
    """Stage what `pathspecs` match by the workspace's own exclusions and no other ignore
    rule (docs/workspace.md § Revisions), in one pass over them all: a pathspec that
    matches nothing adds nothing, and any git failure raises. `ls-files` lists the
    changes and reads no `.gitignore` unless asked, so one a shell command writes into the
    tree hides nothing from history; and an excluded file is never hashed, so no copy of
    an original or a crop lands in the object store. Tracked files first, their edits and
    deletions through `add -u`, which takes up no untracked file, not even one under a
    file that became a directory; then the untracked files outside the exclusions through
    `update-index`, which skips a name the shell removed meanwhile rather than failing the
    commit, and with `--replace` lets a file take a directory's place and back. Both read
    the names on stdin, which no argv limit bounds."""
    chunks = _chunks(pathspecs)
    # A deleted file is listed as modified too. A file history does not record — binary,
    # or past `REVISION_MAX_FILE_BYTES` — is left out of both lists (`recordable`).
    cap = revision_max_file_bytes()
    names = dict.fromkeys(
        name
        for name in _listed(root, chunks, "--modified", "--deleted")
        if recordable(root, Path(os.fsdecode(name)), cap)
    )
    if names:
        _git_raw(
            root,
            "add",
            "-u",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
            stdin=b"".join(b":(literal)" + name + b"\0" for name in names),
        )
    # A repository a shell command made inside the tree is listed as `dir/`: a gitlink.
    names = dict.fromkeys(
        name.rstrip(b"/")
        for name in _listed(root, chunks, "--others", *_EXCLUDES)
        if recordable(root, Path(os.fsdecode(name.rstrip(b"/"))), cap)
    )
    if names:
        _git_raw(
            root,
            "update-index",
            "--add",
            "--remove",
            "--replace",
            "-z",
            "--stdin",
            stdin=b"".join(name + b"\0" for name in names),
        )


def _commit(root: Path, author: str, message: str, pathspecs: list[str]) -> str | None:
    """Stage what `pathspecs` match and commit; None when nothing changed. On any failure
    the index is reset, so what was staged does not ride into the next commit, which
    commits the whole index — never a write reported done without its revision
    (docs/workspace.md § Revisions)."""
    try:
        _stage(root, pathspecs)
        return _commit_staged(root, author, message)
    except GitError:
        reset_index(root)
        raise


def commit_all(root: Path, author: str, message: str) -> str | None:
    """Stage everything and commit; None when the tree was clean. For the shell, whose
    changes nobody can name in advance — under the shell lock, so the dirt is all its.
    A failure anywhere leaves the index reset, never half-staged."""
    return _commit(root, author, message, ["."])


def commit_paths(root: Path, author: str, message: str, paths: list[Path]) -> str | None:
    """Stage only `paths` (relative; files or directories) and commit; None when none of
    them changed. What a host write commits, so a shell's half-written files elsewhere in
    the tree never ride along (docs/workspace.md § Revisions). A path that matches nothing
    — a moved-away directory that was never tracked — stages nothing."""
    if not paths:
        return None
    return _commit(root, author, message, [_literal(path) for path in paths])


def _literal(path: Path) -> str:
    """A workspace path as a pathspec that names exactly it: a library name may hold
    `*`, `?` or brackets, which git would otherwise read as a pattern and match a
    sibling with (docs/workspace.md § Revisions)."""
    return f":(literal){path.as_posix()}"


def reset_index(root: Path) -> None:
    """Unstage everything, the tree untouched: what a failed commit left staged must not
    ride into the next path-scoped commit, which commits the whole index."""
    _git(root, "reset", "-q", check=False)


def loose_objects(root: Path) -> int:
    """How many objects the repository holds unpacked; 0 where there is none yet."""
    if not (git_dir(root) / "HEAD").is_file():
        return 0
    for line in _git(root, "count-objects", "-v").stdout.splitlines():
        key, _, value = line.partition(": ")
        if key == "count":
            return int(value)
    return 0


def pack(root: Path) -> None:
    """Pack the history, a revision stored as a delta against a similar one rather than as
    a whole compressed copy, and drop the objects no revision names (docs/workspace.md
    § Revisions). Pruning at once is safe only under the workspace lock, which every
    writer of the object store holds: nothing staged and not yet committed is in the way."""
    _git(root, "gc", "--quiet", "--prune=now")


def restore_paths(root: Path, paths: list[Path]) -> None:
    """`paths` (relative, tracked at the head) back as the head revision has them, the
    index with them; everything else in the tree is left as it is. The rollback of a
    revert that could not be recorded, which must not discard the edits a failed shell
    commit left unrecorded elsewhere (docs/workspace.md § Revisions)."""
    _git(root, "checkout", "-q", "HEAD", "--", *(_literal(path) for path in paths))


def path_at_head_containing(root: Path, needle: str, pathspec: str) -> Path | None:
    """The first path under `pathspec` whose content at `HEAD` holds `needle` as a fixed
    string, or None: one `git grep` over the committed tree, so a row whose directory is
    missing can tell a document never materialized from one the tree moved
    (docs/workspace.md § Revisions)."""
    proc = _git(
        root,
        "grep",
        "-l",
        "--fixed-strings",
        "-e",
        needle,
        "HEAD",
        "--",
        pathspec,
        check=False,
    )
    if proc.returncode != 0:  # 1: no match; 128: no HEAD yet
        return None
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    _tree, _colon, path = first.partition(":")
    return Path(path) if path else None


def missing_at_head(root: Path, relatives: list[Path]) -> list[Path]:
    """Those of `relatives` that `HEAD` does not hold — all of them when there is no
    `HEAD` yet. One `ls-tree` for the lot: what a materialization asks of every
    document it is asked for (docs/workspace.md § Layout). Each path is a literal
    pathspec, so a name the shell began with `:` is a name, not pathspec magic."""
    proc = _git(
        root,
        "ls-tree",
        "--name-only",
        "-z",
        "HEAD",
        "--",
        *(_literal(relative) for relative in relatives),
        check=False,
    )
    if proc.returncode != 0:
        return list(relatives)
    held = set(proc.stdout.split("\0"))
    return [relative for relative in relatives if relative.as_posix() not in held]


def head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return proc.stdout.strip() or None


def log(root: Path, relative: Path | None = None, limit: int = 100) -> list[Revision]:
    """The commits newest first, each with the paths it touched; `relative` narrows them to
    the commits touching that path. Nothing the service serves lists history: the tests
    read a workspace's commits through this. Read up to `WORKSPACE_LOG_MAX_BYTES`, never
    buffered whole; a record the bound cuts is dropped, the ones before it are complete."""
    if head(root) is None:
        return []
    args = [
        "log",
        f"--max-count={limit}",
        "--name-only",
        "--format=%x1e%H%x1f%an%x1f%aI%x1f%s",
    ]
    if relative is not None:
        args += ["--", _literal(relative)]
    code, raw, truncated = _git_bounded(root, *args, max_bytes=_log_max_bytes())
    if code != 0 and not truncated:
        raise GitError("git log failed")
    records = raw.decode(errors="replace").split("\x1e")
    if truncated:
        records = records[:-1]
    revisions: list[Revision] = []
    for record in records:
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        commit, author, when, subject = header.split("\x1f", 3)
        revisions.append(
            Revision(
                commit=commit,
                author=author,
                message=subject,
                created_at=datetime.fromisoformat(when).astimezone(UTC),
                files=tuple(line for line in body.splitlines() if line.strip()),
            )
        )
    return revisions


def _git_bounded(root: Path, *args: str, max_bytes: int) -> tuple[int, bytes, bool]:
    """Like `_git`, reading at most `max_bytes` of stdout: a shell can commit a file of
    any size, and the host must not hold a whole diff of it to show a few lines. The
    timeout is watched while reading, so a git that stalls after a few bytes fails rather
    than holding a worker thread and the workspace lock (docs/workspace.md)."""
    deadline = time.monotonic() + _timeout()
    with subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        _argv(root, args),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as proc:
        assert proc.stdout is not None
        chunks: list[bytes] = []
        total = 0
        truncated = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([proc.stdout], [], [], remaining)[0]:
                proc.kill()
                proc.wait()
                raise GitError(f"git {args[0]} timed out")
            chunk = os.read(proc.stdout.fileno(), 65_536)
            if not chunk:
                break
            room = max_bytes - total
            if len(chunk) > room:
                chunks.append(chunk[:room])
                truncated = True
                proc.kill()
                break
            chunks.append(chunk)
            total += len(chunk)
        try:
            code = proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    return code, b"".join(chunks), truncated


def show_bytes(root: Path, commit: str, relative: Path, max_bytes: int) -> bytes | None:
    """The file's content at `commit`, or None when it did not exist there. Raises when
    it is larger than `max_bytes`: a revision that size is not restored into memory."""
    code, out, truncated = _git_bounded(
        root, "show", f"{commit}:{relative.as_posix()}", max_bytes=max_bytes
    )
    if truncated:
        raise GitError(f"{relative} at {commit[:12]} exceeds {max_bytes} bytes")
    return out if code == 0 else None


def renames_since(root: Path, commit: str) -> dict[Path, Path]:
    """Where each path renamed after `commit` lives now: old path → current path, with
    chains of renames followed in order. Read under `WORKSPACE_LOG_MAX_BYTES` like the log: a
    history of renames past it raises rather than being held whole."""
    code, raw, truncated = _git_bounded(
        root,
        "log",
        "--reverse",
        "--format=",
        "--name-status",
        "--find-renames",
        "--diff-filter=R",
        "-z",
        f"{commit}..HEAD",
        max_bytes=_log_max_bytes(),
    )
    if truncated:
        raise GitError(f"the renames since {commit[:12]} exceed {_log_max_bytes()} bytes")
    if code != 0:
        return {}
    fields = [field for field in raw.decode(errors="replace").split("\0") if field]
    forward: dict[Path, Path] = {}
    # Each current path's origins, so a rename finds the paths it carries on in one step
    # rather than a scan of every rename read so far.
    back: dict[Path, list[Path]] = {}
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith("R") and index + 2 < len(fields):
            old, new = Path(fields[index + 1]), Path(fields[index + 2])
            # A path that had already moved once moves on from where it was.
            origins = back.pop(old, [])
            if origins:
                for origin in origins:
                    forward[origin] = new
                back.setdefault(new, []).extend(origins)
            elif old not in forward:
                forward[old] = new
                back.setdefault(new, []).append(old)
            index += 3
        else:
            index += 2
    return forward


def changed_paths(root: Path, commit: str) -> list[tuple[str, Path, Path | None]]:
    """What the commit did to each path: `A`dded, `M`odified, `D`eleted, `T`ype-changed
    or `R`enamed — for a rename, the old path and the new one. Read under
    `WORKSPACE_LOG_MAX_BYTES`: a commit whose path list is larger raises."""
    code, raw, truncated = _git_bounded(
        root,
        "diff-tree",
        "--no-commit-id",
        "--root",
        "-r",
        "--name-status",
        "--find-renames",
        "-z",
        commit,
        max_bytes=_log_max_bytes(),
    )
    if truncated:
        raise GitError(f"the paths of {commit[:12]} exceed {_log_max_bytes()} bytes")
    if code != 0:
        return []
    fields = raw.decode(errors="replace").split("\0")
    changes: list[tuple[str, Path, Path | None]] = []
    index = 0
    while index + 1 < len(fields):
        status = fields[index]
        if not status:
            index += 1
            continue
        if status.startswith("R") and index + 2 < len(fields):
            changes.append(("R", Path(fields[index + 1]), Path(fields[index + 2])))
            index += 3
        else:
            changes.append((status[0], Path(fields[index + 1]), None))
            index += 2
    return changes


def show_commit(root: Path, commit: str, max_bytes: int) -> str:
    """The commit's unified diff for text files, cut at `max_bytes`; binaries appear as
    a summary line."""
    code, out, truncated = _git_bounded(
        root, "show", "--format=", "--unified=3", commit, max_bytes=max_bytes
    )
    if truncated:
        return out.decode(errors="replace") + "\n[... diff cut by the workspace ...]\n"
    return out.decode(errors="replace") if code == 0 else ""


def parent_of(root: Path, commit: str) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "-q", f"{commit}^", check=False)
    return proc.stdout.strip() or None


def is_commit(root: Path, commit: str) -> bool:
    if not commit or not all(c in "0123456789abcdef" for c in commit.lower()):
        return False
    return _git(root, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0
