# API

Two planes, one FastAPI app.

- **Data plane `/v1/*`** — authenticated by API key (`Authorization: Bearer sr_...`) or an OAuth access token ([auth.md](./auth.md)). This is the product.
  - `POST /v1/parse`, the `GET /v1/jobs/{id}*` routes (status, `/result`, `/result.md`, `/figures/…`, `/events`) and the `last` forms of all but `/events` additionally accept a single-use upload ticket (`srt_...`, [auth.md](./auth.md) § 5): one upload, then reads of the job that upload created, nothing else.
  - The routes under `/v1/library/documents/{id}` additionally accept a document ticket (`srd_...`, [auth.md](./auth.md) § 6) for that one document.
  - An OAuth access token reaches `/v1/library*` too: every live grant holds `library` ([auth.md](./auth.md) § 4).
- **Control plane `/api/*`** — session cookie (Google OIDC), consumed only by the Nuxt web app.

**Upstream.** Every call goes to **Kano Proxy** on the operator's own key ([chat.md](./chat.md), [auth.md](./auth.md) § 3). There are no user-defined upstreams.

- The vision calls and the catalog: `{KANO_PROXY_BASE_URL}/openai/v1` (`/chat/completions`, `/models`) with `Authorization: Bearer <KANO_PROXY_API_KEY>`.
- The `chat` and `check` agents: the Anthropic-format route `{KANO_PROXY_BASE_URL}/anthropic/v1/messages`.
- With `KANO_PROXY_API_KEY` or `KANO_PROXY_BASE_URL` empty, or `PARSE_MODEL` empty for a parse, the call is refused with **503** `upstream`.

The models are the deployment's ([billing.md](./billing.md) § Models and the operator's upstream):

- The `parse` agent runs `PARSE_MODEL`, with `PARSE_FALLBACK_MODEL` for a blocked page or a retry.
- `check` runs `CHECK_MODEL` (empty: the parse model).
- `chat` offers the live catalog's models that `MODEL_PRICES_JSON` prices, of which each user picks one ([agents.md](./agents.md)).
- `/v1/parse` takes a per-request `model` only from the parse agent's models (**400** otherwise).

`GET /healthz` is public and DB-free (`{"ok": true}`).

- It backs the compose healthchecks.
- It is also routed to the API on the public origin, so an external monitor can reach it: Caddy's `@api` matcher includes it explicitly ([deployment.md](./deployment.md)).

## Errors

All responses are JSON unless SSE is requested. Errors use one shape:

```json
{ "error": { "type": "invalid_request | nothing_to_rename | nothing_to_compact | revision_conflict | auth | rate_limit | insufficient_credits | storage_full | not_convertible | checkout_unavailable | portal_unavailable | plan_required | pay_unavailable | subscription_active | checkout_open | code_invalid | code_expired | code_exhausted | code_redeemed | code_plan_conflict | permission_denied | reauth_required | admin_conflict | upstream | internal", "message": "..." } }
```

Three types belong to the operator console alone and are answered only under `/api/admin` ([admin.md](./admin.md) § Refusals):

| Type | Status | When |
|------|--------|------|
| `permission_denied` | **403** | the route's permission is missing, or a group change breaks the no-escalation rules: it would hand out one the caller does not hold, or touch a group with one |
| `reauth_required` | **403** | the session is older than `ADMIN_REAUTH_HOURS`; sign in again |
| `admin_conflict` | **409** | a rule of the console, which `reason` names beside `message` |

A missing `X-Requested-With` header stays **403** `auth`, so a client can tell the three apart.

## Data plane

### POST /v1/parse

Multipart form (preferred) or JSON with base64 `source`.

| Field | Notes |
|-------|-------|
| `file` | required; PDF or image (jpg/png/webp/heic). Max **128 MiB** (`UPLOAD_MAX_BYTES`), PDFs max **500 pages** (`PAGE_CAP`) |
| `model` | optional; one of the parse agent's models; default `PARSE_MODEL` (below) |
| `pages` | optional selection, e.g. `"1-5,8"`; default all. Not with `folder` or a filing ticket (below) |
| `force` | optional bool; bypass the dedup cache and the in-flight join (below) |
| `folder` | optional; **file the parse into Files** there (below) |
| `retry` | optional bool; **retry a failed parse** instead of starting one (below) |
| … | a bool field takes what JSON's `true`/`false` take in multipart too: `true`/`false`, `t`/`f`, `1`/`0`, `yes`/`no`, `y`/`n`, `on`/`off`, any case; any other value → **400**, never read as false |

**`model`**

- One of the parse agent's models (`GET /v1/models`: `PARSE_MODEL`, then `PARSE_FALLBACK_MODEL`); default `PARSE_MODEL`.
- Any other → **400** ([agents.md](./agents.md) § Enqueue rules).
- With `retry`: the model the failed pages run on; default the parse's own while it is one of the parse agent's models, else `PARSE_MODEL`.

**`pages`**

- Not with `folder` or a filing ticket (**400**): a parse filed into Files covers every page, so one sent without `pages` parses and charges every page unless the cache holds that whole parse.

**`folder`** — file the parse into Files there (below, and [mcp.md](./mcp.md) § Filing into Files).

- The value is a folder's path as `GET /v1/library` lists it (`Papers/2024`), or `/` for the top level.
- Each name on it that matches no live folder is created.
- Refused with **400**, nothing made: a name that cleans to nothing, `.` or `..`, or a folder made past 64 names deep (`filing.FOLDER_PATH_DEPTH_MAX`).
- The folders are made before the upload's own checks, so a request then refused (its size, its pages, the credits, the rate) leaves them standing; sent again, it finds them.
- Only on a durable credential — an API key or an OAuth token. An upload ticket carries its destination from the `parse` tool instead, and sending `folder` with one is **400** ([mcp.md](./mcp.md) § Filing into Files).

**`retry`** — retry a failed parse instead of starting one.

- The file goes to the newest parse of the same bytes and `pages` that the retention still keeps, the library's included. So a retry sends the `pages` its upload sent.
- The newest is a failed one made on the data plane: it goes back on the queue keeping its succeeded pages and runs the failed ones ([jobs.md](./jobs.md) § Retrying failed pages).
- The newest is queued or running (a retry already sent) or succeeded (the file parsed again since, here or in Files): it is answered as it stands and nothing is charged.
- The newest is a failed Files document's own parse: the caller — any of them — retries that document as the library's retry does, from its original, the upload deleted. So a parse the connector filed is retried where it is.
- The newest is a failed re-convert: **409** saying the library retries it.
- Two library retries of one document that overlap are one: the later finds it queued and is answered with it, as a data-plane retry is.
- Not on an upload ticket that files (**400**, the ticket unspent): a retry resumes a parse where it is, so its ticket comes from `parse` without `folder`.
- Not with `folder` either (**400**, nothing made): a retry files nothing.
- **409** when there is none; **400** with `force`; **402** when the balance cannot cover the parse's credits again.

Responses:

- **200** — dedup cache hit ([jobs.md](./jobs.md) § Dedup cache): full result immediately.
- **202** — `{ "job_id": "...", "status": "queued" }`. `status` is the job's as it stands: `queued` for a parse this request started; a joined job, or a retry answered as it stands, carries its own.
  - The same bytes already queued or running for this caller — or finished within `UPLOAD_RETRY_WINDOW_SECONDS` — are that job, joined rather than parsed twice, as the web library's upload is ([jobs.md](./jobs.md) § Dedup cache).
  - `force` starts a second parse anyway.
- With `Accept: text/event-stream` — SSE stream (below) instead; small documents feel synchronous.

A filed parse adds `document_id`, the Files document it is, to either JSON body (not to the stream).

**A parse here is the parse a Files upload gets:**

- the parse agent's settings and the format check, whenever the `check` agent's switch is on ([parsing.md](./parsing.md) § Format check), so `markdown` is the text the web reader opens;
- the figure check and its crops, served below;
- a failed parse retried with `retry`, its succeeded pages kept;
- the in-flight join above.

The connector's commands are this route and the reads below ([mcp.md](./mcp.md)).

**Filed into Files** ([mcp.md](./mcp.md) § Filing into Files): with `folder`, or on an upload ticket the `parse` tool minted with one, the upload is a Files upload into that folder.

- It is the library's own filing (`routes.library.file_parse`), the same intake with the request's `model` and `force`: a document row named after the uploaded file, a taken name suffixed, its original kept, its storage counted.
- A cache hit or a join files the parse it answers with, nothing charged, the fresh upload kept as the original.
- When the folder already holds a live document of that same parse, the request is answered with it and files nothing, so a command run again makes no twin.
- A ticket's destination trashed before the upload is **404**, the upload deleted and the ticket unspent.
- A destination that goes during the upload, or a ticket's deleted for good before it, files at the top level, as the library's upload does.
- A filing refused after the intake — **409** `storage_full`, an original that could not be kept — gives back the ticket the intake spent, unspent and unbound. So the connector's read after the upload fails and its command exits non-zero, a cache hit's included.

The upload is hashed (SHA-256) while streaming to disk; the request must never buffer the whole file in memory.

- **The JSON form** is held whole by `request.json()`, so the body-limit middleware (§ Request bodies) gives it a ceiling of its own: the upload cap in base64 (four bytes per three) plus room for the envelope. It is enforced by the declared length and, for a chunked body that declares none, by counting the bytes as they arrive.
- **The multipart body** is read through a **bounded stream** rather than Starlette's `request.form()`, on this route and on the library's.

The bound is needed because the parser spools each file part to disk as it arrives with no cap of its own, and `store_upload` — where `UPLOAD_MAX_BYTES` is really enforced — only sees the file once the whole body is parsed. A request that declares no length, or an untrue one, would otherwise be written out in full before anything could answer 413.

### GET /v1/jobs/{id} — status

`{ job_id, status: queued|running|succeeded|failed, page_count, pages_done, error?, created_at, finished_at?, credits }`

- `credits` is what the parse cost once it is settled, `0` for a failed one, `null` while its reservation is pending ([billing.md](./billing.md) § Routes).

A parse with any failed page is `failed` as a whole ([parsing.md](./parsing.md) § Vision-only conversion).

- `error` counts the failed pages and names their reasons, and there is no result to fetch.
- `retry` (above) sends the failed pages again and keeps the rest; from the web library, its retry dialog does the same ([jobs.md](./jobs.md) § Retrying failed pages).
- `force` parses the whole file again.

A finished parse is kept `PARSE_RETENTION_DAYS` (30) days after it finished ([jobs.md](./jobs.md) § Retention).

- It is kept longer only while a file in the web library names a parse of the same original, or while an upload ticket that has not expired reads it.
- Then it is deleted with its result and crops.
- From then on this route, `/result`, `/result.md`, `/figures/…` and `/events` answer **404** `No such job`, as for a job that never existed.
- The same bytes uploaded again parse — and are charged — again. Save the result when it arrives.

### GET /v1/jobs/{id}/result — full result

