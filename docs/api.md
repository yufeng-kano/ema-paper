# API

Two planes, one FastAPI app.

**Upstreams.** Vision calls go to **Kano Proxy** (the built-in default, with the key the user obtained through the connect flow, [auth.md](./auth.md) § 6) or to one of the user's **provider connections** — a named OpenAI-compatible Chat Completions endpoint: `name`, `base_url`, API key and the `report_cost` flag, nothing else. Kano Proxy is called at `{KANO_PROXY_BASE_URL}/openai/v1` (`/chat/completions`, `/models`) with `Authorization: Bearer <the user's Kano Proxy key>`; a connection at its own `base_url`. Which upstream and which model an agent runs is a per-agent setting ([agents.md](./agents.md)), resolved at enqueue time onto the job; `/v1/parse` has no per-request upstream override, and a per-request `model` overrides the stored one. Provider connections are reachable through the API only: the web Agent page runs every agent on Kano Proxy ([web.md](./web.md)). The `chat` agent is the exception to the Chat Completions format: it calls Kano Proxy's Anthropic-format route `{KANO_PROXY_BASE_URL}/anthropic/v1/messages` and accepts no provider connection ([chat.md](./chat.md)).

- **Data plane `/v1/*`** — authenticated by API key (`Authorization: Bearer sr_...`) or an OAuth access token ([auth.md](./auth.md)). This is the product. `POST /v1/parse` and the three `GET /v1/jobs/{id}*` routes additionally accept a single-use upload ticket (`srt_...`, [auth.md](./auth.md) § 5): one upload, then reads of the job that upload created, nothing else.
- **Control plane `/api/*`** — session cookie (Google OIDC), consumed only by the Nuxt web app.

`GET /healthz` is public and DB-free (`{"ok": true}`). It backs the compose healthchecks and is also routed to the API on the public origin, so an external monitor can reach it — Caddy's `@api` matcher includes it explicitly ([deployment.md](./deployment.md)). All responses are JSON unless SSE is requested. Errors use one shape:

```json
{ "error": { "type": "invalid_request | auth | rate_limit | payment | upstream | internal", "message": "..." } }
```

## Data plane

### POST /v1/parse

Multipart form (preferred) or JSON with base64 `source`.

| Field | Notes |
|-------|-------|
| `file` | required; PDF or image (jpg/png/webp/heic). Max **128 MB** (`UPLOAD_MAX_BYTES`), PDFs max **500 pages** (`PAGE_CAP`) |
| `model` | optional model id on the parse agent's upstream (Kano Proxy's or the selected connection's catalog); default: the model stored in the parse agent's settings. Neither → 400 ([agents.md](./agents.md) § Enqueue rules) |
| `pages` | optional selection, e.g. `"1-5,8"`; default all |
| `force` | optional bool; bypass dedup cache |

Responses:

- **200** — dedup cache hit ([jobs.md](./jobs.md) § Dedup): full result immediately.
- **202** — `{ "job_id": "...", "status": "queued" }`.
- With `Accept: text/event-stream` — SSE stream (below) instead; small documents feel synchronous.

The upload is hashed (SHA-256) while streaming to disk; the request must never buffer the whole file in memory.

The JSON form is held whole by `request.json()`, so the body-limit middleware gives it a ceiling of its own — the upload cap in base64 (four bytes per three) plus room for the envelope — enforced by the declared length and, for a chunked body that declares none, by counting the bytes as they arrive; only a multipart body on an upload route is exempt from the middleware. The multipart body is read through a **bounded stream** rather than Starlette's `request.form()`, on this route and on the library's. The parser spools each file part to disk as it arrives with no cap of its own, and `store_upload` — where `UPLOAD_MAX_BYTES` is really enforced — only sees the file once the whole body is parsed. A request that declares no length, or an untrue one, would otherwise be written out in full before anything could answer 413.

### GET /v1/jobs/{id} — status

`{ job_id, status: queued|running|succeeded|failed, page_count, pages_done, error?, created_at, finished_at? }`

### GET /v1/jobs/{id}/result — full result

```json
{
  "markdown": "…",
  "pages":   [ { "page": 1, "width_pt": 612, "height_pt": 792, "method": "vision", "error": null } ],
  "figures": [ { "id": "fig1", "page": 3, "bbox": [120, 60, 480, 940], "caption": "Figure 1: …" } ],
  "errors":  [ { "page": 7, "reason": "render failed" } ],
  "meta": {
    "job_id": "…",
    "agent": "parse",
    "model": "provider/model",
    "bbox_format": "yxyx_norm1000",
    "pipeline_version": 2,
    "sha256": "…",
    "cached": false
  }
}
```

`markdown` carries a `<!-- page: N -->` marker line before each page's content ([parsing.md](./parsing.md) § Page markers). Figure placeholders in markdown: `![fig1](sightread://p3/120,60,480,940)` with the caption verbatim on the next line. Cropping is the caller's job (add ~2% margin; denormalize `x_px = x/1000 * page_width_px`).

Results stored before the Kano Proxy migration still carry `meta.profile` (the OpenRouter preset profile they ran); new results carry `meta.agent` instead.

### GET /v1/jobs/{id}/result.md — markdown only

The result's `markdown` string as `text/markdown` — nothing to unwrap, ready to save as a file. Same 404 as `/result` while the job has no result.

### GET /v1/jobs/last* — the upload ticket's own job

`/v1/jobs/last`, `/v1/jobs/last/result` and `/v1/jobs/last/result.md` resolve `last` to the job the presented **upload ticket** is bound to, so a ticket caller (the MCP flow) never needs to extract a job id. Only tickets: a durable credential gets 400 and uses the explicit id routes.

### GET /v1/jobs/{id}/events — SSE progress

Events: `progress` (`{job_id, pages_done, page_count, page, method}` per finished page), then exactly one of `done` (full result payload) / `error`. Reconnectable at any time; a terminal job replays the final event immediately. Keepalive comment every 10 s.

### GET /v1/models

The live model catalog of the caller's **parse** upstream — `GET {KANO_PROXY_BASE_URL}/openai/v1/models` with the user's Kano Proxy key, or `GET {base_url}/models` of the selected connection — as `{data: [{id, name}]}`. No capability filtering (deferred, [agents.md](./agents.md)) and no server cache. 400 when Kano Proxy is selected but not connected. Also accepts a web session cookie. Never hard-code model ids.

## Control plane (session cookie, CSRF-safe: SameSite=Lax + custom header check on mutations)

| Route | Purpose |
|-------|---------|
| `GET /api/auth/login` → Google, `GET /api/auth/callback`, `POST /api/auth/logout` | OIDC flow ([auth.md](./auth.md)) |
| `GET /api/me` | current user (incl. `picture`, the Google avatar URL) + `limits` (`upload_max_bytes`, `page_cap`, `accepted_media_types`, `accepted_extensions`, `prompt_max_chars`) so the upload UI and the prompt dialog enforce the server's numbers rather than their own — the extensions matter because intake accepts a file by extension when the browser reports a generic media type for it |
| `GET/POST /api/keys`, `DELETE /api/keys/{id}` | API keys; `POST` returns the created key (with plaintext) exactly once, unwrapped; `GET` wraps as `{keys: []}` |
| `GET /api/kano` | Kano Proxy state: `{available, connected, masked}` — `available` is false when the deployment has no Kano Proxy configured; the key is masked only ([auth.md](./auth.md) § 6) |
| `GET /api/kano/connect`, `GET /api/kano/callback` | the connect flow: a browser navigation to Kano Proxy's consent and back, ending in a 302 to `/agents?kano=connected\|denied\|error` ([auth.md](./auth.md) § 6) |
| `DELETE /api/kano` | forget the stored Kano Proxy key (local only; it stays listed on Kano Proxy's Access page) |
| `GET /api/agents` | the agent registry with the caller's settings: `{agents: [{name, capabilities, input, output, default_prompt, settings: {connection_id, model, prompt_preset_id, effort}, bbox: {family, format}}]}` — `bbox` is the coordinate convention the pipeline will ask the saved model for ([parsing.md](./parsing.md) § Coordinate conventions), a fact about the model, not a setting ([agents.md](./agents.md)) |
| `PUT /api/agents/{agent}/settings` | partial update of one agent's `connection_id` (`null` = Kano Proxy), `model`, `prompt_preset_id`, `effort` (only the fields present change; `null` restores the default). A `connection_id` on the `chat` agent is refused (400). Changing `connection_id` without sending `model` clears the model — it belonged to the other upstream's catalog. The connection and the preset must be the caller's, and the preset must be for this agent. Answers the stored settings plus the `bbox` the new model implies. 404 for an unknown agent |
| `GET /api/agents/{agent}/models` | the live model catalog of that agent's saved upstream, `{data: [{id, name}]}` — what the Agent page's model picker lists. 400 when Kano Proxy is selected but not connected. For `chat` the catalog is filtered to Claude models ([agents.md](./agents.md)) |
| `GET /api/library/documents/{id}/content` | the document's editable copy from the workspace: `{markdown, revision, notes, notes_revision, has_source}` — materialized on first read ([workspace.md](./workspace.md)). **409** while the parse behind the document has no result yet: the viewer reads the partial result instead, and nothing can be edited until the parse finishes; also **409** when a file is not a regular file or is over `CONTENT_MAX_BYTES` |
| `PUT /api/library/documents/{id}/content` | `{markdown, base_revision}` → `{revision}`; **409** when `base_revision` is not the file's current revision |
| `PUT /api/library/documents/{id}/notes` | `{text, base_revision}` → `{revision}`, same rule |
| `GET /api/library/documents/{id}/revisions` | `{revisions: [{commit, author, message, created_at, files}]}`, newest first |
| `POST /api/library/documents/{id}/revisions/{commit}/revert` | write every path that commit touched as it was before it (where it lives now; a rename moved back, the library reconciled), as a new revision → `{revision}` of this document and the `{commit}` the revert made. **409** while a turn on this document is unfinished, or — for a revision that touched paths beyond this document — while any turn of the user is ([workspace.md](./workspace.md) § Revisions) |
| `GET /api/library/documents/{id}/conversations`, `POST` | the document's chat threads (`{conversations: [{id, title, created_at, updated_at}]}`); `POST` creates an empty one — under the user's row lock, the document checked again once it is held, so a delete racing it ends with a clean 404 or a thread the delete then takes with the document, never a 500 |
| `GET /api/conversations/{id}` | `{conversation, messages: [{id, role, content, turn_id, created_at}], turns: [{id, status, error, finished_at}]}` — an image block in a stored message (a page or figure a tool returned, kept for the model's replay) is sent as `{type: image}` without its bytes, inside `tool_result` content too, since the panel never renders them and a long thread would otherwise carry hundreds of megabytes; a user message's `context: selection` block is sent as the selected passage alone, without the line that introduces it to the model ([chat.md](./chat.md) § Prompt caching); `finished_at` is null while the turn is unfinished whatever its status says (a cancel marks the status first and the writer winds down after), and is what the panel's liveness goes by ([chat.md](./chat.md) § Conversations) |
| `POST /api/conversations/{id}/messages` | `{text, selection?, locale?}` (at most `CHAT_MESSAGE_MAX_CHARS` and `CHAT_SELECTION_MAX_CHARS` characters, **400** past them) → **202** `{turn_id, message_id}`: stores the user message (with the turn's id) and queues a turn that answers in `locale` (`en` or `zh-TW`, default `en`). 429 when the caller already has `MAX_CHAT_TURNS_PER_USER` turns running; 409 while this conversation is still answering; 400 when the chat agent has no model or Kano Proxy is not connected |
| `GET /api/conversations/{id}/events?after=N` | SSE: every `chat_events` row after `N`, then the live tail; `keepalive` comments while idle ([chat.md](./chat.md)). The replay is read in batches of a few hundred rows, each yielded before the next is read, so a long thread is never held whole in the API process |
| `POST /api/conversations/{id}/cancel` | stop the running turn after its current tool call |
| `DELETE /api/conversations/{id}` | delete the thread; the document and its revisions stay. 409 while a turn is queued or running: cancel it first |
| `GET /api/sandbox` | the caller's sandbox: `{state, last_used_at, shells, uptime_seconds, workspace: {bytes, quota_bytes, breakdown: [{path, bytes}]}}` ([sandbox.md](./sandbox.md)); `state` is `unavailable` on a deployment without a runner |
| `POST /api/sandbox/stop`, `POST /api/sandbox/reset` | stop the VM; stop and clear the package cache |
| `GET/POST /api/connections`, `PUT/DELETE /api/connections/{id}` | provider connections (`name`, `base_url`, API key, `report_cost`); key returned masked only; `POST`/`PUT` validate the endpoint + key with `GET {base_url}/models` before storing. Deleting a connection points every agent that used it back at Kano Proxy with no model. `report_cost` (bool, default `false`; optional on `POST`, partial on `PUT`) records the `usage.cost` the endpoint reports as an **estimate** ([parsing.md](./parsing.md) § Upstream usage); changing it alone does not re-validate the endpoint |
| `POST /api/connections/preview-models` | live model catalog (`{data: [{id, name}]}`) for the connection dialog, before or while a connection exists: `{base_url, api_key}` fetches with the candidate key, `{base_url, connection_id}` with that connection's stored key; the URL passes the same rules as saving ([auth.md](./auth.md) § 3); no server cache |
| `GET/POST /api/prompts`, `PUT/DELETE /api/prompts/{id}` | prompt presets (`{id, agent, name, text}`); `POST` takes `agent` (default `parse`, must be a registered agent; fixed afterwards); text capped at `SYSTEM_PROMPT_MAX_CHARS`; deleting a selected preset falls back to the agent's default prompt |
| `GET /api/usage?days=30` | per-day, per-model and per-agent aggregates of tokens + cost from `usage_log`, billed and estimated cost kept apart (§ Usage aggregates) |
| `GET /api/usage/conversations?days=30` | per-conversation totals: `{conversations: [{id, title, document_id, document_name, turns, prompt_tokens, cache_read_tokens, cache_write_tokens, completion_tokens, estimated_cost, unpriced_calls}]}` |
| `GET /api/jobs?limit=50` | recent jobs (history): `job_id`, `agent`, `kind`, `filename`, `status`, `model`, `page_count`, `pages_done`, `error`, timestamps — no per-job cost (usage is aggregated per day/model only); `GET /api/jobs/{id}/result` same payload as data plane, plus partial results (below) |
| `GET /api/jobs/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}` | one stored figure crop as `image/png`, addressed exactly as its `sightread://` placeholder addresses it; 404 when no crop was stored (old results, failed crop). Control plane only — the data plane ships coordinates, not image bytes ([parsing.md](./parsing.md) § Figure crops) |
| `GET /api/library` | the whole file library in one read: `{folders: [...], documents: [...]}`, both from one `REPEATABLE READ` snapshot: the request's session ends the transaction its authentication opened first (an isolation level applies to a connection's first transaction only), then opens the snapshot on the connection it procures next — one connection per request, so a burst of library reads cannot hold the pool with authentication connections while waiting for second ones. A document carries its own fields (`id`, `folder_id`, `name`, `job_id`) plus the parse's live state (`status`, `kind`, `model`, `page_count`, `pages_done`, `size_bytes`, `error`, `created_at`, `finished_at`) — the browser needs no second request to render a row or its progress |
| `POST /api/library/folders`, `PUT/DELETE /api/library/folders/{id}` | create (`name`, optional `parent_id`) / rename **and** move (partial: only the fields present change) / delete. Deleting cascades to the subtree; a `parent_id` inside the folder's own subtree is refused (a directory cannot contain itself). A name that is already taken is suffixed ` (2)` on create and on move, and answers 409 on a rename |
| `POST /api/library/documents` | **the web upload.** Multipart `file` + optional `folder_id`, streamed to disk by the same `jobs.intake` sequence `/v1/parse` runs, then a document row naming the job. The library name is decided from the filename *before* the parse is queued — a filename that cleans to nothing is filed as `upload`, as *file an existing parse* does — so no request can enqueue a billed parse and then fail on its name. 201 with the document; a dedup hit points at the cached job and is already `succeeded`, and a retry of an upload that already landed points at the job still running for it rather than starting a second parse ([jobs.md](./jobs.md) § Dedup). **409** when the upload could not be kept as the document's original (the disk, the tree): the row is deleted again and nothing is filed, the parse stays in the history to file from there, and a retry does not find a suffixed twin ([workspace.md](./workspace.md) § Layout) |
| `POST /api/library/documents/from-job` | **file an existing parse** — JSON `{job_id, folder_id?}`. A document row naming one of the caller's own jobs, named after the job's `filename` (` (2)` on a collision), with no upload and no new parse. 201 with the document. 404 for a job or folder that is not the caller's; 400 for a `failed` job, which has no result to file. A queued or running job is accepted — the document tracks it like an upload's ([web.md](./web.md) § History) |
| `PUT/DELETE /api/library/documents/{id}` | rename / move (`name`, `folder_id`) / delete the library entry; both answer 409 while a conversation about the file is still answering. Deleting removes the entry, never the job or its result ([database.md](./database.md) § Rules); it answers 409 while a conversation about the file is still answering, as does deleting a folder that holds such a file |
| `GET /api/library/documents/{id}/result` | the document's result, same payload as the data plane's `/result`, plus partial results (below) |
| `GET /api/library/documents/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}` | the document's stored figure crop, same contract as the jobs route above |

### Partial results (control plane only)

While a job is still `queued` or `running`, the two control-plane result routes answer **200 with a partial result** instead of 404: the finished pages' stored markdown ([jobs.md](./jobs.md) § Progress) assembled exactly like the final document — page markers, renumbered figure placeholders — with `meta.partial: true`, `pages` listing only the finished pages, and `meta.page_count`/`meta.pages_done` carrying live progress. Figure ids are per-snapshot (a page finishing out of order renumbers them), so nothing may cache a partial result; the viewer re-reads until `partial` disappears. The data plane is unchanged: `/v1/jobs/{id}/result` stays 404 until the job finishes, and SSE remains its progressive interface.

### Usage aggregates

`GET /api/usage` answers `{days, per_day: [...], per_model: [...], per_agent: [...]}`; a `per_day` entry carries `date` and `agents` (the agent names that ran that day), a `per_model` entry `model`, a `per_agent` entry `agent`, and all carry the same figures:

| Field | Meaning |
|-------|---------|
| `calls` | `usage_log` rows (one per upstream call) |
| `prompt_tokens`, `completion_tokens` | summed over every call, priced or not |
| `cost` | **billed**: the sum of OpenRouter's own cost (`cost_source = openrouter`). Only historical rows carry it — OpenRouter was removed and no new call is billed — so for recent periods this is 0 with no billed calls behind it |
| `estimated_cost`, `estimated_calls` | the sum and count of costs Kano Proxy reported (always recorded) or a connection reported with `report_cost` on (`cost_source = estimated`) — an estimate, never added into `cost` |
| `unpriced_calls` | calls whose cost is unknown (`cost_source = unknown`); counted, never priced as 0 |
| `cache_read_tokens`, `cache_write_tokens` | prompt-cache reads and writes, chat rows only ([chat.md](./chat.md) § Prompt caching) |

`calls - estimated_calls - unpriced_calls` is the number of billed calls, so a client can tell "OpenRouter billed $0" from "nothing here went through OpenRouter". The per-model grouping is by model id alone, as before; one id used on two upstreams lands in one entry with its figures still separated by source.

### Why the web upload is a control-plane route

`POST /api/library/documents` duplicates no logic: it reads a multipart body and hands it to `jobs.intake.submit_parse`, exactly as `POST /v1/parse` does. It exists as a separate route because the two planes authenticate differently and must keep doing so — the data plane is bearer-only (API key, OAuth token, upload ticket) and the control plane is session-cookie plus the `X-Requested-With` CSRF pairing. Teaching `/v1/parse` to accept a cookie would put a CSRF-shaped hole in the product's own API; teaching the web app to mint itself an API key would put a durable credential in a browser. So the browser posts to the control plane with the credential it already has, and the bytes take the same path every other `/api/*` request takes: browser → Caddy → FastAPI. Nothing document-shaped enters the Nuxt server ([web.md](./web.md) § Rules).

## Limits (defaults, env-overridable)

- `UPLOAD_MAX_BYTES` = 134217728 (128 MB) — enforced in the app **and** in Caddy; keep in sync ([deployment.md](./deployment.md)).
- `PAGE_CAP` = 500, `MAX_JOBS_PER_USER` = 2 concurrent, `VISION_CONCURRENCY_PER_JOB` = 8.
- `SYSTEM_PROMPT_MAX_CHARS` = 8000 — cap on a prompt preset's text.
- `FIGURES_PER_PAGE_MAX` = 40 — cap on stored figure crops per page ([parsing.md](./parsing.md) § Figure crops).
- Request bodies are capped at twice `CONTENT_MAX_BYTES` plus 64 KiB (**413** past it, by declared length or as the bytes arrive) on every route but the two that stream an upload to disk under `UPLOAD_MAX_BYTES` (`POST /v1/parse`, `POST /api/library/documents`); a multipart label on any other route buys no exemption.
- `UPSTREAM_RESPONSE_MAX_BYTES` = 33554432 (32 MB) — cap on any upstream response body (model catalogs and vision completions); beyond it the call fails as `upstream` ([parsing.md](./parsing.md) § Upstream usage).
- 429 with `Retry-After` when the per-user job cap is hit; upstream 402 (the upstream's credits or allowance exhausted) maps to `payment` and fails only the affected pages.
- `MAX_CHAT_TURNS_PER_USER` = 4 concurrent chat turns (and shells); `CHAT_MAX_OUTPUT_TOKENS` = 64000; `CHAT_MAX_TOOL_CALLS_PER_TURN` = 50; `CHAT_TURN_TIMEOUT_SECONDS` = 900; `CHAT_STREAM_TIMEOUT_SECONDS` = 900; `CHAT_WRITER_GRACE_SECONDS` = 60; `REVERT_MAX_BYTES` = 67108864; `REVERT_MAX_PATHS` = 2000; `WORKSPACE_GIT_TIMEOUT_SECONDS` = 60; `CHAT_MESSAGE_MAX_CHARS` = 32000; `CHAT_SELECTION_MAX_CHARS` = 16000; `CHAT_TOOL_RESULT_MAX_CHARS` = 20000; `CHAT_IMAGE_ROUND_MAX_BYTES` = 33554432; `CHAT_IMAGE_TURN_MAX_BYTES` = 134217728; `SANDBOX_CONTROL_TIMEOUT_SECONDS` = 180 ([chat.md](./chat.md), [sandbox.md](./sandbox.md)).
- `WORKSPACE_QUOTA_BYTES` = 10 GiB per user ([workspace.md](./workspace.md) § Quota).
