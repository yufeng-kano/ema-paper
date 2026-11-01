"""The MCP endpoint: streamable HTTP at `POST /mcp`, mounted in the same app (docs/mcp.md).

A shell and nothing more. `parse` mints a single-use upload ticket (docs/auth.md § 5) and
formats the `curl` commands that carry it; the document bytes go out-of-band through
`/v1/parse`, because base64 in tool arguments would burn the file size over again in model
tokens. The library tools list the Files library inline and, for one document, mint a
document ticket (docs/auth.md § 7) and format the commands that land it on disk or save its
text back over `/v1/library/documents/{id}` (docs/mcp.md § The library tools). No parsing
decision, no model choice, no queue knowledge and no workspace write lives here, so an MCP
spec change touches this file alone (docs/project-structure.md § Boundaries). What makes a
connector parse behave as a Files upload does — the check, the crops, the retry, the
in-flight join — is the data plane's (docs/mcp.md § The `parse` tool).
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
from ..auth.deps import LIBRARY_SCOPE_MESSAGE, bearer_credential, resolve_bearer_identity
from ..auth.oauth_as import LIBRARY_SCOPE
from ..db.models import FILE_KIND, User
from ..errors import ApiError, error_response
from ..routes.v1 import document_entry, library_index
from ..upstream.client import offered_models
from ..workspace import documents as workspace_documents
from ..workspace.layout import path_segment, user_root

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
# Whether that caller's credential reaches the library: an API key, or an OAuth grant holding
# the `library` scope (docs/auth.md § 4).
_current_library: ContextVar[bool] = ContextVar("sightread_mcp_library", default=False)


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


async def _library_user(db: AsyncSession) -> User:
    user = await _current_user(db)
    if not _current_library.get():
        raise PermissionError(LIBRARY_SCOPE_MESSAGE)
    return user


def build_server(app: FastAPI) -> MCPServer:
    """The MCP server and its tools (docs/mcp.md § The `parse` tool, § The library tools)."""
    server = MCPServer(
        "Ema Paper",
        instructions=(
            "Parse PDFs and images into the markdown document the Ema Paper web reader "
            "opens, with its figures. Call `parse` for a single-use upload ticket and "
            "ready-to-run curl commands, then run them in your shell: the file goes "
            "straight to the REST endpoint and the result lands on disk, never through "
            "this connection. " + COORDINATE_CONTRACT + " The library tools read the "
            "reader's Files library the same way: `list_library` finds a document, "
            "`read_document` gives the commands that land its text, notes and figures on "
            "disk, `write_document` the command that saves edited text or notes back."
        ),
    )

    @server.tool(
        description=(
            "Start a parse, or the retry of a failed one: returns a single-use upload "
            "ticket and the exact curl commands that upload a PDF or image and land the "
            "parsed document as a markdown file named after it (page markers included), "
            "and its figures. Takes no arguments — do not send file content here; set "
            "`f=` to your file and run the returned `upload` command in your shell. "
            + COORDINATE_CONTRACT
        )
    )
    async def parse() -> dict[str, Any]:
        async with app.state.sessionmaker() as db:
            # The settings in effect, the database's overrides on `.env`'s, as every route
            # reads them — the parse models among them (docs/admin.md § Settings).
            settings = await app.state.runtime_settings.current(db)
            ticket, token = await upload_tickets.mint(db, settings, await _current_user(db))

        base = settings.app_url.rstrip("/")
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
        return {
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
            # A connector's parse is in no library, so the retention deletes it; how soon
            # is the deployment's own setting (docs/mcp.md § The `parse` tool, docs/jobs.md
            # § Retention).
            "notes": (
                NOTES
                if settings.parse_retention_days == 0
                else f"{NOTES} A parse is kept {settings.parse_retention_days} days after it "
                "finishes and then deleted: the .md you landed is the copy that lasts."
            ),
        }

    @server.tool(
        description=(
            "List the reader's Ema Paper Files library: folders and documents (parsed "
            "PDFs and images, markdown documents, kept files) with their ids and paths. "
            "Use it to find the `document_id` for read_document or write_document. "
            "Optional: `folder`, a folder's path as listed (that folder and everything "
            "below it), and `name`, part of a document's name. When `truncated` is true, "
            "narrow by folder or name."
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
    ) -> dict[str, Any]:
        async with app.state.sessionmaker() as db:
            settings = await app.state.runtime_settings.current(db)
            user = await _library_user(db)
            index = await library_index(db, user.id)
        folders, documents = index["folders"], index["documents"]
        if folder and folder.strip("/"):
            wanted = folder.strip("/")
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
        if name and name.strip():
            needle = name.strip().casefold()
            documents = [entry for entry in documents if needle in entry["name"].casefold()]
        cap = settings.mcp_library_list_max_items
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
            user = await _library_user(db)
            document = await document_entry(db, settings, user.id, document_id)
            # The kept original's own suffix names the file the `original` command lands.
            source = (
                await asyncio.to_thread(
                    workspace_documents.original_of,
                    user_root(settings.workspaces_dir, user.id),
                    document_id,
                )
                if document["original"]
                else None
            )
            ticket, token = await document_tickets.mint(
                db, settings, user, document_id, document_tickets.READ
            )
        base = f"{settings.app_url.rstrip('/')}/v1/library/documents/{document_id}"
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
            user = await _library_user(db)
            document = await document_entry(db, settings, user.id, document_id)
            if document["kind"] == FILE_KIND:
                raise ApiError(
                    400,
                    "invalid_request",
                    "This is a file kept as it was uploaded: it has no text or notes to save",
                )
            ticket, token = await document_tickets.mint(
                db, settings, user, document_id, document_tickets.WRITE
            )
        base = f"{settings.app_url.rstrip('/')}/v1/library/documents/{document_id}"
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
        issuer = self._app.state.settings.app_url.rstrip("/")
        parts = [f'resource_metadata="{issuer}{PROTECTED_RESOURCE_PATH}"']
        if invalid:
            parts.insert(0, 'error="invalid_token"')
        return {"WWW-Authenticate": "Bearer " + ", ".join(parts)}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        credential = bearer_credential(Request(scope))
        identity = None
        if credential is not None:
            async with self._app.state.sessionmaker() as db:
                identity = await resolve_bearer_identity(db, credential)
        if identity is None:
            response = error_response(
                401,
                "auth",
                "Invalid or missing bearer token",
                self._challenge(invalid=credential is not None),
            )
            await response(scope, receive, send)
            return

        user_token = _current_user_id.set(identity.user.id)
        library_token = _current_library.set(
            identity.scopes is None or LIBRARY_SCOPE in identity.scopes
        )
        try:
            await self._transport(scope, receive, send)
        finally:
            _current_library.reset(library_token)
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
