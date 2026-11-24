# Database

PostgreSQL 16+. Schema changes **only** via Alembic migrations under `apps/api/migrations/`, this doc updated first (§ Migrations). Never edit production schema by hand.

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
                 -- deleting the row is deleting the account (§ Deleting an account): every table
                 -- below with a user_id cascades from it; pay_events and audit_log keep
                 -- theirs as NULL
sessions         id PK, user_id FK, token_hash UNIQUE, created_at, expires_at
                 -- created_at is the sign-in: the operator console's re-login window
                 -- runs from it (admin.md § Signing in)
api_keys         id PK, user_id FK, name, key_hash UNIQUE, prefix, created_at,
                 last_used_at, revoked_at NULL
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
                 instructions TEXT NULL,          -- chat: the reader's instructions, appended
                                                  -- after the prompt (chat.md § Instructions);
                                                  -- CHAT_INSTRUCTIONS_MAX_CHARS caps a write,
                                                  -- not a stored value, which may be longer
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
                                                  -- on arrival; off (the default) keeps
                                                  -- it as a file (workspace.md § Files)
                 -- one row per user and agent (agents.md); no row means every column's
                 -- default
jobs             id UUID PK, user_id FK, agent, kind pdf|image, filename, media_type,
                 size_bytes, sha256, pages_spec, model,
                 pipeline_version, bbox_format,
                 connection_base_url NULL,        -- endpoint snapshot at enqueue:
                                                  -- KANO_PROXY_BASE_URL. NULL on a job of
                                                  -- an upstream the deployment no longer
                                                  -- has (OpenRouter, a provider
                                                  -- connection), which never runs (§ Jobs
                                                  -- and usage); part of the dedup key (jobs.md
                                                  -- § Dedup)
                 prompt TEXT,                     -- effective prompt template, verbatim
                 prompt_sha256,                   -- its hash; part of the dedup key
                 fallback_model NULL,             -- snapshot at enqueue: the fallback when
                                                  -- auto_fallback was on, else NULL;
                                                  -- cleared by a retry when the deployment
                                                  -- no longer offers it
                 retry_model NULL,                -- the model a retry runs the remaining
                                                  -- pages on; NULL = `model`, which the
                                                  -- retry left NULL only while the
                                                  -- deployment offered it (jobs.md
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
                 INDEX (user_id, agent, sha256, model, pages_spec, prompt_sha256,
                        pipeline_version)
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
                 INDEX (parent_id) WHERE parent_id IS NOT NULL
                                                  -- a folder delete's cascade reads it
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
                                                  -- failed (jobs.md § Re-converting
                                                  -- a document); ON DELETE SET NULL
                 created_at, updated_at
                 UNIQUE (user_id, folder_id, name) WHERE trashed_at IS NULL
                 UNIQUE (user_id, name) WHERE folder_id IS NULL AND trashed_at IS NULL
                 INDEX (user_id, created_at)
                 INDEX (folder_id) WHERE folder_id IS NOT NULL
                                                  -- a folder delete's cascade reads it
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
                 agent,                           -- parse | check | chat (§ Jobs and usage)
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
                 INDEX (turn_id) WHERE turn_id IS NOT NULL
                                                  -- the turn's settlement and a turn
                                                  -- delete's SET NULL read it
                 CHECK (cost_source IN (...) AND (cost_source = 'unknown') = (cost IS NULL))
conversations    id PK, user_id FK, document_id FK documents NULL, title, session,
                 model NULL, memory_ids JSONB NULL, memory_file TEXT NULL,
                 agents_file TEXT NULL,
                 compacted_message_id NULL,          -- a messages.id, no FK (below)
                 context_tokens NULL, cache_warm_until NULL,
                 head_id NULL,                       -- a chat_turns.id, no FK (below)
                 created_at, updated_at
                 INDEX (document_id, updated_at)
                 INDEX ix_conversations_library (user_id, updated_at)
                       WHERE document_id IS NULL  -- the Files page's list
                 INDEX (cache_warm_until) WHERE cache_warm_until IS NOT NULL
                 INDEX (model)                    -- the console's count of what a price
                                                  -- removed would stop (admin.md § Settings)
                 -- one thread of the chat agent about one document (chat.md), or,
                 -- with `document_id` NULL, a library conversation: the Files
                 -- page's thread about the whole library (chat.md § Library
                 -- conversations); the key stays ON DELETE CASCADE, so deleting a
                 -- document takes its own threads and never a library one, which
                 -- goes only by its own delete or with the user;
                 -- `session` names its shell in the user's sandbox; `model` is
                 -- set by the first turn and kept: the stored thinking blocks
                 -- belong to it (chat.md § History); `memory_ids` is the memory
                 -- snapshot its first turn took, in render order, NULL until a
                 -- turn took one (chat.md § Memory); `memory_file` the snapshot of
                 -- memory.md its first turn took, NULL until then, '' for none,
                 -- taken again by a compaction (chat.md § Memory, *The memory
                 -- file*); `agents_file` the snapshot of the AGENTS.md files on
                 -- the document's path — `files/AGENTS.md` alone for a library
                 -- conversation — rendered, NULL until then, '' for none,
                 -- taken again by a compaction too (chat.md § AGENTS.md);
                 -- `compacted_message_id` the summary row the replay
                 -- starts from, NULL while never compacted — no foreign key:
                 -- a message goes only with its conversation, and a key back
                 -- to `messages` would close a cycle every DDL tool would have
                 -- to break; `context_tokens`
                 -- what the last model call replayed and produced, and
                 -- `cache_warm_until` when the prompt cache it touched expires —
                 -- what the idle compaction pass reads through the partial
                 -- index (chat.md § Compaction); `head_id` the turn whose
                 -- branch the conversation shows, NULL until its first turn —
                 -- no foreign key either, for the same cycle: a turn goes
                 -- only with its conversation (chat.md § History, *An edited
                 -- message starts a version*)
