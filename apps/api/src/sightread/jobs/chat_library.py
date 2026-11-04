"""A library conversation's library tools (docs/chat.md § Library conversations, *The
library tools*): the library listed as the Files page lists it, and changed by the Files
page's own operations — `workspace/filing.py`, the functions the library routes call — each
call under the backup lock taken shared, the workspace lock and a database session of its
own, as `convert_file` runs. Every answer says what was done, a sentence per item, and a
call that changed something carries its undo record, in the shapes the Files page's undo
keeps its steps in (docs/web.md § Files, *Undo*).

Also the path resolver `convert_file` shares: an item named by its place as the Files page
shows it (`DL/DINO/iBOT.pdf`), or by its path under `/workspace/files/`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from ..db.models import FILE_KIND, MARKDOWN_KIND, Document, Folder, Job
from ..errors import ApiError
from ..workspace import documents as workspace_documents
from ..workspace import filing, safe_io
from ..workspace.agents_file import AGENTS_FILE
from ..workspace.layout import DOC_FILE, FILES_DIR, document_relative_dir, path_segment, user_root
from ..workspace.locks import backup_shared, workspace_lock
from .chat_tools import VM_ROOT, ToolOutcome

logger = logging.getLogger(__name__)

VM_FILES = VM_ROOT / FILES_DIR
TOP = "the top of the library"


# --- naming an item ---------------------------------------------------------------------


def library_segments(raw: str) -> list[str] | None:
    """The names a library path gives, as the Files page shows them (`Papers/2017/x.pdf`)
    or under `/workspace/files/`: `[]` for the top (`''`, `/`), None for a path with a `.`
    or `..` component."""
    text = raw.strip().replace("\\", "/")
    if text == VM_FILES.as_posix():
        text = ""
    prefix = f"{VM_FILES.as_posix()}/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    segments = [part.strip() for part in text.split("/") if part.strip()]
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


async def resolve(db: AsyncSession, user_id: int, segments: list[str]) -> Found | None:
    """The live item a path of names leads to, folder by folder; None for nothing live
    there. A document's directory and its `doc.md` both name the document (docs/chat.md
    § Library conversations, *The library tools*)."""
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


def folder_path(folders: dict[int, Folder], folder_id: int | None) -> str:
    """A folder's place as the Files page shows it: its names from the top, '' for the top."""
    return "/".join(row.name for row in workspace_documents.folder_chain(folders, folder_id))


def item_path(folders: dict[int, Folder], folder_id: int | None, name: str) -> str:
    """An item's place as the Files page shows it: its folder's names, then its own."""
    above = folder_path(folders, folder_id)
    return f"{above}/{name}" if above else name


def vm_path(folders: dict[int, Folder], folder_id: int | None, name: str) -> str:
    """Where an item of a folder is in the VM: a document's directory, a file itself."""
    names = [row.name for row in workspace_documents.folder_chain(folders, folder_id)]
    return (VM_ROOT / document_relative_dir(names, name).as_posix()).as_posix()


def _item(kind: str, row_id: int) -> dict:
    """An item as the Files page's undo names it: `file` is any documents row."""
    return {"kind": "folder" if kind == "folder" else "file", "id": row_id}


def _reason(exc: ApiError) -> str:
    """A refusal's message as the end of a sentence."""
    message = exc.message.rstrip(".")
    return message[:1].lower() + message[1:]


# --- what the Files page shows ----------------------------------------------------------

_FORMATS = {
    "jpeg": "JPEG",
    "jpg": "JPEG",
    "svg+xml": "SVG",
    "webp": "WebP",
    "quicktime": "QuickTime",
}
_OFFICE = {
    "doc": "Word document",
    "docx": "Word document",
    "odt": "OpenDocument text document",
    "pages": "Pages document",
    "xls": "Excel spreadsheet",
    "xlsx": "Excel spreadsheet",
    "xlsm": "Excel spreadsheet",
    "ods": "OpenDocument spreadsheet",
    "numbers": "Numbers spreadsheet",
    "ppt": "PowerPoint presentation",
    "pptx": "PowerPoint presentation",
    "odp": "OpenDocument presentation",
    "key": "Keynote presentation",
}
_ARCHIVES = {"zip": "ZIP", "7z": "7-Zip", "rar": "RAR", "tar": "tar", "gz": "gzip", "tgz": "gzip"}


