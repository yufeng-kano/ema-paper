"""The console's permissions, fixed in code (docs/admin.md § Permissions).

Each area has a `read` and, except `audit`, a `write`. A new area is two strings here, its
entry in the web app's area list and its words in both catalogs — no migration; the
built-in group holds it at once.
"""

from __future__ import annotations

from collections.abc import Iterable

# The areas in nav order, with their levels.
AREAS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("users", ("read", "write")),
    ("codes", ("read", "write")),
    ("jobs", ("read", "write")),
    ("groups", ("read", "write")),
    # A record is never changed, so there is nothing to write.
    ("audit", ("read",)),
    ("settings", ("read", "write")),
)

ALL_PERMISSIONS: tuple[str, ...] = tuple(
    f"{area}.{level}" for area, levels in AREAS for level in levels
)


def normalize(permissions: Iterable[str]) -> list[str]:
    """A group's list as it is stored: an unknown string refused, a repeated one counted
    once, every `<area>.write` bringing its `<area>.read`, in `ALL_PERMISSIONS` order."""
    wanted = set(permissions)
    unknown = sorted(wanted - set(ALL_PERMISSIONS))
    if unknown:
        raise ValueError(f"Unknown permission: {', '.join(unknown)}")
    for permission in list(wanted):
        area, _, level = permission.partition(".")
        if level == "write":
            wanted.add(f"{area}.read")
    return [permission for permission in ALL_PERMISSIONS if permission in wanted]
