# MCP server

A **thin shell over the REST service layer** — no parsing logic of its own, no separate auth store. The MCP endpoint mints short-lived **tickets** and hands back copy-paste `curl` commands: an upload ticket for a parse (`parse`), a document ticket for reading a document of the library or saving its text and notes (`read_document`, `write_document`); the one answer it gives inline is the library's index (`list_library`). The document bytes themselves never travel through MCP — base64 file content in tool arguments burns ~1.37× the file size in model tokens, so uploads go out-of-band over the existing REST data plane (same pattern as S3 presigned URLs).

This assumes the MCP client can run shell commands with network access to this host (Claude Code has it natively; claude.ai chat/cowork need "Allow network egress" enabled in sandbox settings). Clients without a shell are out of scope.

## Transport & auth

- Endpoint: `POST /mcp` (streamable HTTP, official `mcp` Python SDK, mounted inside the same FastAPI app).
- Auth: `Authorization: Bearer` with either an OAuth access token (Claude Connectors path — see [auth.md](./auth.md) § 4) or a project API key (scripts/self-hosted agents). Both resolve to a user, and that user's agent settings apply as they do to a Files upload — the `parse` agent's prompt and fallback switch, the `check` agent's switch and instructions ([agents.md](./agents.md)); the models are the deployment's ([billing.md](./billing.md) § Models).
- Never accept tokens in query strings.

## The `parse` tool

Input: none.

Output (JSON): a fresh single-use upload ticket ([auth.md](./auth.md) § 5), the models a parse may run, and ready-to-run commands, built from `APP_URL`:

```json
{
  "token": "srt_…",
  "expires_at": "2026-08-21T15:00:00Z",
  "max_upload_bytes": 134217728,
  "page_cap": 500,
  "models": ["<parse-model>", "<fallback-model>"],
  "upload": "f=doc.pdf; s=\"${f%.*}.progress.sse\"; rm -f \"$s\"; curl -sSN -H 'Authorization: Bearer srt_…' -H 'Accept: text/event-stream' -F file=@\"$f\" https://<host>/v1/parse -o \"$s\" && curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "retry": "…the upload command with -F retry=true…",
  "markdown": "f=doc.pdf; curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "metadata": "f=doc.pdf; curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result -o \"${f%.*}.meta.json\"",
  "figures": "f=doc.pdf; …every placeholder of the .md fetched from /v1/jobs/last/figures/… into \"${f%.*}.figures/figN.png\", non-zero when a fetch failed…",
  "status": "curl -sS -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last",
  "notes": "…set f, read the .md not <name>.progress.sse, what a non-zero exit means, retry, figures, form fields, recovery…"
}
```

**A parse through the connector behaves as a Files upload does** (the owner's decision of 2026-10-31: the connector had missed the web's fixes). It is the same parse with the same settings, and what lands on disk is what the web reader opens:

- **The text is the reading copy.** The parse takes the format check whenever a Files upload would ([parsing.md](./parsing.md) § Format check): paragraphs a page break cut are joined and broken markup is fixed, the text unchanged; a check that fails fails the parse. `result.md` is that copy, as the library's `doc.md` starts from it.
- **The figures are the server's crops.** Each figure is cropped by Ema Paper from its own render after the figure check ([parsing.md](./parsing.md) § Figure crops), and the **figures** command saves every one the `.md` names as `<name>.figures/figN.png` — the picture the web reader shows, not one the agent has to crop again. The placeholders still carry their boxes, and `.meta.json` each figure's `check`: an `unverified` one is what the web reader marks *Crop not verified*.
- **A failed parse is retried, not repeated.** One failed page fails the parse ([parsing.md](./parsing.md) § Vision-only conversion), as on the web. **retry** — the upload command with `-F retry=true`, on a fresh ticket — with the same `f` and the same `-F pages=` as the upload, sends the file to that failed parse again: its succeeded pages are kept and only the failed ones run, on the model `-F model=` names (the fallback for a page the first model blocked) or else the parse's own, as the web's retry dialog does ([jobs.md](./jobs.md) § Retrying failed pages).
- **The same bytes are never parsed twice by accident.** An upload of a file whose parse is still queued or running joins it, as the web library's does ([jobs.md](./jobs.md) § Dedup cache), so re-running a command the shell cut off costs nothing.

The commands:

