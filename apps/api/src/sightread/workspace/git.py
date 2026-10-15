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

from .layout import GITIGNORE

GIT = "git"
GIT_TIMEOUT_SECONDS = 60.0

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
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [GIT, "-C", str(root), *args],
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
    if not (root / ".git").is_dir():
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.name", "sightread")
        _git(root, "config", "user.email", "sightread@localhost")
        _git(root, "config", "core.quotePath", "false")
    ignore = root / ".gitignore"
    if not ignore.exists() or ignore.read_text() != GITIGNORE:
        ignore.write_text(GITIGNORE)


def commit_all(root: Path, author: str, message: str) -> str | None:
    """Stage everything and commit; None when the tree was clean."""
    _git(root, "add", "-A")
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


def head(root: Path) -> str | None:
    proc = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    return proc.stdout.strip() or None


def move(root: Path, source: Path, target: Path) -> None:
    """`git mv`, falling back to a plain rename for a path git does not track yet."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tracked = _git(root, "ls-files", "--error-unmatch", str(source), check=False)
    if tracked.returncode == 0:
        _git(root, "mv", "-k", str(source), str(target))
    else:
        source.rename(target)


def log(root: Path, relative: Path | None = None, limit: int = 100) -> list[Revision]:
    """Newest first. `relative` narrows to commits touching that path."""
    if head(root) is None:
        return []
    args = [
        "log",
        f"--max-count={limit}",
        "--name-only",
        "--format=%x1e%H%x1f%an%x1f%aI%x1f%s",
    ]
    if relative is not None:
        args += ["--", str(relative)]
    out = _git(root, *args).stdout
    revisions: list[Revision] = []
    for record in out.split("\x1e"):
        if not record.strip():
            continue
        header, _, body = record.partition("\n")
        commit, author, when, subject = (header.split("\x1f") + ["", "", "", ""])[:4]
        files = tuple(line for line in body.splitlines() if line.strip())
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


def show(root: Path, commit: str, relative: Path) -> str | None:
    """The file's text at `commit`, or None when it did not exist there."""
    proc = _git(root, "show", f"{commit}:{relative.as_posix()}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def show_commit(root: Path, commit: str) -> str:
    """The commit's unified diff for text files; binaries appear as a summary line."""
    proc = _git(root, "show", "--format=", "--unified=3", commit, check=False)
    return proc.stdout if proc.returncode == 0 else ""


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
