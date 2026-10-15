# Database

PostgreSQL 16+. Schema changes **only** via Alembic migrations under `apps/api/migrations/`, this doc updated first. Never edit production schema by hand.

## Tables

```text
users            id PK, google_sub UNIQUE, email, name,
                 picture NULL,                    -- Google avatar URL, refreshed each sign-in
                 created_at
sessions         id PK, user_id FK, token_hash UNIQUE, created_at, expires_at
api_keys         id PK, user_id FK, name, key_hash UNIQUE, prefix, created_at,
                 last_used_at, revoked_at NULL
openrouter_keys  user_id PK/FK, ciphertext BYTEA, masked, updated_at        -- one per user
provider_connections
                 id PK, user_id FK, name, base_url,
                 ciphertext BYTEA, masked,        -- the connection's API key, AES-GCM
                 model NULL,                      -- the connection's fixed parsing model,
                                                  -- picked in the connection dialog; NULL
                                                  -- only on rows created before the column
                 report_cost BOOL NOT NULL DEFAULT false,
                                                  -- record the usage.cost this endpoint
                                                  -- reports, as an estimate (parsing.md
                                                  -- § Upstream usage); opt-in, never
                                                  -- inferred from base_url
                 created_at, updated_at
                 UNIQUE (user_id, name)
                 -- user-defined OpenAI-compatible endpoints (e.g. a kano-proxy
                 -- /openai/v1 base); a connection is a complete profile — endpoint,
                 -- key and model travel together; the built-in OpenRouter upstream
                 -- is NOT a row here
prompt_presets   id PK, user_id FK, name, text TEXT, created_at, updated_at
                 UNIQUE (user_id, name)
                 -- named transcription prompts; selecting one replaces the template
                 -- entirely (docs/parsing.md § Prompts)
user_settings    user_id PK/FK, default_model, default_profile,
                                                  -- OpenRouter-only defaults: a selected
                                                  -- connection carries its own model
                 default_connection_id FK NULL,   -- provider_connections; NULL = OpenRouter
                 prompt_preset_id FK NULL         -- prompt_presets; NULL = default prompt
                 -- both FKs ON DELETE SET NULL: deleting a connection/preset falls back
                 -- to OpenRouter / the default prompt
jobs             id UUID PK, user_id FK, kind pdf|image, filename, media_type,
                 size_bytes, sha256, pages_spec, model, profile, profile_version,
                 pipeline_version, bbox_format,
                 connection_id NULL,              -- provider connection; NULL = OpenRouter.
                                                  -- deliberately NO FK: immutable history
                                                  -- (see Rules)
                 connection_base_url NULL,        -- endpoint snapshot at enqueue; part of
                                                  -- the dedup key (jobs.md § Dedup)
                 prompt TEXT NULL,                -- effective prompt template, verbatim
                 prompt_sha256,                   -- its hash; part of the dedup key
                 status queued|running|succeeded|failed, error,
                 page_count, pages_done, source_path NULL, source_deleted_at NULL,
                 created_at, started_at, finished_at
                 INDEX (status, created_at)                                  -- queue claim
                 INDEX (user_id, sha256, model, connection_id, profile, profile_version,
                        pages_spec, prompt_sha256, pipeline_version)
                        WHERE status='succeeded'                             -- dedup
job_pages        (job_id FK, page_no) PK, method NULL, status, error NULL,
                 markdown TEXT NULL               -- the page's transcription, written as
                                                  -- the page finishes; what the partial
                                                  -- result reads (jobs.md § Progress);
                                                  -- NULL on failed pages
results          job_id PK/FK, markdown TEXT, pages JSONB, figures JSONB,
                 errors JSONB, meta JSONB, created_at
folders          id PK, user_id FK, parent_id FK folders NULL, name,
                 created_at, updated_at
                 UNIQUE (user_id, parent_id, name)
                 UNIQUE (user_id, name) WHERE parent_id IS NULL   -- the root level
                 -- the web file library's directories; parent_id NULL = root,
                 -- ON DELETE CASCADE on the self-reference, so deleting a folder
                 -- takes its subtree with it
documents        id PK, user_id FK, folder_id FK folders NULL, job_id FK jobs,
                 name, created_at, updated_at
                 UNIQUE (user_id, folder_id, name)
                 UNIQUE (user_id, name) WHERE folder_id IS NULL
                 INDEX (user_id, created_at)
                 -- one row = one file in the library = one parse result
                 -- (docs/web.md § Files). folder_id NULL = root
usage_log        id PK, user_id FK, job_id FK, model, prompt_tokens,
                 completion_tokens,
                 cost NUMERIC(12,6) NULL,         -- NULL exactly when cost_source = unknown
                 cost_source openrouter|estimated|unknown,
                                                  -- where the cost came from: OpenRouter's
                                                  -- billing, a connection's reported
                                                  -- estimate, or nobody (parsing.md
                                                  -- § Upstream usage)
                 created_at
                 INDEX (user_id, created_at)
                 CHECK (cost_source IN (...) AND (cost_source = 'unknown') = (cost IS NULL))
upload_tickets   id PK, user_id FK, token_hash UNIQUE, prefix, job_id FK NULL,
                 created_at, expires_at, spent_at NULL
                 INDEX (user_id, created_at)                                 -- mint rate limit
                 -- single-use upload credential minted by the MCP `parse` tool
                 -- (auth.md § 5); job_id set when the upload spends the ticket
oauth_clients    client_id PK, client_name, redirect_uris JSONB, created_at
oauth_grants     id PK, client_id FK, user_id FK, kind code|access|refresh,
                 token_hash UNIQUE, pkce_challenge NULL, redirect_uri NULL,
                 scope, expires_at, revoked_at NULL, created_at
                 -- redirect_uri is set on `code` rows only: the token endpoint
                 -- checks a code came back from the URI it was issued for
```

