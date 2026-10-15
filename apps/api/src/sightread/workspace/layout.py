"""Where things live inside a user's workspace (docs/workspace.md § Layout).

Pure path arithmetic: nothing here reads or writes a file. The names the library keeps
are labels in a tree; this module is what turns a label into a path segment that cannot
escape the tree or collide with the names the workspace reserves for itself.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

FILES_DIR = "files"
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
_LEADING_DOTS = re.compile(r"^\.+")

GITIGNORE = "\n".join(
    (
        "# Kept out of history: binaries and the sandbox's package cache (docs/workspace.md)",
        f"{SIGHTREAD_DIR}/",
        f"{CACHE_DIR}/",
        f"**/{SOURCE_STEM}.*",
        f"**/{FIGURES_DIR}/",
        "",
    )
)


def user_root(workspaces_dir: str | Path, user_id: int) -> Path:
    """`{WORKSPACES_DIR}/{user_id}` — what the user's VM mounts as /workspace."""
    return Path(workspaces_dir) / str(user_id)


def path_segment(name: str) -> str:
    """A library name as one path component.

    NFC-normalized so the same name typed two ways is one directory; separators and
    control characters replaced; leading dots removed so a name cannot hide the entry or
    collide with the reserved dotfiles; a reserved or empty result is prefixed.
    """
    segment = unicodedata.normalize("NFC", name)
    segment = _UNSAFE.sub(" ", segment)
    segment = _LEADING_DOTS.sub("", segment).strip()
    if not segment or segment in RESERVED_SEGMENTS or segment in (".", ".."):
        segment = f"_{segment}" if segment else "_"
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