def kind_label(document: Document) -> str:
    """What a row is, in the words of the Files page's *Kind* column (docs/web.md § Files,
    *Four columns*): a document by what it was converted from, a file by its type."""
    extension = document.name.rpartition(".")[2].lower() if "." in document.name else ""
    if document.kind == MARKDOWN_KIND:
        return "Markdown document"
    if document.kind == "pdf":
        return "PDF document"
    if document.kind == "image":
        return f"{_FORMATS.get(extension, extension.upper())} image".strip()
    media = (document.media_type or "").lower()
    top, _, subtype = media.partition("/")
    if media == "application/pdf" or extension == "pdf":
        return "PDF document"
    if top in ("image", "video", "audio"):
        return f"{_FORMATS.get(subtype, (extension or subtype).upper())} {top}".strip()
    if extension in _OFFICE:
        return _OFFICE[extension]
    if extension in _ARCHIVES:
        return f"{_ARCHIVES[extension]} archive"
    return f"{extension.upper()} file" if extension else "File"


def _size(count: int | None) -> str:
    if count is None:
        return "size unknown"
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{round(count / 1024)} KB"
    if count < 1024**3:
        return f"{count / 1024 / 1024:.1f} MB"
    return f"{count / 1024**3:.1f} GB"


def _state(job: Job | None) -> str:
    """A document's state as its row shows it: `ready`, `converting`, `queued` or `failed`."""
    if job is None or job.status == "succeeded":
        return "ready"
    if job.status == "queued":
        return "queued"
    if job.status == "running":
        return "converting"
    return "failed"


# --- list_files ---------------------------------------------------------------------------


async def list_files(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    user_id: int,
    author: str,
    tool_input: dict,
) -> ToolOutcome:
    """`list_files`: a folder as the Files page lists it, or the Trash. A read of the turn;
    under the workspace lock, since a ready document never written yet is materialized on
    the way, so the `doc.md` it names is there to read (docs/chat.md § Library
    conversations, *The library tools*)."""
    raw = tool_input.get("path") or ""
    if not isinstance(raw, str):
        return ToolOutcome("`path` is a folder's place as a string, '' for the top.", is_error=True)
    segments = library_segments(raw)
    if segments is None:
        return ToolOutcome(f"{raw} names no folder of the library.", is_error=True)
    limit = settings.chat_list_files_max_entries
    root = user_root(settings.workspaces_dir, user_id)
    try:
        async with workspace_lock(root), sessionmaker() as db:
            if tool_input.get("trash") is True:
                return ToolOutcome(await _list_trash(db, user_id, limit))
            found = await resolve(db, user_id, segments)
            if found is None or found.document is not None:
                return ToolOutcome(f"{raw} names no folder of the library.", is_error=True)
            return ToolOutcome(
                await _list_folder(
                    db,
                    settings,
                    user_id,
                    found.folder,
                    recursive=tool_input.get("recursive") is True,
                    limit=limit,
                    author=author,
                )
            )
    except ApiError as exc:
        return ToolOutcome(f"The library could not be listed: {_reason(exc)}.", is_error=True)


