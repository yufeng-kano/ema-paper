"""Where things live inside a user's workspace (docs/workspace.md § Layout).

Pure path arithmetic: nothing here reads or writes a file. The names the library keeps
are labels in a tree; this module is what turns a label into a path segment that cannot
escape the tree or collide with the names the workspace reserves for itself.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from .safe_io import TEMP_PREFIX

# What one path component may hold on the filesystems the workspace runs on.
SEGMENT_MAX_BYTES = 255
SEGMENT_HASH_CHARS = 12

FILES_DIR = "files"
# Beside the user directories, never inside one: the git directories the VMs cannot see.
REPOS_DIR = ".repos"
SIGHTREAD_DIR = ".sightread"
RESULTS_DIR = "results"
CACHE_DIR = ".cache"
DOC_FILE = "doc.md"
NOTES_FILE = "notes.md"
META_FILE = ".meta.json"
FIGURES_DIR = "figures"
SOURCE_STEM = "source"
LOCK_FILE = "lock"

# What a document directory may not be called: the workspace's own names, and git's.
RESERVED_SEGMENTS = frozenset({SIGHTREAD_DIR, META_FILE, CACHE_DIR, ".git", ".gitignore"})

# Separators and control characters never reach a path; the library already strips the
# separators (routes/library.py), this is the belt to that brace.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\]")

# The repository's `info/exclude`: kept in the git directory, out of the shell's reach.
EXCLUDE_RULES = "\n".join(
    (
        "# Kept out of history: binaries and the sandbox's package cache (docs/workspace.md)",
        f"/{SIGHTREAD_DIR}/",
        f"/{CACHE_DIR}/",
        "/.git/",
        f"**/{SOURCE_STEM}.*",
        f"**/{FIGURES_DIR}/",
        f"**/{TEMP_PREFIX}*",
        "",
    )
)


def user_root(workspaces_dir: str | Path, user_id: int) -> Path:
    """`{WORKSPACES_DIR}/{user_id}` — what the user's VM mounts as /workspace."""
    return Path(workspaces_dir) / str(user_id)


def git_dir(root: Path) -> Path:
    """The user's git directory, `{WORKSPACES_DIR}/.repos/{user_id}`: beside the work tree,
    outside anything the VM mounts (docs/workspace.md § Revisions)."""
    return root.parent / REPOS_DIR / root.name


def segment_name(segment: str) -> str:
    """The name a canonical segment encodes: `path_segment`'s inverse."""
    return segment[1:] if segment.startswith("_") else segment


def is_canonical_segment(segment: str) -> bool:
    """Whether a directory name is what `path_segment` makes of the name it encodes."""
    return path_segment(segment_name(segment)) == segment


def no_symlinks(root: Path, path: Path) -> bool:
    """Whether no component of `path` below `root` is a symlink — the check every fixed
    workspace file passes before the host reads or writes it (docs/workspace.md)."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return False
    return True


def path_segment(name: str) -> str:
    """A library name as one path component.

    NFC-normalized so the same name typed two ways is one directory, with separators and
    control characters replaced. A name that would hide the entry or collide with the
    workspace's own dotfiles — a leading dot, a reserved name, `.`/`..`, nothing — is kept
    whole behind a `_` prefix, and a name that itself starts with `_` gets the prefix too:
    that is what keeps distinct names in one folder distinct directories (`.report` and
    `report` are `_.report` and `report`, never both `report`).
    """
    segment = unicodedata.normalize("NFC", name)
    segment = _UNSAFE.sub(" ", segment).strip()
    if not segment or segment.startswith((".", "_")) or segment in RESERVED_SEGMENTS:
        segment = f"_{segment}"
    if len(segment.encode()) > SEGMENT_MAX_BYTES:
        # A library name may be longer than a path component: keep a prefix the reader
        # recognizes and a hash of the whole name that keeps distinct names distinct.
        digest = hashlib.sha256(name.encode()).hexdigest()[:SEGMENT_HASH_CHARS]
        room = SEGMENT_MAX_BYTES - SEGMENT_HASH_CHARS - 1
        prefix = segment.encode()[:room].decode(errors="ignore").rstrip()
        segment = f"{prefix}~{digest}"
    return segment


def files_root(root: Path) -> Path:
    return root / FILES_DIR


def results_root(root: Path) -> Path:
    return root / SIGHTREAD_DIR / RESULTS_DIR


def document_relative_dir(folder_names: list[str], document_name: str) -> Path:
    """The document's directory relative to the user root: `files/<folders…>/<name>`."""
    path = Path(FILES_DIR)
    for folder in folder_names:
        path /= path_segment(folder)
    return path / path_segment(document_name)


def inside(root: Path, candidate: Path) -> bool:
    """Whether `candidate` (resolved) stays under `root` (resolved) — the one check every
    model-supplied path must pass (docs/chat.md § Tools)."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