messages         id PK, conversation_id FK, role user|assistant, content JSONB,
                 turn_id FK chat_turns NULL, created_at
                 INDEX (conversation_id, id)
                 INDEX (turn_id) WHERE turn_id IS NOT NULL
                                                  -- a turn delete's SET NULL reads it
                 -- content blocks exactly as sent to / received from the model;
                 -- replayed verbatim on every later request (chat.md § History);
                 -- `turn_id` NULL on a row stored before turns were recorded,
                 -- which precedes every turn and is on every branch
chat_turns       id UUID PK, user_id FK, conversation_id FK,
                 message_id,                      -- a messages.id, no FK: the user message
                                                  -- the turn answers
                 kind message|compact|idle_compact NOT NULL DEFAULT 'message',
                 status queued|running|succeeded|failed|cancelled, error NULL,
                 locale, created_at, finished_at NULL,
                 parent_id NULL                   -- a chat_turns.id, no FK (below)
                 INDEX (status, created_at)
                 INDEX (conversation_id, finished_at)
                                                  -- the stream's "is a turn unfinished"
                                                  -- poll, sends, cancels and deletes all
                                                  -- ask by conversation
                 -- the chat agent's queue, one row per user message — or per
                 -- compaction, `kind` saying which (chat.md § Compaction) —
                 -- claimed like jobs; `parent_id` the turn before it on its
                 -- branch, NULL for a conversation's first — a self-reference
                 -- without a foreign key: the rows go only with their
                 -- conversation, which takes them together. Turns with one
                 -- parent are the versions of one message, and
                 -- `conversations.head_id` names the branch shown (chat.md
                 -- § History, *An edited message starts a version*)
chat_events      id PK, conversation_id FK, turn_id FK, seq, event, data JSONB,
                 created_at
                 UNIQUE (conversation_id, seq)
                 INDEX (turn_id)                  -- a turn delete's cascade reads it
                 -- what the worker streamed while the turn ran; the SSE route replays
                 -- and tails it (chat.md § Conversations, turns, events)
