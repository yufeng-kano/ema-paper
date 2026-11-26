"""The MCP endpoint: streamable HTTP at `POST /mcp`, mounted in the same app (docs/mcp.md).

A shell and nothing more. `parse` mints a single-use upload ticket (docs/auth.md § 5) and
formats the `curl` commands that carry it; the document bytes go out-of-band through
`/v1/parse`, because base64 in tool arguments would burn the file size over again in model
tokens. The library tools list the Files library inline and, for one document, mint a
document ticket (docs/auth.md § 6) and format the commands that land it on disk or save its
text back over `/v1/library/documents/{id}` (docs/mcp.md § The library tools). No parsing
decision, no model choice, no queue knowledge and no workspace write lives here, so an MCP
spec change touches this file alone (docs/project-structure.md § Boundaries). What makes a
connector parse behave as a Files upload does — the check, the crops, the retry, the
in-flight join — is the data plane's (docs/mcp.md § The `parse` tool), and so is filing
one into Files: `parse` with `folder` finds or makes the folder through the data plane's
own function and mints a ticket that files there (docs/mcp.md § Filing into Files). The
organizing tools carry no bytes and no text, so they act within the call: each runs
`workspace.organize`, the batch layer the Files Agent's library tools and
`POST /v1/library/<tool>` run, which takes its own locks (docs/mcp.md § The organizing tools).
"""

from __future__ import annotations

import asyncio
import shlex
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ..agents import PARSE
from ..auth import document_tickets, upload_tickets
from ..auth.deps import bearer_credential, resolve_bearer_identity
from ..db.models import FILE_KIND, User
from ..errors import ApiError, error_response
from ..routes.v1 import FOLDER_PATH_MAX, document_entry, library_index, trash_index
from ..upstream.client import offered_models
from ..workspace import documents as workspace_documents
from ..workspace import filing, git, organize
from ..workspace.layout import path_segment

MCP_PATH = "/mcp"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"

COORDINATE_CONTRACT = (
    "Figures come back as `![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` placeholders "
    "with the caption on the next line, plus a `figures` array. Coordinates use "
    "meta.bbox_format (yxyx_norm1000: 0-1000 of page height/width), y before x. The "
    "`figures` command saves every figure as Ema Paper cropped it, the picture its web "
    "reader shows."
)

# Everything the agent needs that is not in a command string (docs/mcp.md § The `parse` tool).
NOTES = (
    "Set `f=` to your file, then run `upload` — the one command uploads it, waits on "
    "the SSE stream until the parse finishes, and lands the document named after the "
    "source: paper.pdf becomes paper.md. It is the document the Ema Paper web reader "
    "opens: format-checked (paragraphs a page break cut are joined, broken markup fixed, "
    "the text unchanged) unless the user turned the check off. Read that .md; NEVER "
    "print <name>.progress.sse — after a parse its final `done` event is the entire "
    "result JSON on one line and will flood your context. If `upload` exits non-zero, "
    "nothing was written to the .md: read curl's own message, then run "
    '`tail -c 600 "${f%.*}.progress.sse"` to see why — a refused upload leaves only its '
    "short JSON error there (`insufficient_credits` with the credits needed and left, "
    "`rate_limit`, a file or model the service refuses), a failed parse ends with an "
    "`error` event naming the failed pages — or run `status`. A parse is every page or "
    "nothing: when it failed, call `parse` for a fresh ticket and run `retry` with the "
    "same `f` and the same `-F pages=` as the upload — the pages that succeeded are kept "
    "and only the failed ones run again. A page the model blocked needs the fallback "
    "model: add `-F model=<id>` to `retry`, the second of `models` when there is one. "
    "Every figure is `![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` with an "
    "`<!-- page: N -->` marker before each page's content (a paragraph joined across a "
    "page break sits under the page it starts on); coordinates are 0-1000 normalized to "
    "that page, y before x. Run `figures` with the same ticket once the .md has landed "
    "to save each figure's crop as <name>.figures/figN.png — cropped by Ema Paper from "
    "its own render after its figure check, the picture the web reader shows; it names "
    "any figure that has no crop and exits non-zero if a fetch failed. `metadata` "
    "(<name>.meta.json) adds the figures array with captions and each figure's `check`: "
    "`unverified` means the check could not confirm the box (the web reader marks it "
    "'Crop not verified'), so look at that crop. Optional form fields on the upload: "
    "`-F model=<id>` (one of `models`; default: the first), `-F pages=1-5,8`, "
    "`-F force=true` (parse again, bypassing the dedup cache and a parse of the same "
    "file still running). A PDF and an image (jpg/png/webp/heic) both go to the same "
    "endpoint. `/v1/jobs/last` resolves to this ticket's own job; the explicit "
    "`/v1/jobs/<job_id>` routes work too (`job_id` is in every `progress` event and in "
    "`meta.job_id`). A dedup hit ends the stream at once. The ticket is good for one "
    "upload or one retry and then only for reading the job it created: once it is spent "
    "or expired, call `parse` again for a fresh one. Re-uploading the same file joins "
    "its parse while it runs and returns it once it has succeeded, so re-running a "
    "command your shell cut off costs nothing."
)

