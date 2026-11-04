"""`python -m sightread.markdown_names [--dry-run]`: the one-time `.md` for the markdown
documents named before 2026-11-03 (docs/self-hosting.md § One-time commands after an
update, docs/workspace.md § Markdown documents). The operator runs it in the api container
after the deploy; nothing runs it by itself. Each document is renamed as a rename on the
Files page renames it — under the workspace lock, its directory moved and committed by
the same `move_then_commit` — so a second run finds nothing left to rename.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from itertools import groupby

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import Settings, get_settings
from .db.models import MARKDOWN_KIND, Document
from .db.session import create_engine, create_sessionmaker
from .errors import ApiError
from .workspace import documents as workspace_documents
from .workspace.agents_file import AGENTS_FILE
from .workspace.documents import MARKDOWN_EXTENSIONS, markdown_name
from .workspace.filing import (
    DOCUMENT_NAME_MAX,
    document_relative,
    free_name,
    move_then_commit,
    sibling_names,
)
from .workspace.layout import user_root
from .workspace.locks import WorkspaceRecovering, workspace_lock
from .workspace.trash import refuse_while_answering

logger = logging.getLogger(__name__)

# Why a document keeps its name this run; a later run renames it (docs/self-hosting.md).
ANSWERING = "a conversation about it is still answering"
IN_TRASHED_FOLDER = "it is inside a folder in the Trash"
RECOVERING = "the workspace is recovering from a shell command"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sightread.markdown_names", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    return parser


async def _rename(db: AsyncSession, settings: Settings, row: Document, *, dry_run: bool) -> str:
    """What becomes of one document, as its line says it after the ids. The new name is
    flushed, so the next document of the folder sees it taken, in a dry run too."""
    user_id, old = row.user_id, row.name
    folders = await workspace_documents.user_folders(db, user_id)
    trashed = row.trashed_at is not None
    if not trashed and not workspace_documents.is_live(row, folders):
        # Its directory waits in the folder's Trash entry under its old name, where Put
        # Back would look for it after the row had been renamed (docs/workspace.md § The
        # Trash).
        return f"{old}\tskipped: {IN_TRASHED_FOLDER}"
    try:
        # The refusal a move to the Trash answers: a turn holds the document's path.
        await refuse_while_answering(db, user_id, [row.id])
    except ApiError:
        return f"{old}\tskipped: {ANSWERING}"
    wanted = markdown_name(old, DOCUMENT_NAME_MAX)
    if trashed:
        # In the Trash a document takes no name, and its entry is named by its id: the row
        # changes alone, and Put Back suffixes the name if its folder took it meanwhile.
        row.name = free_name({AGENTS_FILE}, wanted, DOCUMENT_NAME_MAX)
        await db.flush()
        if not dry_run:
            await db.commit()
        return f"{old} → {row.name}"
    taken = await sibling_names(db, user_id, row.folder_id, exclude_document=row.id, markdown=True)
    before = await document_relative(db, row)
    row.name = free_name(taken, wanted, DOCUMENT_NAME_MAX)
    await db.flush()
    if not dry_run:
        # A directory never made moves nothing: the row changes alone.
        landed = (Document, row.id, {"folder_id": row.folder_id, "name": row.name})
        after = await document_relative(db, row)
        await move_then_commit(db, settings, user_id, before, after, landed=landed)
    return f"{old} → {row.name}"


async def run(args: argparse.Namespace, settings: Settings, db: AsyncSession) -> int:
    rows = (
        await db.execute(
            select(Document.user_id, Document.id, Document.name)
            .where(Document.kind == MARKDOWN_KIND)
            .order_by(Document.user_id, Document.id)
        )
    ).all()
    planned = [row for row in rows if not row.name.lower().endswith(MARKDOWN_EXTENSIONS)]
    failed = False
    for user_id, group in groupby(planned, key=lambda row: row.user_id):
        named = [(row.id, row.name) for row in group]
        lock = (
            contextlib.nullcontext()
            if args.dry_run
            else workspace_lock(user_root(settings.workspaces_dir, user_id))
        )
        try:
            async with lock:
                for document_id, name in named:
                    row = await db.get(Document, document_id, populate_existing=True)
                    if row is None or row.name.lower().endswith(MARKDOWN_EXTENSIONS):
                        continue  # deleted or renamed since it was listed
                    try:
                        said = await _rename(db, settings, row, dry_run=args.dry_run)
                    except Exception as exc:  # noqa: BLE001 - reported, and the run goes on
                        logger.exception("could not rename document %d", document_id)
                        await db.rollback()
                        failed = True
                        message = exc.message if isinstance(exc, ApiError) else type(exc).__name__
                        said = f"{name}\tfailed: {message}"
                    print(f"{user_id}\t{document_id}\t{said}")
        except WorkspaceRecovering:
            for document_id, name in named:
                print(f"{user_id}\t{document_id}\t{name}\tskipped: {RECOVERING}")
        # A dry run's flushed names go; a real run's are committed, and what a skip left
        # open (the row locks of the answering check) ends here.
        await db.rollback()
    return 1 if failed else 0


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as db:
            return await run(args, settings, db)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
