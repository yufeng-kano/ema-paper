# MCP server

A **thin shell over the REST service layer** — no parsing logic of its own, no separate auth store. The MCP endpoint does exactly one thing: mint a short-lived **upload ticket** and hand back copy-paste `curl` commands. The document bytes themselves never travel through MCP — base64 file content in tool arguments burns ~1.37× the file size in model tokens, so uploads go out-of-band over the existing REST data plane (same pattern as S3 presigned URLs).

This assumes the MCP client can run shell commands with network access to this host (Claude Code has it natively; claude.ai chat/cowork need "Allow network egress" enabled in sandbox settings). Clients without a shell are out of scope.

## Transport & auth

- Endpoint: `POST /mcp` (streamable HTTP, official `mcp` Python SDK, mounted inside the same FastAPI app).
- Auth: `Authorization: Bearer` with either an OAuth access token (Claude Connectors path — see [auth.md](./auth.md) § 4) or a project API key (scripts/self-hosted agents). Both resolve to a user; that user's `parse` agent settings apply — its upstream (Kano Proxy or a provider connection), model and prompt ([agents.md](./agents.md)).
- Never accept tokens in query strings.

## The one tool: `parse`

Input: none.

Output (JSON): a fresh single-use upload ticket ([auth.md](./auth.md) § 5) plus ready-to-run commands, built from `APP_URL`:

```json
{
  "token": "srt_…",
  "expires_at": "2026-08-21T15:00:00Z",
  "max_upload_bytes": 134217728,
  "page_cap": 500,
  "upload": "f=doc.pdf; curl -sN -H 'Authorization: Bearer srt_…' -H 'Accept: text/event-stream' -F file=@\"$f\" https://<host>/v1/parse -o progress.sse && curl -s -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "markdown": "f=doc.pdf; curl -s -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "metadata": "f=doc.pdf; curl -s -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result -o \"${f%.*}.meta.json\"",
  "status": "curl -s -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last",
  "notes": "…set f, read the .md not progress.sse, inline crop boxes, form fields, recovery…"
}
```

- **upload** is the whole happy path in one command: set `f` to the file, and the command uploads it, waits on the SSE stream until the job is terminal, then lands the document **named after the source** — `paper.pdf` → `paper.md` (`${f%.*}.md`), so parsing several documents into one directory never collides. No JSON to unwrap; `<!-- page: N -->` markers map every passage to its page. The notes tell the agent to read the `.md` and **never print `progress.sse`**: its final `done` event is the entire result JSON on one line, which floods an agent's context (completion checks are `status` or a `grep` count, both in the notes).
- Crop coordinates ship inline: each `![figN](sightread://pPAGE/YMIN,XMIN,YMAX,XMAX)` placeholder in `result.md` carries its own 0-1000-normalized box, so cropping needs only that file plus a render of the page. The notes say so, or agents assume a second fetch is required.
- `last` resolves to the job this ticket's upload created ([api.md](./api.md) § GET /v1/jobs/last*), so no command needs a job id filled in. The explicit `/v1/jobs/<job_id>` routes still exist for durable credentials; `job_id` is in every SSE `progress` event and in `meta.job_id`.
- **markdown** re-fetches the `.md` alone (a dropped chain, a second look). **metadata** lands `<name>.meta.json` — the structured remainder: `figures` with captions, `pages`, `errors`, `meta`. **status** answers "is it done / what failed".
- `notes` must spell out: optional form fields (`model`, `pages` e.g. `1-5,8`, `force`), PDF or image (jpg/png/webp/heic) both go to the same endpoint, the page markers, and the recovery rule — if the ticket is spent or expired, call `parse` again; re-uploading the same file returns the cached result instantly ([jobs.md](./jobs.md) § Dedup).

Tool description stays short and explicit about the coordinate contract (`bbox_format`, `sightread://` placeholders, "you crop, we don't").

There are no other tools. No base64 source, no MCP-side progress notifications, no result relay — the REST plane already does all three better (SSE, dedup, `-o` to disk).

## Claude Connectors flow (the reason the OAuth AS exists)

1. User adds `https://<host>/mcp` as a custom connector.
2. Claude discovers `/.well-known/oauth-protected-resource` → AS metadata → performs Dynamic Client Registration → browser consent (Google session) → token.
3. `parse` calls arrive with the OAuth bearer; the tickets it mints are scoped to that user, and usage lands in the same `usage_log` as REST calls.

## Implementation notes (as built)

- **Stateless streamable HTTP.** Every request carries its own bearer and gets its own transport, so a connector never depends on session affinity and a token can never be inherited from an earlier session. The SDK's session manager runs for the app's lifetime (FastAPI lifespan).
- **Auth is ours, not the SDK's.** A small ASGI guard in front of the transport resolves the bearer through the same code path as `/v1` and answers 401 with `WWW-Authenticate: Bearer resource_metadata="<APP_URL>/.well-known/oauth-protected-resource"` — the pointer that starts a connector's OAuth discovery (RFC 9728).
- **`parse`** resolves the caller to a user, mints the ticket via the shared ticket module ([auth.md](./auth.md) § 5 — TTL, mint rate limit, opportunistic cleanup live there, not in MCP), and formats the command strings from `APP_URL`. Nothing else.
