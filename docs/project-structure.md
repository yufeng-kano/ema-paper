# Project structure

```text
ema-paper/
  apps/
    api/                      # FastAPI app + worker (one uv project, one image)
      src/sightread/
        main.py               # FastAPI wiring: /v1, /api, /oauth, /mcp, /.well-known
                              # (/api/admin mounted only while OWNER_EMAILS names one)
        config.py             # env settings (pydantic-settings)
        runtime_settings.py   # the thirteen keys the database may override and the cached
                              # reading layer the API, the worker and the CLI share
                              # (docs/admin.md § Settings)
        db/                   # engine, models, session helpers
        auth/                 # oidc.py, sessions.py, api_keys.py, oauth_as.py, crypto.py, deps.py, upload_tickets.py, document_tickets.py
        routes/               # v1.py (data plane), control.py (/api, incl. /api/agents),
                              # library.py (/api/library — the web file library),
                              # chat.py (/api/conversations, a document's and the
                              # library's conversation lists — /api/library/conversations
                              # — and /api/sandbox),
                              # attachments.py (/api/attachments), extensions.py
                              # (/api/skills, /api/subagents), memories.py
                              # (/api/memories), oauth.py, admin.py (/api/admin —
                              # the operator console, docs/admin.md)
        agents/               # the agent registry (docs/agents.md); parse.py holds the
                              # parse agent's definition: default prompt, pipeline version;
                              # chat.py the chat agent's system prompts — DEFAULT_SYSTEM_PROMPT
                              # and a library conversation's LIBRARY_SYSTEM_PROMPT — and tool
                              # definitions, the eight library tools among them;
                              # skills.py and subagents.py the built-in skills and subagents,
                              # each with its `document_only` flag;
                              # memory.py the memory block, its selection and text cleaning;
                              # check.py the check agent's prompt and tools;
                              # summary.py the summary model's title prompt and cleaning
        admin/                # the operator console (docs/admin.md): permissions.py
                              # (ALL_PERMISSIONS, the areas, normalizing a group's list),
                              # deps.py (require_console_session, require_permission),
                              # groups.py (the built-in group, the sign-in owner join,
                              # members, the no-escalation checks), audit.py (the one
                              # helper that writes an audit row in the caller's transaction),
                              # grants.py (an operator's grant of credits or days to an
                              # account, through billing/ledger.py),
                              # codes.py (creating and disabling redeem codes through
                              # billing/codes.py, each with its audit row),
                              # jobs.py (a stuck job's requeue, the overview's checks of
                              # Kano Proxy and Kano Pay),
                              # settings.py (the settings page: a change checked,
                              # confirmed, stored, recorded)
        billing/              # plans, periods and credit arithmetic (docs/billing.md):
                              # credits.py prices a parse and a turn, plans.py reads the
                              # plans and decides the current period; pure, no HTTP
        parsing/              # poppler.py (subprocess wrappers),
                              # images.py (heic/exif/downscale), markdown.py, figures.py,
                              # joins.py (the format check's program rules),
                              # attachments.py (the chat composer's files, prepared)
        workspace/            # the per-user file tree and its git revisions
                              # (docs/workspace.md): layout.py, git.py, documents.py
                              # (with the row lookups beside is_live: owned_folder,
                              # owned_document, live_folder, live_document,
                              # nearest_live, own_subtree, subtree_ids), reconcile.py,
                              # trash.py (deleting for good: Empty Trash and the Trash's
                              # expiry, one function; lock_user, refuse_while_answering,
                              # read_row), accounts.py (a deleted account's marker and
                              # files), filing.py (names, the suffixed insert, the
                              # folders a path names and the filing of a file — what
                              # the upload route, the agent's download and the
                              # connector's filing share — and the Files page's
                              # operations the library routes and the agent's library
                              # tools share: make_folder, ensure_folders, new_document,
                              # move_folder, move_document, copy_document, copy_folder,
                              # trash_document, trash_folder, put_back_document,
                              # put_back_folder, each run under the workspace lock its
                              # caller holds and taking the commits' `author`;
                              # docs/workspace.md § The page's operations are the agent's)
        sandbox/              # client.py: the HTTP client of the sandbox runner;
                              # egress.py: the list of sites the worker writes for the
                              # egress proxy (docs/sandbox.md § Network)
        upstream/             # anthropic.py (Messages API over Kano Proxy, streaming,
                              # tools — the chat agent's client, docs/chat.md);
                              # client.py (httpx client for Kano Proxy's OpenAI-compatible
                              # route: vision calls, the model catalog, usage capture,
                              # backoff)
        jobs/                 # queue.py (claim/enqueue), intake.py (store/dedup/enqueue),
                              # runner.py, sweeper.py, events.py (SSE + event pairs),
                              # chat_runner.py (the chat agent's tool loop),
                              # check.py (the check agent's loop, inside a parse's run),
                              # chat_subagent.py (a subagent's nested loop),
                              # chat_tools.py, chat_events.py (the conversation stream),
                              # chat_memory.py (a turn's memory snapshot, the memory tool),
                              # chat_library.py (a library conversation's eight library
                              # tools: the path resolver convert_file shares, list_files'
                              # listing, each tool's call into workspace/filing.py, its
                              # sentences and its undo record — docs/chat.md § Library
                              # conversations),
                              # chat_web.py (the web tools: Brave search, OpenAlex, the
                              # guarded fetch, reading a URL, downloading into Files),
                              # summary.py (one summary-model call: `/rename`'s title)
        mcp/                  # server.py: thin shell over the service layer — parse (and its Files folder), list_library, read_document, write_document (docs/mcp.md)
        worker.py             # `python -m sightread.worker`
      migrations/             # Alembic
      tests/
      pyproject.toml          # uv-managed
      Dockerfile              # includes poppler-utils
    sandbox/                  # the sandbox runner (docs/sandbox.md): one uv project, its
                              # own image; owns the Docker socket, nothing else does.
                              # The same image runs the egress proxy, sightread_sandbox/
                              # egress.py: the VMs' one way out, with no socket and no secret
    web/                      # Nuxt control plane (docs/web.md)
      app/ or src/            # Nuxt conventions; i18n/ catalog en + zh-TW
                              # the operator console (docs/admin.md): pages/admin/,
                              # middleware/admin.ts, composables/useAdminConsole.ts,
                              # lib/admin.ts (pure: areas, nav, permission checks; vitest),
                              # lib/chart.ts (the usage chart's geometry; vitest),
                              # components/admin/
                              # the Files page's Agent card (docs/web.md § The Files
                              # page's Agent card): components/FilesAgent.vue — the card,
                              # its grip and the phone sheet around ChatPanel in library
                              # mode; the library body in lib/library.ts, the library
                              # tools' steps in lib/steps.ts, the Files links in
                              # lib/markdown.ts (pure; vitest)
      server/routes/          # robots.txt and sitemap.xml, prerendered; no API (docs/web.md § Search engines)
      shared/                 # framework-free code nuxt.config.ts, the app and server/ all read
      package.json
      Dockerfile
  deploy/
    caddy/Caddyfile           # production routing + TLS
    sandbox-image/Dockerfile  # what runs inside a user's VM: uv, poppler, rg, jq, pandoc, Noto fonts…
    sandbox-image/matplotlibrc  # matplotlib's fonts in the VM: Noto first, CJK included
    sandbox-image/pip         # `pip` in the VM: uv's pip, into the default environment
  docs/
  docker-compose.yaml             # local full stack (pg + api + worker + web), HTTP, ports exposed
  docker-compose.production.yaml  # standalone file (-f): caddy/TLS, backups; NOT an override merge
  backups/                    # gitignored; the production backup service writes dumps here
  .env.example                # every env var with placeholder values
  LICENSE                     # MIT
```

