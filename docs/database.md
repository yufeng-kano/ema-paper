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
kano_proxy_keys  user_id PK/FK, ciphertext BYTEA, masked, updated_at        -- one per user:
                                                  -- the Kano Proxy key the connect flow
                                                  -- obtained (auth.md § 3), AES-GCM
kano_connect_states
                 id PK, user_id FK, state_hash UNIQUE,
                 verifier_ciphertext BYTEA,       -- the PKCE code_verifier, AES-GCM
                 created_at, expires_at
                 -- one pending Kano Proxy connect (auth.md § 6): single use — consuming
                 -- a state deletes its row; expired rows are deleted on the user's next
                 -- connect
provider_connections
                 id PK, user_id FK, name, base_url,
                 ciphertext BYTEA, masked,        -- the connection's API key, AES-GCM
                 report_cost BOOL NOT NULL DEFAULT false,
                                                  -- record the usage.cost this endpoint
                                                  -- reports, as an estimate (parsing.md
                                                  -- § Upstream usage); opt-in, never
                                                  -- inferred from base_url
                 created_at, updated_at
                 UNIQUE (user_id, name)
                 -- user-defined OpenAI-compatible endpoints: name, endpoint, key and the
                 -- cost flag, nothing else — the model is chosen per agent
                 -- (agent_settings); the built-in Kano Proxy upstream is NOT a row here
prompt_presets   id PK, user_id FK, agent, name, text TEXT, created_at, updated_at
                 UNIQUE (user_id, name)
                 -- named prompts for one agent (agents.md); selecting one replaces that
                 -- agent's template entirely (docs/parsing.md § Prompts)
agent_settings   (user_id FK, agent) PK,
                 connection_id FK NULL,           -- provider_connections; NULL = Kano Proxy
                 model NULL,                      -- a model id on that upstream; NULL =
                                                  -- not chosen yet
                 prompt_preset_id FK NULL         -- prompt_presets; NULL = the agent's
                                                  -- default prompt
                 -- one row per user and agent (agents.md); no row means every column's
                 -- default. Both FKs ON DELETE SET NULL; deleting a connection also clears
                 -- the model, which belonged to that connection's catalog
jobs             id UUID PK, user_id FK, agent, kind pdf|image, filename, media_type,
                 size_bytes, sha256, pages_spec, model,
                 pipeline_version, bbox_format,
                 source_job_id NULL,              -- the job whose output this job took as
                                                  -- input; NO FK (immutable history, like
                                                  -- connection_id). NULL for `parse`, whose
                                                  -- input is an upload
                 connection_id NULL,              -- provider connection; NULL = the
                                                  -- built-in upstream (Kano Proxy; OpenRouter
                                                  -- on historical rows). Deliberately NO FK:
                                                  -- immutable history (see Rules)
                 connection_base_url NULL,        -- endpoint snapshot at enqueue: the
                                                  -- connection's base_url, or
                                                  -- KANO_PROXY_BASE_URL for the built-in
                                                  -- upstream (NULL only on historical
                                                  -- OpenRouter rows); part of the dedup key
                                                  -- (jobs.md § Dedup)
                 prompt TEXT NULL,                -- effective prompt template, verbatim
                 prompt_sha256,                   -- its hash; part of the dedup key
                 status queued|running|succeeded|failed, error,
                 page_count, pages_done, source_path NULL, source_deleted_at NULL,
                 created_at, started_at, finished_at
                 INDEX (status, created_at)                                  -- queue claim
                 INDEX (user_id, agent, sha256, model, connection_id, pages_spec,
                        prompt_sha256, pipeline_version)
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
                                                  -- billing (historical rows only — no new
                                                  -- row is written with it), an estimate
                                                  -- Kano Proxy or a connection reported, or
                                                  -- nobody (parsing.md § Upstream usage)
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

