"""Git, as the workspace's revision store (docs/workspace.md § Revisions).

Every function is synchronous and runs `git` as a child process with the repository named
explicitly — never the caller's cwd, never the user's global configuration — so the
worker and the API behave identically wherever they run. Async callers wrap these in
`asyncio.to_thread` under the user's lock (`locks.py`).
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..config import get_settings
from . import safe_io
from .layout import EXCLUDE_PATHSPECS, EXCLUDE_RULES, git_dir

GIT = "git"


def _timeout() -> float:
    """How long any one git call may take (`WORKSPACE_GIT_TIMEOUT_SECONDS`)."""
    return float(get_settings().workspace_git_timeout_seconds)


# The most of a log the API reads to list revisions, and the paths it keeps per revision.
LOG_MAX_BYTES = 4 * 1024 * 1024
LOG_FILES_MAX = 200

# The identities commits carry. An agent commit names its conversation in the author.
USER_AUTHOR = "user"
AGENT_AUTHOR_PREFIX = "agent:"


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


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # The git directory is named on every call: a `.git` the shell plants in the work tree
    # is never consulted, and no hook or config of the shell's runs here.
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            GIT,
            "-C",
            str(root),
            "--git-dir",
            str(git_dir(root)),
            "--work-tree",
            str(root),
            *args,
        ],
        env=_env(),
        capture_output=True,
        text=True,
        timeout=_timeout(),
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc


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


def _stage(root: Path, pathspec: str) -> subprocess.CompletedProcess[str]:
    """Stage what `pathspec` matches, ignore rules notwithstanding: `--force`, since a
    `.gitignore` a shell command writes into the tree must hide nothing from history.
    The workspace's own exclusions are then taken back out of the index, to what the
    head holds of them — nothing, they were never committed — so they alone stay out.
    Two calls: git stages nothing for a `:(literal)` pathspec given beside exclusion
    pathspecs (docs/workspace.md § Revisions)."""
    proc = _git(root, "add", "-A", "--force", "--", pathspec, check=False)
    if proc.returncode == 0:
        _git(root, "reset", "-q", "--", *EXCLUDE_PATHSPECS)
    return proc


def commit_all(root: Path, author: str, message: str) -> str | None:
    """Stage everything and commit; None when the tree was clean. For the shell, whose
    changes nobody can name in advance — under the shell lock, so the dirt is all its.
    A failure anywhere leaves the index reset, never half-staged."""
    try:
        staged = _stage(root, ".")
        if staged.returncode != 0:
            raise GitError(staged.stderr.strip() or "git add failed")
        return _commit_staged(root, author, message)
    except GitError:
        reset_index(root)
        raise


def commit_paths(root: Path, author: str, message: str, paths: list[Path]) -> str | None:
    """Stage only `paths` (relative; files or directories) and commit; None when none of
    them changed. What a host write commits, so a shell's half-written files elsewhere in
    the tree never ride along (docs/workspace.md § Revisions)."""
    if not paths:
        return None
    # One `add` per path: a pathspec that matches nothing (a moved-away directory that
    # was never tracked) is skipped; any other failure to stage is an error, never a
    # write reported done without its revision (docs/workspace.md § Revisions). On any
    # failure the index is reset, so what was staged does not ride into the next commit,
    # which commits the whole index.
    try:
        for path in paths:
            proc = _stage(root, _literal(path))
            if proc.returncode != 0 and "did not match any files" not in proc.stderr:
                raise GitError(f"git add failed for {path}: {proc.stderr.strip()}")
        return _commit_staged(root, author, message)
    except GitError:
        reset_index(root)
        raise


def _literal(path: Path) -> str:
    """A workspace path as a pathspec that names exactly it: a library name may hold
    `*`, `?` or brackets, which git would otherwise read as a pattern and match a
    sibling with (docs/workspace.md § Revisions)."""
    return f":(literal){path.as_posix()}"


def reset_index(root: Path) -> None:
    """Unstage everything, the tree untouched: what a failed commit left staged must not
    ride into the next path-scoped commit, which commits the whole index."""
    _git(root, "reset", "-q", check=False)


def restore_paths(root: Path, paths: list[Path]) -> None:
    """`paths` (relative, tracked at the head) back as the head revision has them, the
    index with them; everything else in the tree is left as it is. The rollback of a
    revert that could not be recorded, which must not discard the edits a failed shell
    commit left unrecorded elsewhere (docs/workspace.md § Revisions)."""
    _git(root, "checkout", "-q", "HEAD", "--", *(_literal(path) for path in paths))


def tracked_at_head(root: Path, relative: Path) -> bool:
    proc = _git(root, "cat-file", "-e", f"HEAD:{relative.as_posix()}", check=False)
    return proc.returncode == 0


def restore_from(root: Path, commit: str, author: str, message: str, relative: Path) -> str | None:
    """Write `relative` back as it was at `commit` and commit that: how a removal is undone
    when the rows it was made for did not go (docs/workspace.md § Revisions)."""
    _git(root, "checkout", "-q", commit, "--", _literal(relative))
    return commit_paths(root, author, message, [relative])


def head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return proc.stdout.strip() or None


def move(root: Path, source: Path, target: Path) -> None:
    """Rename through directory descriptors opened without following (`safe_io.rename`);
    the caller's commit stages the old path and the new. Never `git mv`: it follows a
    link in an ancestor of either path (docs/workspace.md § Revisions)."""
    safe_io.rename(root, source, target)


def log(
    root: Path, relative: Path | None = None, limit: int = 100, *, follow: bool = False
) -> list[Revision]:
    """Newest first. `relative` narrows to commits touching that path; `follow` (one file
    only) keeps going through the renames git detects."""
    if head(root) is None:
        return []
    args = [
        "log",
        f"--max-count={limit}",
        "--name-only",
        "--format=%x1e%H%x1f%an%x1f%aI%x1f%s",
    ]
    if follow:
        args.append("--follow")
    if relative is not None:
        args += ["--", _literal(relative)]
    # Read up to a bound, never buffered whole: a shell revision can name any number of
    # files, and the API must not hold them all to list a few commits. A cut-off record
    # is dropped; the ones before it are complete.
    code, raw, truncated = _git_bounded(root, *args, max_bytes=LOG_MAX_BYTES)
    if code != 0 and not truncated:
        raise GitError("git log failed")
    records = raw.decode(errors="replace").split("\x1e")
    cut = records[-1] if truncated else None
    if truncated:
        records = records[:-1]
    revisions: list[Revision] = []
    for record in records + ([cut] if cut and "\n" in cut else []):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        commit, author, when, subject = (header.split("\x1f") + ["", "", "", ""])[:4]
        if len(commit) != 40:
            continue  # the cut fell inside the header: nothing reliable to list
        names = [line for line in body.splitlines() if line.strip()]
        if record is cut:
            # The bound fell inside this revision's list: what fit, and a marker, rather
            # than no revision at all (its last line may be half a name).
            names = names[:-1] + ["... (list cut)"]
        if len(names) > LOG_FILES_MAX:
            names = names[:LOG_FILES_MAX] + [f"... and {len(names) - LOG_FILES_MAX} more"]
        files = tuple(names)
        revisions.append(
            Revision(
                commit=commit,
                author=author,
                message=subject,
                created_at=datetime.fromisoformat(when).astimezone(UTC),
                files=files,
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
        [GIT, "-C", str(root), "--git-dir", str(git_dir(root)), "--work-tree", str(root), *args],
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


def show(root: Path, commit: str, relative: Path, max_bytes: int) -> str | None:
    data = show_bytes(root, commit, relative, max_bytes)
    return None if data is None else data.decode(errors="replace")


def renames_since(root: Path, commit: str) -> dict[Path, Path]:
    """Where each path renamed after `commit` lives now: old path → current path, with
    chains of renames followed in order. Read under `LOG_MAX_BYTES` like the log: a
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
        max_bytes=LOG_MAX_BYTES,
    )
    if truncated:
        raise GitError(f"the renames since {commit[:12]} exceed {LOG_MAX_BYTES} bytes")
    if code != 0:
        return {}
    fields = [field for field in raw.decode(errors="replace").split("\0") if field]
    forward: dict[Path, Path] = {}
    index = 0
    while index < len(fields):
        status = fields[index]
        if status.startswith("R") and index + 2 < len(fields):
            old, new = Path(fields[index + 1]), Path(fields[index + 2])
            # A path that had already moved once moves on from where it was.
            origins = [origin for origin, current in forward.items() if current == old]
            for origin in origins:
                forward[origin] = new
            if old not in forward and not origins:
                forward[old] = new
            index += 3
        else:
            index += 2
    return forward


def changed_paths(root: Path, commit: str) -> list[tuple[str, Path, Path | None]]:
    """What the commit did to each path: `A`dded, `M`odified, `D`eleted, `T`ype-changed
    or `R`enamed — for a rename, the old path and the new one. Read under
    `LOG_MAX_BYTES`: a commit whose path list is larger raises."""
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
        max_bytes=LOG_MAX_BYTES,
    )
    if truncated:
        raise GitError(f"the paths of {commit[:12]} exceed {LOG_MAX_BYTES} bytes")
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