chat_tool_calls  id PK, user_id FK, turn_id FK chat_turns ON DELETE SET NULL, NULL,
                 tool, created_at
                 INDEX (tool, created_at)                  -- the deployment's per-day caps; `tool` a web tool's name
                 INDEX (user_id, tool, created_at)         -- a user's per-day cap
                 INDEX (turn_id)                           -- the turn's settlement
                 -- one row per web tool call that was answered or kept a file —
                 -- `web_search`, `search_papers`, `download_file`: what the per-day caps
                 -- count and what a turn's web searches are charged from (chat.md § Tools,
                 -- billing.md § A chat turn); a deleted turn keeps its rows for the day's
                 -- count, as a purged parse keeps its entry
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
upload_tickets   id PK, user_id FK, token_hash UNIQUE, job_id FK NULL,
                 created_at, expires_at, spent_at NULL,
                 into_library BOOL (false), folder_id FK NULL
                 INDEX (user_id, created_at)                                 -- mint rate limit
                 -- single-use upload credential minted by the MCP `parse` tool
                 -- (auth.md § 5); job_id set when the upload spends the ticket,
                 -- ON DELETE SET NULL; a ticket not yet expired keeps its job from
                 -- the parse retention (jobs.md § Retention); into_library: the upload
                 -- is filed into Files, in folder_id (NULL the top level), ON DELETE
                 -- SET NULL (mcp.md § Filing into Files)
document_tickets id PK, user_id FK, document_id FK, scope read|write,
                 token_hash UNIQUE, created_at, expires_at
                 INDEX (user_id, created_at)                                 -- mint rate limit
                 -- a short-lived credential for one document, minted by the MCP
                 -- library tools (auth.md § 7); not spent by use; both foreign keys
                 -- ON DELETE CASCADE, so a document deleted for good takes its tickets
oauth_clients    client_id PK, client_name, redirect_uris JSONB, created_at
oauth_grants     id PK, client_id FK, user_id FK, kind code|access|refresh,
                 token_hash UNIQUE, pkce_challenge NULL, redirect_uri NULL,
                 scope, expires_at, revoked_at NULL, created_at
                 -- scope is space-separated, `parse library` on every live grant; a
                 -- refresh carries it over (auth.md § 4)
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
                 user_id FK NULL
                 -- processed notifications; a duplicate id is answered and not applied
credit_periods   (user_id FK, period_start) PK, period_end,
                 granted INT, spent INT,          -- the balance's fast path:
                                                  -- remaining = granted - spent
                 created_at, updated_at
                 CHECK (period_end > period_start), CHECK (granted >= 0)
                 -- one row per user per allowance period (Free's UTC month, or a paid
                 -- period), created on first use (billing.md § Periods)
credit_entries   id PK, user_id FK, period_start,
                 kind parse|chat, state reserved|consumed|released,
                 amount INT, job_id FK jobs ON DELETE SET NULL, NULL UNIQUE,
                 turn_id FK chat_turns ON DELETE SET NULL, NULL UNIQUE,
                 created_at, settled_at NULL
                 INDEX (user_id, created_at)
                 CHECK (amount >= 0), CHECK (kind = 'parse' OR job_id IS NULL),
                 CHECK (kind = 'chat' OR turn_id IS NULL)
                 -- what a parse or a turn cost (billing.md § The ledger): one entry per
                 -- job, reserved at enqueue and settled at the terminal state; one per
                 -- turn, its amount raised as the turn runs. A parse the retention
                 -- deletes (jobs.md § Retention) leaves its entry with job_id NULL, and
                 -- a conversation the reader deletes takes its turns and leaves their
                 -- entries with turn_id NULL, so the balance and its record keep agreeing.
                 -- A title the summary model wrote (/rename, billing.md § A chat turn)
                 -- is a chat entry that never had a turn