- All credentials at rest follow [auth.md](./auth.md): hashes for anything we only verify, AES-GCM ciphertext for what we must replay (the Kano Proxy key, every provider connection's key, and a pending connect's PKCE verifier). No plaintext secrets in any column.
- `user_settings.system_prompt` no longer exists: the migration turned each stored custom prompt into a `prompt_presets` row named "Custom prompt" and pointed `prompt_preset_id` at it, so behavior did not change for anyone.
- **The Kano Proxy migration** (`837c0e9a9a4f_kano_proxy_and_agents`) removed OpenRouter and introduced agents in one step, on existing data:
  - every `prompt_presets` row and every `jobs` row became `agent = 'parse'` (the only agent);
  - each `user_settings` row became an `agent_settings('parse')` row when it selected anything: a user on a provider connection keeps it, with the model that connection used to carry (`provider_connections.model`), and keeps the selected prompt preset; a user on OpenRouter gets `connection_id` NULL — Kano Proxy — and `model` NULL, so the first parse after the upgrade asks them to connect Kano Proxy and pick a model rather than guessing one. `user_settings` was then dropped, and with it `default_model` / `default_profile`;
  - `provider_connections.model` was dropped. A connection that was not anyone's selected upstream loses its model; picking it later means picking a model again. Accepted by the operator, with no compatibility path;
  - `openrouter_keys` was dropped — the stored OpenRouter keys are gone, not migrated (nothing can use them);
  - `jobs.profile` and `jobs.profile_version` were dropped rather than kept as history. Nothing reads them once profiles are gone: the dedup key no longer has them, and a historical job's profile is still recorded where a reader sees it, in its stored result's `meta.profile`. Old OpenRouter jobs keep `connection_id` and `connection_base_url` NULL, which no new job ever has, so they can never answer a new upload ([jobs.md](./jobs.md) § Dedup).
- **Editing** a connection's URL or key applies to jobs still queued on it — the worker resolves the row at run time, the same semantics as reconnecting Kano Proxy while jobs are queued. Deliberate: snapshotting encrypted credentials onto job rows would copy secrets around, and a user edits their own connection precisely so pending work uses the fix. The model is not the connection's: it is snapshotted onto `jobs.model` at enqueue from the agent's settings, like every other parse parameter. The dedup cache is protected separately by the `connection_base_url` snapshot ([jobs.md](./jobs.md) § Dedup).
- `jobs.connection_id` carries **no foreign key** on purpose: the upstream a job ran on is immutable history. A FK with SET NULL would relabel a deleted connection's jobs as built-in (Kano Proxy) jobs — a still-queued one would then run against the user's Kano Proxy key, and a succeeded one would answer Kano Proxy dedup lookups with another upstream's output; RESTRICT would make a connection undeletable once any job exists. Instead the id simply outlives the row: the worker fails a queued job whose connection is gone ("the provider connection for this job no longer exists"), and dedup keys keep matching only their own upstream (connection ids are never reused). `jobs.source_job_id` follows the same rule for the same reason.
- **A usage row's cost is only as good as its source.** `cost_source` is written with the row and never revised: `openrouter` is billing, `estimated` is what the endpoint said the call cost, `unknown` has `cost` NULL. The migration that added the column backfilled it from the job each row belongs to — rows of a job with a `connection_id` became `unknown` with `cost` NULL (their reported cost had been discarded and 0 recorded in its place, which was never a price), every other row `openrouter` with its cost untouched. Rows with no job left (`job_id` NULL) count as `openrouter`: jobs are only ever deleted with their user, and the user's usage goes with them. Since OpenRouter was removed no new row is `openrouter`; the historical ones stay readable and keep counting as billed. `provider_connections.report_cost` follows the URL and key rule above — the worker reads it when the job runs, so toggling it affects calls made from then on, including jobs already queued, and never rewrites rows already recorded. Usage per agent is read through the job (`usage_log.job_id → jobs.agent`).
- **A document is a library entry, not a second copy of the result.** `documents` names a parse and places it in a folder; the markdown lives in `results` exactly once, keyed by the job that produced it. Deleting a document deletes the entry only — the job stays in the parse history and its `usage_log` rows stay, because they are the billing record of something that really ran. Deleting a folder cascades to its subfolders and their documents, the way removing a directory does.
- **The root level needs its own unique index.** `folders.parent_id` and `documents.folder_id` are NULL at the root, and PostgreSQL treats NULLs as distinct in a unique constraint, so `UNIQUE (user_id, parent_id, name)` would not stop two root folders called "Invoices". The partial indexes above close that. The application checks the same thing before writing, which is what turns a collision into `report (2).pdf` instead of a 409.
- `results` holds parsed **output** (kept indefinitely); `jobs.source_path` points at a temp file that is deleted at terminal state — the DB never stores document bytes.
- Job claiming and per-user caps: exact queries in [jobs.md](./jobs.md).
- Timestamps are `timestamptz`, UTC.
