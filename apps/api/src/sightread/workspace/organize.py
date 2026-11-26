"""The Files page's operations by path, in batches (docs/chat.md § Library conversations, *The
library tools*; docs/mcp.md § The organizing tools): a new folder, a move, a rename, a copy,
Move to Trash, Put Back and the replay of an undo record, over `filing.py`'s functions — the
ones the library routes call — with the page's guards.

Three callers run them, so the three answer alike: a library conversation's library tools
(`jobs/chat_library.py`, authored `agent:<conversation id>`), the connector's organizing
tools (`mcp/server.py`) and `POST /v1/library/<tool>` (`routes/v1.py`), both authored `api`.
An item is named by its place as the Files page shows it (`DL/DINO/iBOT.pdf`), or by its
path under `/workspace/files/`. Every answer says what was done, a sentence per item, and a
call that changed something carries its undo record, in the shapes the Files page's undo
keeps its steps in (docs/web.md § Files, *Undo*).
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import FILE_KIND, Document, Folder
from ..errors import ApiError
from . import documents as workspace_documents
from . import filing
from .agents_file import VM_ROOT
from .documents import folder_path, item_path
from .layout import DOC_FILE, FILES_DIR, path_segment, user_root
from .locks import backup_shared, workspace_lock

logger = logging.getLogger(__name__)

VM_FILES = (VM_ROOT / FILES_DIR).as_posix()
# How a sentence names the top of the library: the words `agents/chat.py` gives a library
# conversation's `/init` at the top (LIBRARY_INIT_TOP), which this package cannot import.
TOP = "the top of the library"


# --- naming an item ---------------------------------------------------------------------


def library_segments(raw: str) -> list[str] | None:
    """The names a library path gives, as the Files page shows them (`Papers/2017/x.pdf`)
    or under `/workspace/files/`: `[]` for the top (`''`, `/`), None for a path with a `.`
    or `..` component."""
    text = raw.strip().replace("\\", "/")
    if text == VM_FILES:
        text = ""
    prefix = f"{VM_FILES}/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    # Names are stored NFC (`clean_name`): a decomposed é from a macOS name still names them.
    segments = [
        unicodedata.normalize("NFC", part.strip()) for part in text.split("/") if part.strip()
    ]
    if any(part in (".", "..") for part in segments):
        return None
    return segments


def _named[RowT: (Folder, Document)](rows: list[RowT], segment: str) -> RowT | None:
    """The row a segment names: by its name as the Files page shows it, else by the name of
    its directory in the workspace (`path_segment`)."""
    return next((row for row in rows if row.name == segment), None) or next(
        (row for row in rows if path_segment(row.name) == segment), None
    )


async def _documents_in(db: AsyncSession, user_id: int, folder_id: int | None) -> list[Document]:
    """The documents and files a folder holds, the Trash's left out."""
    return list(
        (
            await db.execute(
                select(Document).where(
                    Document.user_id == user_id,
                    filing.placed_in(Document.folder_id, folder_id),
                    Document.trashed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class Found:
    """What a path names: a live folder, a live document or file, or — both None — the top."""

    folder: Folder | None = None
    document: Document | None = None


async def resolve(
    db: AsyncSession, user_id: int, segments: list[str], folders: dict[int, Folder] | None = None
) -> Found | None:
    """The live item a path of names leads to, folder by folder; None for nothing live
    there. A document's directory and its `doc.md` both name the document (docs/chat.md
    § Library conversations, *The library tools*). `folders`: the user's, when the caller
    resolves many paths against one reading."""
    if folders is None:
        folders = await workspace_documents.user_folders(db, user_id)
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    place: Folder | None = None
    for index, segment in enumerate(segments):
        parent_id = place.id if place is not None else None
        children = [
            row for row in folders.values() if row.parent_id == parent_id and row.id not in trashed
        ]
        folder = _named(children, segment)
        if folder is not None:
            place = folder
            continue
        document = _named(await _documents_in(db, user_id, parent_id), segment)
        rest = segments[index + 1 :]
        if document is None or not (
            rest == [] or (rest == [DOC_FILE] and document.kind != FILE_KIND)
        ):
            return None
        return Found(document=document)
    return Found(folder=place)


async def trash_entries(db: AsyncSession, user_id: int) -> list[tuple[str, Folder | Document]]:
    """What the Trash holds, each item trashed on its own, newest first, with the place
    Put Back returns it to — the Trash tab's *Original location* — and its name."""
    folders = await workspace_documents.user_folders(db, user_id)
    documents = (
        await db.execute(
            select(Document).where(Document.user_id == user_id, Document.trashed_at.is_not(None))
        )
    ).scalars()
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    entries: list[tuple[str, Folder | Document]] = []
    for row in folders.values():
        if row.trashed_at is not None:
            home = workspace_documents.nearest_live(folders, row.parent_id, trashed=trashed)
            entries.append((item_path(folders, home, row.name), row))
    for row in documents:
        home = workspace_documents.nearest_live(folders, row.folder_id, trashed=trashed)
        entries.append((item_path(folders, home, row.name), row))
    entries.sort(key=lambda entry: entry[1].trashed_at, reverse=True)
    return entries


def _item(kind: str, row_id: int) -> dict:
    """An item as the Files page's undo names it: `file` is any documents row."""
    return {"kind": "folder" if kind == "folder" else "file", "id": row_id}


def _reason(exc: ApiError) -> str:
    """A refusal's message as the end of a sentence."""
    message = exc.message.rstrip(".")
    return message[:1].lower() + message[1:]


# --- what a call did ----------------------------------------------------------------------


@dataclass
class Outcome:
    """What a call did, item by item: its sentences, how many items were done and refused,
    the undo record of what was done, and the first refusal — the whole call's, or the
    first item's — whose status a route answers with when nothing was done (docs/api.md
    § The library on the data plane)."""

    lines: list[str] = field(default_factory=list)
    done: int = 0
    failed: int = 0
    undo: dict | None = None
    refusal: ApiError | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def is_error(self) -> bool:
        """Nothing was done, and something was refused: a batch that did anything, or had
        nothing to do, is not an error (docs/chat.md § Library conversations, *A batch does
        what it can*)."""
        return self.done == 0 and self.refusal is not None

    def refuse(self, line: str, refusal: ApiError) -> Outcome:
        """One more refused item, said in `line`."""
        self.failed += 1
        self.lines.append(line)
        self.refusal = self.refusal or refusal
        return self

    def recorded(self, record: dict | None) -> Outcome:
        """The call's undo record, kept only when it did something."""
        self.undo = record if self.done else None
        return self


def _refused(line: str, status: int, message: str) -> Outcome:
    """A refusal of the whole call: nothing was done."""
    return Outcome([line], refusal=ApiError(status, "invalid_request", message))


def _paths(tool_input: dict, settings: Settings) -> list[str] | Outcome:
    paths = tool_input.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        message = "`paths` is a list of the items' places, at least one."
        return _refused(message, 400, message)
    if len(paths) > settings.chat_library_max_paths:
        message = (
            f"At most {settings.chat_library_max_paths} paths go in one call; nothing was done."
        )
        return _refused(message, 400, message)
    return paths


async def _destination(db: AsyncSession, user_id: int, raw: object) -> tuple[bool, int | None]:
    """The folder `to` names: `(found, id)`, the id None for the top."""
    segments = library_segments(raw) if isinstance(raw, str) else None
    found = await resolve(db, user_id, segments) if segments is not None else None
    if found is None or found.document is not None:
        return False, None
    return True, found.folder.id if found.folder is not None else None


def _no_destination(verb: str, raw: object) -> Outcome:
    """A `to` that names no live folder: the whole call refused, nothing made — a move or a
    copy goes only into a folder that exists (docs/mcp.md § The organizing tools)."""
    return _refused(
        f"Nothing was {verb}: {raw!r} names no folder; make it with new_folder first.",
        404,
        "No such folder",
    )


async def _each(
    db: AsyncSession, outcome: Outcome, what: str, step: Callable[[], Awaitable[str | None]]
) -> None:
    """One item of a batch, its own transaction: done, it says what it did; refused, it is
    rolled back and says why — `what` is the item's action, as in "move DL/x.pdf" — and the
    next goes on (docs/chat.md § Library conversations, *A batch does what it can*)."""
    try:
        line = await step()
    except ApiError as exc:
        await db.rollback()
        outcome.refuse(f"Could not {what}: {_reason(exc)}.", exc)
        return
    except Exception:  # this item's failure, never the others'
        logger.exception("a library operation failed to %s", what)
        await db.rollback()
        outcome.refuse(
            f"Could not {what}: it failed unexpectedly.",
            ApiError(500, "internal", "It failed unexpectedly"),
        )
        return
    if line is not None:
        outcome.lines.append(line)


async def _resolved_items(
    db: AsyncSession, user_id: int, paths: list[str], outcome: Outcome, action: str
) -> list[tuple[str, str, int]]:
    """Each path's item as `(the item's action, kind, id)` — `action` is a template such
    as "move {}" — resolved before anything changes: the items named are the ones the call
    works on, whatever an earlier one moves. A path that names nothing live is that item's
    error."""
    named = []
    folders = await workspace_documents.user_folders(db, user_id)
    for raw in paths:
        what = action.format(raw.strip() or "/")
        segments = library_segments(raw)
        found = await resolve(db, user_id, segments, folders) if segments else None
        if found is None and segments == []:
            outcome.refuse(
                f"Could not {what}: the top of the library is no item.",
                ApiError(400, "invalid_request", "The top of the library is no item"),
            )
        elif found is None:
            outcome.refuse(
                f"Could not {what}: nothing is there.",
                ApiError(404, "invalid_request", "Nothing is there"),
            )
        elif found.document is not None:
            named.append((what, "document", found.document.id))
        else:
            assert found.folder is not None
            named.append((what, "folder", found.folder.id))
    return named


# --- one item, as the page's operations and the undo replay take it -------------------------


async def _move_item(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    author: str,
    kind: str,
    row_id: int,
    to: int | None,
    name: str | None = None,
) -> tuple[str, dict | None]:
    """One item into the folder `to` — under `name` too when given, a rename's refusal of a
    taken name with it, as the page's undo moves an item back under its old name — and the
    move's part of the undo record, None when it was there already."""
    folders = await workspace_documents.user_folders(db, user_id)
    if kind == "folder":
        folder = await workspace_documents.live_folder(db, user_id, row_id)
        origin, old = folder.parent_id, folder.name
    else:
        document = await workspace_documents.live_document(db, user_id, row_id)
        origin, old = document.folder_id, document.name
    shown = item_path(folders, origin, old)
    if origin == to and name in (None, old):
        return f"{shown} ({kind} {row_id}) is there already.", None
    if kind == "folder":
        moved: Folder | Document = await filing.move_folder(
            db, settings, user_id, row_id, name=name, parent_id=to, move=True, author=author
        )
    else:
        moved = await filing.move_document(
            db, settings, user_id, row_id, name=name, folder_id=to, move=True, author=author
        )
    move = {"item": _item(kind, row_id), "from": origin, "to": to, "fromName": old}
    return f"Moved {shown} ({kind} {row_id}) to {item_path(folders, to, moved.name)}.", {
        **move,
        "toName": moved.name,
    }


async def _rename_item(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    author: str,
    kind: str,
    row_id: int,
    wanted: str,
) -> tuple[str, dict | None]:
    """One item renamed where it is, and the undo record, None when the name was its own."""
    folders = await workspace_documents.user_folders(db, user_id)
    if kind == "folder":
        folder = await workspace_documents.live_folder(db, user_id, row_id)
        old, place = folder.name, folder.parent_id
        renamed: Folder | Document = await filing.move_folder(
            db, settings, user_id, row_id, name=wanted, parent_id=None, move=False, author=author
        )
    else:
        document = await workspace_documents.live_document(db, user_id, row_id)
        old, place = document.name, document.folder_id
        renamed = await filing.move_document(
            db, settings, user_id, row_id, name=wanted, folder_id=None, move=False, author=author
        )
    new = renamed.name
    if new == old:
        return f"{item_path(folders, place, old)} ({kind} {row_id}) is named {new} already.", None
    record = {"type": "rename", "item": _item(kind, row_id), "from": old, "to": new}
    return (
        f"Renamed {item_path(folders, place, old)} ({kind} {row_id}) to "
        f"{item_path(folders, place, new)}."
    ), record


async def _trash_item(
    db: AsyncSession, settings: Settings, user_id: int, author: str, kind: str, row_id: int
) -> str:
    folders = await workspace_documents.user_folders(db, user_id)
    if kind == "folder":
        shown = folder_path(folders, row_id)
        await filing.trash_folder(db, settings, user_id, row_id, author=author)
    else:
        document = await workspace_documents.live_document(db, user_id, row_id)
        shown = item_path(folders, document.folder_id, document.name)
        await filing.trash_document(db, settings, user_id, row_id, author=author)
    return f"Moved {shown} ({kind} {row_id}) to the Trash."


async def _put_back_item(
    db: AsyncSession, settings: Settings, user_id: int, author: str, kind: str, row_id: int
) -> str:
    if kind == "folder":
        back: Folder | Document = await filing.put_back_folder(
            db, settings, user_id, row_id, author=author
        )
        place = back.parent_id
    else:
        back = await filing.put_back_document(db, settings, user_id, row_id, author=author)
        place = back.folder_id
    folders = await workspace_documents.user_folders(db, user_id)
    return (
        f"Put back {back.name} ({kind} {row_id}): it is at {item_path(folders, place, back.name)}."
    )


# --- the operations -----------------------------------------------------------------------


async def make_folders(
    db: AsyncSession, settings: Settings, user_id: int, names: list[str]
) -> tuple[list[int], int | None, ApiError | None]:
    """Every folder of a path that is missing, each as the page's *New folder* makes one:
    `(the ids made, outermost first; the last folder's id; a refusal)`. A folder already
    there is found as `resolve` finds one, by its name or its directory's; a file holding
    a name on the way is a refusal. Every name is cleaned before the first folder is made,
    so a name refused leaves nothing made; a folder refused after others were made leaves
    them, and the refusal says why — its message the end of a sentence — for the caller to
    report with them."""
    if len(names) > filing.FOLDER_PATH_DEPTH_MAX:
        return (
            [],
            None,
            ApiError(
                400,
                "invalid_request",
                f"a folder path is at most {filing.FOLDER_PATH_DEPTH_MAX} folders deep",
            ),
        )
    wanted = [filing.clean_name(raw, filing.FOLDER_NAME_MAX) for raw in names]
    folders = await workspace_documents.user_folders(db, user_id)
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    parent_id: int | None = None
    made: list[int] = []
    for name in wanted:
        if not made:
            children = [
                row
                for row in folders.values()
                if row.parent_id == parent_id and row.id not in trashed
            ]
            found = _named(children, name)
            if found is not None:
                parent_id = found.id
                continue
            document = _named(await _documents_in(db, user_id, parent_id), name)
            if document is not None:
                place = folder_path(folders, parent_id) or TOP
                return (
                    [],
                    None,
                    ApiError(409, "invalid_request", f"{place} holds a file named {document.name}"),
                )
        try:
            row = await filing.make_folder(db, settings, user_id, parent_id, name)
        except ApiError as exc:
            await db.rollback()
            path = item_path(await workspace_documents.user_folders(db, user_id), parent_id, name)
            return (
                made,
                parent_id,
                ApiError(
                    exc.status_code,
                    exc.error_type,
                    f"the folder {path} could not be made ({_reason(exc)})",
                ),
            )
        made.append(row.id)
        parent_id = row.id
    return made, parent_id, None


async def _new_folder(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    raw = tool_input.get("path")
    segments = library_segments(raw) if isinstance(raw, str) else None
    if not segments:
        message = "`path` is the new folder's place, as in 'DL/DINO/old'."
        return _refused(message, 400, message)
    made, parent_id, refusal = await make_folders(db, settings, user_id, segments)
    if not made and refusal is not None:
        return Outcome([f"No folder was made: {refusal.message}."], refusal=refusal)
    folders = await workspace_documents.user_folders(db, user_id)
    if not made:
        return Outcome(
            [f"The folder {folder_path(folders, parent_id)} (folder {parent_id}) already exists."]
        )
    listed = [f"{folder_path(folders, row_id)} (folder {row_id})" for row_id in made]
    answer = f"Made the folder{'s' if len(made) > 1 else ''} {', '.join(listed)}"
    # Folders made before a refusal stand: said, with their undo, as a batch says what it
    # did (docs/chat.md § Library conversations, *A batch does what it can*).
    line = f"{answer}, but {refusal.message}." if refusal is not None else f"{answer}."
    return Outcome([line], done=len(made), refusal=refusal).recorded(
        {"type": "appear", "items": [_item("folder", made[0])]}
    )


async def _move(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, Outcome):
        return paths
    found, to = await _destination(db, user_id, tool_input.get("to"))
    if not found:
        return _no_destination("moved", tool_input.get("to"))
    outcome = Outcome()
    moves: list[dict] = []
    for what, kind, row_id in await _resolved_items(db, user_id, paths, outcome, "move {}"):

        async def step(kind=kind, row_id=row_id) -> str | None:
            line, move = await _move_item(db, settings, user_id, author, kind, row_id, to)
            if move is not None:
                outcome.done += 1
                moves.append(move)
            return line

        await _each(db, outcome, what, step)
    return outcome.recorded({"type": "move", "moves": moves})


async def _rename(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    raw, wanted = tool_input.get("path"), tool_input.get("name")
    if not isinstance(raw, str) or not isinstance(wanted, str) or not wanted.strip():
        message = "`rename` takes the item's `path` and its new `name`."
        return _refused(message, 400, message)
    outcome = Outcome()
    record: dict | None = None
    for what, kind, row_id in await _resolved_items(db, user_id, [raw], outcome, "rename {}"):

        async def step(kind=kind, row_id=row_id) -> str | None:
            nonlocal record
            line, record = await _rename_item(db, settings, user_id, author, kind, row_id, wanted)
            if record is not None:
                outcome.done += 1
            return line

        await _each(db, outcome, what, step)
    return outcome.recorded(record)


async def _copy(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, Outcome):
        return paths
    found, to = await _destination(db, user_id, tool_input.get("to"))
    if not found:
        return _no_destination("copied", tool_input.get("to"))
    outcome = Outcome()
    copies: list[dict] = []
    for what, kind, row_id in await _resolved_items(db, user_id, paths, outcome, "copy {}"):

        async def step(kind=kind, row_id=row_id) -> str | None:
            folders = await workspace_documents.user_folders(db, user_id)
            if kind == "folder":
                shown = folder_path(folders, row_id)
                made: Folder | Document = await filing.copy_folder(
                    db, settings, user_id, row_id, to, author=author
                )
            else:
                document = await workspace_documents.live_document(db, user_id, row_id)
                shown = item_path(folders, document.folder_id, document.name)
                made = await filing.copy_document(db, settings, user_id, row_id, to, author=author)
            outcome.done += 1
            copies.append(_item(kind, made.id))
            return (
                f"Copied {shown} ({kind} {row_id}) to "
                f"{item_path(folders, to, made.name)} ({kind} {made.id})."
            )

        await _each(db, outcome, what, step)
    return outcome.recorded({"type": "appear", "items": copies})


async def _trash(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, Outcome):
        return paths
    outcome = Outcome()
    items: list[dict] = []
    for what, kind, row_id in await _resolved_items(
        db, user_id, paths, outcome, "move {} to the Trash"
    ):

        async def step(kind=kind, row_id=row_id) -> str | None:
            line = await _trash_item(db, settings, user_id, author, kind, row_id)
            outcome.done += 1
            items.append(_item(kind, row_id))
            return line

        await _each(db, outcome, what, step)
    return outcome.recorded({"type": "trash", "items": items})


async def _put_back(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, Outcome):
        return paths
    outcome = Outcome()
    items: list[dict] = []
    # The Trash as its listing names it when the call began — where Put Back returns an
    # item, then its name, newest first — read once before anything is put back, since
    # putting one back can change where the next returns: each path takes the newest item
    # of that name not taken by an earlier path, each name in it as the Files page shows it
    # or as its directory is named, as `resolve` reads a name.
    entries = await trash_entries(db, user_id)
    named: list[tuple[str, str, int]] = []
    for raw in paths:
        what = f"put back {raw.strip() or '/'}"
        segments = library_segments(raw) or []
        entry = next(
            (
                (path, row)
                for path, row in entries
                if len(path.split("/")) == len(segments)
                and all(
                    segment in (name, path_segment(name))
                    for name, segment in zip(path.split("/"), segments, strict=True)
                )
            ),
            None,
        )
        if entry is None:
            outcome.refuse(
                f"Could not {what}: the Trash holds nothing there.",
                ApiError(404, "invalid_request", "The Trash holds nothing there"),
            )
            continue
        entries.remove(entry)
        row = entry[1]
        named.append((what, "folder" if isinstance(row, Folder) else "document", row.id))

    for what, kind, row_id in named:

        async def step(kind=kind, row_id=row_id) -> str | None:
            line = await _put_back_item(db, settings, user_id, author, kind, row_id)
            outcome.done += 1
            items.append(_item(kind, row_id))
            return line

        await _each(db, outcome, what, step)
    return outcome.recorded({"type": "appear", "items": items})


# --- the undo replay (docs/mcp.md § The organizing tools, *undo*) ---------------------------


class StepItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["folder", "file"]
    id: int


class StepMove(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    item: StepItem
    from_: int | None = Field(alias="from")
    to: int | None
    fromName: str  # noqa: N815 — the Files page's own key
    toName: str  # noqa: N815


class MoveStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["move"]
    moves: list[StepMove] = Field(min_length=1)


class RenameStep(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["rename"]
    item: StepItem
    from_: str = Field(alias="from")
    to: str


class ItemsStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["trash", "appear"]
    items: list[StepItem] = Field(min_length=1)


# The four shapes an answer's `undo` takes, the Files page's `Step` (apps/web/app/lib/steps.ts).
STEP = TypeAdapter(Annotated[MoveStep | RenameStep | ItemsStep, Field(discriminator="type")])


async def _undo(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> Outcome:
    """An undo record taken back, or done again with `redo`, as the Files page's ⌘Z and
    ⇧⌘Z replay a step (`replay` in `pages/files.vue`): by the ids it names, each item its
    own transaction, in reverse order going back. A trash is put back and an `appear` — a
    new folder, a copy, a Put Back — goes to the Trash, never deleted for good."""
    try:
        step = STEP.validate_python(tool_input.get("step"))
    except ValidationError:
        message = (
            "`step` is not an undo record: pass the `undo` an earlier answer returned, unchanged."
        )
        return _refused(message, 400, message)
    back = tool_input.get("redo") is not True
    count = (
        len(step.moves)
        if isinstance(step, MoveStep)
        else (len(step.items) if isinstance(step, ItemsStep) else 1)
    )
    if count > settings.chat_library_max_paths:
        message = (
            f"At most {settings.chat_library_max_paths} items go in one call; nothing was done."
        )
        return _refused(message, 400, message)
    outcome = Outcome()

    def kind_of(item: StepItem) -> str:
        return "folder" if item.kind == "folder" else "document"

    async def now(kind: str, row_id: int) -> tuple[int | None, str]:
        """Where an item is and what it is named."""
        if kind == "folder":
            folder = await workspace_documents.live_folder(db, user_id, row_id)
            return folder.parent_id, folder.name
        document = await workspace_documents.live_document(db, user_id, row_id)
        return document.folder_id, document.name

    # A record kept a while may be older than the reader's own changes: a move replays only
    # an item still in the folder the record left it in, a rename only one still named what
    # it left it, so a later move or rename is never taken back with it.
    if isinstance(step, ItemsStep):
        hide = (step.type == "trash") != back
        for item in reversed(step.items) if back else step.items:
            kind = kind_of(item)

            async def put(kind=kind, row_id=item.id) -> str | None:
                if hide:
                    line = await _trash_item(db, settings, user_id, author, kind, row_id)
                else:
                    line = await _put_back_item(db, settings, user_id, author, kind, row_id)
                outcome.done += 1
                return line

            verb = "move {} {} to the Trash" if hide else "put back {} {}"
            await _each(db, outcome, verb.format(kind, item.id), put)
    elif isinstance(step, MoveStep):
        for move in reversed(step.moves) if back else step.moves:
            kind = kind_of(move.item)

            async def place(kind=kind, row_id=move.item.id, move=move) -> str | None:
                folder, named = await now(kind, row_id)
                if folder != (move.to if back else move.from_):
                    raise ApiError(409, "invalid_request", "It was moved since")
                # Back under its old name too, when the destination had suffixed it and the
                # suffixed name is still its own; a name the reader gave it since stays.
                suffixed = move.toName != move.fromName and named == move.toName
                name = move.fromName if back and suffixed else None
                to = move.from_ if back else move.to
                line, moved = await _move_item(
                    db, settings, user_id, author, kind, row_id, to, name
                )
                if moved is not None:
                    outcome.done += 1
                return line

            await _each(db, outcome, f"move {kind} {move.item.id}", place)
    else:
        kind = kind_of(step.item)

        async def rename() -> str | None:
            _, named = await now(kind, step.item.id)
            if named != (step.to if back else step.from_):
                raise ApiError(409, "invalid_request", "It was renamed since")
            line, record = await _rename_item(
                db, settings, user_id, author, kind, step.item.id, step.from_ if back else step.to
            )
            if record is not None:
                outcome.done += 1
            return line

        await _each(db, outcome, f"rename {kind} {step.item.id}", rename)
    return outcome


OPERATIONS: dict[str, Callable[..., Awaitable[Outcome]]] = {
    "new_folder": _new_folder,
    "move": _move,
    "rename": _rename,
    "copy": _copy,
    "trash": _trash,
    "put_back": _put_back,
    "undo": _undo,
}


async def run(
    db: AsyncSession, settings: Settings, *, user_id: int, author: str, name: str, tool_input: dict
) -> Outcome:
    """One call of an operation by its name, under the locks its caller holds — the backup
    lock shared, then the workspace lock (docs/workspace.md § The page's operations are the
    agent's) — on `db`. A refusal of the whole call is its outcome, never raised."""
    try:
        return await OPERATIONS[name](db, settings, user_id, author, tool_input)
    except ApiError as exc:
        await db.rollback()
        return Outcome([f"Nothing was done: {_reason(exc)}."], refusal=exc)


async def run_locked(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    user_id: int,
    author: str,
    name: str,
    tool_input: dict,
) -> Outcome:
    """`run` for a caller that holds no lock — a library conversation's tool, the
    connector's: the backup lock taken shared, the workspace lock and a session of its own,
    in that order. A lock refused (a workspace recovering) is the call's outcome too."""
    root = user_root(settings.workspaces_dir, user_id)
    try:
        async with backup_shared(root), workspace_lock(root), sessionmaker() as db:
            return await run(
                db, settings, user_id=user_id, author=author, name=name, tool_input=tool_input
            )
    except ApiError as exc:
        return Outcome([f"Nothing was done: {_reason(exc)}."], refusal=exc)
