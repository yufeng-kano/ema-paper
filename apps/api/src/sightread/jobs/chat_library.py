"""A library conversation's library tools (docs/chat.md § Library conversations, *The
library tools*): the library listed as the Files page lists it, and changed by the Files
page's own operations. The six that the connector shares — new_folder, move, rename, copy,
trash and put_back — are `workspace/organize.py`'s, run under its locks; `list_files`, the
paths in the VM and `new_document` are this agent's alone. Every answer says what was done,
a sentence per item, and a call that changed something carries its undo record, in the
shapes the Files page's undo keeps its steps in (docs/web.md § Files, *Undo*).

Also the path resolver `convert_file` shares (`organize.resolve`): an item named by its place
as the Files page shows it (`DL/DINO/iBOT.pdf`), or by its path under `/workspace/files/`.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents.chat import LIBRARY_INIT_TOP
from ..config import Settings
from ..db.models import FILE_KIND, MARKDOWN_KIND, Document, Folder, Job
from ..errors import ApiError
from ..workspace import documents as workspace_documents
from ..workspace import filing, organize, safe_io
from ..workspace.agents_file import AGENTS_FILE
from ..workspace.documents import folder_path, item_path
from ..workspace.layout import DOC_FILE, document_relative_dir, user_root
from ..workspace.locks import backup_shared, workspace_lock
from ..workspace.organize import library_segments, resolve
from .chat_tools import VM_ROOT, ToolOutcome

logger = logging.getLogger(__name__)


# --- naming an item ---------------------------------------------------------------------


def vm_path(folders: dict[int, Folder], folder_id: int | None, name: str) -> str:
    """Where an item of a folder is in the VM: a document's directory, a file itself."""
    names = [row.name for row in workspace_documents.folder_chain(folders, folder_id)]
    return (VM_ROOT / document_relative_dir(names, name).as_posix()).as_posix()


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


def _state(status: str | None) -> str:
    """A document's state as its row shows it, from its job's status (None: no job):
    `ready`, `converting`, `queued` or `failed`."""
    if status is None or status == "succeeded":
        return "ready"
    if status == "queued":
        return "queued"
    if status == "running":
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
    """`list_files`: a folder as the Files page lists it, or the Trash. In order with the
    turn's writes and under the workspace lock: it lists what the round's earlier calls left,
    and a ready document never written yet is materialized on the way, so the `doc.md` it
    names is there to read (docs/chat.md § Library conversations, *The library tools*)."""
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
            folders = await workspace_documents.user_folders(db, user_id)
            found = await resolve(db, user_id, segments, folders)
            if found is None or found.document is not None:
                return ToolOutcome(f"{raw} names no folder of the library.", is_error=True)
            return ToolOutcome(
                await _list_folder(
                    db,
                    settings,
                    user_id,
                    folders,
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
    folders: dict[int, Folder],
    folder: Folder | None,
    *,
    recursive: bool,
    limit: int,
    author: str,
) -> str:
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

    def order(rows):
        return sorted(rows, key=lambda row: (row.name.casefold(), row.name))

    # Indexed once: a listing of hundreds of folders counts each one's items.
    folders_in: dict[int | None, list[Folder]] = {}
    for row in live_folders:
        folders_in.setdefault(row.parent_id, []).append(row)
    documents_in: dict[int | None, list[Document]] = {}
    for row in documents:
        documents_in.setdefault(row.folder_id, []).append(row)

    def held(folder_id: int | None) -> tuple[list[Folder], list[Document]]:
        return (
            order(folders_in.get(folder_id, [])),
            order(documents_in.get(folder_id, [])),
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
    # Only the statuses of the documents listed: a library holds far more than a page shows.
    job_ids = {row.job_id for row in shown if isinstance(row, Document) and row.job_id is not None}
    statuses: dict[uuid.UUID, str] = (
        dict((await db.execute(select(Job.id, Job.status).where(Job.id.in_(job_ids)))).all())
        if job_ids
        else {}
    )
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
            place = folder_path(folders, row.folder_id) or LIBRARY_INIT_TOP
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
            state = _state(statuses.get(row.job_id) if row.job_id is not None else None)
            if state == "ready":
                await _materialized(db, settings, folders, row, author=author)
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
    db: AsyncSession, settings: Settings, folders: dict[int, Folder], row: Document, *, author: str
) -> None:
    """A ready document's directory, written now when it never was — a markdown document
    never opened, a copy — as a read of its content writes it (docs/workspace.md § Layout).
    One that cannot be written is listed all the same: reading it says why."""
    root = user_root(settings.workspaces_dir, row.user_id)
    relative = document_relative_dir(
        [folder.name for folder in workspace_documents.folder_chain(folders, row.folder_id)],
        row.name,
    )
    if safe_io.entry_kind(root, root / relative / DOC_FILE) == "file":
        return
    try:
        await workspace_documents.materialize_document(
            db, settings, row, locked=True, author=author
        )
    except ApiError as exc:
        logger.warning("document %d could not be written for list_files: %s", row.id, exc.message)


async def _list_trash(db: AsyncSession, user_id: int, limit: int) -> str:
    entries = await organize.trash_entries(db, user_id)
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


# The six the connector shares, run by `organize`; `new_document` is this agent's alone.
SHARED_TOOL_NAMES = frozenset({"new_folder", "move", "rename", "copy", "trash", "put_back"})
WRITE_TOOL_NAMES = SHARED_TOOL_NAMES | {"new_document"}


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
    if name in SHARED_TOOL_NAMES:
        outcome = await organize.run_locked(
            sessionmaker, settings, user_id=user_id, author=author, name=name, tool_input=tool_input
        )
        return ToolOutcome(outcome.text, is_error=outcome.is_error, library=outcome.undo)
    root = user_root(settings.workspaces_dir, user_id)
    try:
        async with backup_shared(root), workspace_lock(root), sessionmaker() as db:
            return await _new_document(db, settings, user_id, author, tool_input)
    except ApiError as exc:
        return ToolOutcome(f"Nothing was done: {_reason(exc)}.", is_error=True)


def _reason(exc: ApiError) -> str:
    """A refusal's message as the end of a sentence."""
    message = exc.message.rstrip(".")
    return message[:1].lower() + message[1:]


def _item(kind: str, row_id: int) -> dict:
    """An item as the Files page's undo names it: `file` is any documents row."""
    return {"kind": "folder" if kind == "folder" else "file", "id": row_id}


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
    made, folder_id, refused = await organize.make_folders(db, settings, user_id, names)
    refusal = refused.message if refused is not None else None
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