```json
{
  "markdown": "…",
  "pages":   [ { "page": 1, "width_pt": 612, "height_pt": 792, "method": "vision", "model": "provider/model", "error": null } ],
  "figures": [ { "id": "fig1", "page": 3, "bbox": [120, 60, 480, 940], "caption": "Figure 1: …", "check": "verified" } ],
  "errors":  [],
  "meta": {
    "job_id": "…",
    "agent": "parse",
    "model": "provider/model",
    "bbox_format": "yxyx_norm1000",
    "pipeline_version": 10,
    "sha256": "…",
    "cached": false,
    "check": { "model": "provider/model", "joins": 2, "edits": 1 },
    "figure_check": { "verified": 1, "adjusted": 0, "unverified": 0 }
  }
}
```

`markdown` is the document as the web reader opens it:

- A checked parse gives the format check's reading copy. `meta.check` says so, with its model and how many joins and edits it made.
- A parse with no reading copy (its reader switched the check off) gives the transcription page by page.

The exact transcription of a checked parse is the dedup cache's record; the data plane never returns it, the control plane's `…/result` does ([parsing.md](./parsing.md) § Format check).

`markdown` carries a `<!-- page: N -->` marker line before each page's content ([parsing.md](./parsing.md) § Page markers).

Figure placeholders in `markdown`: `![fig1](sightread://p3/120,60,480,940)`, with the caption verbatim on the next line. A figure's `caption` in `figures` is that line, and empty for a figure without one ([parsing.md](./parsing.md) § Coordinate contract).

The crop Ema Paper made of each figure — the one the web reader shows — is served below (`/figures`). A caller cropping for itself adds ~2% margin and denormalizes `x_px = x/1000 * page_width_px`.

A figure's `check` is what the figure check made of its box ([parsing.md](./parsing.md) § Figure check):

- `verified`;
- `adjusted`: the box was moved to take in what it cut off, then confirmed;
- `unverified`: the check could not confirm the box — crop it with a wider margin, or look at it.

`meta.figure_check` counts the three. A result with `pipeline_version` below 10 carries neither.

A page's `model` is the model that wrote it: `meta.model` for most, another for a page a fallback or a retry finished ([parsing.md](./parsing.md) § Fallback model). A result written before the field carries none.

`errors` is empty on every result the worker writes, because a failed page fails the job. An older result can list `{page, reason}` entries for pages it omits.

### GET /v1/jobs/{id}/result.md — markdown only

The result's `markdown` string as `text/markdown` — nothing to unwrap, ready to save as a file. It answers the same 404 as `/result` while the job has no result:

- queued or running: `This job has no result yet`;
- a failed parse: `This parse failed (<its error>) and has no result`.

### GET /v1/jobs/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax} — one figure's crop

One stored figure crop of the job's parse as `image/png`, with `Cache-Control: private, no-store`.

- It is addressed exactly as its `sightread://` placeholder addresses it.
- It is the crop the web reader shows: made from the parse's own render after the figure check, with its margin ([parsing.md](./parsing.md) § Figure crops).
- 404 when the job stored no crop for that box (a crop that failed).
- A running parse serves the crops of the pages it has finished.
- The connector's **figures** command fetches every placeholder of the `.md` this way ([mcp.md](./mcp.md)).

### GET /v1/jobs/last* — the upload ticket's own job

`/v1/jobs/last`, `/v1/jobs/last/result`, `/v1/jobs/last/result.md` and `/v1/jobs/last/figures/…` resolve `last` to the job the presented **upload ticket** is bound to, so a ticket caller (the MCP flow) never needs to extract a job id.

- Only tickets: a durable credential gets 400 and uses the explicit id routes.
- A ticket that has not created a job yet — its upload is still arriving, was refused, or was not sent — gets **409** saying so. A refused or unsent one can still upload once ([auth.md](./auth.md) § 5).

### GET /v1/jobs/{id}/events — SSE progress

Events:

1. `progress` per finished page: `{job_id, pages_done, page_count, page, method}`.
2. Then exactly one of `done` (full result payload) / `error`.

Reconnectable at any time; a terminal job replays the final event immediately. Keepalive comment every 10 s.

### GET /v1/models

The models a parse may run — `PARSE_MODEL`, then `PARSE_FALLBACK_MODEL` when set — as `{data: [{id, name}]}`.

- `name` is null: the deployment names no model.
- It is the same answer as `GET /api/agents/parse/models`.
- 503 with no parse model configured.
- The ids come from the deployment's configuration, never from code.

### The library on the data plane — `/v1/library*`

Papers are read and annotated in Ema Paper, then handed to Claude — a deck made in claude.ai, Claude Code's own work — so Claude reads the library and saves a document's text and notes back.

These are the control plane's library reads and its two saves on a data-plane credential, and nothing more: no rename, move, Trash, copy, upload into the library or revert here. The connector's library tools are commands over these routes ([mcp.md](./mcp.md) § The library tools).

Credentials: an API key, an OAuth access token, or — on the routes of its own document only — a document ticket ([auth.md](./auth.md) § 6).

Every route answers for the caller's live rows only: a document in the Trash, or in a folder that is, is **404** as on the control plane.

- `GET /v1/library` — `{folders: [{id, parent_id, name, path}], documents: [{id, folder_id, name, path, kind, status, media_type, page_count, size_bytes, created_at, finished_at}]}`: the live library from the one snapshot `GET /api/library` reads, without the Trash.
  - `path` is the folder names and the name joined with `/`, as the Files page shows them (not the directory names on disk).
  - `kind` is `pdf` or `image` for a parse, `markdown`, or `file`.
  - `status` is the parse's, null for the other two.
  - A document ticket is refused (**401**).
- `GET /v1/library/documents/{id}` — that one row in the same shape, plus `original` (whether `…/original` answers).
- `GET /v1/library/documents/{id}/doc.md` and `…/notes.md` — the file as `text/markdown; charset=utf-8` with `ETag: "<revision>"`.
  - The revision is the one `GET /api/library/documents/{id}/content` reports: the SHA-256 of the text.
  - The document is materialized on the first read, with that route's refusals: **409** while its parse has no result, for a file over `CONTENT_MAX_BYTES` or not a regular file, and while the workspace recovers from an unconfirmed shell command.
  - A file of the library has no text of its own: **404**.
- `PUT /v1/library/documents/{id}/doc.md` and `…/notes.md` — the body is the new text as raw UTF-8 (`curl --data-binary`).
  - At most `CONTENT_MAX_BYTES`: **413** past it.
  - **400** for bytes that are not UTF-8.
  - `If-Match` carries the ETag of the copy it was edited from: **428** without one, so nothing is overwritten blind.
  - It is the web's save ([workspace.md](./workspace.md) § Revisions): **409** `revision_conflict` when the file changed since that read — read it again and merge.
  - One revision per save, authored `api`; no revision when the text is unchanged.
  - Not refused while a conversation answers, as the web's is not.
  - **200** `{revision}` with the new `ETag`.
- `GET /v1/library/documents/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}`, `…/images/{name}`, `…/original` and `…/file` (`?download=true` for an attachment, on either) — the control plane's routes of the same names on a data-plane credential:
  - `…/figures/…`: a placeholder's crop;
  - `…/images/{name}`: an image inserted into `doc.md` or `notes.md` (`figures/img-….png|jpg`);
  - `…/original`: a parse's original upload;
  - `…/file`: a library file's bytes.

## Control plane (session cookie, CSRF-safe: SameSite=Lax + custom header check on mutations)

### Account

| Route | Purpose |
|-------|---------|
| `GET /api/auth/login` → Google, `GET /api/auth/callback`, `POST /api/auth/logout` | OIDC flow ([auth.md](./auth.md)) |
| `GET /api/me` | the current user, the plan and balance, the parse agent's switch, the default view, the console flag and the server's `limits` |
| `PUT /api/me/preferences` | which view a converted PDF or image opens on the first time it is read |
| `DELETE /api/account` | **delete the account** |
| `GET/POST /api/keys`, `DELETE /api/keys/{id}` | API keys; `POST` returns the created key (with plaintext) exactly once, unwrapped; `GET` wraps as `{keys: []}` |
| `GET /api/site` (public) | `{support_email, operator_name}`: the deployment's support address and the operator's name |

**`GET /api/me`**