- **upload** is the whole happy path in one command: set `f` to the file, and the command uploads it, waits on the SSE stream until the job is terminal, then lands the document **named after the source** — `paper.pdf` → `paper.md` (`${f%.*}.md`), so parsing several documents into one directory never collides. No JSON to unwrap; `<!-- page: N -->` markers map every passage to its page — a paragraph the check joined across a page break, to the page it starts on ([parsing.md](./parsing.md) § Page markers). The stream goes to `<name>.progress.sse`, removed first, so what is there is always this run's and two documents never share one. The notes tell the agent to read the `.md` and **never print the progress file** after a parse: its final `done` event is the entire result JSON on one line, which floods an agent's context.
- **Every command fails loudly and writes no error into a document**, with any `curl` (no option newer than the oldest ones in use). The upload's own `curl` lands whatever the server answers in the progress file; the read that follows it fails with `-f`, which writes nothing on an HTTP error, so the chain exits non-zero. A refused upload (402 `insufficient_credits` with the credits needed and left, 429, a file or model the service refuses) leaves only its short JSON error in the progress file and its ticket unspent, so the read gets 409; a failed parse ends the stream with an `error` event naming the failed pages, and the read gets 404. Either way the notes send the agent to curl's own message, the tail of the progress file or **status**, never to a `.md` holding an error envelope — and a read that fails, a spent ticket's after its hour among them, leaves an earlier `.md` as it was.
- `last` resolves to the job this ticket's upload created ([api.md](./api.md) § GET /v1/jobs/last*), so no command needs a job id filled in. The explicit `/v1/jobs/<job_id>` routes still exist for durable credentials; `job_id` is in every SSE `progress` event and in `meta.job_id`.
- **markdown** re-fetches the `.md` alone (a dropped chain, a second look). **metadata** lands `<name>.meta.json` — the structured remainder: `figures` with captions and their `check`, `pages`, `meta`. **figures** lands the crops: it needs the `.md` and the ticket that made it, names each figure the parse holds no crop for (404), and exits non-zero when the `.md` is missing or any other fetch failed. **status** answers "is it done / what failed".
- `notes` must spell out: what a non-zero exit means and where to read why, **retry** with the upload's own `pages` and when to name the fallback, **figures** and the `unverified` status, optional form fields (`model` — one of `models`, the first by default —, `pages` e.g. `1-5,8`, `force`), PDF or image (jpg/png/webp/heic) both go to the same endpoint, the page markers and the coordinate order, and the recovery rule — if the ticket is spent or expired, call `parse` again; re-uploading the same file returns its parse, finished or still running, without parsing it twice ([jobs.md](./jobs.md) § Dedup margin). A parse through the connector is in no one's library, so it is kept `PARSE_RETENTION_DAYS` (30) days after it finishes and then deleted ([jobs.md](./jobs.md) § Retention): the files the commands land on disk are the copy that lasts.

`models` lists the parse agent's models (`PARSE_MODEL`, then `PARSE_FALLBACK_MODEL` when set), the answer `GET /v1/models` gives a durable credential: a ticket is refused there ([auth.md](./auth.md) § 5), so the tool hands them over itself.

The server instructions and the tool description stay short and explicit about the coordinate contract: the placeholder as `sightread://pPAGE/YMIN,XMIN,YMAX,XMAX`, `bbox_format` `yxyx_norm1000`, and the crops the **figures** command saves.

No base64 source, no MCP-side progress notifications, no result relay — the REST plane already does all three better (SSE, dedup, `-o` to disk).

## The library tools

Since 2026-11-01 (the owner's decision: papers are read and annotated in Ema Paper, then handed to Claude — a deck made in claude.ai, Claude Code's own work), the connector also reads the Files library and saves a document's text and notes back. The same rule as `parse`: the tools mint and format, the bytes go over the REST routes ([api.md](./api.md) § The library on the data plane) straight to disk, and the agent is assumed to have a shell with network access. Each tool needs the grant's `library` scope ([auth.md](./auth.md) § 4): a connector approved before it existed gets a tool error saying to remove it in Claude's settings and add it again. An API key has it.

**`list_library`** — input: `folder` (optional, a folder's `path` as this tool shows it, e.g. `Papers/2024`: that folder and everything below it) and `name` (optional, part of a document's name, any case). Output, inline: `{folders: [{id, path}], documents: [{id, name, path, kind, status, page_count}], total, truncated}` — the live library (no Trash) ordered by path, at most `MCP_LIBRARY_LIST_MAX_ITEMS` (300) documents; `truncated` says more matched, with `total` how many, and the description says to narrow by `folder` or `name`. Inline because it is the index the agent picks a document from: rows, never content. A `folder` that matches no live folder is a tool error that says to call the tool without it and use a `path` under `folders`.

