# API

Two planes, one FastAPI app.

**Upstreams.** Vision calls go to OpenRouter (the built-in default, billed to the user's own OpenRouter key) or to one of the user's **provider connections** — a complete profile: a named OpenAI-compatible Chat Completions endpoint (`base_url` + API key + a fixed `model` from that endpoint's catalog), e.g. a kano-proxy `/openai/v1` base. The active upstream is the user's `default_connection_id` setting (`NULL` = OpenRouter) and is resolved at enqueue time onto the job; `/v1/parse` has no per-request connection override yet. Preset profiles and the `/v1/models` catalog are OpenRouter-only; a custom connection runs its own stored model (a per-request `model` still overrides it) with the default (or preset) prompt. `default_model`/`default_profile` in user settings apply to the OpenRouter path only.

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
| `model` | optional model id on the active upstream (OpenRouter, or the default connection's catalog); default from user settings on OpenRouter, or the connection's own stored model |
| `profile` | optional preset profile id; mutually exclusive with raw `model` + `bbox_prompt` overrides ([parsing.md](./parsing.md)); refused (400) when the user's default connection is a custom one |
| `pages` | optional selection, e.g. `"1-5,8"`; default all |
| `force` | optional bool; bypass dedup cache |

Responses:

- **200** — dedup cache hit ([jobs.md](./jobs.md) § Dedup): full result immediately.
- **202** — `{ "job_id": "...", "status": "queued" }`.
- With `Accept: text/event-stream` — SSE stream (below) instead; small documents feel synchronous.

The upload is hashed (SHA-256) while streaming to disk; the request must never buffer the whole file in memory.

The multipart body is read through a **bounded stream** rather than Starlette's `request.form()`, on this route and on the library's. The parser spools each file part to disk as it arrives with no cap of its own, and `store_upload` — where `UPLOAD_MAX_BYTES` is really enforced — only sees the file once the whole body is parsed. A request that declares no length, or an untrue one, would otherwise be written out in full before anything could answer 413.

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
    "model": "google/gemini-…",
    "profile": "gemini-yxyx",
    "bbox_format": "yxyx_norm1000",
    "pipeline_version": 2,
    "sha256": "…",
    "cached": false
  }
}
```

`markdown` carries a `<!-- page: N -->` marker line before each page's content ([parsing.md](./parsing.md) § Page markers). Figure placeholders in markdown: `![fig1](sightread://p3/120,60,480,940)` with the caption verbatim on the next line. Cropping is the caller's job (add ~2% margin; denormalize `x_px = x/1000 * page_width_px`).

### GET /v1/jobs/{id}/result.md — markdown only

The result's `markdown` string as `text/markdown` — nothing to unwrap, ready to save as a file. Same 404 as `/result` while the job has no result.

### GET /v1/jobs/last* — the upload ticket's own job

`/v1/jobs/last`, `/v1/jobs/last/result` and `/v1/jobs/last/result.md` resolve `last` to the job the presented **upload ticket** is bound to, so a ticket caller (the MCP flow) never needs to extract a job id. Only tickets: a durable credential gets 400 and uses the explicit id routes.

### GET /v1/jobs/{id}/events — SSE progress

Events: `progress` (`{job_id, pages_done, page_count, page, method}` per finished page), then exactly one of `done` (full result payload) / `error`. Reconnectable at any time; a terminal job replays the final event immediately. Keepalive comment every 10 s.

### GET /v1/models

Also accepts a web session cookie (with `GET /v1/profiles`, the two read-only routes the web app may call on the data plane). OpenRouter model catalog filtered to `architecture.input_modalities` containing `"image"`, cached server-side ~1 h. Each entry carries `recommended: true` when a preset profile targets it. Never hard-code model ids.

### GET /v1/profiles

Preset profiles (id, name, model, bbox_format, description).

## Control plane (session cookie, CSRF-safe: SameSite=Lax + custom header check on mutations)

| Route | Purpose |
|-------|---------|
| `GET /api/auth/login` → Google, `GET /api/auth/callback`, `POST /api/auth/logout` | OIDC flow ([auth.md](./auth.md)) |
| `GET /api/me` | current user (incl. `picture`, the Google avatar URL) + settings (`default_model`, `default_profile`, `default_connection_id`, `prompt_preset_id`) + the shipped default prompt (`defaults.system_prompt`) + whether an OpenRouter key is stored (masked, never the value) + `limits` (`upload_max_bytes`, `page_cap`, `accepted_media_types`, `accepted_extensions`) so the upload UI enforces the server's numbers rather than its own — the extensions matter because intake accepts a file by extension when the browser reports a generic media type for it |
| `GET/POST /api/keys`, `DELETE /api/keys/{id}` | API keys; `POST` returns the created key (with plaintext) exactly once, unwrapped; `GET` wraps as `{keys: []}` |
| `GET/PUT/DELETE /api/openrouter-key` | read masked form / store (validated against `GET https://openrouter.ai/api/v1/key` before save) / remove |
| `GET/POST /api/connections`, `PUT/DELETE /api/connections/{id}` | provider connections (a profile: `name`, `base_url`, API key, `model`, `report_cost`); key returned masked only; `POST` requires `model` and `POST`/`PUT` validate the endpoint + key with `GET {base_url}/models` before storing. `report_cost` (bool, default `false`; optional on `POST`, partial on `PUT`) records the `usage.cost` the endpoint reports as an **estimate** ([parsing.md](./parsing.md) § Upstream usage); changing it alone does not re-validate the endpoint |
| `POST /api/connections/preview-models` | live model catalog (`{data: [{id, name}]}`) for the connection dialog, before or while a connection exists: `{base_url, api_key}` fetches with the candidate key, `{base_url, connection_id}` with that connection's stored key; the URL passes the same rules as saving ([auth.md](./auth.md) § 3); no server cache |
| `GET/POST /api/prompts`, `PUT/DELETE /api/prompts/{id}` | prompt presets (`{id, name, text}`); text capped at `SYSTEM_PROMPT_MAX_CHARS`; deleting the selected preset falls back to the default prompt |
| `PUT /api/settings` | default model / profile (OpenRouter-only defaults) / `default_connection_id` / `prompt_preset_id` (partial update: only the fields present in the body change; `null` restores the respective default). `default_profile` is refused when a custom connection is selected — profiles run on OpenRouter only; a connection's model lives on the connection itself |
| `GET /api/usage?days=30` | per-day and per-model aggregates of tokens + cost from `usage_log`, billed and estimated cost kept apart (§ Usage aggregates) |
| `GET /api/jobs?limit=50` | recent jobs (history): `job_id`, `kind`, `filename`, `status`, `model`, `profile`, `page_count`, `pages_done`, `error`, timestamps — no per-job cost (usage is aggregated per day/model only); `GET /api/jobs/{id}/result` same payload as data plane, plus partial results (below) |
| `GET /api/jobs/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}` | one stored figure crop as `image/png`, addressed exactly as its `sightread://` placeholder addresses it; 404 when no crop was stored (old results, failed crop). Control plane only — the data plane ships coordinates, not image bytes ([parsing.md](./parsing.md) § Figure crops) |
| `GET /api/library` | the whole file library in one read: `{folders: [...], documents: [...]}`. A document carries its own fields (`id`, `folder_id`, `name`, `job_id`) plus the parse's live state (`status`, `kind`, `model`, `page_count`, `pages_done`, `size_bytes`, `error`, `created_at`, `finished_at`) — the browser needs no second request to render a row or its progress |
| `POST /api/library/folders`, `PUT/DELETE /api/library/folders/{id}` | create (`name`, optional `parent_id`) / rename **and** move (partial: only the fields present change) / delete. Deleting cascades to the subtree; a `parent_id` inside the folder's own subtree is refused (a directory cannot contain itself). A name that is already taken is suffixed ` (2)` on create and on move, and answers 409 on a rename |
| `POST /api/library/documents` | **the web upload.** Multipart `file` + optional `folder_id`, streamed to disk by the same `jobs.intake` sequence `/v1/parse` runs, then a document row naming the job. 201 with the document; a dedup hit points at the cached job and is already `succeeded`, and a retry of an upload that already landed points at the job still running for it rather than starting a second parse ([jobs.md](./jobs.md) § Dedup) |
| `POST /api/library/documents/from-job` | **file an existing parse** — JSON `{job_id, folder_id?}`. A document row naming one of the caller's own jobs, named after the job's `filename` (` (2)` on a collision), with no upload and no new parse. 201 with the document. 404 for a job or folder that is not the caller's; 400 for a `failed` job, which has no result to file. A queued or running job is accepted — the document tracks it like an upload's ([web.md](./web.md) § History) |
| `PUT/DELETE /api/library/documents/{id}` | rename / move (`name`, `folder_id`) / delete the library entry. Deleting removes the entry, never the job or its result ([database.md](./database.md) § Rules) |
| `GET /api/library/documents/{id}/result` | the document's result, same payload as the data plane's `/result`, plus partial results (below) |
| `GET /api/library/documents/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}` | the document's stored figure crop, same contract as the jobs route above |

### Partial results (control plane only)

While a job is still `queued` or `running`, the two control-plane result routes answer **200 with a partial result** instead of 404: the finished pages' stored markdown ([jobs.md](./jobs.md) § Progress) assembled exactly like the final document — page markers, renumbered figure placeholders — with `meta.partial: true`, `pages` listing only the finished pages, and `meta.page_count`/`meta.pages_done` carrying live progress. Figure ids are per-snapshot (a page finishing out of order renumbers them), so nothing may cache a partial result; the viewer re-reads until `partial` disappears. The data plane is unchanged: `/v1/jobs/{id}/result` stays 404 until the job finishes, and SSE remains its progressive interface.

### Usage aggregates

`GET /api/usage` answers `{days, per_day: [...], per_model: [...]}`; a `per_day` entry carries `date`, a `per_model` entry `model`, and both carry the same figures:

| Field | Meaning |
|-------|---------|
| `calls` | `usage_log` rows (one per upstream call) |
| `prompt_tokens`, `completion_tokens` | summed over every call, priced or not |
| `cost` | **billed**: the sum of OpenRouter's own cost (`cost_source = openrouter`) — the same number this field always carried, since connection calls used to record 0 |
| `estimated_cost`, `estimated_calls` | the sum and count of costs a connection reported with `report_cost` on (`cost_source = estimated`) — an estimate, never added into `cost` |
| `unpriced_calls` | calls whose cost is unknown (`cost_source = unknown`); counted, never priced as 0 |

`calls - estimated_calls - unpriced_calls` is the number of billed calls, so a client can tell "OpenRouter billed $0" from "nothing here went through OpenRouter". The per-model grouping is by model id alone, as before; one id used on both OpenRouter and a connection lands in one entry with its figures still separated by source.

### Why the web upload is a control-plane route

`POST /api/library/documents` duplicates no logic: it reads a multipart body and hands it to `jobs.intake.submit_parse`, exactly as `POST /v1/parse` does. It exists as a separate route because the two planes authenticate differently and must keep doing so — the data plane is bearer-only (API key, OAuth token, upload ticket) and the control plane is session-cookie plus the `X-Requested-With` CSRF pairing. Teaching `/v1/parse` to accept a cookie would put a CSRF-shaped hole in the product's own API; teaching the web app to mint itself an API key would put a durable credential in a browser. So the browser posts to the control plane with the credential it already has, and the bytes take the same path every other `/api/*` request takes: browser → Caddy → FastAPI. Nothing document-shaped enters the Nuxt server ([web.md](./web.md) § Rules).

## Limits (defaults, env-overridable)

- `UPLOAD_MAX_BYTES` = 134217728 (128 MB) — enforced in the app **and** in Caddy; keep in sync ([deployment.md](./deployment.md)).
- `PAGE_CAP` = 500, `MAX_JOBS_PER_USER` = 2 concurrent, `VISION_CONCURRENCY_PER_JOB` = 8.
- `SYSTEM_PROMPT_MAX_CHARS` = 8000 — cap on a prompt preset's text.
- `FIGURES_PER_PAGE_MAX` = 40 — cap on stored figure crops per page ([parsing.md](./parsing.md) § Figure crops).
- `UPSTREAM_RESPONSE_MAX_BYTES` = 33554432 (32 MB) — cap on any upstream response body (model catalogs and vision completions); beyond it the call fails as `upstream` ([parsing.md](./parsing.md) § Upstream usage).
- 429 with `Retry-After` when the per-user job cap is hit; upstream 402 (OpenRouter credits exhausted) maps to `payment` and fails only the affected pages.
