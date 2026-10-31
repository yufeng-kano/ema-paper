# MCP server

A **thin shell over the REST service layer** — no parsing logic of its own, no separate auth store. The MCP endpoint does exactly one thing: mint a short-lived **upload ticket** and hand back copy-paste `curl` commands. The document bytes themselves never travel through MCP — base64 file content in tool arguments burns ~1.37× the file size in model tokens, so uploads go out-of-band over the existing REST data plane (same pattern as S3 presigned URLs).

This assumes the MCP client can run shell commands with network access to this host (Claude Code has it natively; claude.ai chat/cowork need "Allow network egress" enabled in sandbox settings). Clients without a shell are out of scope.

## Transport & auth

- Endpoint: `POST /mcp` (streamable HTTP, official `mcp` Python SDK, mounted inside the same FastAPI app).
- Auth: `Authorization: Bearer` with either an OAuth access token (Claude Connectors path — see [auth.md](./auth.md) § 4) or a project API key (scripts/self-hosted agents). Both resolve to a user, and that user's agent settings apply as they do to a Files upload — the `parse` agent's prompt and fallback switch, the `check` agent's switch and instructions ([agents.md](./agents.md)); the models are the deployment's ([billing.md](./billing.md) § Models).
- Never accept tokens in query strings.

## The one tool: `parse`

Input: none.

Output (JSON): a fresh single-use upload ticket ([auth.md](./auth.md) § 5), the models a parse may run, and ready-to-run commands, built from `APP_URL`:

```json
{
  "token": "srt_…",
  "expires_at": "2026-08-21T15:00:00Z",
  "max_upload_bytes": 134217728,
  "page_cap": 500,
  "models": ["<parse-model>", "<fallback-model>"],
  "upload": "f=doc.pdf; curl -sSN --fail-with-body -H 'Authorization: Bearer srt_…' -H 'Accept: text/event-stream' -F file=@\"$f\" https://<host>/v1/parse -o progress.sse && curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "retry": "…the upload command with -F retry=true…",
  "markdown": "f=doc.pdf; curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "metadata": "f=doc.pdf; curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result -o \"${f%.*}.meta.json\"",
  "figures": "f=doc.pdf; …every placeholder of the .md fetched from /v1/jobs/last/figures/… into \"${f%.*}.figures/figN.png\"…",
  "status": "curl -sS -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last",
  "notes": "…set f, read the .md not progress.sse, what a non-zero exit means, retry, figures, form fields, recovery…"
}
```

**A parse through the connector behaves as a Files upload does** (the owner's decision of 2026-10-31: the connector had missed the web's fixes). It is the same parse with the same settings, and what lands on disk is what the web reader opens:

- **The text is the reading copy.** The parse takes the format check whenever a Files upload would ([parsing.md](./parsing.md) § Format check): paragraphs a page break cut are joined and broken markup is fixed, the text unchanged; a check that fails fails the parse. `result.md` is that copy, as the library's `doc.md` starts from it.
- **The figures are the server's crops.** Each figure is cropped by Ema Paper from its own render after the figure check ([parsing.md](./parsing.md) § Figure crops), and the **figures** command saves every one the `.md` names as `<name>.figures/figN.png` — the picture the web reader shows, not one the agent has to crop again. The placeholders still carry their boxes, and `.meta.json` each figure's `check`: an `unverified` one is what the web reader marks *Crop not verified*.
- **A failed parse is retried, not repeated.** One failed page fails the parse ([parsing.md](./parsing.md) § Vision-only conversion), as on the web. **retry** — the upload command with `-F retry=true`, on a fresh ticket — sends the file to that failed parse again: its succeeded pages are kept and only the failed ones run, on the model `-F model=` names (the fallback for a page the first model blocked) or else the parse's own, as the web's retry dialog does ([jobs.md](./jobs.md) § Retrying failed pages).
- **The same bytes are never parsed twice by accident.** An upload of a file whose parse is still queued or running joins it, as the web library's does ([jobs.md](./jobs.md) § Dedup cache), so re-running a command the shell cut off costs nothing.

The commands:

