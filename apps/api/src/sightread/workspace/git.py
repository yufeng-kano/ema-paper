"""Git, as the workspace's revision store (docs/workspace.md § Revisions).

Every function is synchronous and runs `git` as a child process with the repository named
explicitly — never the caller's cwd, never the user's global configuration — so the
worker and the API behave identically wherever they run. Async callers wrap these in
`asyncio.to_thread` under the user's lock (`locks.py`).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import safe_io
from .layout import EXCLUDE_RULES, git_dir

GIT = "git"
GIT_TIMEOUT_SECONDS = 60.0
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
        timeout=GIT_TIMEOUT_SECONDS,
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


def commit_all(root: Path, author: str, message: str) -> str | None:
    """Stage everything and commit; None when the tree was clean. For the shell, whose
    changes nobody can name in advance — under the shell lock, so the dirt is all its."""
    _git(root, "add", "-A")
    return _commit_staged(root, author, message)


def commit_paths(root: Path, author: str, message: str, paths: list[Path]) -> str | None:
    """Stage only `paths` (relative; files or directories) and commit; None when none of
    them changed. What a host write commits, so a shell's half-written files elsewhere in
    the tree never ride along (docs/workspace.md § Revisions)."""
    if not paths:
        return None
    # One `add` per path: a pathspec that matches nothing (a moved-away directory that
    # was never tracked) is skipped; any other failure to stage is an error, never a
    # write reported done without its revision (docs/workspace.md § Revisions).
    for path in paths:
        proc = _git(root, "add", "-A", "--", str(path), check=False)
        if proc.returncode != 0 and "did not match any files" not in proc.stderr:
            raise GitError(f"git add failed for {path}: {proc.stderr.strip()}")
    return _commit_staged(root, author, message)


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
        args += ["--", str(relative)]
    # Read up to a bound, never buffered whole: a shell revision can name any number of
    # files, and the API must not hold them all to list a few commits. A cut-off record
    # is dropped; the ones before it are complete.
    code, raw, truncated = _git_bounded(root, *args, max_bytes=LOG_MAX_BYTES)
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
        commit, author, when, subject = (header.split("\x1f") + ["", "", "", ""])[:4]
        names = [line for line in body.splitlines() if line.strip()]
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
    any size, and the host must not hold a whole diff of it to show a few lines."""
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
            chunk = proc.stdout.read(65_536)
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
            code = proc.wait(timeout=GIT_TIMEOUT_SECONDS)
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
    chains of renames followed in order."""
    proc = _git(
        root,
        "log",
        "--reverse",
        "--format=",
        "--name-status",
        "--find-renames",
        "--diff-filter=R",
        "-z",
        f"{commit}..HEAD",
        check=False,
    )
    if proc.returncode != 0:
        return {}
    fields = [field for field in proc.stdout.split("\0") if field]
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
    or `R`enamed — for a rename, the old path and the new one."""
    proc = _git(
        root,
        "diff-tree",
        "--no-commit-id",
        "--root",
        "-r",
        "--name-status",
        "--find-renames",
        "-z",
        commit,
        check=False,
    )
    if proc.returncode != 0:
        return []
    fields = proc.stdout.split("\0")
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


def paths_at(root: Path, commit: str, current: Path) -> tuple[Path | None, Path | None]:
    """Where a file that is now at `current` lived at `commit`, and at that commit's parent.

    `git log --follow` walks renames: each entry names the path the file had in that
    commit. The parent's path is the next older entry's, which is the old name when the
    commit itself was the rename. None when the file did not exist at that point.
    """
    proc = _git(
        root,
        "log",
        "--follow",
        "--name-only",
        "--format=%x1e%H",
        "--",
        str(current),
        check=False,
    )
    if proc.returncode != 0:
        return None, None
    entries: list[tuple[str, Path]] = []
    for record in proc.stdout.split("\x1e"):
        if not record.strip():
            continue
        sha, _, body = record.partition("\n")
        names = [line for line in body.splitlines() if line.strip()]
        if names:
            entries.append((sha.strip(), Path(names[0])))
    for index, (sha, path) in enumerate(entries):
        if sha == commit:
            older = entries[index + 1][1] if index + 1 < len(entries) else path
            return path, older
    # The commit did not touch this file: it sat at the path of the newest entry at or
    # before it. Without that entry the file did not exist yet.
    for sha, path in entries:
        if _git(root, "merge-base", "--is-ancestor", sha, commit, check=False).returncode == 0:
            return path, path
    return None, None
