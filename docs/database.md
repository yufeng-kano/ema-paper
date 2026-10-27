# Database

PostgreSQL 16+. Schema changes **only** via Alembic migrations under `apps/api/migrations/`, this doc updated first. Never edit production schema by hand.

## Tables

```text
users            id PK, google_sub UNIQUE, email, name,
                 picture NULL,                    -- Google avatar URL, refreshed each sign-in
                 default_view VARCHAR(16) NOT NULL DEFAULT 'markdown',
                                                  -- original | markdown: the view a converted
                                                  -- PDF or image opens on the first time
                                                  -- (web.md § Result viewer)
                 redeem_failures INT NOT NULL DEFAULT 0, redeem_failed_at NULL,
                                                  -- refused redeem-code attempts in the
                                                  -- window (billing.md § Redeem codes)
                 created_at
                 -- deleting the row is deleting the account (see Rules): every table
                 -- below with a user_id cascades from it, pay_events keep theirs as NULL
sessions         id PK, user_id FK, token_hash UNIQUE, created_at, expires_at
api_keys         id PK, user_id FK, name, key_hash UNIQUE, prefix, created_at,
                 last_used_at, revoked_at NULL
prompt_presets   id PK, user_id FK, agent, name, text TEXT, created_at, updated_at
                 UNIQUE (user_id, name)
                 -- named prompts for one agent (agents.md); selecting one replaces that
                 -- agent's template entirely (docs/parsing.md § Prompts)
agent_settings   (user_id FK, agent) PK,
                 model NULL,                      -- chat: an offered model; NULL = the
                                                  -- default (billing.md § Models). NULL on
                                                  -- parse and check, whose models are the
                                                  -- deployment's
                 effort low|medium|high|xhigh|max NOT NULL DEFAULT 'medium',
                                                  -- the chat agent's reasoning depth
                 idle_compact BOOLEAN NOT NULL DEFAULT true,
                                                  -- chat: the worker compacts the user's
                                                  -- long conversations before their cache
                                                  -- expires (chat.md § Compaction)
                 prompt TEXT NULL,                -- the user's own template; NULL = the
                                                  -- agent's default (agents.md)
                 prompt_preset_id FK NULL,        -- prompt_presets, the legacy selection;
                                                  -- never set together with `prompt`
                 instructions TEXT NULL,          -- chat: the reader's instructions, appended
                                                  -- after the prompt (chat.md § Instructions)
                 answer_language VARCHAR(16) NULL,
                 translate_language VARCHAR(16) NULL,
                                                  -- chat: language tags; NULL = the reader's
                                                  -- locale of each message
                 memory_auto BOOL NOT NULL DEFAULT true,
                 memory_paused BOOL NOT NULL DEFAULT false,
                                                  -- chat: the memory switches (chat.md § Memory)
                 auto_fallback BOOL NOT NULL DEFAULT false,
                                                  -- parse: send blocked pages to it on its own
                                                  -- (parsing.md § Fallback model)
                 auto_check BOOL NOT NULL DEFAULT true,
                                                  -- check: check every Files upload before
                                                  -- it opens (parsing.md § Format check)
                 auto_convert BOOL NOT NULL DEFAULT false
                                                  -- parse: convert an uploaded PDF or image
                                                  -- on arrival; off (the default since
                                                  -- 2026-10-26) keeps it as a file
                                                  -- (workspace.md § Files)
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
                 connection_id NULL,              -- history only: the provider connection
                                                  -- a job of an earlier release ran on;
                                                  -- NULL on every job since (Kano Proxy;
                                                  -- OpenRouter on the oldest rows). No FK,
                                                  -- and the table it named is gone (see
                                                  -- Rules)
                 connection_base_url NULL,        -- endpoint snapshot at enqueue:
                                                  -- KANO_PROXY_BASE_URL (a connection's
                                                  -- base_url on historical rows, NULL only
                                                  -- on OpenRouter rows); part of the dedup
                                                  -- key (jobs.md § Dedup)
                 prompt TEXT NULL,                -- effective prompt template, verbatim
                 prompt_sha256,                   -- its hash; part of the dedup key
                 fallback_model NULL,             -- snapshot at enqueue: the fallback when
                                                  -- auto_fallback was on, else NULL
                 retry_model NULL,                -- the model a retry runs the remaining
                                                  -- pages on; NULL = `model` (jobs.md
                                                  -- § Retrying failed pages)
                 check_model NULL, check_effort NULL,
                 check_prompt TEXT NULL,          -- the format check, snapshotted when the
                                                  -- job is queued or retried: model, effort,
                                                  -- system prompt with the instructions;
                                                  -- NULL check_model = no check (parsing.md
                                                  -- § Format check)
                 status queued|running|succeeded|failed, error,
                 page_count, pages_done, source_path NULL, source_deleted_at NULL,
                 retain_source,                   -- a library upload keeps its original
                                                  -- (jobs.md § Retention)
                 worker_generation BIGINT NULL,   -- the generation of the worker that
                                                  -- claimed it, NULL while queued: the
                                                  -- fence every write to a running job
                                                  -- carries (jobs.md § Queue)
                 created_at, started_at, finished_at
                                                  -- a finished parse no document and no
                                                  -- unexpired upload ticket keeps is
                                                  -- deleted PARSE_RETENTION_DAYS after
                                                  -- finished_at (jobs.md § Retention)
                 INDEX (status, created_at)                                  -- queue claim
                 INDEX (user_id, agent, sha256, model, connection_id, pages_spec,
                        prompt_sha256, pipeline_version)
                        WHERE status='succeeded'                             -- dedup
                 SEQUENCE worker_generation_seq   -- PostgreSQL; one value per worker start
                                                  -- (jobs.md § Queue). The SQLite fallback
                                                  -- takes the highest on record plus one
job_pages        (job_id FK, page_no) PK, method NULL, status, error NULL,
                 markdown TEXT NULL,              -- the page's transcription, written as
                                                  -- the page finishes; what the partial
                                                  -- result reads (jobs.md § Progress);
                                                  -- NULL on failed pages
                 model NULL,                      -- the model that wrote the page, or that
                                                  -- failed it last; NULL on rows written
                                                  -- before the column (the job's model)
                 figures JSONB NULL               -- the figure check's status of each box
                                                  -- the page's markdown holds, keyed
                                                  -- "ymin,xmin,ymax,xmax": verified,
                                                  -- adjusted or unverified; NULL on failed
                                                  -- pages and pages parsed without the
                                                  -- check (parsing.md § Figure check)
results          job_id PK/FK, markdown TEXT, pages JSONB, figures JSONB,
                 errors JSONB, meta JSONB,
                 reading_markdown TEXT NULL,      -- the checked copy a library document's
                                                  -- doc.md starts from; NULL = not checked
                                                  -- (parsing.md § Format check)
                 created_at
folders          id PK, user_id FK, parent_id FK folders NULL, name,
                 trashed_at NULL,                 -- put in the Trash at this moment; its
                                                  -- subtree is in the Trash with it
                                                  -- (workspace.md § The Trash)
                 created_at, updated_at
                 UNIQUE (user_id, parent_id, name) WHERE trashed_at IS NULL
                 UNIQUE (user_id, name) WHERE parent_id IS NULL AND trashed_at IS NULL
                                                  -- the root level
                 INDEX (trashed_at) WHERE trashed_at IS NOT NULL
                                                  -- what the Trash's expiry reads
                                                  -- (workspace.md § The Trash)
                 -- the web file library's directories; parent_id NULL = root,
                 -- ON DELETE CASCADE on the self-reference, so deleting a folder
                 -- takes its subtree with it. A folder in the Trash keeps its
                 -- parent_id: the place Put Back returns it to
documents        id PK, user_id FK, folder_id FK folders NULL, job_id FK jobs NULL,
                                                  -- ON DELETE CASCADE, which the retention
                                                  -- never sets off: it keeps every job a
                                                  -- document names (jobs.md § Retention)
                 name, trashed_at NULL,           -- as for folders
                 kind parse|markdown|file NOT NULL DEFAULT 'parse',
                                                  -- what the row is: a parse, a markdown
                                                  -- document, or a file kept as it was
                                                  -- uploaded (workspace.md § Files)
                 media_type VARCHAR(255) NULL,    -- file: the type its bytes are served
                                                  -- as, from its name; NULL otherwise
                 size_bytes BIGINT NULL,          -- file: its size in the tree, refreshed
                                                  -- by reconciliation; NULL otherwise
                 view VARCHAR(16) NULL,           -- original | markdown: the view the reader
                                                  -- last switched a PDF or image to; NULL
                                                  -- never (web.md § Result viewer)
                 reconvert_job_id FK jobs NULL,   -- the re-convert queued, running or
                                                  -- failed (docs/jobs.md § Re-converting
                                                  -- a document); ON DELETE SET NULL
                 created_at, updated_at
                 UNIQUE (user_id, folder_id, name) WHERE trashed_at IS NULL
                 UNIQUE (user_id, name) WHERE folder_id IS NULL AND trashed_at IS NULL
                 INDEX (user_id, created_at)
                 INDEX (job_id)                   -- the retention's keep rule and a job
                                                  -- delete's foreign-key action read it
                 INDEX (reconvert_job_id) WHERE reconvert_job_id IS NOT NULL
                 INDEX (trashed_at) WHERE trashed_at IS NOT NULL
                                                  -- as for folders
                 -- one row = one item in the library: a parse result (kind parse),
                 -- a markdown document uploaded as .md or written new (markdown,
                 -- job_id NULL), or a file of any other kind kept once in the tree
                 -- (file, job_id NULL; workspace.md § Files). folder_id NULL = root
usage_log        id PK, user_id FK, job_id FK NULL, turn_id FK chat_turns NULL,
                 agent,                           -- parse | chat; backfilled from the job
                 model, prompt_tokens, completion_tokens,
                 cache_read_tokens, cache_write_tokens,
                                                  -- prompt caching, 0 on parse rows
                                                  -- (chat.md § Prompt caching)
                 cost NUMERIC(12,6) NULL,         -- NULL exactly when cost_source = unknown
                 cost_source openrouter|estimated|unknown,
                                                  -- where the cost came from: OpenRouter's
                                                  -- billing (historical rows only — no new
                                                  -- row is written with it), an estimate
                                                  -- Kano Proxy or a connection reported, or
                                                  -- nobody (parsing.md § Upstream usage)
                 created_at
                 INDEX (user_id, created_at)
                 INDEX (job_id)                   -- a job delete's SET NULL reads it
                 CHECK (cost_source IN (...) AND (cost_source = 'unknown') = (cost IS NULL))
conversations    id PK, user_id FK, document_id FK documents, title, session,
                 model NULL, memory_ids JSONB NULL, memory_file TEXT NULL,
                 compacted_message_id NULL,          -- a messages.id, no FK (below)
                 context_tokens NULL, cache_warm_until NULL, created_at, updated_at
                 INDEX (document_id, updated_at)
                 INDEX (cache_warm_until) WHERE cache_warm_until IS NOT NULL
                 -- one thread of the chat agent about one document (chat.md);
                 -- `session` names its shell in the user's sandbox; `model` is
                 -- set by the first turn and kept: the stored thinking blocks
                 -- belong to it (chat.md § History); `memory_ids` is the memory
                 -- snapshot its first turn took, in render order, NULL until a
                 -- turn took one (chat.md § Memory); `memory_file` the snapshot of
                 -- memory.md its first turn took, NULL until then, '' for none,
                 -- taken again by a compaction (chat.md § Memory, *The memory
                 -- file*); `compacted_message_id` the summary row the replay
                 -- starts from, NULL while never compacted — no foreign key:
                 -- a message goes only with its conversation, and a key back
                 -- to `messages` would close a cycle every DDL tool would have
                 -- to break; `context_tokens`
                 -- what the last model call replayed and produced, and
                 -- `cache_warm_until` when the prompt cache it touched expires —
                 -- what the idle compaction pass reads through the partial
                 -- index (chat.md § Compaction)
messages         id PK, conversation_id FK, role user|assistant, content JSONB,
                 turn_id FK chat_turns NULL, created_at
                 INDEX (conversation_id, id)
                 -- content blocks exactly as sent to / received from the model;
                 -- replayed verbatim on every later request (chat.md § History)
                 -- `normalize library names` (c8f28f7cd45e): a data migration that NFC-normalizes
                 -- every folder and document name and suffixes the later of two entries
                 -- of one parent that then share a name, folders before documents, by
                 -- id — the library and the workspace keep one namespace per level
                 -- (workspace.md § Layout)
chat_turns       id UUID PK, user_id FK, conversation_id FK, message_id FK messages,
                 kind message|compact|idle_compact NOT NULL DEFAULT 'message',
                 status queued|running|succeeded|failed|cancelled, error NULL,
                 locale, created_at, started_at NULL, finished_at NULL
                 INDEX (status, created_at)
                 INDEX (conversation_id, finished_at)
                                                  -- the stream's "is a turn unfinished"
                                                  -- poll, sends, cancels and deletes all
                                                  -- ask by conversation
                 -- the chat agent's queue, one row per user message — or per
                 -- compaction, `kind` saying which (chat.md § Compaction) —
                 -- claimed like jobs
chat_events      id PK, conversation_id FK, turn_id FK, seq, event, data JSONB,
                 created_at
                 UNIQUE (conversation_id, seq)
                 -- what the worker streamed while the turn ran; the SSE route replays
                 -- and tails it (chat.md § Conversations, turns, events)
chat_tool_calls  id PK, user_id FK, turn_id FK chat_turns ON DELETE SET NULL, NULL,
                 tool, created_at
                 INDEX (tool, created_at)                  -- the deployment's per-day caps
                 INDEX (user_id, tool, created_at)         -- a user's per-day cap
                 INDEX (turn_id)                           -- the turn's settlement
                 -- one row per web tool call that was answered or kept a file —
                 -- `web_search`, `search_papers`, `download_file` — what the per-day
                 -- caps count and what a turn's web searches are charged from (chat.md
                 -- § Tools, billing.md § A chat turn); a deleted turn keeps its rows
                 -- for the day's count, as a purged parse keeps its entry
skills           id PK, user_id FK, name, description TEXT, argument_hint, body TEXT,
                 user_invocable BOOL, model_invocable BOOL, created_at, updated_at
                 UNIQUE (user_id, name)
                 -- the chat agent's skills of the user's own, and the user's edits of a
                 -- built-in under the built-in's name; built-ins themselves live in code
                 -- and deleting an edit restores the built-in (chat.md § Skills)
subagents        id PK, user_id FK, name, description TEXT, model NULL,
                 writes BOOL DEFAULT false, tools JSONB, prompt TEXT,
                 created_at, updated_at
                 UNIQUE (user_id, name)
                 -- the same for subagents; `model` NULL = the conversation's model,
                 -- `writes` whether it may change files, `tools` a subset of
                 -- view_page, view_figure, the editor and bash, bash only with
                 -- `writes` (chat.md § Subagents)
memories         id PK, user_id FK, kind term|preference|about|document, text TEXT,
                 document_id FK documents NULL,   -- NULL = every document; ON DELETE CASCADE:
                                                  -- a note about one document goes with it,
                                                  -- never widened to every document
                 pinned BOOL NOT NULL DEFAULT false,
                 conversation_id FK conversations NULL,
                                                  -- the conversation that saved it; ON DELETE
                                                  -- SET NULL: deleting a conversation keeps
                                                  -- what it saved
                 source_ref VARCHAR(255) NULL,    -- the tool_use_id of the call that saved it,
                                                  -- or of the forget call whose Undo saved
                                                  -- it again (chat.md § Memory)
                 created_at, updated_at
                 INDEX (user_id, updated_at)
                 INDEX (document_id) WHERE document_id IS NOT NULL
                 INDEX (conversation_id) WHERE conversation_id IS NOT NULL
                                                  -- what a document's or a conversation's
                                                  -- delete cascades through, so it never
                                                  -- scans every reader's memories
                 CHECK (kind IN ('term', 'preference', 'about', 'document'))
                 -- the chat agent's memories of the reader (chat.md § Memory)
chat_attachments id UUID PK, user_id FK, name, kind image|text, media_type, size_bytes,
                 chars NULL, created_at
                 INDEX (user_id, created_at)
                 -- a prepared upload waiting for its message: the file is
                 -- {UPLOAD_DIR}/chat-attachment-{id}.{png|jpg|txt}; the send embeds it
                 -- in the message and deletes both (chat.md § What a user message carries)
upload_tickets   id PK, user_id FK, token_hash UNIQUE, prefix, job_id FK NULL,
                 created_at, expires_at, spent_at NULL
                 INDEX (user_id, created_at)                                 -- mint rate limit
                 -- single-use upload credential minted by the MCP `parse` tool
                 -- (auth.md § 5); job_id set when the upload spends the ticket,
                 -- ON DELETE SET NULL; a ticket not yet expired keeps its job from
                 -- the parse retention (jobs.md § Retention)
oauth_clients    client_id PK, client_name, redirect_uris JSONB, created_at
oauth_grants     id PK, client_id FK, user_id FK, kind code|access|refresh,
                 token_hash UNIQUE, pkce_challenge NULL, redirect_uri NULL,
                 scope, expires_at, revoked_at NULL, created_at
                 -- redirect_uri is set on `code` rows only: the token endpoint
                 -- checks a code came back from the URI it was issued for
subscriptions    user_id PK/FK, subscription_id UNIQUE, status, plan_id,
                 period_start, period_end,       -- the paid period Pay reports
                 cancel_at_period_end BOOL, version BIGINT,
                                                  -- Pay's version (ms); an update with an
                                                  -- older one is ignored
                 updated_at
                 CHECK (period_end > period_start)
                 -- the user's Kano Pay subscription, canonical copy (billing.md § Kano
                 -- Pay); no row = Free. Every user has Free without a row
checkouts        user_id PK/FK, plan_id, idempotency_key UNIQUE,
                 transaction_id UNIQUE NULL, created_at, completed_at NULL
                 -- the one checkout a user may have open: the key is written before
                 -- Pay is called, so a retry repeats the same request
pay_events       id PK,                           -- Pay's event id
                 type, user_id FK NULL, received_at
                 -- processed notifications; a duplicate id is answered and not applied
credit_periods   (user_id FK, period_start) PK, period_end, plan_id NULL,
                 granted INT, spent INT,          -- the balance's fast path:
                                                  -- remaining = granted - spent
                 created_at, updated_at
                 CHECK (period_end > period_start), CHECK (granted >= 0)
                 -- one row per user per allowance period (Free's UTC month, or a paid
                 -- period), created on first use (billing.md § Periods); plan_id NULL
                 -- = Free
credit_entries   id PK, user_id FK, period_start,
                 kind parse|chat, state reserved|consumed|released,
                 amount INT, job_id FK jobs ON DELETE SET NULL, NULL UNIQUE,
                 turn_id FK chat_turns ON DELETE SET NULL, NULL UNIQUE,
                 model NULL, created_at, settled_at NULL
                 INDEX (user_id, created_at)
                 CHECK (amount >= 0), CHECK (kind = 'parse' OR job_id IS NULL),
                 CHECK (kind = 'chat' OR turn_id IS NULL)
                 -- what a parse or a turn cost (billing.md § The ledger): one entry per
                 -- job, reserved at enqueue and settled at the terminal state; one per
                 -- turn, its amount raised as the turn runs. A parse the retention
                 -- deletes (jobs.md § Retention) leaves its entry with job_id NULL, and
                 -- a conversation the reader deletes takes its turns and leaves their
                 -- entries with turn_id NULL, so the balance and its record keep agreeing
credit_packs     id PK,                           -- Pay's purchaseId, or redeem-<redemption id>
                 user_id FK, pack_id, credits INT, spent INT,
                 status paid|refunded, purchased_at, expires_at,
                 version BIGINT,                  -- Pay's version (ms); older is ignored
                 source pay|code,                 -- bought, or granted by a redeem code
                 redemption_id FK redemptions ON DELETE SET NULL, NULL UNIQUE,
                 created_at, updated_at
                 INDEX (user_id, expires_at)
                 CHECK (credits >= 0), CHECK (spent >= 0), CHECK (expires_at > purchased_at)
                 CHECK (source IN ('pay', 'code'))
                 -- a top-up pack bought on Pro (billing.md § Top-up packs): credits from
                 -- the pack's configuration when it was paid, spent as charges take from it
                 -- once the period's grant is gone; counts while paid and unexpired
credit_pack_entries (entry_id FK credit_entries, pack_id FK credit_packs) PK, amount INT
                 CHECK (amount >= 0)
                 -- how much of an entry's amount each pack covered, so a release, a retry
                 -- or a settlement moves exactly that back or forward
pack_checkouts   user_id PK/FK, pack_id, idempotency_key UNIQUE,
                 purchase_id UNIQUE NULL, created_at, completed_at NULL
                 -- the one pack checkout a user may have open, keyed as `checkouts` is;
                 -- purchase_id is Pay's purchaseId once Pay answered
redeem_codes     id PK, code UNIQUE,              -- the random characters, no prefix
                 kind credits|plan, credits INT NULL, valid_days INT NULL,
                 plan_id NULL, days INT NULL,     -- a plan code: whose days, how many
                 max_uses INT, uses INT, redeem_by NULL, label, disabled_at NULL,
                 created_at
                 CHECK (kind IN ('credits', 'plan')), CHECK (max_uses >= 1),
                 CHECK (uses >= 0)
                 -- a code the operator hands out (billing.md § Redeem codes); no user_id:
                 -- it outlives every account that used it
redemptions      id PK, code_id FK redeem_codes, user_id FK, kind, credits INT NULL,
                 plan_id NULL, days INT NULL,     -- what the code granted, as it stood then
                 redeemed_at
                 INDEX (user_id)
                 UNIQUE (code_id, user_id)        -- one redemption per user per code
plan_grants      user_id PK/FK, plan_id, days_banked INT,
                 period_start NULL, period_end NULL,
                                                  -- the open granted period, when one runs
                 updated_at
                 CHECK (days_banked >= 0)
                 -- the bank of granted days (billing.md § Periods, Granted days)
```