credit_packs     id PK,                           -- Pay's purchaseId, redeem-<redemption id>,
                                                  -- or grant-<request id> (admin.md § Users
                                                  -- and grants)
                 user_id FK, pack_id, credits INT, spent INT,
                 status paid|refunded, purchased_at, expires_at,
                 version BIGINT,                  -- Pay's version (ms); older is ignored
                 source pay|code|grant,           -- bought, a redeem code's, or an
                                                  -- operator's grant
                 redemption_id FK redemptions ON DELETE SET NULL, NULL UNIQUE,
                 created_at, updated_at
                 INDEX (user_id, expires_at)
                 CHECK (credits >= 0), CHECK (spent >= 0), CHECK (expires_at > purchased_at)
                 CHECK (source IN ('pay', 'code', 'grant'))
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
groups           id PK, name UNIQUE,              -- a-z 0-9 _ -, 1-32 (admin.md § Groups)
                 permissions JSONB,               -- a list of permission strings,
                                                  -- normalized (admin.md § Permissions);
                                                  -- [] on the built-in group, which has all
                 builtin BOOL NOT NULL DEFAULT false,
                 created_at, updated_at
                 UNIQUE (builtin) WHERE builtin   -- one built-in group: owner
                 -- binds people to permissions; code reads the permissions and the builtin
                 -- flag, never a name. The baseline migration seeds the built-in row, and
                 -- the code creates it when it finds none, since a database built from the
                 -- models — the tests' — has no seed
group_members    (group_id FK, user_id FK) PK,
                 added_by FK users ON DELETE SET NULL, NULL,
                                                  -- who added the member: the member
                                                  -- themselves for a sign-in owner join
                 added_at
                 INDEX (user_id)
                 -- group_id and user_id ON DELETE CASCADE: a deleted group or account
                 -- takes its memberships (admin.md § Groups)
audit_log        id BIGINT PK,
                 actor_id FK users ON DELETE SET NULL, NULL,
                 permission VARCHAR(32) NULL,     -- what the route required; NULL for a
                                                  -- sign-in's owner join
                 action VARCHAR(64),              -- <area>.<verb> (admin.md § The audit log)
                 target_type VARCHAR(32),         -- group | user | code | job | setting
                 target_id VARCHAR(64),           -- no FK: a record outlives its target;
                                                  -- a job's is its UUID as text, a setting's its key
                 before JSONB NULL, after JSONB NULL,
                                                  -- ids, amounts, flags, ISO dates; a
                                                  -- grant's optional reason, the one free
                                                  -- text; never an address, a name,
                                                  -- content or a secret
                 ip VARCHAR(45) NULL,             -- the request's client address
                 created_at
                 INDEX (created_at), INDEX (actor_id, created_at),
                 INDEX (target_type, target_id)
                 -- one row per change made through the operator console, written in the
                 -- change's own transaction; no route updates or deletes one, nothing sweeps
                 -- the table; a deleted actor leaves actor_id NULL
runtime_settings key VARCHAR(64) PK,              -- one of the thirteen keys the database may
                                                  -- override, as .env names it: CREDIT_USD
                 value JSONB,                     -- a JSON string: the text .env would hold
                 updated_by FK users ON DELETE SET NULL, NULL,
                 updated_at
                 -- the operator console's settings over .env (admin.md § Settings): a key
                 -- without a row is .env's; a row whose key is not one of the thirteen is
                 -- ignored; written only by the settings routes, in the transaction of
                 -- their audit record