# A filing ticket's notes, after NOTES (docs/mcp.md § Filing into Files). A file parsed before
# is filed free only as the same parse, which the dedup cache answers; `{age}` is how recent.
FILED_NOTES = (
    "This ticket files the parse into the reader's Ema Paper Files library, in {where}. "
    "Once `upload` succeeds, the document is in that folder under the uploaded file's "
    "name; a name already taken there gets a ` (2)` suffix. For a readable entry, copy the "
    "file under a readable name (e.g. `cp 2508.10104v1.pdf DINOv3.pdf`) and set `f` to the "
    "copy; never rename the reader's own file. It is filed as if the reader had uploaded "
    "it on the Files page: its original kept, shown at once, opening when the parse "
    "succeeds, kept until the reader deletes it. If that folder already holds this same "
    "parse (an earlier run of the command, or the reader's own upload), the upload answers "
    "with that document and adds no second copy. A filed parse covers every page, so "
    "`-F pages=` is refused. A file you parsed before is filed from that parse, not parsed "
    "or charged again, only when the earlier upload sent no `-F pages=`, this upload sends "
    "the same `-F model=` (none if it sent none), and that parse succeeded{age}; otherwise "
    "every page is parsed and charged, so tell the reader before you run it. A refused "
    "filing (`storage_full`: the reader's storage is full) files nothing and makes "
    "`upload` exit non-zero. If the parse fails, the document shows as failed in Files: "
    "call `parse` without `folder` for a fresh ticket and run `retry`, which retries the "
    "document where it is. The .md you land is a copy: to change the document in Ema "
    "Paper, find its id with list_library and use read_document and write_document."
)
# An unfiled ticket's way into Files.
UNFILED_NOTES = (
    " An `upload` on this ticket adds nothing to the reader's Files library. To keep a "
    "paper in Ema Paper, call `parse` with `folder` and run `upload` again with the same "
    "file: it is filed from this parse, not parsed or charged again, only when this "
    "ticket's upload sent no `-F pages=` and its parse succeeded, and the filing upload "
    "sends the same `-F model=` (none if this one sent none){age}; otherwise every page is "
    "parsed and charged. A `retry` on this ticket resumes a failed parse where it is, a "
    "Files document's in Files."
)

# The library tools' own notes (docs/mcp.md § The library tools).
READ_NOTES = (
    "Set `d=` to the local directory you want (the document's name by default), then run "
    "`download`: it lands, in that directory, doc.md (the document's text as the Ema "
    "Paper web reader holds it) and notes.md (the reader's notes beside it), "
    "document.json (the library row), and under figures/ the crop of every "
    "`![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` placeholder in either file as "
    "pPAGE_YMIN_XMIN_YMAX_XMAX.png, plus every inserted image as img-<hash>.png|jpg — the "
    "name its `![…](figures/img-…)` link already points at. Coordinates are 0-1000 "
    "normalized to that page, y before x; `<!-- page: N -->` marks where each page "
    "starts. Read the files with your own tools, in the parts you need. A placeholder with "
    "no stored crop is named and skipped; any other failure makes the command exit "
    "non-zero with curl's own message. A parse still queued or running has no doc.md yet "
    "(HTTP 409): check `document.status` and try again once it is `succeeded`. "
    "Downloading again replaces doc.md and notes.md in `d`, so save your edits first. "
    "`original` (when present) lands the original upload as source.<ext>. To change the "
    "text or the notes, call `write_document` for the save command: it sends the "
    ".doc.md.etag / .notes.md.etag the download stored beside each file. The ticket "
    "reads only this document; once it expires, call `read_document` again."
)
FILE_NOTES = (
    "This is a file kept in the Ema Paper library as it was uploaded, not a parsed "
    "document: `download` lands the file itself under its name. It has no text or notes "
    "to save."
)
WRITE_NOTES = (
    "Edit the files `read_document`'s `download` landed in `d` — doc.md, the document's "
    "text, or notes.md, the reader's notes — then set `f=doc.md` or `f=notes.md` with the "
    "same `d` and run `save`. It sends the file with the ETag the download stored "
    "(`.<f>.etag`), and on success stores the new one, so you can edit and save again. "
    "Keep the `<!-- page: N -->` markers and the "
    "`![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` placeholders as they are: the web "
    "reader draws them. A new `![…](figures/img-…)` link has no image behind it, so do "
    "not add one. A non-zero exit prints the server's short JSON error and leaves the "
    "local file untouched. `revision_conflict` (HTTP 409) means the file changed in Ema "
    "Paper since your download (the reader or their Agent edited it): run `download` "
    "again with another `d`, carry your edits over into that copy and save from there. "
    "Every save is one revision of the document's history. The ticket reaches only this "
    "document; once it expires, call `write_document` again."
)