## Rules

- All credentials at rest follow [auth.md](./auth.md): hashes for anything we only verify, AES-GCM ciphertext for what we must replay upstream (the OpenRouter key and every provider connection's key). No plaintext secrets in any column.
- `user_settings.system_prompt` no longer exists: the migration turned each stored custom prompt into a `prompt_presets` row named "Custom prompt" and pointed `prompt_preset_id` at it, so behavior did not change for anyone.
- **Editing** a connection's model only affects new jobs: `jobs.model` is snapshotted at enqueue like every other parse parameter. Editing its URL or key applies to jobs still queued on it — the worker resolves the row at run time, the same semantics as rotating the OpenRouter key while jobs are queued. Deliberate: snapshotting encrypted credentials onto job rows would copy secrets around, and a user edits their own connection precisely so pending work uses the fix. The dedup cache is protected separately by the `connection_base_url` snapshot ([jobs.md](./jobs.md) § Dedup).
- `jobs.connection_id` carries **no foreign key** on purpose: the upstream a job ran on is immutable history. A FK with SET NULL would relabel a deleted connection's jobs as OpenRouter jobs — a still-queued one would then run against OpenRouter billing the wrong key, and a succeeded one would answer OpenRouter dedup lookups with another upstream's output; RESTRICT would make a connection undeletable once any job exists. Instead the id simply outlives the row: the worker fails a queued job whose connection is gone ("the provider connection for this job no longer exists"), and dedup keys keep matching only their own upstream (connection ids are never reused).
- **A usage row's cost is only as good as its source.** `cost_source` is written with the row and never revised: `openrouter` is billing, `estimated` is what the endpoint said the call cost, `unknown` has `cost` NULL. The migration that added the column backfilled it from the job each row belongs to — rows of a job with a `connection_id` became `unknown` with `cost` NULL (their reported cost had been discarded and 0 recorded in its place, which was never a price), every other row `openrouter` with its cost untouched. Rows with no job left (`job_id` NULL) count as `openrouter`: jobs are only ever deleted with their user, and the user's usage goes with them. `provider_connections.report_cost` follows the URL and key rule below — the worker reads it when the job runs, so toggling it affects calls made from then on, including jobs already queued, and never rewrites rows already recorded.
- **A document is a library entry, not a second copy of the result.** `documents` names a parse and places it in a folder; the markdown lives in `results` exactly once, keyed by the job that produced it. Deleting a document deletes the entry only — the job stays in the parse history and its `usage_log` rows stay, because they are the billing record of something that really ran. Deleting a folder cascades to its subfolders and their documents, the way removing a directory does.
- **The root level needs its own unique index.** `folders.parent_id` and `documents.folder_id` are NULL at the root, and PostgreSQL treats NULLs as distinct in a unique constraint, so `UNIQUE (user_id, parent_id, name)` would not stop two root folders called "Invoices". The partial indexes above close that. The application checks the same thing before writing, which is what turns a collision into `report (2).pdf` instead of a 409.
- `results` holds parsed **output** (kept indefinitely); `jobs.source_path` points at a temp file that is deleted at terminal state — the DB never stores document bytes.
- Job claiming and per-user caps: exact queries in [jobs.md](./jobs.md).
- Timestamps are `timestamptz`, UTC.
