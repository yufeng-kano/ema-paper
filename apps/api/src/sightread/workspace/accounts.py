"""A deleted account's files in the workspaces (docs/auth.md § Deleting the account,
docs/workspace.md § Layout).

The account's rows go in one commit and its files after it. Before the rows go, a marker
beside the git directory records the account's job ids, so the worker's sweep can finish
what a process that died after the commit left; the marker is the last thing removed.
Every removal goes through `safe_io`, which follows no link.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from . import safe_io
from .layout import (
    DELETED_SUFFIX,
    REPOS_DIR,
    deletion_marker,
    git_dir,
    images_dir,
    sources_dir,
    user_root,
)
from .locks import SHELL_SUFFIX, WORKSPACE_SUFFIX, WRITING_SUFFIX

logger = logging.getLogger(__name__)


def write_marker(workspaces_dir: str | Path, user_id: int, job_ids: list[uuid.UUID]) -> None:
    """The marker, holding the account's job ids as JSON: written whole, flushed to disk
    and renamed into place, the directory flushed after it — the rows' commit is durable,
    so the marker must be before it, or a power cut could leave the sweep an empty one."""
    marker = deletion_marker(workspaces_dir, user_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    staging = marker.with_name(f"{marker.name}.tmp")
    with open(staging, "w") as handle:
        handle.write(json.dumps([str(job_id) for job_id in job_ids]))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(staging, marker)
    directory = os.open(marker.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def marked_users(workspaces_dir: str | Path) -> list[int]:
    """The users whose marker stands."""
    try:
        names = os.listdir(Path(workspaces_dir) / REPOS_DIR)
    except FileNotFoundError:
        return []
    stems = (name.removesuffix(DELETED_SUFFIX) for name in names if name.endswith(DELETED_SUFFIX))
    return sorted(int(stem) for stem in stems if stem.isdigit())


def read_marker(workspaces_dir: str | Path, user_id: int) -> list[uuid.UUID]:
    """The job ids the marker holds. One that cannot be read — a disk that lost it, a hand
    that edited it — is logged and read as none: the account's directories do not depend
    on them, and only its crops wait for a hand then (FileNotFoundError still raises)."""
    text = deletion_marker(workspaces_dir, user_id).read_text()
    try:
        return [uuid.UUID(value) for value in json.loads(text)]
    except (ValueError, TypeError, AttributeError):
        logger.warning("the deletion marker of user %d cannot be read; no job ids", user_id)
        return []


def discard_marker(workspaces_dir: str | Path, user_id: int) -> None:
    deletion_marker(workspaces_dir, user_id).unlink(missing_ok=True)


def discard_workspace(workspaces_dir: str | Path, user_id: int) -> bool:
    """The account's user directory, git directory, originals, images and lock files.
    A path that cannot be removed is logged and the rest still go; False when any could
    not, so the marker stays for the next sweep."""
    base = Path(workspaces_dir)
    root = user_root(base, user_id)
    repos = git_dir(root).parent
    locks = (WORKSPACE_SUFFIX, SHELL_SUFFIX, WRITING_SUFFIX, f"{WRITING_SUFFIX}.tmp")
    paths = [
        root,
        git_dir(root),
        sources_dir(root),
        images_dir(root),
        *(repos / f"{user_id}{suffix}" for suffix in locks),
        # A marker a process killed mid-write left staged.
        repos / f"{user_id}{DELETED_SUFFIX}.tmp",
    ]
    removed = True
    for path in paths:
        try:
            safe_io.remove_tree(base, path)
        except (OSError, safe_io.UnsafeEntry):
            logger.exception("could not remove %s of deleted account %d", path.name, user_id)
            removed = False
    return removed