# What a download greps out of doc.md and notes.md, and how each line becomes a route and a
# file name: a figure placeholder's crop as the workspace names it, an inserted image under
# its own name (docs/mcp.md § The library tools).
PICTURES = (
    r"'!\[fig[0-9]+\]\(sightread://p[0-9]+/[0-9]+,[0-9]+,[0-9]+,[0-9]+\)"
    r"|figures/img-[0-9a-f]{16}\.(png|jpg)'"
)
PICTURE_ROUTES = (
    r"-e 's#^!\[fig[0-9]+\]\(sightread://p([0-9]+)/([0-9]+),([0-9]+),([0-9]+),([0-9]+)\)$"
    r"#figures/\1/\2,\3,\4,\5 p\1_\2_\3_\4_\5.png#' "
    r"-e 's#^figures/(img-[0-9a-f]{16}\.(png|jpg))$#images/\1 \1#'"
)


# The organizing tools' shared inputs, at module level: the SDK resolves a tool's annotations
# in this module's namespace (docs/mcp.md § The organizing tools).
PATHS_FIELD = Field(
    description=(
        "The items' paths exactly as list_library lists them (documents, files of any kind, "
        "folders), e.g. ['DL/Attention Is All You Need.pdf']"
    ),
    min_length=1,
)
TO_FIELD = Field(
    description=(
        "An existing folder's path as list_library lists it, or / for the top level; make a "
        "missing one with new_folder first"
    ),
    max_length=FOLDER_PATH_MAX,
)


def _local_name(name: str) -> str:
    """The local name a command lands a document under, as one shell word: the directory
    name the workspace gives it (`path_segment`: no separator, never `.`, `..` or a
    hidden name), and behind `./` when it starts with `-`, so no command reads it as an
    option or curl as stdout (docs/mcp.md § The library tools)."""
    segment = path_segment(name)
    return shlex.quote(f"./{segment}" if segment.startswith("-") else segment)