```

## Migrations

- **The chain starts at one baseline.** `4449f68533c0` (`baseline`, no `down_revision`) creates the schema production carries at that revision: every table, column, default, constraint and index, under the name production gives it.
  - It also creates what autogenerate cannot see: the `worker_generation_seq` sequence (PostgreSQL only) and one row, the built-in group `owner` (`builtin` true, an empty permission list, no members; [admin.md](./admin.md) § Groups).
  - Its downgrade drops all of it.
  - The revisions after it form one line, each revising the one before it, and bring that schema to the tables above. The last is the chain's one head.
  - `alembic upgrade head` runs the revisions a database lacks when it is at `4449f68533c0` or a later revision of the chain, and the whole chain on an empty one.
- **A database on a revision outside the chain is not on it.** `alembic upgrade head` stops on it with *Can't locate revision*, and the api, which runs it before it serves, does not start.
  - Such a database — an install last updated before the baseline, or a backup taken from one — is first brought to `4449f68533c0`, once, by the last release that carries the earlier revisions, commit `945b2e9` ([self-hosting.md](./self-hosting.md) § Keep it running).
- **A new migration** starts from `alembic revision` (`--autogenerate` against a database at head), never a hand-built file.
  - The post-write hook in `alembic.ini` runs `ruff format` over it.
  - `migrations/env.py` renders the models' `UtcDateTime` as `sa.DateTime(timezone=True)`, so a revision imports nothing from the app.
  - It revises the head and leaves the database equal to the models — columns, defaults, constraints, indexes and sequences, names included. `tests/test_migrations.py` compares an upgraded database with one built from the models ([testing.md](./testing.md) § Backend).
  - A revision that has shipped is never edited; a correction is a new revision.
- **Constraint names are the models'.** A constraint the models name is created under that name.
  - One they leave unnamed gets PostgreSQL's default (`<table>_<column>_fkey`, `<table>_<column>_key`), which `create_all` and a migration give alike.
  - A migration that gives a constraint a name gives it the same name in the models, because autogenerate matches an unnamed constraint by its columns and would never see the two disagree.
- **A migration that rewrites existing rows** ships with a test in `tests/test_migrations.py` that upgrades a scratch database from the previous head with such rows in place and checks what it left.
  - A downgrade that could undo its change only by deleting or relabelling rows a user made refuses while such rows exist, naming the count.
- **A squash** replaces the chain with a new baseline that keeps the head's revision id, so a database at head runs nothing. The new baseline:
  1. is generated with `alembic revision --autogenerate --rev-id <head>` against an empty database;
  2. is given the sequence and the seed rows by hand;
  3. has its generated column lines moved into the chain's column order (ADD COLUMN appends, so pg_dump lists the chain's columns in the order they were added);
  4. is proven equal to the chain by a `pg_dump` of a database each builds — schema and seed rows — before the old revisions are deleted, with the tests that only they needed.

## Rules

### Credentials

- Credentials we only verify are stored as their SHA-256 hash ([auth.md](./auth.md)): sessions, API keys, upload and document tickets, OAuth codes and tokens.
- No column holds an upstream key or any secret we would have to replay, encrypted or not.
- The one credential stored as written is a redeem code (`redeem_codes.code`, [billing.md](./billing.md) § Redeem codes). It is never logged, and the console shows it whole only in the answer that created it ([admin.md](./admin.md) § Redeem codes).

### Settings

- **A stored setting is the text, not the value it reads as.** `runtime_settings.value` is a JSON string holding exactly what `.env` would — `"0.002"`, `"[{\"planId\": \"pro\", …}]"` — never the number or the JSON it parses to.
  - The reason: `jsonb` keeps no object key order, and `MODEL_PRICES_JSON`'s order is the chat agent's model order.
  - The settings are parsed and checked from that text as from `.env` ([admin.md](./admin.md) § Settings).

### The console's tables

**The console's tables say who may do what, and what was done; the permissions themselves are code.**

- `groups.permissions` is a JSON list rather than an array type, because the SQLite fallback the tests run on has none.
  - It is normalized before it is written and read whole; no query looks inside it.
  - The strings it may hold are `ALL_PERMISSIONS` ([admin.md](./admin.md) § Permissions), so a new permission needs no migration.
- One group carries `builtin`, which the code checks instead of a name.
- `audit_log` is append-only: a row is written in the transaction of the change it records and never updated or deleted.
  - Its `before` and `after` hold ids, amounts, flags, permission strings, group names, plan ids and dates as ISO 8601 strings — never an address, a person's name, content or a secret. Who a row is about is joined from `users` when it is read.
  - `target_id` is text with no foreign key — a group's, an account's or a code's id, a job's UUID, a setting's key — so a record outlives what it names.
  - The one free text a row holds is a grant's optional `reason` in `after`, the operator's own note, written under the same rule ([admin.md](./admin.md) § Users and grants). It stays when the account it is about is deleted, as the row does.
  - A day grant's `after` also keeps the request's `request_id`, since days have no row of their own to carry the id. That is the one query that looks inside `after`: a day grant sent again is found by `after ->> 'request_id'` among that account's `users.grant_days` rows, narrowed by the `(target_type, target_id)` index, under the account's row lock.

### Jobs and usage

- **The upstream key is read at run time**, never snapshotted onto a job: the worker presents `KANO_PROXY_API_KEY` as configured when the job runs, so a rotated key applies to queued work.
  - The model is snapshotted onto `jobs.model` at enqueue, like every other parse parameter.
  - The dedup cache is protected separately by the `connection_base_url` snapshot ([jobs.md](./jobs.md) § Dedup).
- **A job with no endpoint snapshot never runs.**
  - `jobs.connection_base_url` is NULL only on a job of an upstream the deployment no longer has — OpenRouter, or a user's provider connection.
  - It is never NULL on a new job, which snapshots `KANO_PROXY_BASE_URL`.
  - The result of such a job never answers a dedup lookup.
  - The worker fails such a job when it takes it, a retry's included, rather than run it on the operator's key: Kano Proxy would transcribe only the pages the other upstream failed, and the mixed result would then be keyed as a Kano Proxy one ([jobs.md](./jobs.md) § Dedup; [jobs.md](./jobs.md) § Retrying failed pages).
- **A usage row's cost is only as good as its source.** `cost_source` is written with the row and never revised.
  - `openrouter` is billing. No new row is `openrouter`; the historical ones stay readable and keep counting as billed.
  - `estimated` is what the endpoint said the call cost.
  - `unknown` has `cost` NULL.
  - A row whose job the retention deletes ([jobs.md](./jobs.md) § Retention) keeps the `cost_source` and the `agent` it was written with.
- `usage_log.agent` is written with the row — `parse`, `check` or `chat` — and every row carries one, whatever its job or turn became.
  - A row with no job and no turn is a parse or a check whose job is gone, a chat call whose conversation was deleted, or a title's call (`/rename`, [chat.md](./chat.md) § Conversations, turns, events). The last two are written with `agent` `chat`.
  - Chat rows carry `turn_id` while their turn exists, and the cache token columns.
  - Parse and check rows carry `job_id` and zeros.
- Job claiming and per-user caps: exact queries in [jobs.md](./jobs.md).

### The library

- **A document is a library entry, not a second copy of the result.** `documents` names a parse and places it in a folder; the markdown lives in `results` exactly once, keyed by the job that produced it.
  - `kind` says which of the three a row is. For `parse` and `markdown`, `job_id` agrees: a parse has one, a markdown document none.
    - `parse`: a parse, named by `job_id`.
    - `markdown`: a markdown document has no parse. `job_id` is NULL, and its baseline is the `.md` it was uploaded as, kept as its original, or nothing for one written new ([workspace.md](./workspace.md) § Markdown documents).
    - `file` ([workspace.md](./workspace.md) § Files): no parse, no baseline, the one copy of its bytes in the tree at `files/<folder>/<name>`. Its `media_type` and `size_bytes` are on the row because the file itself is the only other place they could be read from. Reconciliation removes the row when the file is gone from the tree — the one row it removes ([workspace.md](./workspace.md) § The tree is the truth).
  - Deleting a document deletes the entry only. Its parse then falls under the retention ([jobs.md](./jobs.md) § Retention): kept while any document of the user names a parse of the same original or an upload ticket that has not expired names it, else deleted `PARSE_RETENTION_DAYS` after it finished.
  - The parse's `usage_log` rows and its ledger entry outlive it with `job_id` NULL, because they are the billing record of something that really ran.
  - Deleting a folder cascades to its subfolders and their documents, the way removing a directory does.
- `results` holds parsed **output**, kept as long as its job ([jobs.md](./jobs.md) § Retention). The DB never stores document bytes.
- `jobs.source_path` points at a temp file that is deleted at terminal state, unless the job is the parse behind a library document: then the worker files it as that document's original, outside the VM's mount, and puts a copy of it in the document's workspace directory ([jobs.md](./jobs.md) § Retention, [workspace.md](./workspace.md)).
- **A document's editable content is a file, not a column.** `doc.md` and `notes.md` live in the user's workspace, and their history is that workspace's git log.
  - `documents` keeps the entry, the parse behind it and its place in the tree.
  - The workspace is reconciled to it after every chat turn ([workspace.md](./workspace.md) § The tree is the truth).
- **The root level needs its own unique index.** `folders.parent_id` and `documents.folder_id` are NULL at the root, and PostgreSQL treats NULLs as distinct in a unique constraint, so `UNIQUE (user_id, parent_id, name)` would not stop two root folders called "Invoices". The partial indexes above close that.
  - The application checks the same thing before writing, which is what turns a collision into `report (2).pdf` instead of a 409.
  - It checks across both tables: a folder and a document in one parent cannot share a name, because the workspace gives each name one directory ([workspace.md](./workspace.md) § Layout).
  - The indexes cannot express that, so the name is chosen under a lock on the user's row.

### The Trash

- **The Trash is a column, not a table.** Putting a folder or a document in the Trash sets its `trashed_at` and nothing else.
  - The row keeps its place (`parent_id`, `folder_id`) and its name, which is where *Put Back* returns it.
  - Everything under a trashed folder is in the Trash with it without a write of its own.
- ***Live* is computed, not stored.** A folder is live when neither it nor any folder above it has `trashed_at`. A document is live when it has none and its folder (if any) is live.
  - Every route of the library, the reader and the agent answers 404 for what is not live ([workspace.md](./workspace.md) § The Trash).
- **Names are unique among the live rows only.** The unique indexes carry `WHERE trashed_at IS NULL`, so a trashed `report.pdf` does not keep its folder from taking a new one.
  - Put Back resolves the collision the way a move does, with ` (2)`.
  - A row inside a trashed folder still holds its name there, which nothing can contest: a trashed folder takes no new entries.
  - That is decided again under the user's row lock, which a move to the Trash takes too. A folder or a file created while its destination goes to the Trash either lands first and goes with it, or is refused (404); an upload whose parse is already under way lands at the top level instead, since the parse is not undone.
- An item trashed on its own stays its own entry in the Trash when a folder above it is trashed later.
- Deleting a row for good — from the Trash, or a live one deleted immediately — takes its own subtree only. An item inside it that was trashed separately is moved up to the deleted folder's parent first, so the cascade cannot take it, and it stays in the Trash.
- The Trash is emptied by the reader — *Delete Immediately*, *Empty Trash* — and by the worker's sweep.
  - The sweep deletes for good, the way *Delete Immediately* does, every item whose own `trashed_at` is older than `TRASH_RETENTION_DAYS` (30; 0 = never), reading the partial `trashed_at` indexes ([workspace.md](./workspace.md) § The Trash).
  - An expired folder takes its whole subtree, and nothing in it outlives it: an item trashed on its own inside it was trashed earlier, nothing inside a trashed folder being trashable, so it has expired too.

### Deleting an account

- **Deleting a user is deleting the account** ([auth.md](./auth.md) § Deleting the account): one `DELETE` of the `users` row, under its row lock.
- Every table with a `user_id` cascades from it: sessions, API keys, OAuth grants, upload tickets, document tickets, agent settings, skills, subagents, memories, chat attachments, chat tool calls, folders, documents, conversations with their messages, turns and events, jobs with their pages and results, `usage_log`, subscriptions, checkouts, pack checkouts, credit periods, credit entries, credit packs, redemptions, plan grants, and the account's memberships of the operator console's groups (`group_members`, by `user_id`).
- Some rows outlive the account:
  - `pay_events` keep their rows with `user_id` NULL: the record of an event outlives the user it was about.
  - The console's `audit_log` rows keep their rows too, with `actor_id` NULL. A row the account made keeps what was changed and loses who; a row about the account keeps its `target_id`, a number no address resolves from any more ([admin.md](./admin.md) § The audit log).
  - A membership the account added keeps its row with `added_by` NULL.
  - `oauth_clients`, which belong to no user, stay.
- Nothing refuses for the console: deleting the last owner's account leaves the owner group empty.
- PostgreSQL never reuses an id, so the same Google account signing in later is a new row: a new, empty account, on Free from zero.

### Timestamps

- Timestamps are `timestamptz`, UTC.