## Boundaries

- `routes/*` and `mcp/*` are thin: validation + auth + calls into `jobs`/`parsing` services. MCP owns zero business logic. `jobs/intake.py` is the shared sequence **all three** entry points run (store → hash → probe → dedup → enqueue), so REST, MCP and the web library's upload cannot drift apart — `routes/library.py` adds a name and a folder to the job that sequence returns, nothing more.
- `parsing/poppler.py` is the **only** place that spawns Poppler; everything else consumes its typed results. No PDF library imports anywhere (AGPL ban, [parsing.md](./parsing.md)).
- `upstream/` is the only package that talks to the upstream, Kano Proxy on the operator's key: `client.py` for vision calls and the catalog, `anthropic.py` for the chat and check agents' Messages API calls — the only modules that ever read `KANO_PROXY_API_KEY`.
- Nothing in `jobs/` or `workspace/` imports from `routes/`: what a route and the worker both run — *Empty Trash* and the Trash's expiry, the locks and the commit-then-discard they share, and the Files page's operations the agent's library tools run (a new folder or document, a move or a rename, a copy, Move to Trash and Put Back, in `workspace/filing.py`) — lives in `workspace/`, and the routes import it from there. They sit in `filing.py` rather than `trash.py` because `filing.py` already imports `trash.py`'s locks, and the Trash's half needs `filing.py`'s names: one module, no cycle. The routes keep what is theirs: the payloads, the bodies' validation, *Delete Immediately*, and the workspace lock taken around each call.
- `workspace/` is the only package that touches `WORKSPACES_DIR` and the only one that runs git; `sandbox/client.py` is the only module that calls the runner. The runner (`apps/sandbox`) is the only process with the Docker socket; the egress proxy beside it reads one file the worker writes (`sandbox/egress.py`) and talks to nothing of the service.
- `agents/` defines agents and nothing else: no HTTP, no database. Runners and settings consume its definitions ([agents.md](./agents.md)).
- `jobs/queue.py` is the only module that knows the queue is Postgres.
- `admin/` is the only package that decides a console permission or writes `groups`, `group_members`, `audit_log` and `runtime_settings`; `routes/admin.py` is thin over it, and the Google callback and the dev login call `admin/groups.py` for the owner join. Every console mutation writes its audit row through `admin/audit.py` before the route's one commit. Nothing outside it compares a group's name. Whether the worker runs and what a requeue does stay in `jobs/queue.py`, the one module that knows the queue is PostgreSQL: `admin/jobs.py` calls it, and reads the worker lock only through it.
- Web: pages thin, logic in composables; API access through one typed client module. The console's permission logic is the pure `app/lib/admin.ts`, so it is tested without a DOM; its pages read permissions from `useAdminConsole` only.