- **upload** is the whole happy path in one command: set `f` to the file, and the command uploads it, waits on the SSE stream until the job is terminal, then lands the document **named after the source** — `paper.pdf` → `paper.md` (`${f%.*}.md`), so parsing several documents into one directory never collides. No JSON to unwrap; `<!-- page: N -->` markers map every passage to its page. The notes tell the agent to read the `.md` and **never print `progress.sse`** after a parse: its final `done` event is the entire result JSON on one line, which floods an agent's context.
- **Every command fails loudly and writes no error into a document.** The upload sends `--fail-with-body` and the reads `-f`, so a refusal or a failure exits non-zero and stops the chain. A refused upload (402 `insufficient_credits` with the credits needed and left, 429, a file or model the service refuses) leaves only its short JSON error in `progress.sse`; a failed parse ends the stream with an `error` event naming the failed pages. Either way the notes send the agent to the tail of `progress.sse` or to **status**, never to a `.md` holding an error envelope — and a read that fails, a spent ticket's after its hour among them, leaves an earlier `.md` as it was.
- `last` resolves to the job this ticket's upload created ([api.md](./api.md) § GET /v1/jobs/last*), so no command needs a job id filled in. The explicit `/v1/jobs/<job_id>` routes still exist for durable credentials; `job_id` is in every SSE `progress` event and in `meta.job_id`.
- **markdown** re-fetches the `.md` alone (a dropped chain, a second look). **metadata** lands `<name>.meta.json` — the structured remainder: `figures` with captions and their `check`, `pages`, `meta`. **figures** lands the crops. **status** answers "is it done / what failed".
- `notes` must spell out: what a non-zero exit means and where to read why, **retry** and when to name the fallback, **figures** and the `unverified` status, optional form fields (`model` — one of `models`, the first by default —, `pages` e.g. `1-5,8`, `force`), PDF or image (jpg/png/webp/heic) both go to the same endpoint, the page markers and the coordinate order, and the recovery rule — if the ticket is spent or expired, call `parse` again; re-uploading the same file returns its parse, finished or still running, without parsing it twice ([jobs.md](./jobs.md) § Dedup margin). A parse through the connector is in no one's library, so it is kept `PARSE_RETENTION_DAYS` (30) days after it finishes and then deleted ([jobs.md](./jobs.md) § Retention): the files the commands land on disk are the copy that lasts.

`models` lists the parse agent's models (`PARSE_MODEL`, then `PARSE_FALLBACK_MODEL` when set), the answer `GET /v1/models` gives a durable credential: a ticket is refused there ([auth.md](./auth.md) § 5), so the tool hands them over itself.

The server instructions and the tool description stay short and explicit about the coordinate contract: the placeholder as `sightread://pPAGE/YMIN,XMIN,YMAX,XMAX`, `bbox_format` `yxyx_norm1000`, and the crops the **figures** command saves.

There are no other tools. No base64 source, no MCP-side progress notifications, no result relay — the REST plane already does all three better (SSE, dedup, `-o` to disk).

## Claude Connectors flow (the reason the OAuth AS exists)

1. User adds `https://<host>/mcp` as a custom connector.
2. Claude discovers `/.well-known/oauth-protected-resource` → AS metadata → performs Dynamic Client Registration → browser consent (Google session) → token.
3. `parse` calls arrive with the OAuth bearer; the tickets it mints are scoped to that user, and usage lands in the same `usage_log` as REST calls.

## Implementation notes (as built)

- **Stateless streamable HTTP.** Every request carries its own bearer and gets its own transport, so a connector never depends on session affinity and a token can never be inherited from an earlier session. The SDK's session manager runs for the app's lifetime (FastAPI lifespan).
- **Auth is ours, not the SDK's.** A small ASGI guard in front of the transport resolves the bearer through the same code path as `/v1` and answers 401 with `WWW-Authenticate: Bearer resource_metadata="<APP_URL>/.well-known/oauth-protected-resource"` — the pointer that starts a connector's OAuth discovery (RFC 9728).
- **`parse`** resolves the caller to a user, mints the ticket via the shared ticket module ([auth.md](./auth.md) § 5 — TTL, mint rate limit, opportunistic cleanup live there, not in MCP), reads the parse agent's models from the settings, and formats the command strings from `APP_URL`. Nothing else: the check, the crops, the retry and the in-flight join are the data plane's ([api.md](./api.md)), so MCP still owns no parsing decision.
