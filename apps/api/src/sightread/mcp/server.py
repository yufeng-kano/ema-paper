"""The MCP endpoint: streamable HTTP at `POST /mcp`, mounted in the same app (docs/mcp.md).

A shell and nothing more. The one tool mints a single-use upload ticket (docs/auth.md § 5)
and formats the `curl` commands that carry it; the document bytes go out-of-band through
`/v1/parse`, because base64 in tool arguments would burn the file size over again in model
tokens. No parsing decision, no model choice and no queue knowledge lives here, so an MCP
spec change touches this file alone (docs/project-structure.md § Boundaries). What makes a
connector parse behave as a Files upload does — the check, the crops, the retry, the
in-flight join — is the data plane's (docs/mcp.md § The one tool).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC
from typing import Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from ..agents import PARSE
from ..auth import upload_tickets
from ..auth.deps import bearer_credential, resolve_bearer
from ..db.models import User
from ..errors import error_response
from ..upstream.client import offered_models

MCP_PATH = "/mcp"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"

COORDINATE_CONTRACT = (
    "Figures come back as `![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` placeholders "
    "with the caption on the next line, plus a `figures` array. Coordinates use "
    "meta.bbox_format (yxyx_norm1000: 0-1000 of page height/width), y before x. The "
    "`figures` command saves every figure as Ema Paper cropped it, the picture its web "
    "reader shows."
)

# Everything the agent needs that is not in a command string (docs/mcp.md § The one tool).
NOTES = (
    "Set `f=` to your file, then run `upload` — the one command uploads it, waits on "
    "the SSE stream until the parse finishes, and lands the document named after the "
    "source: paper.pdf becomes paper.md. It is the document the Ema Paper web reader "
    "opens: format-checked (paragraphs a page break cut are joined, broken markup fixed, "
    "the text unchanged) unless the user turned the check off. Read that .md; NEVER "
    "print progress.sse — after a parse its final `done` event is the entire result JSON "
    "on one line and will flood your context. If `upload` exits non-zero, nothing was "
    "written to the .md: run `tail -c 600 progress.sse` to see why — a refused upload "
    "leaves only its short JSON error there (`insufficient_credits` with the credits "
    "needed and left, `rate_limit`, a file or model the service refuses), a failed "
    "parse ends with an `error` event naming the failed pages — or run `status`. A "
    "parse is every page or nothing: when it failed, call `parse` for a fresh ticket "
    "and run `retry` with the same `f` — the pages that succeeded are kept and only the "
    "failed ones run again. A page the model blocked needs the fallback model: add "
    "`-F model=<id>` to `retry`, the second of `models` when there is one. Every figure is "
    "`![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` with an `<!-- page: N -->` "
    "marker before each page's content; coordinates are 0-1000 normalized to that page, "
    "y before x. Run `figures` to save each figure's crop as <name>.figures/figN.png — "
    "cropped by Ema Paper from its own render after its figure check, the picture the "
    "web reader shows. `metadata` (<name>.meta.json) adds the figures array with "
    "captions and each figure's `check`: `unverified` means the check could not confirm "
    "the box (the web reader marks it 'Crop not verified'), so look at that crop. "
    "Optional form fields on the upload: `-F model=<id>` (one of `models`; default: the "
    "first), `-F pages=1-5,8`, `-F force=true` (parse again, bypassing the dedup cache "
    "and a parse of the same file still running). A PDF and an image "
    "(jpg/png/webp/heic) both go to the same endpoint. `/v1/jobs/last` resolves to this "
    "ticket's own job; the explicit `/v1/jobs/<job_id>` routes work too (`job_id` is in "
    "every `progress` event and in `meta.job_id`). A dedup hit ends the stream at once. "
    "The ticket is good for one upload or one retry and then only for reading the job "
    "it created: once it is spent or expired, call `parse` again for a fresh one. "
    "Re-uploading the same file joins its parse while it runs and returns it once it "
    "has succeeded, so re-running a command your shell cut off costs nothing."
)

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
    """The MCP server and its one tool (docs/mcp.md § The one tool)."""
    server = MCPServer(
        "Ema Paper",
        instructions=(
            "Parse PDFs and images into the markdown document the Ema Paper web reader "
            "opens, with its figures. Call `parse` for a single-use upload ticket and "
            "ready-to-run curl commands, then run them in your shell: the file goes "
            "straight to the REST endpoint and the result lands on disk, never through "
            "this connection. " + COORDINATE_CONTRACT
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
        # `2026-08-21T15:00:00Z`, the shape docs/mcp.md shows.
        expires_at = ticket.expires_at.astimezone(UTC).replace(microsecond=0).isoformat()
        # The upload fails loudly on a refusal (`--fail-with-body` keeps the short error in
        # progress.sse) and every read with `-f`, which writes nothing on an error: an
        # error envelope never lands in, or overwrites, a document (docs/mcp.md).
        land = f'&& curl -sSf {auth} {base}/v1/jobs/last/result.md -o "${{f%.*}}.md"'
        send = f"curl -sSN --fail-with-body {auth} -H 'Accept: text/event-stream'"
        return {
            "token": token,
            "expires_at": expires_at.replace("+00:00", "Z"),
            "max_upload_bytes": settings.upload_max_bytes,
            "page_cap": settings.page_cap,
            # The ids `-F model=` takes; `GET /v1/models` refuses a ticket (docs/auth.md § 5).
            "models": offered_models(settings, PARSE.name),
            # `f` names the source once; every output file is derived from it, so parsing
            # several documents into one directory never collides on a generic name.
            "upload": f'f=doc.pdf; {send} -F file=@"$f" {base}/v1/parse -o progress.sse {land}',
            "retry": (
                f'f=doc.pdf; {send} -F file=@"$f" -F retry=true {base}/v1/parse '
                f"-o progress.sse {land}"
            ),
            "markdown": (
                f'f=doc.pdf; curl -sSf {auth} {base}/v1/jobs/last/result.md -o "${{f%.*}}.md"'
            ),
            "metadata": (
                f'f=doc.pdf; curl -sSf {auth} {base}/v1/jobs/last/result -o "${{f%.*}}.meta.json"'
            ),
            # Every placeholder of the landed .md, fetched as the crop the web reader shows.
            "figures": (
                f'f=doc.pdf; d="${{f%.*}}.figures"; mkdir -p "$d" && '
                r"grep -oE '!\[fig[0-9]+\]\(sightread://p[0-9]+/[0-9]+,[0-9]+,[0-9]+,[0-9]+\)' "
                r'"${f%.*}.md" | '
                r"sed -E 's#^!\[(fig[0-9]+)\]\(sightread://p([0-9]+)/([0-9,]+)\)$#\1 \2 \3#' | "
                f'while read -r n p b; do curl -sSf {auth} "{base}/v1/jobs/last/figures/$p/$b" '
                f'-o "$d/$n.png" || echo "no crop stored for $n"; done'
            ),
            "status": f"curl -sS {auth} {base}/v1/jobs/last",
            # A connector's parse is in no library, so the retention deletes it; how soon
            # is the deployment's own setting (docs/mcp.md § The one tool, docs/jobs.md
            # § Retention).
            "notes": (
                NOTES
                if settings.parse_retention_days == 0
                else f"{NOTES} A parse is kept {settings.parse_retention_days} days after it "
                "finishes and then deleted: the .md you landed is the copy that lasts."
            ),
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
        user = None
        if credential is not None:
            async with self._app.state.sessionmaker() as db:
                user = await resolve_bearer(db, credential)
        if user is None:
            response = error_response(
                401,
                "auth",
                "Invalid or missing bearer token",
                self._challenge(invalid=credential is not None),
            )
            await response(scope, receive, send)
            return

        token = _current_user_id.set(user.id)
        try:
            await self._transport(scope, receive, send)
        finally:
            _current_user_id.reset(token)


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