## Rules

- All credentials at rest follow [auth.md](./auth.md): hashes for anything we only verify, AES-GCM ciphertext for what we must replay (the Kano Proxy key, every provider connection's key, and a pending connect's PKCE verifier). No plaintext secrets in any column.
- `user_settings.system_prompt` no longer exists: the migration turned each stored custom prompt into a `prompt_presets` row named "Custom prompt" and pointed `prompt_preset_id` at it, so behavior did not change for anyone.
- **The billing migration** (`subscriptions and credits`) created `subscriptions`, `checkouts`, `pay_events`, `credit_periods` and `credit_entries`, empty, with their constraints and indexes ([billing.md](./billing.md) § The ledger). No existing row is read or rewritten: every user is on Free without a row, and nothing reads the tables until the ledger step of [billing.md](./billing.md) § Transition. Its downgrade drops the five tables and nothing else — a ledger already written is lost with them, which is the point of not downgrading past a step that wrote one.
- **The agent prompt migration** (`skills subagents attachments and agent prompt`) gave `agent_settings` its `prompt` column and moved every selected preset into it, clearing `prompt_preset_id`: a `parse` preset's text as it was (a parse preset replaced the template), a `chat` preset's text behind the chat agent's default system prompt of that release and a blank line (a chat preset was appended to it) — the default is copied into the migration, never imported, so the migration stays what it was when it ran. The presets themselves stay, unselected; nothing else reads them. A `chat` preset holding one of the template's tokens is not moved: it was appended verbatim, and inside the template the token would be substituted, so it stays the selected preset. The downgrade puts the selections back before it drops the column: an agent whose prompt is a moved preset selects that preset again (matched by its text), and a prompt written after the upgrade becomes a preset of its own named *Custom prompt* (numbered past a taken name) where the old schema can say it — a `parse` prompt as it is, a `chat` prompt as what it adds behind that release's default; a `chat` prompt that edits the default itself has no such form and goes back to the default. A moved `chat` prompt can exceed `SYSTEM_PROMPT_MAX_CHARS`, which caps writes, not stored rows: the dialog says so and the reader shortens it before the next save. The same revision created `skills`, `subagents` and `chat_attachments`.
- **The instructions and memory migration** (`agent instructions and memories`) added `agent_settings.instructions`, `answer_language`, `translate_language` (NULL: none, and the reader's locale as before), `memory_auto` (true) and `memory_paused` (false), `conversations.memory_ids` (NULL: a conversation takes its snapshot at its next turn) and the `memories` table, empty, with its three indexes. No row is rewritten. Its downgrade drops them, the memories with them.
- **The failed pages migration** (`retry failed pages`) added `agent_settings.fallback_model` (NULL) and `auto_fallback` (false), `jobs.fallback_model` and `jobs.retry_model` (NULL) and `job_pages.model` (NULL: the job's model). No row is rewritten: a job already `succeeded` with page errors keeps its result as it was ([parsing.md](./parsing.md) § Vision-only conversion). Its downgrade drops the five columns.
- **The operator upstream migration** (`operator upstream`, [billing.md](./billing.md) § Transition step 4) dropped `kano_proxy_keys`, `kano_connect_states` and `provider_connections` — the stored keys are gone, not migrated: nothing can use them — and `agent_settings.connection_id` and `fallback_model`, and set `agent_settings.model` to NULL on every `parse` and `check` row (their models are the deployment's). A `chat` row keeps its model. `jobs.connection_id` stays as history; the worker fails a queued job that carries one. Its downgrade recreates the three tables and the two columns, empty: there is no key to put back.
- **The format check migration** (`format check`) added `agent_settings.auto_check` (true), `jobs.check_model`, `check_effort` and `check_prompt` (NULL: no check) and `results.reading_markdown` (NULL: not checked). No row is rewritten: a job queued before it runs unchecked, and a result written before it has no reading copy. Its downgrade drops the five columns.
- **The figure check migration** (`figure check`) added `job_pages.figures` (NULL: no check). No row is rewritten: a page kept from before it keeps no statuses, and its figures carry no `check`. Its downgrade drops the column.
- **The ledger migration** (`credit entries outlive turns`) changed `credit_entries.turn_id` from `ON DELETE CASCADE` to `ON DELETE SET NULL` and replaced the check that a chat entry has a turn with one that a parse entry has none: a conversation deleted by its reader takes its turns and leaves their entries behind, still counted in the period and in `/api/billing/entries`. No row is rewritten. Its downgrade restores the cascade and refuses while any chat entry has no turn, naming the count — those entries would otherwise have to be deleted from the billing record.
- **The library files migration** (`library files`) added `documents.kind` — `parse`, `markdown` or `file`; NOT NULL, default `parse` — backfilled from `job_id` (a row with a job is a `parse`, one without a `markdown`), `documents.media_type` and `documents.size_bytes` (NULL on every existing row: only a file carries them) and `agent_settings.auto_convert` (true). No row is rewritten otherwise. Its downgrade refuses while any row is a file, naming the count — without `kind` those rows would read as markdown documents whose text no route could find — and otherwise drops the four columns.
- **The chat tool calls migration** (`chat tool calls`) created `chat_tool_calls`, empty, with its indexes; no existing row is read or rewritten, and nothing reads it until a turn makes a web tool call ([chat.md](./chat.md) § Tools). Its downgrade drops the table — the day's counts and the record behind the searches already charged are lost with it, the entries themselves not.
- **The compaction migration** (`conversation compaction and memory file`) added `chat_turns.kind` (`message` on every row), `agent_settings.idle_compact` (`true` on every row: idle compaction is on unless the reader turns it off), and on `conversations` the nullable `memory_file`, `compacted_message_id` (a message id without a foreign key, since a message goes only with its conversation), `context_tokens` and `cache_warm_until`, NULL on every row, with the partial index the idle compaction pass reads ([chat.md](./chat.md) § Compaction, § Memory). The only rows rewritten are skills named `new` or `compact`, the composer's own commands from this version on ([chat.md](./chat.md) § Skills): each is renamed to the next free `name-2`, `name-3`… of its reader. Otherwise no row is read or rewritten: an open conversation goes on as it was, takes its memory file snapshot at its next turn, and is first measured by its next model call. Its downgrade drops the columns and the index and leaves the new names; a conversation compacted meanwhile then replays its whole history again, the summary row among it.
- **The redeem codes migration** (`redeem codes`) created `redeem_codes`, `redemptions` and `plan_grants`, empty, added `source` (default `pay`) and `redemption_id` to `credit_packs` and `redeem_failures` (default 0) and `redeem_failed_at` to `users` ([billing.md](./billing.md) § Redeem codes). Every existing pack reads as bought and every user as never having tried a code; nothing else is read or rewritten. Its downgrade drops the three tables and the four columns — codes and their redemptions are lost with them, and a redeemed pack's row stays as a bought one's, so it is not run on a database that redeemed one.
- **The credit packs migration** (`credit packs`) created `credit_packs`, `credit_pack_entries` and `pack_checkouts`, empty, with their constraints and indexes ([billing.md](./billing.md) § The ledger). No existing row is read or rewritten: nothing changes for a user until a pack is bought. Its downgrade drops the three tables and nothing else — packs already bought are lost with them, so it is not run on a database that sold one.
- **The reader views migration** (`reader views`) added `users.default_view` (`markdown` on every row) and `documents.view` (NULL on every row), and turned `agent_settings.auto_convert` off: its default became `false` and every existing row was set to `false` — the switch had been on for everyone as the default the library files migration gave it, and since 2026-10-26 an uploaded PDF or image is kept as it is until the reader asks for Markdown ([web.md](./web.md) § Files). Its downgrade drops the two columns and restores the default of `true`; the rows stay off.
- **The parse retention migration** (`parse retention and account deletion`) did the same for `credit_entries.job_id`: `fk_credit_entries_job_id_jobs` went from `ON DELETE CASCADE` to `ON DELETE SET NULL`, and `ck_credit_entries_parse_job`, which held that an entry has a job exactly when it is a parse, became the check that a chat entry has no job (`kind = 'parse' OR job_id IS NULL`): a parse the retention deletes ([jobs.md](./jobs.md) § Retention) leaves its entry behind, still counted in its period and in `/api/billing/entries`. It added the indexes the purge and a job's foreign-key actions read, `ix_documents_job_id` and `ix_usage_log_job_id`, and the partial `ix_documents_trashed_at` and `ix_folders_trashed_at` (`WHERE trashed_at IS NOT NULL`) the Trash's expiry reads. No row is rewritten and none is deleted: the parses already past their time go at the first sweep after the deploy. Its downgrade drops the four indexes, restores the cascade and the old check, and refuses while any parse entry has no job, naming the count — those entries would otherwise have to be deleted from the billing record.
- **The re-convert migration** (`reconvert documents`) added `documents.reconvert_job_id` (NULL: no re-convert) and its partial index. No row is rewritten. Its downgrade drops both: a re-convert still running then finishes as a parse no document takes, which the retention deletes in its time.
- **The markdown documents migration** (`markdown documents`) dropped NOT NULL from `documents.job_id` and nothing else: every existing row keeps its job, and no row is rewritten. Its downgrade refuses while any document has no job, naming the count — restoring NOT NULL would otherwise have to delete the reader's markdown documents and their conversations with them. Delete them first, deliberately, if the downgrade is really wanted.
- **The trash migration** (`library trash`) added `folders.trashed_at` and `documents.trashed_at`, NULL on every existing row — nothing is in the Trash — and narrowed the two name constraints and the two root-level indexes to the rows not in the Trash (`uq_folders_parent_name` and `uq_documents_folder_name` were constraints and became partial unique indexes under the same names). Its downgrade refuses while anything is in the Trash, naming the count: restoring the full constraints would have to pick which of two same-named rows survives. Empty the Trash first, deliberately, if the downgrade is really wanted.
- **The built-in subagent removal** (`remove three built-in subagents`) deleted every `subagents` row named `verifier`, `reader` or `translation-checker`: the readers' edits of built-ins that left the code, which would otherwise have stayed on as subagents of their own ([chat.md](./chat.md) § Subagents). Its downgrade restores none of them; the built-ins come back with the code they belong to, unedited.
- **The Kano Proxy migration** (`837c0e9a9a4f_kano_proxy_and_agents`) removed OpenRouter and introduced agents in one step, on existing data:
  - every `prompt_presets` row and every `jobs` row became `agent = 'parse'` (the only agent);
  - each `user_settings` row became an `agent_settings('parse')` row when it selected anything: a user on a provider connection keeps it, with the model that connection used to carry (`provider_connections.model`), and keeps the selected prompt preset; a user on OpenRouter gets `connection_id` NULL — Kano Proxy — and `model` NULL, so the first parse after the upgrade asks them to connect Kano Proxy and pick a model rather than guessing one. `user_settings` was then dropped, and with it `default_model` / `default_profile`;
  - `provider_connections.model` was dropped. A connection that was not anyone's selected upstream loses its model; picking it later means picking a model again. Accepted by the operator, with no compatibility path;
  - `openrouter_keys` was dropped — the stored OpenRouter keys are gone, not migrated (nothing can use them);
  - `jobs.profile` and `jobs.profile_version` were dropped rather than kept as history. Nothing reads them once profiles are gone: the dedup key no longer has them, and a historical job's profile is still recorded where a reader sees it, in its stored result's `meta.profile`. Old OpenRouter jobs keep `connection_id` and `connection_base_url` NULL, which no new job ever has, so they can never answer a new upload ([jobs.md](./jobs.md) § Dedup).
- **The upstream key is read at run time**, never snapshotted onto a job: the worker presents `KANO_PROXY_API_KEY` as configured when the job runs, so a rotated key applies to queued work. The model is snapshotted onto `jobs.model` at enqueue, like every other parse parameter. The dedup cache is protected separately by the `connection_base_url` snapshot ([jobs.md](./jobs.md) § Dedup).
- `jobs.connection_id` is **history**: the provider connection a job of an earlier release ran on, kept so a result parsed through another endpoint never answers a Kano Proxy dedup lookup (the key keeps the column), and so a job still queued on one fails when the worker takes it ("the provider connection for this job no longer exists") rather than run on the operator's key. It never had a foreign key — the upstream a job ran on is immutable — and no new job sets it. `jobs.source_job_id` follows the same no-FK rule for the same reason.
- **A usage row's cost is only as good as its source.** `cost_source` is written with the row and never revised: `openrouter` is billing, `estimated` is what the endpoint said the call cost, `unknown` has `cost` NULL. The migration that added the column backfilled it from the job each row belongs to — rows of a job with a `connection_id` became `unknown` with `cost` NULL (their reported cost had been discarded and 0 recorded in its place, which was never a price), every other row `openrouter` with its cost untouched. Rows with no job left (`job_id` NULL) count as `openrouter`: jobs were only ever deleted with their user then, and the user's usage went with them. A row whose job the retention deletes since ([jobs.md](./jobs.md) § Retention) keeps the `cost_source` and the `agent` it was written with. Since OpenRouter was removed no new row is `openrouter`; the historical ones stay readable and keep counting as billed. `provider_connections.report_cost` follows the URL and key rule above — the worker reads it when the job runs, so toggling it affects calls made from then on, including jobs already queued, and never rewrites rows already recorded. Usage per agent is read from the row's own `agent` (below), so a row whose job is gone still counts under its agent.
- **A document is a library entry, not a second copy of the result.** `documents` names a parse and places it in a folder; the markdown lives in `results` exactly once, keyed by the job that produced it. A markdown document has no parse: `job_id` is NULL, and its baseline is the `.md` it was uploaded as, kept as its original, or nothing for one written new ([workspace.md](./workspace.md) § Markdown documents). `kind` says which it is (`parse` or `markdown`), and `job_id` agrees: a parse has one, a markdown document none. **A file** (`kind` `file`, [workspace.md](./workspace.md) § Files) is the third kind of row: no parse, no baseline, the one copy of its bytes in the tree at `files/<folder>/<name>`, its `media_type` and `size_bytes` on the row because the file itself is the only other place they could be read from; reconciliation removes the row when the file is gone from the tree — the one row it removes ([workspace.md](./workspace.md) § The tree is the truth). Deleting a document deletes the entry only; its parse then falls under the retention ([jobs.md](./jobs.md) § Retention) — kept while any document of the user names a parse of the same original or an upload ticket that has not expired names it, else deleted `PARSE_RETENTION_DAYS` after it finished — and its `usage_log` rows and its ledger entry outlive it with `job_id` NULL, because they are the billing record of something that really ran. Deleting a folder cascades to its subfolders and their documents, the way removing a directory does.
- **The Trash is a column, not a table.** Putting a folder or a document in the Trash sets its `trashed_at` and nothing else: the row keeps its place (`parent_id`, `folder_id`) and its name, which is where *Put Back* returns it, and everything under a trashed folder is in the Trash with it without a write of its own. So *live* is computed, not stored — a folder is live when neither it nor any folder above it has `trashed_at`, a document when it has none and its folder (if any) is live — and every route of the library, the reader and the agent answers 404 for what is not live ([workspace.md](./workspace.md) § The Trash). Names are unique among the live rows only: the unique indexes carry `WHERE trashed_at IS NULL`, so a trashed `report.pdf` does not keep its folder from taking a new one, and Put Back resolves the collision the way a move does, with ` (2)`. A row inside a trashed folder still holds its name there, which nothing can contest: a trashed folder takes no new entries. That is decided again under the user's row lock, which a move to the Trash takes too: a folder or a file created while its destination goes to the Trash either lands first and goes with it, or is refused (404) — an upload whose parse is already under way lands at the top level instead, since the parse is not undone. An item trashed on its own stays its own entry in the Trash when a folder above it is trashed later. Deleting a row for good — from the Trash, or a live one deleted immediately — takes its own subtree only: an item inside it that was trashed separately is moved up to the deleted folder's parent first, so the cascade cannot take it, and it stays in the Trash. The Trash is emptied by the reader — *Delete Immediately*, *Empty Trash* — and by the worker's sweep, which deletes for good, the way *Delete Immediately* does, every item whose own `trashed_at` is older than `TRASH_RETENTION_DAYS` (30; 0 = never), reading the partial `trashed_at` indexes ([workspace.md](./workspace.md) § The Trash). An expired folder takes its whole subtree, and nothing in it outlives it: an item trashed on its own inside it was trashed earlier, nothing inside a trashed folder being trashable, so it has expired too.
- **The root level needs its own unique index.** `folders.parent_id` and `documents.folder_id` are NULL at the root, and PostgreSQL treats NULLs as distinct in a unique constraint, so `UNIQUE (user_id, parent_id, name)` would not stop two root folders called "Invoices". The partial indexes above close that. The application checks the same thing before writing, which is what turns a collision into `report (2).pdf` instead of a 409 — and it checks across both tables: a folder and a document in one parent cannot share a name, because the workspace gives each name one directory ([workspace.md](./workspace.md) § Layout). The indexes cannot express that, so the name is chosen under a lock on the user's row.
- **Deleting a user is deleting the account** ([auth.md](./auth.md) § Deleting the account): one `DELETE` of the `users` row, under its row lock, and every table with a `user_id` cascades from it — sessions, API keys, OAuth grants, upload tickets, agent settings, prompt presets, skills, subagents, memories, chat attachments, folders, documents, conversations with their messages, turns and events, jobs with their pages and results, `usage_log`, subscriptions, checkouts, credit periods and credit entries. `pay_events` keep their rows with `user_id` NULL — the record of an event outlives the user it was about — and `oauth_clients`, which belong to no user, stay. PostgreSQL never reuses an id, so the same Google account signing in later is a new row: a new, empty account, on Free from zero.
- `results` holds parsed **output**, kept as long as its job (`PARSE_RETENTION_DAYS` after the parse finishes, or while a document names a parse of that original or an upload ticket that has not expired names the job, [jobs.md](./jobs.md) § Retention); the DB never stores document bytes. `jobs.source_path` points at a temp file that is deleted at terminal state unless the job belongs to a library document, in which case the worker moves it into the document's workspace directory ([workspace.md](./workspace.md)).
- **A document's editable content is a file, not a column.** `doc.md` and `notes.md` live in the user's workspace and their history is that workspace's git log; `documents` keeps the entry, the parse behind it and its place in the tree, and the workspace is reconciled to it after every chat turn ([workspace.md](./workspace.md) § The tree is the truth).
- `usage_log.agent` is written with the row (`parse` or `chat`) and was backfilled from `jobs.agent`; rows with no job and no turn are historical and count as `parse`. Chat rows carry `turn_id` and the cache token columns; parse rows carry `job_id` and zeros.
- Job claiming and per-user caps: exact queries in [jobs.md](./jobs.md).
- Timestamps are `timestamptz`, UTC.