def _stamp(moment: datetime) -> str:
    """`2026-08-21T15:00:00Z`, the shape docs/mcp.md shows."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# The authenticated user for the request being served. Set by the ASGI guard below and read
# by the tool; the streamable-HTTP transport runs each request's handler in a task spawned
# from that request, so the value a tool sees is the one its own caller presented.
_current_user_id: ContextVar[int | None] = ContextVar("sightread_mcp_user_id", default=None)


async def _current_user(db: AsyncSession) -> User:
    user_id = _current_user_id.get()
    user = (
        None
        if user_id is None
        else (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    )
    if user is None:
        raise PermissionError("This MCP session is not authenticated")
    return user


def build_server(app: FastAPI) -> MCPServer:
    """The MCP server and its tools (docs/mcp.md § The `parse` tool, § The library tools)."""
    server = MCPServer(
        "Ema Paper",
        instructions=(
            "Parse PDFs and images into the markdown document the Ema Paper web reader "
            "opens, with its figures, and keep them in the reader's Files library when "
            "asked. Call `parse` for a single-use upload ticket and ready-to-run curl "
            "commands, then run them in your shell: the file goes straight to the REST "
            "endpoint and the result lands on disk, never through this connection. To keep "
            "a paper in Ema Paper (the reader asks to save, keep or file it there, or "
            "names a folder of their Ema Paper Files library, not a directory on disk), "
            "pass `parse` a `folder`: a folder path exactly as `list_library` lists it, or "
            "`/` for the top level. The parsed document then appears in that Files folder "
            "as if the reader had uploaded it there. A paper parsed earlier without a "
            "folder is filed from that parse, without being parsed or charged again, when "
            "it is the same parse (the notes say when). Without `folder`, nothing is added "
            "to Files. " + COORDINATE_CONTRACT + " "
            "The library tools read the reader's Files library the same way: "
            "`list_library` finds a document or a folder path, `read_document` gives the "
            "commands that land its text, notes and figures on disk, `write_document` the "
            "command that saves edited text or notes back. "
            "The organizing tools put the Files library in order as its Files page does, at "
            "once and with no shell or browser: `new_folder`, `move`, `rename`, `copy`, "
            "`trash`, `put_back` and `undo`. Name items by path exactly as `list_library` "
            "lists them, `/` for the top level, and make a folder with `new_folder` before "
            "moving anything into it. When the reader asks you to organize, act without "
            "asking first: every answer says what was done and carries `undo`, which the "
            "`undo` tool takes back. Nothing is deleted for good: only the reader empties "
            "the Trash, on the website."
        ),
    )

    # The organizing tools change the library but remove nothing for good: every step comes
    # back with `undo`, and the Trash keeps what `trash` takes (docs/mcp.md § Annotations).
    organizing = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, open_world_hint=False
    )

    async def organized(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        """One organizing tool's call: `workspace.organize` under its own locks, authored
        `api`, shielded so a client that stops waiting never cuts a batch off between items
        (docs/mcp.md § The organizing tools). Nothing done and something refused is a tool
        error with the sentences."""
        async with app.state.sessionmaker() as db:
            settings = await app.state.runtime_settings.current(db)
            user_id = (await _current_user(db)).id
        outcome = await asyncio.shield(
            organize.run_locked(
                app.state.sessionmaker,
                settings,
                user_id=user_id,
                author=git.API_AUTHOR,
                name=name,
                tool_input=tool_input,
            )
        )
        if outcome.is_error:
            raise ValueError(outcome.text)
        return {
            "result": outcome.text,
            "done": outcome.done,
            "failed": outcome.failed,
            "undo": outcome.undo,
        }

    @server.tool(
        description=(
            "Start a parse, or the retry of a failed one: returns a single-use upload "
            "ticket and the exact curl commands that upload a PDF or image and land the "
            "parsed document as a markdown file named after it (page markers included), "
            "and its figures. Do not send file content here; set `f=` to your file and run "
            "the returned `upload` command in your shell. One ticket per file. "
            "`folder` also files the parsed document into the reader's Ema Paper Files "
            "library, as an upload on the Files page does: pass it when the reader wants "
            "the paper kept in Ema Paper or names a folder of their Files library (not a "
            "directory on disk), and leave it out otherwise. Take the path from "
            "list_library exactly as listed, since names are case-sensitive and a path "
            "that matches no folder creates the missing ones: only name a new folder when "
            "the reader asked for one. To file a paper you parsed without a folder, call "
            "parse with the folder and run upload again with the same file: it is filed "
            "from the earlier parse, not charged again, when it is the same parse (the "
            "notes say when). To retry a failed parse, filed or not, call parse without "
            "`folder`. A document already in Files is moved, renamed or trashed with the "
            "organizing tools, never parsed again. " + COORDINATE_CONTRACT
        ),
        # It may make folders and file a document; it never removes or overwrites one.
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        ),
    )
    async def parse(
        folder: Annotated[
            str | None,
            Field(
                description=(
                    "Optional. A folder path in the reader's Files library exactly as "
                    "list_library lists it (e.g. Papers/2024), or / for the top level. The "
                    "parsed document is filed there; folders on the path that do not exist "
                    "are created. Leave it out to keep the parse out of Files."
                ),
                max_length=FOLDER_PATH_MAX,
            ),
        ] = None,
    ) -> dict[str, Any]:
        async with app.state.sessionmaker() as db:
            # The settings in effect, the database's overrides on `.env`'s, as every route
            # reads them — the parse models among them (docs/admin.md § Settings).
            settings = await app.state.runtime_settings.current(db)
            user = await _current_user(db)
            user_id = user.id
            destination = None
            if folder is not None:
                if not folder.strip():
                    raise ValueError(
                        "`folder` is empty: give a folder path from list_library, or / for "
                        "the top level of Files"
                    )
                # The rate first, so a call it refuses makes no folder nobody is told of.
                await upload_tickets.check_mint_rate(db, settings, user_id)
                # Found or made by the data plane's own function (docs/mcp.md § Filing
                # into Files), before the ticket that carries it is minted.
                destination = await filing.find_or_make_folder_path(db, settings, user_id, folder)
                user = await _current_user(db)
            ticket, token = await upload_tickets.mint(
                db,
                settings,
                user,
                into_library=destination is not None,
                folder_id=None if destination is None else destination["id"],
            )

        # How recent a parse must be to be filed free: the dedup cache keeps a day's margin
        # before the retention (docs/mcp.md § Filing into Files, *Already parsed*), so with
        # a one-day retention none is, and with none any is.
        retention = settings.parse_retention_days
        never = " (never on this service, which keeps a parse 1 day)" if retention == 1 else None
        if destination is not None:
            where = (
                "the top level of Files"
                if destination["id"] is None
                else f"Files › {destination['path']}"
            )
            age = never or ("" if retention == 0 else f" less than {retention - 1} days ago")
            notes = f"{NOTES} {FILED_NOTES.format(where=where, age=age)}"
            if destination["created"]:
                notes += (
                    " This call created the folder(s) "
                    + ", ".join(destination["created"])
                    + ": tell the reader."
                )
        else:
            # A connector's parse is in no library, so the retention deletes it; how soon
            # is the deployment's own setting (docs/mcp.md § The `parse` tool, docs/jobs.md
            # § Retention).
            notes = (
                NOTES
                if settings.parse_retention_days == 0
                else f"{NOTES} A parse an `upload` on this ticket makes is kept "
                f"{settings.parse_retention_days} days after it finishes and then deleted: "
                "the .md you landed is the copy that lasts."
            )
            notes += UNFILED_NOTES.format(
                age=never or ("" if retention == 0 else f", within {retention - 1} days of its end")
            )

        base = settings.app_url
        auth = f"-H 'Authorization: Bearer {token}'"
        # The upload lands whatever the server answers in a progress file of its own,
        # removed first so it is always this run's; the read after it fails loudly with
        # `-f`, which writes nothing on an error — a refused upload's ticket is unspent
        # (409), a failed parse has no result (404) — so an error envelope never lands in,
        # or overwrites, a document. Options any curl has (docs/mcp.md).
        land = f'&& curl -sSf {auth} {base}/v1/jobs/last/result.md -o "${{f%.*}}.md"'
        send = (
            f"""s="${{f%.*}}.progress.sse"; rm -f "$s"; """
            f"curl -sSN {auth} -H 'Accept: text/event-stream'"
        )
        answer: dict[str, Any] = {
            "token": token,
            "expires_at": _stamp(ticket.expires_at),
            "max_upload_bytes": settings.upload_max_bytes,
            "page_cap": settings.page_cap,
            # The ids `-F model=` takes; `GET /v1/models` refuses a ticket (docs/auth.md § 5).
            "models": offered_models(settings, PARSE.name),
            # `f` names the source once; every output file is derived from it, so parsing
            # several documents into one directory never collides on a generic name.
            "upload": f'f=doc.pdf; {send} -F file=@"$f" {base}/v1/parse -o "$s" {land}',
            "retry": (
                f'f=doc.pdf; {send} -F file=@"$f" -F retry=true {base}/v1/parse -o "$s" {land}'
            ),
            "markdown": (
                f'f=doc.pdf; curl -sSf {auth} {base}/v1/jobs/last/result.md -o "${{f%.*}}.md"'
            ),
            "metadata": (
                f'f=doc.pdf; curl -sSf {auth} {base}/v1/jobs/last/result -o "${{f%.*}}.meta.json"'
            ),
            # Every placeholder of the landed .md, fetched as the crop the web reader shows:
            # a 404 is a figure with no crop, any other failure the command's exit status.
            "figures": (
                f'f=doc.pdf; m="${{f%.*}}.md"; d="${{f%.*}}.figures"; '
                f'[ -f "$m" ] || {{ echo "$m is missing: run upload or markdown first"; false; }} '
                f'&& mkdir -p "$d" && '
                r"grep -oE '!\[fig[0-9]+\]\(sightread://p[0-9]+/[0-9]+,[0-9]+,[0-9]+,[0-9]+\)' "
                r'"$m" | '
                r"sed -E 's#^!\[(fig[0-9]+)\]\(sightread://p([0-9]+)/([0-9,]+)\)$#\1 \2 \3#' | "
                f"{{ rc=0; while read -r n p b; do c=$(curl -sf {auth} "
                f'"{base}/v1/jobs/last/figures/$p/$b" -o "$d/$n.png" -w "%{{http_code}}"); '
                f'if [ "$c" = 404 ]; then echo "no crop stored for $n"; '
                f'elif [ "$c" != 200 ]; then echo "$n: fetch failed (HTTP $c)"; rc=1; fi; '
                f'done; [ "$rc" = 0 ]; }}'
            ),
            "status": f"curl -sS {auth} {base}/v1/jobs/last",
            "notes": notes,
        }
        if destination is not None:
            answer["folder"] = destination
        return answer

    @server.tool(
        description=(
            "List the reader's Ema Paper Files library: folders and documents (parsed "
            "PDFs and images, markdown documents, and files of any kind kept as they are, "
            "videos too, of kind `file`) with their ids and paths. Use it to find the "
            "`document_id` for read_document or write_document, a folder `path` for "
            "parse's `folder` and the organizing tools' `to`, and the paths new_folder, "
            "move, rename, copy and trash take. "
            "Optional: `folder`, a folder's path as listed (that folder and everything "
            "below it), and `name`, part of a document's name. `trash: true` lists the "
            "Trash instead, each item by the path put_back takes. When `truncated` is "
            "true, narrow by folder or name."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def list_library(
        folder: Annotated[
            str | None, Field(description="A folder's path as listed, e.g. Papers/2024")
        ] = None,
        name: Annotated[
            str | None, Field(description="Part of a document's name, any case")
        ] = None,
        trash: Annotated[
            bool,
            Field(description="List the Trash instead: the items put_back can bring back"),
        ] = False,
    ) -> dict[str, Any]:
        async with app.state.sessionmaker() as db:
            settings = await app.state.runtime_settings.current(db)
            user = await _current_user(db)
            user_id = user.id
            if trash:
                # The Trash and the live folders a `folder` must name, from its one snapshot.
                trashed = await trash_index(db, user_id)
                rows = await workspace_documents.user_folders(db, user_id)
                hidden = workspace_documents.trashed_folder_ids(rows.values())
                folders = [
                    {"path": workspace_documents.item_path(rows, row.parent_id, row.name)}
                    for row in rows.values()
                    if row.id not in hidden
                ]
                documents = []
            else:
                index = await library_index(db, user_id)
                folders, documents, trashed = index["folders"], index["documents"], []
        wanted = folder.strip("/") if folder else ""
        if wanted:
            if wanted not in {entry["path"] for entry in folders}:
                raise ValueError(
                    f"No folder of the library has the path {wanted!r}: call list_library "
                    "without `folder` and use one of the `path` values under `folders`"
                )
            folders = [
                entry
                for entry in folders
                if entry["path"] == wanted or entry["path"].startswith(wanted + "/")
            ]
            documents = [entry for entry in documents if entry["path"].startswith(wanted + "/")]
            trashed = [entry for entry in trashed if entry["path"].startswith(wanted + "/")]
        if name and name.strip():
            needle = name.strip().casefold()
            documents = [entry for entry in documents if needle in entry["name"].casefold()]
            trashed = [entry for entry in trashed if needle in entry["name"].casefold()]
        cap = settings.mcp_library_list_max_items
        if trash:
            return {
                "trash": [
                    {
                        "id": entry["id"],
                        "kind": entry["kind"],
                        "name": entry["name"],
                        "path": entry["path"],
                        "trashed_at": _stamp(entry["trashed_at"]),
                    }
                    for entry in trashed[:cap]
                ],
                "total": len(trashed),
                "truncated": len(trashed) > cap,
            }
        return {
            "folders": [{"id": entry["id"], "path": entry["path"]} for entry in folders],
            "documents": [
                {key: entry[key] for key in ("id", "name", "path", "kind", "status", "page_count")}
                for entry in documents[:cap]
            ],
            "total": len(documents),
            "truncated": len(documents) > cap,
        }

    @server.tool(
        description=(
            "Read one document of the reader's Ema Paper library: returns a short-lived "
            "ticket and the curl commands that land its text (doc.md), the reader's notes "
            "(notes.md) and its figures on disk, in a directory you name with `d=`. Takes "
            "the `document_id` from list_library; run the returned `download` command in "
            "your shell. The text never comes through this connection."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def read_document(
        document_id: Annotated[int, Field(description="The document's id from list_library")],
    ) -> dict[str, Any]:
        async with app.state.sessionmaker() as db:
            settings = await app.state.runtime_settings.current(db)
            user = await _current_user(db)
            # The suffix of what `…/original` answers with — the kept original, or the
            # upload until it is filed — names the file the `original` command lands.
            document, source = await document_entry(db, settings, user.id, document_id)
            ticket, token = await document_tickets.mint(
                db, settings, user, document_id, document_tickets.READ
            )
        base = f"{settings.app_url}/v1/library/documents/{document_id}"
        auth = f"-H 'Authorization: Bearer {token}'"
        local = _local_name(document["name"])
        answer: dict[str, Any] = {
            "token": token,
            "expires_at": _stamp(ticket.expires_at),
            "document": document,
        }
        if document["kind"] == FILE_KIND:
            answer["download"] = (
                f"n={local}; curl -sSf {auth} '{base}/file?download=true' -o \"$n\""
            )
            answer["notes"] = FILE_NOTES
            return answer
        answer["download"] = (
            f'd={local}; mkdir -p -- "$d/figures" && '
            f'curl -sSf {auth} {base} -o "$d/document.json" && '
            f'curl -sSf {auth} {base}/doc.md -o "$d/doc.md" --etag-save "$d/.doc.md.etag.new" '
            f'&& mv -f -- "$d/.doc.md.etag.new" "$d/.doc.md.etag" && '
            f'curl -sSf {auth} {base}/notes.md -o "$d/notes.md" '
            f'--etag-save "$d/.notes.md.etag.new" '
            f'&& mv -f -- "$d/.notes.md.etag.new" "$d/.notes.md.etag" && '
            f'grep -ohE {PICTURES} -- "$d/doc.md" "$d/notes.md" | sort -u | '
            f"sed -E {PICTURE_ROUTES} | "
            f"{{ rc=0; while read -r p n; do c=$(curl -sf {auth} "
            f'"{base}/$p" -o "$d/figures/$n" -w "%{{http_code}}"); '
            f'if [ "$c" = 404 ]; then echo "no picture stored for $n"; '
            f'rm -f -- "$d/figures/$n"; '
            f'elif [ "$c" != 200 ]; then echo "$n: fetch failed (HTTP $c)"; rc=1; fi; '
            f'done; [ "$rc" = 0 ]; }}'
        )
        if source is not None:
            answer["original"] = (
                f'd={local}; mkdir -p -- "$d" && '
                f'curl -sSf {auth} {base}/original -o "$d"/{shlex.quote("source" + source.suffix)}'
            )
        answer["notes"] = READ_NOTES
        return answer

    @server.tool(
        description=(
            "Save edited text or notes of one document back to the reader's Ema Paper "
            "library: returns a short-lived ticket and the curl command that sends the "
            "doc.md or notes.md read_document's download landed, with the revision it was "
            "read at, so a file changed in Ema Paper meanwhile is never overwritten. Takes "
            "the `document_id`; set `d=` and `f=` and run `save` in your shell."
        ),
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, open_world_hint=False
        ),
    )
    async def write_document(
        document_id: Annotated[int, Field(description="The document's id from list_library")],
    ) -> dict[str, Any]:
        async with app.state.sessionmaker() as db:
            settings = await app.state.runtime_settings.current(db)
            user = await _current_user(db)
            document, _ = await document_entry(db, settings, user.id, document_id)
            if document["kind"] == FILE_KIND:
                raise ApiError(
                    400,
                    "invalid_request",
                    "This is a file kept as it was uploaded: it has no text or notes to save",
                )
            ticket, token = await document_tickets.mint(
                db, settings, user, document_id, document_tickets.WRITE
            )
        base = f"{settings.app_url}/v1/library/documents/{document_id}"
        auth = f"-H 'Authorization: Bearer {token}'"
        local = _local_name(document["name"])
        return {
            "token": token,
            "expires_at": _stamp(ticket.expires_at),
            "document": document,
            # The ETag the download stored is the base; a 200 stores the new one, anything
            # else leaves the local file and its ETag as they were (docs/mcp.md).
            "save": (
                f'd={local}; f=doc.md; e="$d/.$f.etag"; if [ ! -s "$e" ]; then '
                f'echo "$e is missing: run read_document\'s download into this d first"; '
                f"false; else c=$(curl -sS -X PUT {auth} "
                f'-H "If-Match: $(cat -- "$e")" '
                f"-H 'Content-Type: text/markdown; charset=utf-8' "
                f'--data-binary @"$d/$f" "{base}/$f" --etag-save "$e.new" '
                f'-o "$e.answer" -w "%{{http_code}}"); '
                f'if [ "$c" = 200 ]; then mv -f -- "$e.new" "$e"; rm -f -- "$e.answer"; '
                f'echo "saved $d/$f"; else cat -- "$e.answer" 2>/dev/null; echo; '
                f'echo "$d/$f was not saved (HTTP $c)"; rm -f -- "$e.new" "$e.answer"; '
                f"false; fi; fi"
            ),
            "notes": WRITE_NOTES,
        }

    # --- the organizing tools (docs/mcp.md § The organizing tools) ---------------------------

    @server.tool(
        description=(
            "Make a folder in the reader's Ema Paper Files library, with any missing folders "
            "on its way, as the Files page's New folder does. A folder already there is "
            "reported, not made twice. Acts at once, no shell needed; the answer says what "
            "was done and carries `undo`."
        ),
        annotations=organizing,
    )
    async def new_folder(
        path: Annotated[
            str,
            Field(
                description="The new folder's path, e.g. DL/Transformer",
                max_length=FOLDER_PATH_MAX,
            ),
        ],
    ) -> dict[str, Any]:
        return await organized("new_folder", {"path": path})

    @server.tool(
        description=(
            "Move documents, files of any kind (videos too) or folders of the reader's Ema "
            "Paper Files library into a folder, as a drag on the Files page does. `to` must "
            "exist: make it with new_folder first. A name already taken there gets a ' (2)' "
            "suffix. Acts at once, item by item: the answer says what was done for each and "
            "carries `undo`, which the undo tool takes back. If a call times out, check "
            "list_library before calling it again."
        ),
        annotations=organizing,
    )
    async def move(
        paths: Annotated[list[str], PATHS_FIELD], to: Annotated[str, TO_FIELD]
    ) -> dict[str, Any]:
        return await organized("move", {"paths": paths, "to": to})

    @server.tool(
        description=(
            "Rename one document, file or folder of the reader's Ema Paper Files library, as "
            "the Files page does. `name` is the new name alone, without a folder; keep a "
            "file's extension unless the reader asks otherwise (a markdown document always "
            "keeps .md). A name already taken in that folder is refused. The answer carries "
            "`undo`."
        ),
        annotations=organizing,
    )
    async def rename(
        path: Annotated[str, Field(description="The item's path as list_library lists it")],
        name: Annotated[
            str,
            Field(
                description="The new name alone, e.g. Attention Is All You Need.pdf",
                min_length=1,
                max_length=filing.DOCUMENT_NAME_MAX,
            ),
        ],
    ) -> dict[str, Any]:
        return await organized("rename", {"path": path, "name": name})

    @server.tool(
        description=(
            "Copy documents, files or folders of the reader's Ema Paper Files library into a "
            "folder, as Paste on the Files page does: a document with its text, notes, "
            "original and images, a folder with everything in it. Copies count against the "
            "reader's storage. `to` must exist; a name taken there gets ' (2)'. Every call "
            "makes new copies, so after a timeout check list_library before calling again. "
            "The answer carries `undo`, which moves the copies to the Trash."
        ),
        annotations=organizing,
    )
    async def copy(
        paths: Annotated[list[str], PATHS_FIELD], to: Annotated[str, TO_FIELD]
    ) -> dict[str, Any]:
        return await organized("copy", {"paths": paths, "to": to})

    @server.tool(
        description=(
            "Move documents, files or folders of the reader's Ema Paper Files library to its "
            "Trash, as the Files page's Move to Trash does. Nothing is deleted for good: the "
            "Trash keeps them until the reader empties it or its expiry removes them, and "
            "put_back or undo brings them back. Only the reader deletes for good, on the "
            "website. The answer carries `undo`."
        ),
        annotations=organizing,
    )
    async def trash(paths: Annotated[list[str], PATHS_FIELD]) -> dict[str, Any]:
        return await organized("trash", {"paths": paths})

    @server.tool(
        description=(
            "Put items of the reader's Ema Paper Trash back, as the Files page's Put Back "
            "does: to the folder each came from, or the nearest one above it that is still "
            "there, a taken name suffixed ' (2)'. `paths` as list_library with trash=true "
            "lists them; when two share a path, the one trashed last. The answer carries "
            "`undo`."
        ),
        annotations=organizing,
    )
    async def put_back(
        paths: Annotated[
            list[str],
            Field(
                description="The items' paths as list_library with trash=true lists them",
                min_length=1,
            ),
        ],
    ) -> dict[str, Any]:
        return await organized("put_back", {"paths": paths})

    @server.tool(
        description=(
            "Take back a step new_folder, move, rename, copy, trash or put_back made in the "
            "reader's Ema Paper Files library, as ⌘Z on the Files page does: a move goes "
            "back under its old name, a rename back to the old name, a trash is put back, "
            "and new folders, copies and put-backs go to the Trash. `step` is the `undo` an "
            "earlier answer returned, unchanged; `redo: true` does the step again. Take back "
            "the latest step first."
        ),
        annotations=organizing,
    )
    async def undo(
        step: Annotated[
            dict[str, Any],
            Field(description="The `undo` an earlier answer returned, unchanged"),
        ],
        redo: Annotated[bool, Field(description="Do the step again instead")] = False,
    ) -> dict[str, Any]:
        return await organized("undo", {"step": step, "redo": redo})

    return server


class BearerGuard:
    """Bearer auth in front of the MCP transport.

    A 401 from here carries `WWW-Authenticate` with the protected-resource metadata URL —
    that pointer is what makes a Claude connector discover the authorization server and
    start the OAuth flow (docs/auth.md § 4, RFC 9728).
    """

    def __init__(self, app: FastAPI, transport) -> None:
        self._app = app
        self._transport = transport

    def _challenge(self, invalid: bool) -> dict[str, str]:
        issuer = self._app.state.settings.app_url
        parts = [f'resource_metadata="{issuer}{PROTECTED_RESOURCE_PATH}"']
        if invalid:
            parts.insert(0, 'error="invalid_token"')
        return {"WWW-Authenticate": "Bearer " + ", ".join(parts)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        credential = bearer_credential(Request(scope))
        user = None
        if credential is not None:
            async with self._app.state.sessionmaker() as db:
                user = await resolve_bearer_identity(db, credential)
        if user is None:
            response = error_response(
                401,
                "auth",
                "Invalid or missing bearer token",
                self._challenge(invalid=credential is not None),
            )
            await response(scope, receive, send)
            return

        user_token = _current_user_id.set(user.id)
        try:
            await self._transport(scope, receive, send)
        finally:
            _current_user_id.reset(user_token)


def mount_mcp(app: FastAPI) -> None:
    """Register `POST /mcp` on the FastAPI app and remember how to run its session manager."""
    server = build_server(app)
    transport = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Stateless: every request carries its own bearer and is served on its own
        # transport, so a connector never depends on server-side session affinity.
        stateless_http=True,
        # Host/Origin filtering belongs to Caddy in front of us; this endpoint is
        # authenticated with a bearer a browser cannot mint (docs/deployment.md).
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # A Route (not a Mount) so the path stays exactly `/mcp` for every method.
    app.router.routes.append(Route(MCP_PATH, endpoint=BearerGuard(app, transport)))
    app.state.mcp = server


@asynccontextmanager
async def mcp_session_manager(app: FastAPI):
    """Run the MCP session manager for the lifetime of the app (required by the SDK)."""
    async with app.state.mcp.session_manager.run():
        yield