**`read_document`** — input: `document_id`. Output: a `read` document ticket ([auth.md](./auth.md) § 7), `expires_at`, `document` (`{id, name, path, kind, status, page_count, original}`) and the commands:

- **download** — for a parse or a markdown document: `d=` names a local directory (the document's name, a `/` in it as `-`, quoted as one shell word), and the command lands, in that directory, `doc.md` and `notes.md` as the web reader holds them, `document.json` (the row), and under `figures/` the crop of every `sightread://` placeholder in either file as `p<page>_<ymin>_<xmin>_<ymax>_<xmax>.png` (the name the workspace gives it) and every inserted image as `img-<hash>.png|jpg` — the name its `![…](figures/img-…)` link already points at, so the markdown renders where it landed. It saves each file's ETag beside it (`.doc.md.etag`, `.notes.md.etag`) for the save. A placeholder with no stored crop is named and skipped; any other failed fetch, a missing file included, makes the command exit non-zero. Downloading again replaces `doc.md` and `notes.md` in `d`. For a file of the library, **download** lands the file itself under its name, and there is nothing to save.
- **original** — a parse's original upload as `source.<ext>` in `d`, present only when one is kept: separate, as it can be the size of the upload.
- `notes` spell out the layout above, that a parse still queued or running has no `doc.md` yet (409), that the placeholder's coordinates are `yxyx_norm1000`, and that saving goes through `write_document`.

**`write_document`** — input: `document_id`. Output: a `write` document ticket, `expires_at`, `document`, and **save**: with `d` as **download** used it and `f` set to `doc.md` or `notes.md`, the command `PUT`s the local file with `If-Match` from its `.etag` and, on 200, stores the new ETag, so the next save follows on; anything else prints the server's short JSON error and exits non-zero, the local file untouched. `notes` spell out: edit the downloaded files in place; keep the `<!-- page: N -->` markers and the figure placeholders as they are, since the web reader draws them; a new `figures/img-…` link has no image behind it; on **409** `revision_conflict` the file changed in Ema Paper since the download (the reader or its Agent edited it) — download it again into another `d`, carry the edits over and save from there; every save is one revision of the document's history, authored `api`. A file of the library is a tool error: it has no text to save.

The tools never take or return the text itself, so a document's size never reaches the model's context through MCP; the agent reads the landed files with its own tools, in the parts it needs.


## Claude Connectors flow (the reason the OAuth AS exists)

1. User adds `https://<host>/mcp` as a custom connector.
2. Claude discovers `/.well-known/oauth-protected-resource` → AS metadata → performs Dynamic Client Registration → browser consent (Google session) → token.
3. `parse` calls arrive with the OAuth bearer; the tickets it mints are scoped to that user, and usage lands in the same `usage_log` as REST calls.

## Implementation notes (as built)

- **Stateless streamable HTTP.** Every request carries its own bearer and gets its own transport, so a connector never depends on session affinity and a token can never be inherited from an earlier session. The SDK's session manager runs for the app's lifetime (FastAPI lifespan).
- **Auth is ours, not the SDK's.** A small ASGI guard in front of the transport resolves the bearer through the same code path as `/v1` and answers 401 with `WWW-Authenticate: Bearer resource_metadata="<APP_URL>/.well-known/oauth-protected-resource"` — the pointer that starts a connector's OAuth discovery (RFC 9728).
- **The library tools** resolve the caller and its scope in the same guard, read the library through the control plane's own reads (the one snapshot, the live rows, the document's own liveness) and mint through the document ticket module ([auth.md](./auth.md) § 7). The commands use options of any `curl` the `parse` commands may meet (`--etag-save` since 7.68), are written for bash, dash and zsh, and quote every name the reader gave as one shell word.
- **`parse`** resolves the caller to a user, mints the ticket via the shared ticket module ([auth.md](./auth.md) § 5 — TTL, mint rate limit, opportunistic cleanup live there, not in MCP), reads the parse agent's models from the settings, and formats the command strings from `APP_URL`. Nothing else: the check, the crops, the retry and the in-flight join are the data plane's ([api.md](./api.md)), so MCP still owns no parsing decision.