async def _list_folder(
    db: AsyncSession,
    settings: Settings,
    user_id: int,
    folder: Folder | None,
    *,
    recursive: bool,
    limit: int,
    author: str,
) -> str:
    folders = await workspace_documents.user_folders(db, user_id)
    trashed = workspace_documents.trashed_folder_ids(folders.values())
    live_folders = [row for row in folders.values() if row.id not in trashed]
    documents = list(
        (
            await db.execute(
                select(Document).where(Document.user_id == user_id, Document.trashed_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    job_ids = {row.job_id for row in documents if row.job_id is not None}
    jobs = (
        {
            job.id: job
            for job in (await db.execute(select(Job).where(Job.id.in_(job_ids)))).scalars()
        }
        if job_ids
        else {}
    )

    def order(rows):
        return sorted(rows, key=lambda row: (row.name.casefold(), row.name))

    def held(folder_id: int | None) -> tuple[list[Folder], list[Document]]:
        return (
            order(row for row in live_folders if row.parent_id == folder_id),
            order(row for row in documents if row.folder_id == folder_id),
        )

    # The entries in the page's order — the folder's AGENTS.md, its folders, then its
    # documents and files — each folder's own after it when `recursive`.
    entries: list[Folder | Document] = []

    def walk(folder_id: int | None) -> None:
        subfolders, inside = held(folder_id)
        entries.extend(row for row in inside if row.kind == FILE_KIND and row.name == AGENTS_FILE)
        for row in subfolders:
            entries.append(row)
            if recursive:
                walk(row.id)
        entries.extend(
            row for row in inside if not (row.kind == FILE_KIND and row.name == AGENTS_FILE)
        )

    folder_id = folder.id if folder is not None else None
    walk(folder_id)
    shown = entries[:limit]
    lines = []
    for row in shown:
        if isinstance(row, Folder):
            count = sum(len(part) for part in held(row.id))
            lines.append(
                f"folder {folder_path(folders, row.id)} (folder {row.id}, "
                f"{count} item{'s' if count != 1 else ''})"
            )
            continue
        path = item_path(folders, row.folder_id, row.name)
        at = vm_path(folders, row.folder_id, row.name)
        if row.kind == FILE_KIND and row.name == AGENTS_FILE:
            place = folder_path(folders, row.folder_id) or TOP
            lines.append(
                f"AGENTS.md of {place} (document {row.id}): the standing instructions for "
                f"this folder and the folders inside it, at {at}"
            )
        elif row.kind == FILE_KIND:
            lines.append(
                f"file {path} (document {row.id}, {kind_label(row)}, {_size(row.size_bytes)}) "
                f"at {at}"
            )
        else:
            job = jobs.get(row.job_id) if row.job_id is not None else None
            state = _state(job)
            if state == "ready":
                await _materialized(db, settings, row, author=author)
            lines.append(
                f"document {path} (document {row.id}, {kind_label(row)}, {state}) "
                f"text: {at}/{DOC_FILE}"
            )
    if len(entries) > len(shown):
        lines.append(f"[{len(entries) - len(shown)} more not listed: list a folder inside.]")
    place = folder_path(folders, folder_id)
    named = f"{place} (folder {folder_id})" if folder_id is not None else "The top of the library"
    if not entries:
        return f"{named} is empty."
    count = sum(len(part) for part in held(folder_id))
    head = f"{named} holds {count} item{'s' if count != 1 else ''}"
    return f"{head}{', and everything under it' if recursive else ''}:\n" + "\n".join(lines)


async def _materialized(
    db: AsyncSession, settings: Settings, row: Document, *, author: str
) -> None:
    """A ready document's directory, written now when it never was — a markdown document
    never opened, a copy — as a read of its content writes it (docs/workspace.md § Layout).
    One that cannot be written is listed all the same: reading it says why."""
    root = user_root(settings.workspaces_dir, row.user_id)
    relative = document_relative_dir(
        await workspace_documents.folder_names(db, row.user_id, row.folder_id), row.name
    )
    if safe_io.entry_kind(root, root / relative / DOC_FILE) == "file":
        return
    try:
        await workspace_documents.materialize_document(
            db, settings, row, locked=True, author=author
        )
    except ApiError as exc:
        logger.warning("document %d could not be written for list_files: %s", row.id, exc.message)


async def _trash_entries(db: AsyncSession, user_id: int) -> list[tuple[str, Folder | Document]]:
    """What the Trash holds, each item trashed on its own, newest first, with the place
    Put Back returns it to — the Trash tab's *Original location* — and its name."""
    folders = await workspace_documents.user_folders(db, user_id)
    documents = (
        await db.execute(
            select(Document).where(Document.user_id == user_id, Document.trashed_at.is_not(None))
        )
    ).scalars()
    entries: list[tuple[str, Folder | Document]] = []
    for row in folders.values():
        if row.trashed_at is not None:
            home = workspace_documents.nearest_live(folders, row.parent_id)
            entries.append((item_path(folders, home, row.name), row))
    for row in documents:
        home = workspace_documents.nearest_live(folders, row.folder_id)
        entries.append((item_path(folders, home, row.name), row))
    entries.sort(key=lambda entry: entry[1].trashed_at, reverse=True)
    return entries


async def _list_trash(db: AsyncSession, user_id: int, limit: int) -> str:
    entries = await _trash_entries(db, user_id)
    if not entries:
        return "The Trash is empty."
    lines = []
    for path, row in entries[:limit]:
        when = f"trashed {row.trashed_at:%Y-%m-%d %H:%M UTC}"
        if isinstance(row, Folder):
            lines.append(f"folder {path} (folder {row.id}, {when})")
        elif row.kind == FILE_KIND:
            lines.append(
                f"file {path} (document {row.id}, {kind_label(row)}, {_size(row.size_bytes)}, "
                f"{when})"
            )
        else:
            lines.append(f"document {path} (document {row.id}, {kind_label(row)}, {when})")
    if len(entries) > limit:
        lines.append(f"[{len(entries) - limit} more not listed.]")
    count = len(entries)
    return (
        f"The Trash holds {count} item{'s' if count != 1 else ''}, each named by where Put "
        "Back returns it:\n" + "\n".join(lines)
    )


# --- the seven that change the library ----------------------------------------------------


@dataclass
class Batch:
    """What a call did, item by item: its sentences, the undo record's parts, and whether
    anything was done at all."""

    lines: list[str]
    done: int = 0
    failed: int = 0

    def outcome(self, library: dict | None) -> ToolOutcome:
        return ToolOutcome(
            "\n".join(self.lines),
            is_error=self.done == 0 and self.failed > 0,
            library=library if self.done else None,
        )


def _paths(tool_input: dict, settings: Settings) -> list[str] | ToolOutcome:
    paths = tool_input.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return ToolOutcome("`paths` is a list of the items' places, at least one.", is_error=True)
    if len(paths) > settings.chat_library_max_paths:
        return ToolOutcome(
            f"At most {settings.chat_library_max_paths} paths go in one call; nothing was done.",
            is_error=True,
        )
    return paths


async def _destination(db: AsyncSession, user_id: int, raw: object) -> tuple[bool, int | None]:
    """The folder `to` names: `(found, id)`, the id None for the top."""
    segments = library_segments(raw) if isinstance(raw, str) else None
    found = await resolve(db, user_id, segments) if segments is not None else None
    if found is None or found.document is not None:
        return False, None
    return True, found.folder.id if found.folder is not None else None


async def _each(
    db: AsyncSession, batch: Batch, what: str, step: Callable[[], Awaitable[str | None]]
) -> None:
    """One item of a batch, its own transaction: done, it says what it did; refused, it is
    rolled back and says why — `what` is the item's action, as in "move DL/x.pdf" — and the
    next goes on (docs/chat.md § Library conversations, *A batch does what it can*)."""
    try:
        line = await step()
    except ApiError as exc:
        await db.rollback()
        batch.failed += 1
        batch.lines.append(f"Could not {what}: {_reason(exc)}.")
        return
    except Exception:  # noqa: BLE001 - this item's failure, never the others'
        logger.exception("a library tool failed to %s", what)
        await db.rollback()
        batch.failed += 1
        batch.lines.append(f"Could not {what}: it failed unexpectedly.")
        return
    if line is not None:
        batch.lines.append(line)


async def _resolved_items(
    db: AsyncSession, user_id: int, paths: list[str], batch: Batch, action: str
) -> list[tuple[str, str, int]]:
    """Each path's item as `(the item's action, kind, id)` — `action` is a template such
    as "move {}" — resolved before anything changes: the items named are the ones the call
    works on, whatever an earlier one moves. A path that names nothing live is that item's
    error."""
    named = []
    for raw in paths:
        what = action.format(raw.strip() or "/")
        segments = library_segments(raw)
        found = await resolve(db, user_id, segments) if segments else None
        if found is None:
            batch.failed += 1
            reason = "the top of the library is no item" if segments == [] else "nothing is there"
            batch.lines.append(f"Could not {what}: {reason}.")
        elif found.document is not None:
            named.append((what, "document", found.document.id))
        else:
            assert found.folder is not None
            named.append((what, "folder", found.folder.id))
    return named


async def run_tool(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    user_id: int,
    author: str,
    name: str,
    tool_input: dict,
) -> ToolOutcome:
    """One call of a library tool that changes the library, under the backup lock taken
    shared, the workspace lock and a session of its own; a refusal of the whole call — the
    lock, a destination that is not a folder — is an error result with its reason."""
    root = user_root(settings.workspaces_dir, user_id)
    try:
        async with backup_shared(root), workspace_lock(root), sessionmaker() as db:
            run = TOOLS[name]
            return await run(db, settings, user_id, author, tool_input)
    except ApiError as exc:
        return ToolOutcome(f"Nothing was done: {_reason(exc)}.", is_error=True)


async def _new_folder(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    raw = tool_input.get("path")
    segments = library_segments(raw) if isinstance(raw, str) else None
    if not segments:
        return ToolOutcome("`path` is the new folder's place, as in 'DL/DINO/old'.", is_error=True)
    made, parent_id, refusal = await _make_folders(db, settings, user_id, segments)
    if not made and refusal is not None:
        return ToolOutcome(f"No folder was made: {refusal}.", is_error=True)
    folders = await workspace_documents.user_folders(db, user_id)
    if not made:
        return ToolOutcome(
            f"The folder {folder_path(folders, parent_id)} (folder {parent_id}) already exists."
        )
    listed = [f"{folder_path(folders, row_id)} (folder {row_id})" for row_id in made]
    answer = f"Made the folder{'s' if len(made) > 1 else ''} {', '.join(listed)}"
    # Folders made before a refusal stand: said, with their undo, as a batch says what it
    # did (docs/chat.md § Library conversations, *A batch does what it can*).
    return ToolOutcome(
        f"{answer}, but {refusal}." if refusal is not None else f"{answer}.",
        library={"type": "appear", "items": [_item("folder", made[0])]},
    )


async def _make_folders(
    db: AsyncSession, settings: Settings, user_id: int, names: list[str]
) -> tuple[list[int], int | None, str | None]:
    """Every folder of a path that is missing, each as the page's *New folder* makes one:
    `(the ids made, outermost first; the last folder's id; a refusal)`. A folder already
    there is found as `resolve` finds one, by its name or its directory's; a file holding
    a name on the way is a refusal. Every name is cleaned before the first folder is made,
    so a name refused leaves nothing made; a folder refused after others were made leaves
    them, and the refusal says why, for the caller to report with them."""
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
                return [], None, f"{place} holds a file named {document.name}"
        try:
            row = await filing.make_folder(db, settings, user_id, parent_id, name)
        except ApiError as exc:
            await db.rollback()
            path = item_path(await workspace_documents.user_folders(db, user_id), parent_id, name)
            return made, parent_id, f"the folder {path} could not be made ({_reason(exc)})"
        made.append(row.id)
        parent_id = row.id
    return made, parent_id, None


async def _move(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, ToolOutcome):
        return paths
    found, to = await _destination(db, user_id, tool_input.get("to"))
    if not found:
        return ToolOutcome(
            f"Nothing was moved: {tool_input.get('to')!r} names no folder.", is_error=True
        )
    batch = Batch([])
    moves: list[dict] = []

    for what, kind, row_id in await _resolved_items(db, user_id, paths, batch, "move {}"):

        async def step(kind=kind, row_id=row_id) -> str | None:
            folders = await workspace_documents.user_folders(db, user_id)
            if kind == "folder":
                folder = await workspace_documents.live_folder(db, user_id, row_id)
                origin, old = folder.parent_id, folder.name
            else:
                document = await workspace_documents.live_document(db, user_id, row_id)
                origin, old = document.folder_id, document.name
            shown = item_path(folders, origin, old)
            if origin == to:
                return f"{shown} ({kind} {row_id}) is there already."
            if kind == "folder":
                moved: Folder | Document = await filing.move_folder(
                    db, settings, user_id, row_id, name=None, parent_id=to, move=True, author=author
                )
            else:
                moved = await filing.move_document(
                    db, settings, user_id, row_id, name=None, folder_id=to, move=True, author=author
                )
            landed = item_path(folders, to, moved.name)
            batch.done += 1
            moves.append(
                {
                    "item": _item(kind, row_id),
                    "from": origin,
                    "to": to,
                    "fromName": old,
                    "toName": moved.name,
                }
            )
            return f"Moved {shown} ({kind} {row_id}) to {landed}."

        await _each(db, batch, what, step)
    return batch.outcome({"type": "move", "moves": moves})


async def _rename(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    raw, wanted = tool_input.get("path"), tool_input.get("name")
    if not isinstance(raw, str) or not isinstance(wanted, str) or not wanted.strip():
        return ToolOutcome("`rename` takes the item's `path` and its new `name`.", is_error=True)
    batch = Batch([])
    record: dict | None = None
    for what, kind, row_id in await _resolved_items(db, user_id, [raw], batch, "rename {}"):

        async def step(kind=kind, row_id=row_id) -> str | None:
            nonlocal record
            folders = await workspace_documents.user_folders(db, user_id)
            if kind == "folder":
                folder = await workspace_documents.live_folder(db, user_id, row_id)
                old, place = folder.name, folder.parent_id
                renamed: Folder | Document = await filing.move_folder(
                    db,
                    settings,
                    user_id,
                    row_id,
                    name=wanted,
                    parent_id=None,
                    move=False,
                    author=author,
                )
            else:
                document = await workspace_documents.live_document(db, user_id, row_id)
                old, place = document.name, document.folder_id
                renamed = await filing.move_document(
                    db,
                    settings,
                    user_id,
                    row_id,
                    name=wanted,
                    folder_id=None,
                    move=False,
                    author=author,
                )
            new = renamed.name
            if new == old:
                return f"{item_path(folders, place, old)} ({kind} {row_id}) is named {new} already."
            batch.done += 1
            record = {"type": "rename", "item": _item(kind, row_id), "from": old, "to": new}
            return (
                f"Renamed {item_path(folders, place, old)} ({kind} {row_id}) to "
                f"{item_path(folders, place, new)}."
            )

        await _each(db, batch, what, step)
    return batch.outcome(record)


async def _copy(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, ToolOutcome):
        return paths
    found, to = await _destination(db, user_id, tool_input.get("to"))
    if not found:
        return ToolOutcome(
            f"Nothing was copied: {tool_input.get('to')!r} names no folder.", is_error=True
        )
    batch = Batch([])
    copies: list[dict] = []
    for what, kind, row_id in await _resolved_items(db, user_id, paths, batch, "copy {}"):

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
            batch.done += 1
            copies.append(_item(kind, made.id))
            return (
                f"Copied {shown} ({kind} {row_id}) to "
                f"{item_path(folders, to, made.name)} ({kind} {made.id})."
            )

        await _each(db, batch, what, step)
    return batch.outcome({"type": "appear", "items": copies})


async def _trash(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, ToolOutcome):
        return paths
    batch = Batch([])
    items: list[dict] = []
    for what, kind, row_id in await _resolved_items(
        db, user_id, paths, batch, "move {} to the Trash"
    ):

        async def step(kind=kind, row_id=row_id) -> str | None:
            folders = await workspace_documents.user_folders(db, user_id)
            if kind == "folder":
                shown = folder_path(folders, row_id)
                await filing.trash_folder(db, settings, user_id, row_id, author=author)
            else:
                document = await workspace_documents.live_document(db, user_id, row_id)
                shown = item_path(folders, document.folder_id, document.name)
                await filing.trash_document(db, settings, user_id, row_id, author=author)
            batch.done += 1
            items.append(_item(kind, row_id))
            return f"Moved {shown} ({kind} {row_id}) to the Trash."

        await _each(db, batch, what, step)
    return batch.outcome({"type": "trash", "items": items})


async def _put_back(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    paths = _paths(tool_input, settings)
    if isinstance(paths, ToolOutcome):
        return paths
    batch = Batch([])
    items: list[dict] = []
    # The Trash as `list_files {trash: true}` named it when the call began — where Put Back
    # returns an item, then its name, newest first — read once before anything is put back,
    # since putting one back can change where the next returns: each path takes the newest
    # item of that name not taken by an earlier path, each name in it as the Files page
    # shows it or as its directory is named, as `resolve` reads a name.
    entries = await _trash_entries(db, user_id)
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
            batch.failed += 1
            batch.lines.append(f"Could not {what}: the Trash holds nothing there.")
            continue
        entries.remove(entry)
        row = entry[1]
        named.append((what, "folder" if isinstance(row, Folder) else "document", row.id))

    for what, kind, row_id in named:

        async def step(kind=kind, row_id=row_id) -> str | None:
            if kind == "folder":
                back: Folder | Document = await filing.put_back_folder(
                    db, settings, user_id, row_id, author=author
                )
                place = back.parent_id
            else:
                back = await filing.put_back_document(db, settings, user_id, row_id, author=author)
                place = back.folder_id
            folders = await workspace_documents.user_folders(db, user_id)
            batch.done += 1
            items.append(_item(kind, row_id))
            landed = item_path(folders, place, back.name)
            return f"Put back {back.name} ({kind} {row_id}): it is at {landed}."

        await _each(db, batch, what, step)
    return batch.outcome({"type": "appear", "items": items})


async def _new_document(
    db: AsyncSession, settings: Settings, user_id: int, author: str, tool_input: dict
) -> ToolOutcome:
    raw = tool_input.get("path")
    segments = library_segments(raw) if isinstance(raw, str) else None
    if not segments:
        return ToolOutcome(
            "`path` is the new document's folders and name, as in 'Notes/Plan'.", is_error=True
        )
    text = tool_input.get("text")
    if text is not None and not isinstance(text, str):
        return ToolOutcome("`text` is the document's markdown, or ''.", is_error=True)
    if text and len(text.encode()) > settings.content_max_bytes:
        return ToolOutcome(
            f"The text is longer than a document takes ({settings.content_max_bytes:,} bytes); "
            "nothing was made.",
            is_error=True,
        )
    *names, name = segments
    # Cleaned before any folder is made, so a name refused leaves nothing made.
    name = filing.clean_name(name, filing.DOCUMENT_NAME_MAX)
    made, folder_id, refusal = await _make_folders(db, settings, user_id, names)
    row: Document | None = None
    if refusal is None:
        try:
            row = await filing.new_document(
                db, settings, user_id, folder_id, name, text=text or None
            )
        except ApiError as exc:
            refusal = _reason(exc)
    folders = await workspace_documents.user_folders(db, user_id)
    listed = ", ".join(folder_path(folders, row_id) for row_id in made)
    plural = "s" if len(made) > 1 else ""
    if row is None:
        if not made:
            return ToolOutcome(f"No document was made: {refusal}.", is_error=True)
        # The folders made on the way stand, said with their undo, as `new_folder` says
        # them.
        return ToolOutcome(
            f"No document was made: {refusal}. Made on the way: the folder{plural} {listed}.",
            library={"type": "appear", "items": [_item("folder", made[0])]},
        )
    # Its directory written at once, so the answer's doc.md is there to read; the document
    # stands whether or not it could be (`list_files` writes it on the way).
    written = ""
    try:
        await workspace_documents.materialize_document(
            db, settings, row, locked=True, author=author
        )
    except ApiError as exc:
        written = f", though its directory could not be written yet ({_reason(exc)})"
    path = item_path(folders, folder_id, row.name)
    also = f", with the new folder{plural} {listed}" if made else ""
    return ToolOutcome(
        f"Made the document {path} (document {row.id}){also}; its text is at "
        f"{vm_path(folders, folder_id, row.name)}/{DOC_FILE}{written}.",
        library={"type": "appear", "items": [_item("document", row.id)]},
    )


TOOLS: dict[str, Callable[..., Awaitable[ToolOutcome]]] = {
    "new_folder": _new_folder,
    "move": _move,
    "rename": _rename,
    "copy": _copy,
    "trash": _trash,
    "put_back": _put_back,
    "new_document": _new_document,
}
WRITE_TOOL_NAMES = frozenset(TOOLS)