- The current user, `picture` (the Google avatar URL) included.
- `plan` (`free` or the paid plan id), `credits_remaining` (the period's and the top-up packs' together), `period_end` (when the period resets), `chat_min_credits` and `pack_checkout_available` ([billing.md](./billing.md) § Routes).
- `auto_convert` (top level, beside `plan`) is the parse agent's switch ([agents.md](./agents.md)), so the Files page applies to a PDF or an image the cap the server will: `upload_max_bytes` while it converts them, `file_max_bytes` while it keeps them.
- `default_view` (`original` or `markdown`, the row's `users.default_view`) is the view a converted PDF or image opens on the first time it is read ([web.md](./web.md) § Result viewer, *Original and Markdown*).
- `console` (top level) is true while the operator console is on (`OWNER_EMAILS` names an address) and the account holds any of its permissions. The account menu's *Admin console* item is shown on it. No re-login window applies to it ([admin.md](./admin.md) § Signing in).
- `limits`: `upload_max_bytes`, `accepted_media_types`, `accepted_extensions`, `markdown_media_types`, `markdown_extensions`, `markdown_max_bytes`, `image_media_types`, `image_max_bytes`, `prompt_max_chars`, `attachment_max_bytes`, `attachments_per_message`, `attachment_extensions`, `instructions_max_chars`, `memory_max_chars`, `sections_per_message`, `section_heading_max_chars`, `selection_max_chars`, `conversation_title_max_chars`, `idle_compact_lead_seconds`, `trash_retention_days`, `file_max_bytes`.
  - The upload UI, the prompt dialog, the instructions and memory editors, the composer and a conversation's rename field enforce the server's numbers rather than their own.
  - The composer clips a section's heading to `section_heading_max_chars` before it sends, rather than have one long heading refuse the whole message.
  - The extensions matter because intake accepts a file by extension when the browser reports a generic media type for it.
  - `accepted_media_types` and `accepted_extensions` include markdown (`text/markdown`, `.md`, `.markdown`), repeated in `markdown_media_types` and `markdown_extensions` so the page can tell one apart. Markdown becomes a markdown document rather than a parse and is capped at `markdown_max_bytes` (`CONTENT_MAX_BYTES`) instead of `upload_max_bytes`.
  - The two accepted lists say what an upload is *converted* as, not what is taken: a file outside them is kept as a file.
  - `image_media_types` and `image_max_bytes` (`DOCUMENT_IMAGE_MAX_BYTES`) are what an image inserted into a document may be ([workspace.md](./workspace.md) § Inserted images).
  - `trash_retention_days` (`TRASH_RETENTION_DAYS`, 0 = never) is how long the Trash keeps an item, which the Trash tab states beside its count ([workspace.md](./workspace.md) § The Trash).
  - `file_max_bytes` (`FILE_MAX_BYTES`) is the most a file of any other kind may be ([workspace.md](./workspace.md) § Files).

**`PUT /api/me/preferences`**

- JSON `{default_view}`: `original` or `markdown` (**400** otherwise). Answers `{default_view}`.
- It is which view a converted PDF or image opens on the first time it is read, until the reader has switched it once ([web.md](./web.md) § Result viewer, *Original and Markdown*).

**`DELETE /api/account`**

- Deletes every row and every file of the user, at once ([auth.md](./auth.md) § Deleting the account) → 204, the session cookie cleared as logout clears it.
- **409** `subscription_active` while a subscription still renews: cancel it in the portal first.
- **409** `checkout_open` while a checkout the user opened can still be paid (younger than `KANO_PAY_CHECKOUT_LIFETIME_SECONDS`).
- **503** `pay_unavailable` when Pay cannot be reached to say and the user has a subscription or a checkout on record.
- **409** `invalid_request` while a parse or an answer of the user's is running.
- 409 while the workspace is recovering from an unconfirmed shell command, as every workspace route.

**`GET /api/site`**

- `support_email` and `operator_name` are what `.env` sets (`SUPPORT_EMAIL`, `OPERATOR_NAME`, [deployment.md](./deployment.md)), each `null` when unset.
- They are what the public policy pages print ([web.md](./web.md) § Pages).

### Agents

| Route | Purpose |
|-------|---------|
| `GET /api/agents` | the agent registry with the caller's settings |
| `PUT /api/agents/{agent}/settings` | partial update of one agent's settings |
| `GET /api/agents/{agent}/models` | the models the agent may run, `{data: [{id, name}]}` |
| `GET /api/agents/chat/context?locale=` | **what the chat agent receives**, for the Agent page's drawer |
| `GET /api/agents/chat/memory-file` | the chat agent's memory file |
| `PUT /api/agents/chat/memory-file` | write the memory file |

**`GET /api/agents`**

`{agents: [{name, input, output, default_prompt, prompt_tokens, tools, settings: {model, prompt, effort, instructions, answer_language, translate_language, memory_auto, memory_paused, idle_compact, fallback_model, auto_fallback, auto_check, auto_convert}, bbox: {family, format}}]}`

- `model` is the model the agent runs: `PARSE_MODEL` for `parse`, `CHECK_MODEL` or the parse model for `check`, the caller's pick for `chat`. For `chat`, `null` is the default: the first of `GET /api/agents/chat/models`, which only the live catalog can say.
- `fallback_model` is `PARSE_FALLBACK_MODEL` or `null` on `parse`. It and the `parse` and `check` models are facts of the deployment, not settings ([billing.md](./billing.md) § Models).
- `prompt` is the caller's own template, or `null` for `default_prompt`.
- `prompt_tokens` are the tokens the template substitutes (the dialog's chips).
- `bbox` is the coordinate convention the pipeline will ask the saved model for ([parsing.md](./parsing.md) § Coordinate conventions): a fact about the model, not a setting ([agents.md](./agents.md)).
- `tools` are the names of the tools a turn of the caller's declares, in their order ([chat.md](./chat.md) § Tools):
  - empty on every agent but `chat`;
  - `bash` only with a shell;
  - the web tools only where the deployment offers them;
  - `convert_file` only with a parse model.

**`PUT /api/agents/{agent}/settings`**

A partial update: only the fields present change; `null` restores the default. **400** for a setting another agent owns. 404 for an unknown agent.

| Field | Agents | Value |
|-------|--------|-------|
| `prompt` | every agent | trimmed, capped at `SYSTEM_PROMPT_MAX_CHARS` (400 past it); empty or equal to the default stores `null` |
| `effort` | every agent | `low`, `medium`, `high`, `xhigh` or `max` (**400** otherwise; [agents.md](./agents.md)) |
| `model` | `chat` | one of `GET /api/agents/chat/models`, read live (**400** otherwise, 502/503 as that route answers); `null` restores the default, its first entry |
| `auto_fallback` | `parse` | a boolean, `null` refused: whether a blocked page goes to `PARSE_FALLBACK_MODEL` on its own ([parsing.md](./parsing.md) § Fallback model) |
| `auto_convert` | `parse` | a boolean, `null` refused: whether an uploaded PDF or image is converted on arrival or kept as a file; off unless the reader turns it on ([workspace.md](./workspace.md) § Files) |
| `instructions` | `chat`, `check` | text, cleaned and capped (below) |
| `answer_language`, `translate_language` | `chat` | a language tag, or `null` to follow the reader's locale |
| `memory_auto`, `memory_paused` | `chat` | booleans, `null` refused |
| `idle_compact` | `chat` | a boolean, `null` refused: whether the worker compacts the caller's long conversations before their cache expires; on by default ([chat.md](./chat.md) § Compaction) |
| `auto_check` | `check` | a boolean, `null` refused ([parsing.md](./parsing.md) § Format check) |

- The `parse` and `check` agents' models are the deployment's.
- `instructions` is trimmed, cleaned of zero-width, bidirectional and control characters (tab, newline and carriage return kept; a vertical tab, form feed or separator becomes a newline) and capped at `CHAT_INSTRUCTIONS_MAX_CHARS` (400 past it). Empty stores `null` ([chat.md](./chat.md) § Instructions).
- The two languages are language tags: `^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$`, at most 16 characters; 400 otherwise.
- Answers the stored settings plus the `bbox` the new model implies.

**`GET /api/agents/{agent}/models`**

What the chat agent's model picker, the subagent dialog and the retry dialog list ([billing.md](./billing.md) § Models).

- `parse`: its fixed model and fallback.
- `check`: its model.
- `chat`: the models the operator's Kano Proxy catalog (`GET {KANO_PROXY_BASE_URL}/openai/v1/models`, fetched live, no cache) lists and `MODEL_PRICES_JSON` prices, in `MODEL_PRICES_JSON`'s order, each with the catalog's `name`.
- For `chat`, 503 with Kano Proxy unconfigured and 502 when its catalog cannot be read; for `parse` and `check`, 503 with no parse model configured.

**`GET /api/agents/chat/context?locale=`**

`{parts: [{kind: prompt|instructions|catalogue|agents_file|memory_file|memory, text, chars, own?, skills?, count?, paused?}], turn: {text, chars}}`, rendered by the code the runner uses.

- `parts` are the system blocks in the order they are sent, each with its text and its length in characters. No token count: counting tokens is an upstream call.
- `prompt` carries `own` (the reader's prompt or the default). The prompt is shown with its tokens unreplaced.
- `catalogue` carries how many skills the agent may load on its own (`model_invocable`); the subagents it lists are in its text.
- `agents_file` is the root AGENTS.md (`files/AGENTS.md`) alone, as no document is named; absent when there is none ([chat.md](./chat.md) § AGENTS.md).
- `memory_file` is the memory file as a new conversation would read it; absent while the file is empty ([chat.md](./chat.md) § Memory, *The memory file*).
- `memory` carries how many memories a new conversation would load — every document's, none of one document's own — and whether memory is `paused`.
- `turn` is the context block a message sent now in `locale` (default `en`, the composer's pattern) would carry, its revision line reading `<revision>`.
- Reads only: nothing is materialized.

**`GET /api/agents/chat/memory-file`**

- The chat agent's memory file, `memory.md` at the root of the caller's workspace ([chat.md](./chat.md) § Memory, *The memory file*): `{text, max_chars}`.
- `text` is empty while there is none.
- `max_chars` is `CHAT_MEMORY_FILE_MAX_CHARS`, what a turn reads of it.
- **409** when the file is not a plain text file.

**`PUT /api/agents/chat/memory-file`**

- `{text}` → `{text}`: writes the file as a revision of the caller's own, under the workspace lock, and answers what was stored: the text cleaned as instructions are, trimmed, with a newline at the end; an empty text is stored empty.
- **400** past `max_chars`.
- **409** while any turn of the caller is unfinished (a turn may be writing it), or while the workspace is recovering.
- A new conversation reads the saved text; an open one keeps the snapshot its first turn took until it is compacted.

### Skills and subagents

| Route | Purpose |
|-------|---------|
| `GET /api/skills`, `POST`, `PUT/DELETE /api/skills/{name}` | the chat agent's skills ([chat.md](./chat.md) § Skills) |
| `GET /api/subagents`, `POST`, `PUT/DELETE /api/subagents/{name}` | the chat agent's subagents ([chat.md](./chat.md) § Subagents), the same contract |

**Skills**

- `GET` → `{skills: [{name, description, argument_hint, body, user_invocable, model_invocable, builtin, modified, default, document_only}]}`: built-ins first in their code order, then the caller's own by name.
  - `document_only` is whether the skill works on a document: true for the built-ins `check-figures`, `fix-math` and `translate`, an edit of one included; false for a skill of the caller's own. A library conversation's composer leaves such a skill out and its send refuses it ([chat.md](./chat.md) § Skills).
  - `default` is a built-in's shipped `{description, argument_hint, body, user_invocable, model_invocable}`; null for the caller's own.
  - `modified` is whether the caller's edit stands.
- `POST` creates one of the caller's own → 201.
  - **409** for a name taken or a built-in's.
  - **400** for `new`, `compact`, `rename` or `init`, the composer's own commands. A rename to one of them is refused the same way.
  - At most `CHAT_SKILLS_MAX`, counted under a lock on the caller's user row, so two creations at the cap cannot both pass.
- `PUT` writes the fields present.
  - On a built-in it stores the caller's edit; an edit equal to the default is removed instead.
  - `name` renames one of the caller's own and is refused on a built-in.
- `DELETE` deletes one of the caller's own. On a built-in it removes the edit: *restore default*.
- Names `^[a-z0-9][a-z0-9-]{0,63}$`, `description` at most 1024 characters, `argument_hint` 128, `body` `SYSTEM_PROMPT_MAX_CHARS`.
- 404 for a name that is neither.

**Subagents**

- `GET` → `{subagents: [{name, description, model, writes, tools, prompt, builtin, modified, default, document_only}]}`.
- `document_only` is true for the built-in `translator`, edited or not, which a library conversation does not offer.
- `model` is a model id of at most 255 characters, or `null` (the conversation's).
- `writes` is whether it may change files (default `false`).
- `tools` is a non-empty subset of `view_page`, `view_figure`, `str_replace_based_edit_tool`, `bash` (**400** otherwise); `bash` only with `writes` (**400** otherwise).
- `prompt` is at most `SYSTEM_PROMPT_MAX_CHARS`.
- At most `CHAT_SUBAGENTS_MAX` of the caller's own, counted the same way.

### Memories

| Route | Purpose |
|-------|---------|
| `GET /api/memories` | the caller's memories ([chat.md](./chat.md) § Memory) |
| `POST /api/memories` | save a memory: the thread's Undo |
| `PUT /api/memories/{id}` | partial update of one memory |
| `DELETE /api/memories/{id}`, `DELETE /api/memories` | delete one of the caller's memories, or all of them (*Clear all memories*); 204. The agent's settings, the memory switches included, stay |

**`GET /api/memories`**

- `{memories: [{id, kind, text, document_id, document_name, pinned, conversation_id, conversation_title, source_ref, created_at, updated_at}]}`, newest first.
- `document_name` and `conversation_title` name the scope and the source while they exist.

**`POST /api/memories`**

- `{kind, text, document_id?, pinned?, conversation_id?, source_ref?}` → 201 the memory.
- It is how the thread's Undo puts back what the agent forgot and saves again what Undo removed.
- `text` is cleaned and capped like the tool's: `CHAT_MEMORY_MAX_CHARS`, 400 past it, and 400 for an empty one.
- `document_id` must be a live parse or markdown document of the caller's, and `conversation_id` one of the caller's conversations (404 otherwise).
- **409** at `CHAT_MEMORIES_MAX`, counted under a lock on the caller's user row.

**`PUT /api/memories/{id}`**

- A partial update of `kind`, `text`, `document_id` (`null` = every document) and `pinned` → the memory.
- The same checks; 404 when it is not the caller's.

### Chat

| Route | Purpose |
|-------|---------|
| `POST /api/attachments` | **an attachment for the composer** |
| `DELETE /api/attachments/{id}` | drop one of the caller's attachments not yet sent; 204, 404 when it is not the caller's or already sent |
| `GET /api/library/documents/{id}/conversations`, `POST` | the document's chat threads |
| `GET /api/library/conversations`, `POST` | **the Files page's chat threads**, the library conversations ([chat.md](./chat.md) § Library conversations) |
| `GET /api/conversations/{id}` | the conversation with the messages and turns of the branch it shows |
| `POST /api/conversations/{id}/messages` | send a message and queue a turn |
| `POST /api/conversations/{id}/messages/{message_id}/edit` | send a user message again with new text, as a version of it |
| `GET /api/conversations/{id}/messages/{message_id}/attachments/{index}` | the bytes of an image attached to a user message |
| `GET /api/conversations/{id}/events?after=N` | SSE: the branch's events after `N`, then the live tail |
| `POST /api/conversations/{id}/cancel` | stop the running turn after its current tool call |
| `PUT /api/conversations/{id}` | rename it, move it to another model, or show another version of a message |
| `POST /api/conversations/{id}/title` | a title the summary model writes |
| `DELETE /api/conversations/{id}` | delete the thread, a document's or a library conversation; the documents and their revisions stay. 409 while a turn is queued or running: cancel it first |

Every `/api/conversations/{id}/…` route takes a library conversation as it takes a document's, with the differences the send and the edit name.

**`POST /api/attachments`**

- Multipart `file`, streamed to the upload directory under `CHAT_ATTACHMENT_MAX_BYTES` (**413** past it) and prepared there ([chat.md](./chat.md) § What a user message carries) → 201 `{id, name, kind: image|text, media_type, size_bytes, chars}`.
- **415** for a type that is neither an image nor a PDF nor text (`attachment_extensions`).
- **400** for an image that does not decode or decodes past `CHAT_PAGE_IMAGE_MAX_PIXELS`, a PDF without a text layer, a text file that is not UTF-8.
- **429** `rate_limit` when `CHAT_ATTACHMENTS_PENDING_MAX` of the caller's attachments are already waiting for a message (unsent and younger than `UPLOAD_MAX_AGE_SECONDS`). Checked before the body is read, and again under a lock on the caller's user row once the file is prepared.

**The conversation payload**

`{id, title, model, compactable, context_tokens, context_window, cache_warm_until, recache_credits, created_at, updated_at}` ([chat.md](./chat.md) § Compaction):

- `model` is the model its turns run on.
- `compactable` is whether it holds an assistant message since its last summary on the branch it shows: what `/compact` needs.
- `context_tokens` is what the last call replayed and produced; null before the first call, and after a compaction.
- `context_window` is the model's context size: its `context` in `MODEL_PRICES_JSON`, else `CHAT_CONTEXT_WINDOW_TOKENS`.
- `cache_warm_until` is when the prompt cache the last call wrote or read expires; null when that call reached no cache.
- `recache_credits` is what writing `context_tokens` to the cache again costs; null with `context_tokens` null.

**`GET/POST /api/library/documents/{id}/conversations`**

- `GET` → `{conversations: [...]}`, the conversation payload: the one continued last first, only those holding at least one message ([chat.md](./chat.md) § Conversations, turns, events).
- `POST` creates an empty one, which the web app does only to send a conversation's first message.
- It is created under the user's row lock, the document checked again once it is held, so a delete racing it ends with a clean 404 or a thread the delete then takes with the document, never a 500.

**`GET/POST /api/library/conversations`**

- `GET` → `{conversations: [...]}`, the conversation payload as the document's list gives it: the caller's conversations with no document (`conversations.document_id` NULL), apart from every document's, only those holding at least one message, the one continued last (`updated_at`) first.
- `POST` → **201** a new, empty one with no document (a shell session of its own), made once the caller's workspace has been made sure of: there is no document to materialize. The web app makes one only to send a conversation's first message.
- CSRF-guarded as every mutation.

**`GET /api/conversations/{id}`**

`{conversation, messages: [{id, role, content, turn_id, created_at}], turns: [{id, kind, status, error, finished_at, credits, versions}]}`, `conversation` as the list gives it.

- The messages and turns are those of the branch the conversation shows alone, root to head. The rows of a version the reader edited away stay unsent until a `version` switch brings its branch back.
- `versions`, on a `message` turn, are the ids of the turns that are versions of one message — the siblings under one parent, itself among them — in the order they were made; null when it has none ([chat.md](./chat.md) § History is append-only, *An edited message starts a version*).
- `kind` is `message`, `compact` or `idle_compact` ([chat.md](./chat.md) § Compaction).
- `finished_at` is null while the turn is unfinished, whatever its status says: a cancel marks the status first and the writer winds down after. The panel's liveness goes by it ([chat.md](./chat.md) § Conversations).
- A stored message's blocks are sent as the panel draws them:

| Stored block | Sent as |
|--------------|---------|
| an image block (a page, a figure or an image file a tool returned, kept for the model's replay), inside `tool_result` content too | `{type: image}` without its bytes |
| an attached image | `{type: image, attachment: {name}}` without its bytes; the panel asks for them by the block's index |
| an attached file | `{type: text, context: attachment, attachment: {name, kind, chars}}` without its text |
| `context: selection` | the selected passage alone, without the line that introduces it to the model ([chat.md](./chat.md) § Prompt caching) |
| `context: quote` | the quoted passage alone |
| `context: section` | `{type: text, context: section, section}`: the heading without the section's text |
| `context: skill` | `{type: text, context: skill, skill}` without the body |
| `context: turn` | not at all |
| `context: compact` (the compaction instruction) | not at all |
| `context: init` (the `/init` instruction) | the bare marker `{type: text, context: init}` |
| `context: summary` (the summary a compaction left, which the panel shows behind *View summary*) | with its text |
| `context: library` (the Files page as a library conversation's message carried it) | `{type: text, context: library, library: {folder, items}}`: the Files paths without the model's text |
| `tool_result` | keeps its `subagent` record ([chat.md](./chat.md) § Subagents) and its `library` record, the undo record of a library tool's call ([chat.md](./chat.md) § Library conversations, *The library tools*) |

- An image block's bytes are left out because the panel never renders them and a long thread would otherwise carry hundreds of megabytes.
- The messages are read in batches of a few hundred rows, each message stripped of its image bytes as its batch is read, so a long thread is never held whole in the API process.

**`POST /api/conversations/{id}/messages`**

`{text, selection?, sections?, quote?, command?, attachments?, library?, locale?}` → **202** `{turn_id, message_id}`: stores the user message (with the turn's id, its blocks in the order of [chat.md](./chat.md) § What a user message carries) and queues a turn that answers in `locale`.

- `text`, `selection` and `quote`: at most `CHAT_MESSAGE_MAX_CHARS`, `CHAT_SELECTION_MAX_CHARS` and `CHAT_SELECTION_MAX_CHARS` characters; **400** past them.
- `sections`: at most `CHAT_SECTIONS_PER_MESSAGE` of `{heading, text}` ([chat.md](./chat.md) § What a user message carries).
  - The heading is at most `CHAT_SECTION_HEADING_MAX_CHARS` characters and not empty.
  - The text is at most `CHAT_SELECTION_MAX_CHARS`, or `null` for a section too long to send.
- `command`, one of:
  - a skill's name, `text` then its arguments;
  - `compact`, the runner's own, `text` then the focus of the summary ([chat.md](./chat.md) § Compaction);
  - `init`, the runner's own too, `text` then what the caller says about the folder. The message then carries the instruction that sets up the folder's AGENTS.md and no selection, sections or quote ([chat.md](./chat.md) § AGENTS.md).
- `attachments`: at most `CHAT_ATTACHMENTS_PER_MESSAGE` ids from `POST /api/attachments`.
- `library`, a library conversation's only: `{folder_id: int | null, items: [{kind: "folder" | "document", id: int}]}`, the folder open on the Files page and the rows selected there ([chat.md](./chat.md) § Library conversations).
  - At most `CHAT_LIBRARY_ITEMS_PER_MESSAGE` items.
  - Ids that are not the caller's, are in the Trash or no longer exist are left out.
  - An unknown or trashed folder is read as the top of the library.
  - Stored as the message's `context: library` block.
- `locale`: a language tag such as the web app's `en` or `zh-TW`; default `en`.

Refusals:

- **400** for a message with no text, no command and no attachment; for a `command` that names no skill the caller can run; and for a `compact` or an `init` with attachments.
- In a library conversation:
  - **400** for a `selection` or `sections` (*A library conversation has no document passage*);
  - **400** for `library` past `CHAT_LIBRARY_ITEMS_PER_MESSAGE` items;
  - **400** for a `command` naming a `document_only` skill (*This skill works on a document: open one to use it*);
  - its `init` sets up the AGENTS.md of the folder `library` names.
- In a document conversation, **400** for `library`.
- **400** `nothing_to_compact` for a `compact` on a conversation with no assistant message since its last summary.
- **409** `storage_full` when the workspace is at the plan's storage, checked before anything is stored ([workspace.md](./workspace.md) § Quota).
- **409** for an attachment that is not the caller's pending one or whose prepared file is gone (swept after a day).
- 429 with `Retry-After: CHAT_RETRY_AFTER_SECONDS` (15) when the caller already has `MAX_CHAT_TURNS_PER_USER` unfinished turns (queued, running, or cancelled and not yet wound down).
- 409 while this conversation is still answering, or when its stored history, with this message and its attachments, would exceed `CHAT_HISTORY_MAX_BYTES` ([chat.md](./chat.md) § History is append-only). The attachments are not claimed then, so a new conversation can carry them.
- **503** `upstream` when Kano Proxy is not configured.
- On a conversation's first message, its model is read from `GET /api/agents/chat/models`'s list, fetched then, and then fixed on the conversation ([chat.md](./chat.md) § Upstream):
  - the caller's pick, **400** when the list no longer carries it;
  - else the list's first, **503** `upstream` when the list is empty;
  - **502** when the catalog cannot be read.
- 409 when the conversation's document is in the Trash, decided under the user's row lock that *Move to Trash* takes too, so no turn starts on a trashed file. A library conversation has no document to check, and its turn block no revision line.

**`POST /api/conversations/{id}/messages/{message_id}/edit`**

`{text, locale?}` → **202** `{turn_id, message_id}`: sends `message_id`, a user message of a `message` turn, again with `text` in place of its text, as a new message with a turn of its own. The new turn is the edited turn's sibling, whose branch the conversation then shows ([chat.md](./chat.md) § History is append-only, *An edited message starts a version*).

- Its attachments, selection, sections, quote and command are carried as stored, and a library conversation's `context: library` block too.
- The command's skill is rendered again (**400** when it names no skill the caller can run any more), with a fresh turn block.
- `text` is at most `CHAT_MESSAGE_MAX_CHARS` (**400** past it); **400** for a message left with no text, no command and no attachment.
- **404** for an assistant message, a compaction turn's row, a summary, or a message of another conversation.
- Otherwise the send's own answers: **409** while a turn of the conversation is unfinished, 429 at the caller's turn cap, 409 when the branch's history with this message would exceed `CHAT_HISTORY_MAX_BYTES`, 409 `storage_full`, 409 for a document in the Trash, **503** `upstream`; and the send's credit minimum.
- The answer runs on the conversation's model at the effort in force.
- The conversation's `compacted_message_id`, `context_tokens` and `cache_warm_until` columns are set for the new branch.

**`GET /api/conversations/{id}/messages/{message_id}/attachments/{index}`**

- The bytes of the image attached to that user message as its content block `index`, with its media type.
- `Cache-Control: private, max-age=CHAT_ATTACHMENT_CACHE_SECONDS, immutable`, since a stored message never changes.
- 404 for any other block.

**`GET /api/conversations/{id}/events?after=N`**

- SSE: every `chat_events` row after `N` on the branch the conversation shows, then the live tail. A turn on another branch is left out of the replay ([chat.md](./chat.md) § Conversations, turns, events).
- `keepalive` comments while idle ([chat.md](./chat.md)).
- A library tool's `tool_result` frame carries `library`, the undo record of what the call changed, when it changed something ([chat.md](./chat.md) § Library conversations, *The library tools*).
- The replay is read in batches of a few hundred rows, each yielded before the next is read, so a long thread is never held whole in the API process.

**`PUT /api/conversations/{id}`**

`{title?, model?, version?}` → the conversation as the list gives it. **400** with none of the three.

- `title` renames it.
  - The title is cleaned to one line like a memory's text and cut at 80 characters (`TITLE_MAX_CHARS`); **400** when nothing is left.
  - `updated_at` stays as it was, so a rename does not move the thread up the list.
  - A turn running on it does not refuse it ([chat.md](./chat.md) § Conversations, turns, events).
- `model` moves it to another model for its later turns.
  - One of `GET /api/agents/chat/models` (read live, **400** otherwise, 502/503 as that route answers).
  - Refused with **409** while a turn of the conversation is unfinished.
  - `cache_warm_until` is cleared with the move ([chat.md](./chat.md) § History is append-only).
- `version` shows another version of a message ([chat.md](./chat.md) § History is append-only, *An edited message starts a version*).
  - The id of a `message` turn of the conversation (**400** otherwise), whose branch — down to the newest turn under it — becomes what `GET` sends and the next message continues.
  - Refused with **409** while a turn is unfinished.
  - The conversation's `compacted_message_id`, `context_tokens` and `cache_warm_until` columns are set for that branch, and `updated_at` stays as it was, as with a rename.

**`POST /api/conversations/{id}/title`**

`{locale?}` → the conversation as the list gives it, with a title the summary model wrote: the composer's `/rename` with no name ([chat.md](./chat.md) § Conversations, turns, events).

- One call on `SUMMARY_MODEL`, else the conversation's model, on the document's name and the first and the latest exchange of the branch it shows.
- Written in the answer language the reader chose, or else in `locale` (the web app's, `en` by default).
- Billed by its tokens on a `chat` entry of its own with no turn ([billing.md](./billing.md) § A chat turn).
- `updated_at` stays as it was, as with a rename, and a running turn does not refuse it.
- **400** `nothing_to_rename` while the branch holds no message turn.
- **402** `insufficient_credits` under `CHAT_MIN_CREDITS`.
- **429** `rate_limit` with `Retry-After` while another title of the reader's is being written.
- **503** without Kano Proxy or with the model unpriced.
- **502** when the call fails or answers no title (the title unchanged).

### Sandbox and billing

| Route | Purpose |
|-------|---------|
| `GET /api/sandbox` | the caller's sandbox and the workspace's storage ([sandbox.md](./sandbox.md)) |
| `POST /api/sandbox/stop`, `POST /api/sandbox/reset` | stop the VM; stop and clear the package cache |
| `GET /api/billing` | the plan, the balance, the packs and the credit rates ([billing.md](./billing.md) § Routes) |
| `POST /api/billing/redeem` | **redeem a code** ([billing.md](./billing.md) § Redeem codes) |
| `GET /api/billing/catalog` (public) | the plans and packs on sale with Pay's prices, the Free plan and the credit rates (`{available, plans, packs, free, rates}`) |
| `POST /api/billing/checkout` | a Pay checkout `{url}` for a plan |
| `POST /api/billing/packs/checkout` | a Pay checkout `{url}` for a pack; **409** `plan_required` while the caller's period is Free ([billing.md](./billing.md) § Top-up packs) |
| `POST /api/billing/portal` | Pay's customer portal `{url}` |
| `POST /api/billing/pay-webhook` (signed, no session) | Pay's notifications, a subscription's or a purchase's ([billing.md](./billing.md) § Kano Pay) |

**`GET /api/sandbox`**

- `{state, last_used_at, granted, workspace: {bytes, quota_bytes, files_bytes, trash_bytes, history_bytes}}`.
- `granted` is whether the plan gives the chat agent a sandbox.
- `quota_bytes` is the plan's storage ([billing.md](./billing.md) § Plans); `bytes` is what counts against it, the sum of the files, the Trash and the history ([workspace.md](./workspace.md) § Quota).
- `state` is `unavailable` on a deployment without a runner, and when the runner cannot be reached or does not answer readably.

**`GET /api/billing`**

- The plan.
- The period's balance and what the period is (`period.source`: a subscription's, granted days or Free).
- The top-up packs, bought or granted by a redeem code (`source`).
- The bank of granted days (`plan_grant`).
- The credit rates.

**`POST /api/billing/redeem`**

- JSON `{code}`, typed any way: case, spaces and hyphens are dropped before the lookup.
- A credits code, which became a pack → `{kind: "credits", credits, expires_at}`.
- A plan code → `{kind: "plan", plan_id, days, days_banked, period_end}`; `period_end` is null while a paid subscription runs and the days wait.
- **404** `code_invalid` for a code that does not exist or is disabled.
- **409** `code_expired`, `code_exhausted`, `code_redeemed` (this account used it before) or `code_plan_conflict`.
- **429** `rate_limit` with `Retry-After` after `REDEEM_MAX_FAILURES` refusals inside `REDEEM_FAILURE_WINDOW_SECONDS`.

**`POST /api/billing/pay-webhook`**

- A notification whose subject has no account — a deleted one's — is acknowledged with 200 and nothing recorded.

### The operator console

| Route | Purpose |
|-------|---------|
| `GET /api/admin/me`, `GET /api/admin/overview`, `GET /api/admin/users`, `GET /api/admin/users/{id}`, `POST /api/admin/users/{id}/grants`, `GET/POST /api/admin/codes`, `POST /api/admin/codes/{id}/disable`, `GET /api/admin/jobs`, `POST /api/admin/jobs/{id}/requeue`, `GET /api/admin/usage`, `GET/POST /api/admin/groups`, `GET/PATCH/DELETE /api/admin/groups/{id}`, `POST /api/admin/groups/{id}/members`, `DELETE /api/admin/groups/{id}/members/{user_id}`, `GET /api/admin/audit`, `GET /api/admin/settings`, `PUT/DELETE /api/admin/settings/{key}` | **the operator console**: the routes, their permissions and answers are [admin.md](./admin.md) § Routes |

- Mounted only while `OWNER_EMAILS` names an address. Otherwise every `/api/admin` path is the 404 of a path that does not exist.
- The session only: an `Authorization` header, whatever it carries, is **401** even beside a valid cookie.
- Its own refusals are the three console types of § Errors: **403** `permission_denied` (a missing permission), **403** `reauth_required` (a session older than `ADMIN_REAUTH_HOURS`), **409** `admin_conflict` (a rule of the console).
- Paged lists answer `{items, total, page, page_size}`.
- A redeem code is whole only in the answer that creates it, masked everywhere else.
- No route returns a document, a note, a conversation or any other content of an account, and none signs anyone in as another user.

### The library

| Route | Purpose |
|-------|---------|
| `GET /api/library` | the whole file library in one read |
| `POST /api/library/folders`, `PUT/DELETE /api/library/folders/{id}` | create (`name`, optional `parent_id`) / rename **and** move (partial: only the fields present change) / delete |
| `POST /api/library/documents` | **the web upload** |
| `POST /api/library/documents/new` | **write a new markdown document** |
| `PUT/DELETE /api/library/documents/{id}` | rename / move (`name`, `folder_id`) / delete the library entry |
| `POST /api/library/documents/{id}/trash`, `POST /api/library/folders/{id}/trash` | **Move to Trash** ([workspace.md](./workspace.md) § The Trash) |
| `POST /api/library/documents/{id}/restore`, `POST /api/library/folders/{id}/restore` | **Put Back** |
| `DELETE /api/library/trash` | **Empty Trash** ([workspace.md](./workspace.md) § The Trash) |
| `POST /api/library/documents/{id}/copy`, `POST /api/library/folders/{id}/copy` | **a copy** |
| *a document in the Trash* | 404 on every document route but `restore` and `DELETE` |
| `GET /api/library/documents/{id}/file` | **a file's bytes** ([workspace.md](./workspace.md) § Files) |
| `GET /api/library/documents/{id}/original` | **a parsed document's original** ([workspace.md](./workspace.md) § Layout) |
| `POST /api/library/documents/{id}/convert` | **convert a file into a document** ([jobs.md](./jobs.md) § Converting a file) |
| `PUT /api/library/documents/{id}/view` | remember the view the reader switched a PDF or an image to |
| `GET /api/library/documents/{id}/content` | the document's editable copy from the workspace |
| `PUT /api/library/documents/{id}/content` | save `doc.md` |
| `PUT /api/library/documents/{id}/notes` | `{text, base_revision}` → `{revision}`, same rule |
| `POST /api/library/documents/{id}/revisions/{commit}/revert` | revert a revision, as a new revision |
| `GET /api/library/documents/{id}/result` | the document's result |
| `GET /api/library/documents/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}` | one stored figure crop of the document's parse |
| `GET /api/library/documents/{id}/pages` | the parse page by page |
| `GET /api/library/documents/{id}/pages/{page}/image` | one page of the document's original as `image/png` |
| `POST /api/library/documents/{id}/retry` | **retry the failed pages** |
| `POST /api/library/documents/{id}/reconvert` | **re-convert the document** |
| `DELETE /api/library/documents/{id}/reconvert` | **dismiss a failed re-convert** |
| `POST /api/library/images` | **an image inserted into a document** |
| `GET /api/library/documents/{id}/images/{name}` | the document's inserted image |

**`GET /api/library`**

`{folders: [...], documents: [...], trash: {folders: [...], documents: [...]}}`, all from one `REPEATABLE READ` snapshot.

- The request's session ends the transaction its authentication opened first (an isolation level applies from a transaction's first statement, which authentication has spent), then opens the snapshot on the connection it procures next.
- One connection per request, so a burst of library reads cannot hold the pool with authentication connections while waiting for second ones.
- A document carries:
  - its own fields: `id`, `folder_id`, `name`, `job_id`;
  - `storage_bytes`, its share of the storage: its directory, its original and its inserted images, the service's copies not counted ([workspace.md](./workspace.md) § Quota). For a parse or a markdown document it is `null` when the workspace cannot be measured (a directory nested too deep), and in the one-document answers of the routes below, which do not walk the workspace. For a file it is always its size (`size_bytes`);
  - the parse's live state: `status`, `kind`, `model`, `page_count`, `pages_done`, `size_bytes` (the upload), `error`, `created_at`, `failed_pages` and `checking`;
  - `failed_pages`: for a `failed` parse, how many of its selected pages have no succeeded conversion — what a retry sends — else `null`;
  - `checking`: `true` for a `running` parse with a format check whose every page is done, the check being what it waits for ([parsing.md](./parsing.md) § Format check);
  - `reconvert`: `null`, or the live state of the document's re-convert while it is queued or running, or after it failed: `{status, model, page_count, pages_done, error, failed_pages, checking}`, the same fields read from the new parse ([jobs.md](./jobs.md) § Re-converting a document).
- The browser needs no second request to render a row or its progress.
- Every row carries `view`: the view its reader last switched a PDF or an image to in the result viewer — `original` or `markdown`, `null` while never switched (`PUT …/view`, below; [web.md](./web.md) § Result viewer, *Original and Markdown*).
- A parse carries `original`, whether `…/original` (below) answers; `null` on the other kinds. It answers when:
  - its original upload was kept; or
  - between a finished parse's end and the filing of its original (its retention not yet run, `source_deleted_at` unset, [jobs.md](./jobs.md) § Retention), the upload it is filed from is still there and the row is the one it is filed for: the last row naming the parse that holds no original, which retention moves it to whatever the quota. The others take a copy only while the quota lets them, and read `false` until they hold one.
- The upload is looked at before the listing, so a reader is never told a failed parse kept none in that moment.
- A markdown document has no parse: `job_id`, `status`, `model`, `page_count`, `pages_done`, `size_bytes`, `error`, `failed_pages` and `reconvert` are `null`, `checking` is `false` and `kind` is `markdown` ([workspace.md](./workspace.md) § Markdown documents).
- A file ([workspace.md](./workspace.md) § Files) reads the same way with `kind` `file`, `size_bytes` its size in the tree and `media_type` the type its bytes are served as (`null` on the other kinds).
- Which of the three a row is, the row itself says (`documents.kind`, [database.md](./database.md)).
- `folders` and `documents` are the live rows only.
- `trash` lists what was put in the Trash: each item that was trashed itself, never what sits inside a trashed folder ([database.md](./database.md) § Rules).
  - Its items are the same payloads plus `trashed_at` and `origin`, the folder names from the top level down to where *Put Back* returns it (`[]` for the top level).
  - A folder also carries `items`, how many documents are inside it.

**`POST /api/library/folders`, `PUT/DELETE /api/library/folders/{id}`**

- Deleting — a live folder, or one in the Trash, for good — cascades to the subtree, except what in it was trashed separately, which stays in the Trash ([database.md](./database.md) § Rules).
- A `parent_id` inside the folder's own subtree is refused (a directory cannot contain itself).
- A name that is already taken is suffixed ` (2)` on create and on move, and answers 409 on a rename.
- Create also makes the folder's directory in the workspace, under the workspace lock, so the agent's shell sees it at once ([workspace.md](./workspace.md) § Layout). A workspace recovering from an unconfirmed shell command answers 409.

**`POST /api/library/documents`**

Multipart `file` + optional `folder_id` → 201 with the document.

- The body as a whole is bounded by the largest of `FILE_MAX_BYTES`, `UPLOAD_MAX_BYTES` and `CONTENT_MAX_BYTES`, each kind's own cap applying as its bytes arrive.
- A body cut off before its closing boundary, or a part whose headers run past a few KiB, is **400** with nothing kept.
- A PDF or an image is parsed only while the parse agent's `auto_convert` is on (`GET /api/agents`, off unless the reader turns it on, [agents.md](./agents.md)). With it off, it is kept as a file too and converted later by `…/convert` (below).

A PDF or an image the parse agent converts:

- Streamed to disk by the same `jobs.intake` sequence `/v1/parse` runs, then a document row naming the job.
- The library name is decided from the filename *before* the parse is queued (a filename that cleans to nothing is filed as `upload`), so no request can enqueue a billed parse and then fail on its name.
- A dedup hit points at the cached job and is already `succeeded`. A retry of an upload that already landed points at the job still running for it rather than starting a second parse ([jobs.md](./jobs.md) § Dedup).
- **409** when the upload could not be kept as the document's original (the disk, the tree). The row is deleted again and nothing is filed: the parse, named by no file, falls under the retention ([jobs.md](./jobs.md) § Retention), and a retry does not find a suffixed twin ([workspace.md](./workspace.md) § Layout).

A `.md` or `.markdown` file, or one sent as `text/markdown`, other than an `AGENTS.md` (which is kept as a file, [chat.md](./chat.md) § AGENTS.md):

- It is not parsed: it streams to the upload directory under `CONTENT_MAX_BYTES` (**413** past it) and must be UTF-8 (**400** otherwise).
- It is filed as a markdown document under a name ending in `.md`, added when the file's name has neither `.md` nor `.markdown` ([workspace.md](./workspace.md) § Markdown documents).
- No job; its upload is kept as its original, with the same 409 when that cannot be kept.

**Any other file is kept as a file** ([workspace.md](./workspace.md) § Files):

- It streams straight into the user's workspace, to a `.sr-tmp-upload-*` name at the top of `files/`. One an upload died in is swept by the next upload, the next reconciliation or the worker's sweep ([workspace.md](./workspace.md) § Files).
- Under `FILE_MAX_BYTES`: **413** past it, by declared length or as the bytes arrive.
- Under the plan's storage: **409** `storage_full` when it would not fit ([workspace.md](./workspace.md) § Quota).
- It is renamed into place under the workspace lock as its row is written: no job, no original, no baseline.
- 201 with the document, `kind` `file`.

**`POST /api/library/documents/new`**

- JSON `{name, folder_id?}` → 201 with the document: a row with no job and no original, empty until it is written.
- A name without `.md` or `.markdown` gets `.md` ([workspace.md](./workspace.md) § Markdown documents).
- A name already taken in that folder is suffixed ` (2)`, as a new folder's is.
- 404 for a folder that is not the caller's.

**`PUT/DELETE /api/library/documents/{id}`**

- A markdown document renamed without `.md` or `.markdown` gets `.md` ([workspace.md](./workspace.md) § Markdown documents).
- Both answer 409 while a conversation about the file is still answering, as does deleting a folder that holds such a file.
- Deleting removes the entry, not the job or its result, which then fall under the parse retention ([jobs.md](./jobs.md) § Retention).
- `DELETE` is *Delete Immediately*: it takes a live file, or one in the Trash for good (the Trash's own copy of it with it).
- A file inside a trashed folder is not addressable on its own (404): the folder is what the Trash holds.

**Move to Trash** (`POST …/trash`)

- The row is marked and its directory leaves `files/` for the Trash, as one revision → 200 with the item's payload, `trashed_at` set.
- A folder goes with everything in it.
- 404 for an item that is not live (already in the Trash, or inside a folder that is).
- 409 while a conversation about a file in it is still answering, as for a delete.

**Put Back** (`POST …/restore`)

- To the folder it was trashed from when that folder is live, else to the nearest live folder above it, else the top level.
- A name taken there meanwhile is suffixed ` (2)`, as a move's is.
- The directory comes back from the Trash as one revision → 200 with the payload as it is now.
- 404 for an item that was not trashed itself (a live one, or one inside a trashed folder).

**Empty Trash** (`DELETE /api/library/trash`)

- Every item in it, for good, rows and files → 204.
- The worker deletes an item the same way, by the same code, once its own `trashed_at` is older than `TRASH_RETENTION_DAYS` ([workspace.md](./workspace.md) § The Trash).

**A copy** (`POST …/copy`)

- JSON `{folder_id}` (a folder's: `{parent_id}`), `null` or absent for the top level → 201 with the new item.
- A document is copied with its own `doc.md`, `notes.md`, original and inserted images as they are now; a folder with every live folder and document under it ([workspace.md](./workspace.md) § Copies).
- The parse behind a document is shared, not repeated: the copy names the same job.
- Conversations and memories stay with the original.
- The name is the original's, with ` (2)` where the destination already holds it: a copy into the same folder is a duplicate.
- 404 for a source or a destination that is not live.
- 400 for a folder copied into its own subtree.
- 409 when the workspace is at its quota, or when the item's files wait in the Trash after an interrupted move and could not be brought back ([workspace.md](./workspace.md) § Copies).

**A document in the Trash**

- Every `/api/library/documents/{id}/…` route but `restore` and `DELETE` — content, notes, reverts, the result, figures and images, conversations — and `POST /api/library/images` answer 404 for a document in the Trash or inside a folder that is ([database.md](./database.md) § Rules).
- So do a move, an upload or a copy into a trashed folder.
- A trashed file is not opened, edited, talked about or written into until it is put back.

**`GET /api/library/documents/{id}/file`**

- The one copy, read from the tree, with its `media_type`, its length and a `Content-Disposition`.
- `inline` for what a browser shows safely on its own: an image, a video or audio file, a PDF, plain text.
- `attachment` for everything else, HTML, SVG and XML included, since a page served from this origin would run with the session. `?download=1` forces `attachment`.
- Always under a policy of its own, which the Caddyfile leaves to this route ([deployment.md](./deployment.md) § Security headers):
  - `Content-Security-Policy: sandbox` with `X-Frame-Options: DENY`;
  - except a PDF served inline, which a browser's viewer will not open in a sandbox: that answer carries `frame-ancestors 'self'` and `SAMEORIGIN`, so the result viewer's Original frame on this origin may show it and no other page may;
  - plus `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`.
- One `Range` answers 206 (**416** past the end; several ranges are answered whole), for the video and audio a browser seeks in.
- 404 for a parse or a markdown document, which have no file of this kind: a parse's original is `…/original` whole and `…/pages/{n}/image` one page at a time, and their text `…/content`.

**`GET /api/library/documents/{id}/original`**

- The upload the parse was made from, kept beside the git directory, for the result viewer's *Original* view ([web.md](./web.md) § Result viewer, *Original and Markdown*).
- Served as `…/file` serves a file: inline, its media type the parse's, its length, one `Range` answered 206, the same security headers (a PDF framed by this origin alone, an image sandboxed), `nosniff`, never cached.
- `?download=1` forces `attachment`, for a failed parse's *Download original* ([web.md](./web.md) § Files).
- Until a finished parse's original is filed ([jobs.md](./jobs.md) § Retention), the upload it is filed from answers in its place for the row it is filed for (`original` in the library read, above), so the moment after a parse ends never reads as an original lost.
- 404 for a markdown document, for a file (its bytes are `…/file`), for a document in the Trash, and for a parse whose original was not kept (`original: false` in the library read).

**`POST /api/library/documents/{id}/convert`**

- JSON `{view?}`.
- `view` (`original` or `markdown`, **400** otherwise, absent or `null` for none) is written to the row as the view the document opens on. That is how *Markdown* chosen in the result viewer makes the document open on Markdown once it is converted ([web.md](./web.md) § Result viewer, *Original and Markdown*).
- Only a file intake would parse — a PDF or an image — converts: **409** `not_convertible` otherwise; 404 for a document.
- Under the workspace lock, the file is read in place and copied into the upload directory as the job's source, hashed on the way as an upload is, so a file over `UPLOAD_MAX_BYTES` answers **413** before anything is queued.
- The file is then moved out of the tree, and the row becomes a parse naming the job, so its id, name and place stay. A row that does not commit brings the file back.
- The parse's credits are reserved as for an upload: **402** `insufficient_credits` leaves the file where it was.
- The per-user cap and the dedup cache apply as to an upload (429; a hit is `succeeded` at once).
- 200 with the document, its state as a queued upload's. When the parse ends, the worker files the source as the document's original and `source.<ext>` as for any upload ([jobs.md](./jobs.md) § Retention).

**`PUT /api/library/documents/{id}/view`**

- JSON `{view}`, `original` or `markdown`: the view the reader just switched a PDF or an image to in the result viewer, remembered on the row (`view` in the library read) so the document opens on it next time ([web.md](./web.md) § Result viewer, *Original and Markdown*) → 204.
- **400** for any other value, and for a document with one view: a markdown document, a file that is not a PDF or an image, and `markdown` on a PDF or an image not yet converted, which `…/convert` takes instead.
- 404 for a document that is not live.
- Written without the workspace lock: it changes the row and nothing in the tree.

**`GET /api/library/documents/{id}/content`**

- `{markdown, revision, notes, notes_revision}`, materialized on first read ([workspace.md](./workspace.md)).
- **409** while the parse behind the document has no result yet. Files opens a parse only once it has one, and `doc.md` starts from the result's reading copy when the parse was checked ([parsing.md](./parsing.md) § Format check).
- **409** when a file is not a regular file or is over `CONTENT_MAX_BYTES`.
- **404** for a file of the library ([workspace.md](./workspace.md) § Files), which has no editable copy: its bytes are `…/file`.

**`PUT /api/library/documents/{id}/content`**

- `{markdown, base_revision}` → `{revision}`.
- **409** of type `revision_conflict` when `base_revision` is not the file's current revision: the one 409 the viewer answers by reloading.
- A 409 of any other type (the workspace recovering from an unconfirmed shell command, a revision that could not be recorded) leaves the reader's draft unsaved, to be retried.
- Every route that takes the workspace lock — this one, notes, moves, deletes, reverts, content reads — answers **409** *the workspace is recovering from a shell command that could not be confirmed finished* while such a command may still be writing the tree: a dead worker's, or one the runner could not vouch for. It answers so until a recovery has confirmed the shell stopped and recorded the tree ([workspace.md](./workspace.md) § Revisions).

**`POST /api/library/documents/{id}/revisions/{commit}/revert`**

- Writes every path that commit touched as it was before it (where it lives now; a rename moved back, the library reconciled), as a new revision → `{revision}` of this document and the `{commit}` the revert made.
- **409** while a turn on this document is unfinished.
- For a revision that touched paths beyond this document, **409** while any turn of a conversation about one of the user's documents is unfinished, a library conversation's not counted ([workspace.md](./workspace.md) § Revisions).

**`GET /api/library/documents/{id}/result`**

- The data plane's `/result` shape, except that `markdown` is the exact transcription (`results.markdown`), not the reading copy ([parsing.md](./parsing.md) § Format check); plus partial results (below).
- 404 for a markdown document, which has none (its text is `content`), and for a file, whose bytes are `…/file`.
- Every other route of a document's own (the revert, conversations, pages, figures, images, retry, re-convert) answers a file with 404 the same way ([workspace.md](./workspace.md) § Files).

**`GET /api/library/documents/{id}/figures/{page}/{ymin},{xmin},{ymax},{xmax}`**

- One stored figure crop of the document's parse as `image/png`, addressed exactly as its `sightread://` placeholder addresses it.
- A crop its parse does not hold is looked for among the caller's other succeeded parses of the same original, so a placeholder from before a re-convert keeps its picture ([parsing.md](./parsing.md) § Figure crops).
- 404 when no crop was stored (a failed crop), and for a markdown document.
- Served on the data plane too, for a job (`/v1/jobs/{id}/figures/…`) and for a library document (`/v1/library/documents/{id}/figures/…`) ([parsing.md](./parsing.md) § Figure crops).

**`GET /api/library/documents/{id}/pages`**

- The parse page by page — the failed re-convert's, when the document has one, since that is what a retry would send: `{pages: [{page, status: succeeded|failed|pending, reason, model}]}`.
- One entry per page of the parse's selection, in order.
- `pending` has no row yet: queued, running, or never reached before a job-ending failure.
- `reason` is a failed page's ([parsing.md](./parsing.md) § Vision-only conversion).
- `model` is the model that wrote or failed it.
- What the retry dialog lists ([web.md](./web.md) § Files). 404 for a markdown document.

**`GET /api/library/documents/{id}/pages/{page}/image`**

- One page of the document's original as `image/png` — Poppler's render for a PDF, the image itself for an image — scaled to a long edge of `PAGE_PREVIEW_LONG_EDGE`, with `Cache-Control: private, no-store`.
- It is the retry dialog's picture of a failed page.
- At most `PAGE_PREVIEW_RENDERS` previews are made at once per API process; a request beyond waits for one.
- 404 for a markdown document, a page outside the document, or a document that kept no original.

**`POST /api/library/documents/{id}/retry`**

- JSON `{model?}`: `model` a model id of at most 255 characters, one the parse agent runs (**400** otherwise, [agents.md](./agents.md) § Enqueue rules); default: the job's own while it is one of the parse agent's models, else `PARSE_MODEL` (**503** with no `PARSE_MODEL`).
- The job behind the document — or its failed re-convert, when the document's own parse succeeded — goes back on the queue keeping its succeeded pages, and runs the rest on that model ([jobs.md](./jobs.md) § Retrying failed pages) → 202 `{job_id, status: "queued"}`.
- 404 for a markdown document.
- **402** `insufficient_credits` when the balance cannot cover the job's credits, which the retry reserves again ([billing.md](./billing.md) § A parse).
- **409** when neither the parse nor a re-convert is `failed`, or when the document kept no original to run on.
- **409** while its original is still being filed: the moment after the failure, until its retention has run. Retried a moment later, it runs.

**`POST /api/library/documents/{id}/reconvert`**

- JSON `{model?}`: `model` a model id of at most 255 characters; default: the parse agent's own.
- A new parse of the document's original, with the parse agent's settings of the moment and the format check as a Files upload takes it, never answered from the dedup cache.
- The document keeps its version until that parse succeeds, then takes it as one revision ([jobs.md](./jobs.md) § Re-converting a document) → 202 `{job_id, status: "queued"}`.
- 404 for a markdown document.
- **409** when the document's parse has not succeeded, when a re-convert is queued or running, or when the document kept no original.
- **409** while its original is still being filed: the moment after the parse ended, until its retention has run. Re-converted a moment later, it runs.
- 429 at the per-user job cap, as an upload.
- The enqueue rules' 400s and 503 for a model or an upstream the settings cannot run ([agents.md](./agents.md) § Enqueue rules).

**`DELETE /api/library/documents/{id}/reconvert`**

- → 204: the document stops pointing at the failed re-convert.
- The failed parse, of the document's own original, is kept with it by the parse retention ([jobs.md](./jobs.md) § Retention).
- 404 when the document has no re-convert; **409** unless it failed.

**`POST /api/library/images`**

- Multipart `document_id` + `file`, streamed to disk under `DOCUMENT_IMAGE_MAX_BYTES` (**413** past it).
- The file must decode as a PNG, JPEG, WebP, GIF or HEIC/HEIF image of at most `CHAT_PAGE_IMAGE_MAX_PIXELS`, read from its header (**400** otherwise): the decoder is never offered another format.
- It is prepared and kept as the document's image ([workspace.md](./workspace.md) § Inserted images) → 201 `{path}`, the `figures/img-<hash>.png` (or `.jpg`) the markdown line names.
- 404 for a document that is not the caller's.
- 409 when the quota has no room, or for a parsed document whose result has not arrived yet (it has no directory to hold the image).
- The text is the caller's to write: the route changes no `doc.md` or `notes.md`.

**`GET /api/library/documents/{id}/images/{name}`**

- The document's inserted image, `img-<hash>.png` or `.jpg` and nothing else, with its media type and `Cache-Control: private, no-store` (as a crop is, [parsing.md](./parsing.md) § Figure crops).
- 404 for any other name or an image the document does not hold.

### Partial results (control plane only)

While a job is still `queued` or `running`, the control-plane result route (`GET /api/library/documents/{id}/result`) answers **200 with a partial result** instead of 404:

- The markdown is the finished pages' stored markdown ([jobs.md](./jobs.md) § Progress), assembled exactly like the final document: page markers, renumbered figure placeholders.
- `meta.partial` is `true`.
- `pages` lists only the finished pages.
- `meta.page_count` and `meta.pages_done` carry live progress.

Figure ids are per-snapshot: a page finishing out of order renumbers them. So nothing may cache a partial result; the viewer re-reads until `partial` disappears.

The data plane has no partial result: `/v1/jobs/{id}/result` stays 404 until the job finishes, and SSE is its progressive interface.

### Why the web upload is a control-plane route

`POST /api/library/documents` duplicates no logic: it reads a multipart body and hands it to `jobs.intake.submit_parse`, exactly as `POST /v1/parse` does.

It is a separate route because the two planes authenticate differently and must keep doing so:

- The data plane is bearer-only (API key, OAuth token, upload ticket).
- The control plane is session cookie plus the `X-Requested-With` CSRF pairing.

Teaching `/v1/parse` to accept a cookie would put a CSRF-shaped hole in the product's own API. Teaching the web app to mint itself an API key would put a durable credential in a browser. So the browser posts to the control plane with the credential it already has, and the bytes take the path every other `/api/*` request takes: browser → Caddy → FastAPI. Nothing document-shaped enters the Nuxt server ([web.md](./web.md) § Rules).

## Limits (defaults, env-overridable)

### Uploads, files and storage

| Variable | Default | What it caps |
|---|---|---|
| `UPLOAD_MAX_BYTES` | 134217728 (128 MiB) | a file uploaded to be parsed; enforced in the app |
| `FILE_MAX_BYTES` | 2000000000 (2 GB) | the most a file of any other kind may be when uploaded to the library ([workspace.md](./workspace.md) § Files) |
| `REVISION_MAX_FILE_BYTES` | 1048576 (1 MiB) | the largest file a revision records, beside the binary test ([workspace.md](./workspace.md) § Revisions) |
| `DOCUMENT_IMAGE_MAX_BYTES` | 20971520 (20 MiB) | one image inserted into a document ([workspace.md](./workspace.md) § Inserted images) |
| `DOCUMENT_IMAGE_LONG_EDGE` | 2400 px | the long edge an inserted image is kept at |
| `WORKSPACE_QUOTA_BYTES` | 10 GiB | the ceiling every plan's storage must fit under; a user's cap is their plan's ([workspace.md](./workspace.md) § Quota, [billing.md](./billing.md) § Plans) |
| `TRASH_RETENTION_DAYS` | 30 | days an item stays in the Trash before the worker deletes it for good ([workspace.md](./workspace.md) § The Trash) |
| `PARSE_RETENTION_DAYS` | 30 | days a finished parse no file names is kept ([jobs.md](./jobs.md) § Retention) |
| `UPLOAD_MAX_AGE_SECONDS` | 86400 (a day) | how old an upload entry grows before the sweeper takes it, an unsent attachment's included ([chat.md](./chat.md)) |

- `TRASH_RETENTION_DAYS` = 0 or `PARSE_RETENTION_DAYS` = 0 turns that one off.
- Caddy's `@rest` cap must cover `UPLOAD_MAX_BYTES` in base64 plus 64 KiB, so raise that with it ([deployment.md](./deployment.md) § Caddy routing).
- `FILE_MAX_BYTES` is enforced in the app and, on that one route (`POST /api/library/documents`), in Caddy ([deployment.md](./deployment.md) § Caddy routing).

### Parsing

| Variable | Default | What it caps |
|---|---|---|
| `PAGE_CAP` | 500 | |
| `MAX_JOBS_PER_USER` | 2 | concurrent jobs: the ceiling; what a user may run at once is their plan's `maxJobs` under it ([billing.md](./billing.md) § Plans) |
| `VISION_CONCURRENCY_PER_JOB` | 8 | |
| `VISION_MAX_OUTPUT_TOKENS` | 16000 | per vision answer ([parsing.md](./parsing.md) § Upstream usage) |
| `FIGURES_PER_PAGE_MAX` | 40 | stored figure crops per page, and the boxes a page's figure check reads ([parsing.md](./parsing.md) § Figure crops) |
| `FIGURE_CHECK_ROUNDS` | 2 | model rounds one page's figure check may take, one call each; at least 1 ([parsing.md](./parsing.md) § Figure check) |
| `PAGE_PREVIEW_LONG_EDGE` | 1200 px | a page preview's long edge |
| `PAGE_PREVIEW_RENDERS` | 2 | previews made at once per API process |
| `CHECK_SPOTS_MAX` | 80 | spots sent to the check agent ([parsing.md](./parsing.md) § Format check) |
| `CHECK_MAX_CALLS` | 16 | calls one check may make |
| `CHECK_MAX_OUTPUT_TOKENS` | 16000 | per check answer |

### Upstream responses

| Variable | Default | What it caps |
|---|---|---|
| `UPSTREAM_RESPONSE_MAX_BYTES` | 33554432 (32 MiB) | any upstream response body (model catalogs and vision completions); beyond it the call fails as `upstream` ([parsing.md](./parsing.md) § Upstream usage) |
| `UPSTREAM_SSE_LINE_MAX_BYTES` | 4194304 (4 MiB) | one line of the chat agent's event stream ([chat.md](./chat.md) § Upstream) |
| `UPSTREAM_CONTENT_BLOCKS_MAX` | 4096 | the content blocks one answer may hold ([chat.md](./chat.md) § Upstream) |

### The chat agent

| Variable | Default | What it caps |
|---|---|---|
| `SYSTEM_PROMPT_MAX_CHARS` | 16000 | an agent's prompt, a skill's body and a subagent's prompt |
| `CHAT_INSTRUCTIONS_MAX_CHARS` | 8000 | the chat agent's instructions ([chat.md](./chat.md) § Instructions) |
| `CHAT_MEMORY_MAX_CHARS` | 500 | one memory ([chat.md](./chat.md) § Memory) |
| `CHAT_MEMORIES_MAX` | 200 | a user's memories |
| `CHAT_MEMORY_BLOCK_MAX_CHARS` | 12000 | the memories one conversation loads |
| `CHAT_SECTIONS_PER_MESSAGE` | 4 | whole sections added to one message ([chat.md](./chat.md) § What a user message carries) |
| `CHAT_SECTION_HEADING_MAX_CHARS` | 512 | the heading of one such section |
| `CHAT_LIBRARY_ITEMS_PER_MESSAGE` | 200 | Files-page items one library conversation's message carries ([chat.md](./chat.md) § Library conversations) |
| `CHAT_LIBRARY_MAX_PATHS` | 100 | paths one library tool call takes |
| `CHAT_LIST_FILES_MAX_ENTRIES` | 500 | lines one `list_files` answers |
| `CHAT_ATTACHMENT_MAX_BYTES` | 20971520 (20 MiB) | per attached file ([chat.md](./chat.md)) |
| `CHAT_ATTACHMENTS_PER_MESSAGE` | 8 | |
| `CHAT_ATTACHMENT_TEXT_MAX_CHARS` | 100000 | of a PDF's or text file's text |
| `CHAT_ATTACHMENT_IMAGE_LONG_EDGE` | 1568 px | |
| `CHAT_ATTACHMENTS_PENDING_MAX` | 32 | a user's attachments waiting for a message at once |
| `CHAT_ATTACHMENT_CACHE_SECONDS` | 31536000 (a year) | how long the browser may cache an attached image |
| `CHAT_ATTACHMENT_PDF_TIMEOUT_SECONDS` | 60 | for a PDF's text |
| `CHAT_SKILLS_MAX` | 64 | a user's own skills ([chat.md](./chat.md)) |
| `CHAT_SUBAGENTS_MAX` | 32 | a user's own subagents |
| `CHAT_SUBAGENT_MAX_TOOL_CALLS` | 1000 | per subagent run |

The chat agent's default prompt is about 13,200 characters, its AGENTS.md section ([chat.md](./chat.md) § AGENTS.md) included. A deployment that sets `SYSTEM_PROMPT_MAX_CHARS` must keep it above that, or a reader's edit of the default is refused. A prompt equal to the default is stored as none and never measured against the cap.

### Chat turns

Specified in [chat.md](./chat.md) and [sandbox.md](./sandbox.md).

| Variable | Default | What it caps |
|---|---|---|
| `MAX_CHAT_TURNS_PER_USER` | 4 | concurrent chat turns (and so the shell commands running at once) |
| `CHAT_MAX_OUTPUT_TOKENS` | 64000 | |
| `CHAT_MAX_TOOL_CALLS_PER_TURN` | 1000 | |
| `CHAT_OVERDRAFT_MAX_TOOL_CALLS` | 25 | more tool calls once the credits run out mid-turn ([billing.md](./billing.md) § A chat turn) |
| `CHAT_TURN_TIMEOUT_SECONDS` | 3600 | |
| `CHAT_STREAM_TIMEOUT_SECONDS` | 900 | |
| `CHAT_WRITER_GRACE_SECONDS` | 60 | |
| `REVERT_MAX_BYTES` | 67108864 | |
| `REVERT_MAX_PATHS` | 2000 | |
| `WORKSPACE_GIT_TIMEOUT_SECONDS` | 60 | |
| `CHAT_MESSAGE_MAX_CHARS` | 32000 | |
| `CHAT_SELECTION_MAX_CHARS` | 16000 | |
| `CHAT_TOOL_RESULT_MAX_CHARS` | 20000 | |
| `CHAT_IMAGE_ROUND_MAX_BYTES` | 33554432 | |
| `CHAT_IMAGE_TURN_MAX_BYTES` | 134217728 | |
| `SANDBOX_CONTROL_TIMEOUT_SECONDS` | 180 | |

### The chat agent's web tools

Specified in [chat.md](./chat.md) § Tools.

| Variable | Default | What it caps |
|---|---|---|
| `WEB_SEARCH_MAX_PER_TURN` | 10 | |
| `WEB_SEARCH_MAX_PER_DAY` | 2000 | the deployment's |
| `PAPER_SEARCH_MAX_PER_TURN` | 10 | |
| `PAPER_SEARCH_MAX_PER_DAY` | 1000 | the deployment's |
| `READ_URL_MAX_PER_TURN` | 30 | |
| `READ_URL_MAX_BYTES` | 5242880 | |
| `DOWNLOAD_MAX_BYTES` | 100000000 | |
| `DOWNLOAD_MAX_PER_DAY` | 20 | per user |
| `WEB_TOOL_TIMEOUT_SECONDS` | 30 | |
| `DOWNLOAD_TIMEOUT_SECONDS` | 120 | |

### Request bodies

Request bodies are capped at six times `CONTENT_MAX_BYTES` plus 64 KiB: **413** past it, by declared length or as the bytes arrive.

- Six times is the worst case of JSON escaping: a one-byte control character becomes a six-byte `\uXXXX`, and an emoji's four bytes a twelve-byte surrogate pair. So no document under the content cap is refused for its encoding.
- A multipart body is exempt on the four routes that stream an upload to disk under a cap of their own:
  - `POST /v1/parse` under `UPLOAD_MAX_BYTES`.
  - `POST /api/library/documents`: a file it parses under `UPLOAD_MAX_BYTES`, a markdown file under `CONTENT_MAX_BYTES`, a file of any other kind under `FILE_MAX_BYTES`.
  - `POST /api/attachments` under `CHAT_ATTACHMENT_MAX_BYTES`.
  - `POST /api/library/images` under `DOCUMENT_IMAGE_MAX_BYTES`.
- A multipart label on any other route buys no exemption.
- The label is read in any letter case, and of a repeated `Content-Type` the first one counts, as the route reads it.
- A JSON body on those four routes is counted too: on `POST /v1/parse` against a ceiling of its own (§ POST /v1/parse), on the others against the cap above.

### Refusals

- **402** `insufficient_credits`, with `need`, `remaining` and `period_end` inside `error`, when the parse's pages cost more than the balance: the period's and the active top-up packs' together ([billing.md](./billing.md) § Refusals, [billing.md](./billing.md) § Top-up packs). The upload is not stored, and an upload ticket is not spent.
- A chat send under the minimum balance answers the same 402.
- **429** with `Retry-After` when the per-user job cap is hit.
- An upstream 402 (the upstream's credits or allowance exhausted) maps to `payment` and fails the affected pages. The other pages still run, and the job fails with them at its end.

### The operator console and redeem codes

The `ADMIN_*` limits are specified in [admin.md](./admin.md) § Configuration.

| Variable | Default | What it caps |
|---|---|---|
| `ADMIN_PAGE_SIZE` | 50 | rows a page of an operator-console list holds; at most 500 |
| `ADMIN_REAUTH_HOURS` | 12 | hours a session may be old for the console |
| `ADMIN_GRANT_VALID_DAYS` | 365 | days granted credits are valid unless the operator chooses otherwise |
| `ADMIN_GRANT_MAX_CREDITS` | 1000000 | the most credits one grant may give |
| `ADMIN_GRANT_MAX_DAYS` | 3650 | the most days one grant may give |
| `REDEEM_CODES_MAX_COUNT` | 1000 | redeem codes one create makes, in the console and with the CLI; at most 10,000 ([billing.md](./billing.md) § Redeem codes) |
