# Project structure

## Layout

```text
ema-paper/
  apps/
    api/                          # FastAPI app + worker: one uv project, one image
      src/sightread/
        main.py                   # the app: /api, /v1, /oauth, /.well-known, /mcp (/api/admin while OWNER_EMAILS names one)
        worker.py                 # `python -m sightread.worker`: drains the queue, runs the periodic tasks
        config.py                 # env settings (pydantic-settings); .env.example lists them
        runtime_settings.py       # the settings in effect: .env with the database's overrides (docs/admin.md § Settings)
        errors.py                 # the one error shape both planes answer with (docs/api.md)
        body_limit.py             # the ceiling on request bodies that are not streamed uploads
        codes.py                  # `python -m sightread.codes`: the operator's redeem codes
        stopping.py               # pause(): the wait a shutdown cuts short, for the worker's periodic tasks
        db/
          models.py               # SQLAlchemy models (docs/database.md)
          session.py              # engine and request-scoped sessions
        auth/                     # credentials (docs/auth.md)
          oidc.py                 # Google sign-in
          sessions.py             # server-side web sessions
          api_keys.py             # project API keys
          oauth_as.py             # the OAuth 2.1 authorization server's state
          crypto.py               # credential hashing and token minting
          deps.py                 # the FastAPI dependencies both planes share
          tickets.py              # what upload and document tickets share
          upload_tickets.py       # single-use upload tickets for the MCP `parse` tool
          document_tickets.py     # document tickets for the MCP library tools
        routes/                   # HTTP routes, one module per surface
          v1.py                   # /v1: the data plane (docs/api.md)
          control.py              # /api: sign-in, the account, keys, agent settings
          library.py              # /api/library: the Files library (docs/web.md § Files)
          multipart.py            # the streaming multipart reader of the library's upload
          chat.py                 # /api/conversations and /api/sandbox
          attachments.py          # /api/attachments: the chat composer's files
          extensions.py           # /api/skills and /api/subagents
          memories.py             # /api/memories
          billing.py              # /api/billing, the Kano Pay webhook among them
          admin.py                # /api/admin: the operator console (docs/admin.md)
          oauth.py                # /oauth and /.well-known: the authorization server's endpoints
        mcp/
          server.py               # the MCP endpoint at /mcp (docs/mcp.md)
        agents/                   # the agent registry: what each agent is (docs/agents.md)
          parse.py                # the parse agent: default prompt, pipeline version
          bbox.py                 # the box convention each model family is asked for, and its conversion back
          check.py                # the format check agent: prompt and tools
          chat.py                 # the chat agent: system prompts and tool definitions
          skills.py               # the chat agent's built-in skills, merged with a user's
          subagents.py            # the chat agent's built-in subagents, merged with a user's
          memory.py               # the memory block: what a memory holds, which load, how text is cleaned
          summary.py              # the summary model's instructions and the reading of its answer
        parsing/                  # the parse pipeline's local work (docs/parsing.md)
          poppler.py              # Poppler subprocess wrappers
          images.py               # image input normalization
          markdown.py             # markdown assembly and the `sightread://` placeholder contract
          figures.py              # figure crops
          figure_check.py         # the figure check's raster work and what its model call shows
          joins.py                # the format check's program rules
          attachments.py          # the chat composer's files, prepared for the model
        jobs/                     # the queue, the runners and the streams (docs/jobs.md, docs/chat.md)
          queue.py                # the PostgreSQL queue: enqueue, claim, requeue, the worker lock
          intake.py               # parse intake: store, hash, probe, dedup, enqueue (`submit_parse`)
          runner.py               # the parse agent's runner
          check.py                # the format check's run, inside a parse's run
          sweeper.py              # the worker's sweep: retention, the Trash's expiry, repairs
          events.py               # SSE progress streams and the LISTEN/NOTIFY plumbing both streams share
          chat_runner.py          # the chat agent's runner: one turn, one tool loop
          chat_tools.py           # what the chat agent's tools do on the host
          chat_library.py         # a library conversation's library tools
          chat_web.py             # the web tools: search, reading a URL, downloading into Files
          chat_subagent.py        # a subagent's nested loop
          chat_memory.py          # a turn's memory snapshot and the `memory` tool
          chat_compact.py         # compaction and the worker's idle pass
          chat_versions.py        # a conversation's turns as a tree of versions
          chat_events.py          # the conversation's event stream
          summary.py              # the summary model's one call: `/rename`'s title
        workspace/                # every user's file tree and its git revisions (docs/workspace.md)
          layout.py               # where things live in a workspace: pure path arithmetic
          safe_io.py              # opening workspace files without following a link
          locks.py                # the workspace, shell and backup locks and the writing marker
          git.py                  # git as the revision store
          documents.py            # a document's files, and the row lookups beside `is_live`
          filing.py               # filing into the library, and the Files page's operations
          trash.py                # deleting for good from the Trash (`empty_trash`)
          reconcile.py            # the rows brought in line with the tree
          accounts.py             # a deleted account's marker and files
          memory_file.py          # the chat agent's memory.md
          agents_file.py          # the AGENTS.md files on a document's path
        sandbox/                  # the service's side of the sandbox (docs/sandbox.md)
          client.py               # the runner's HTTP client
          egress.py               # the sites list the worker writes for the egress proxy
        upstream/                 # the model upstream: Kano Proxy on the operator's key
          client.py               # its OpenAI-format route: vision calls, the model catalog
          anthropic.py            # its Messages API route: the chat, check and summary calls
        billing/                  # plans, credits and payment (docs/billing.md)
          plans.py                # plans and allowance periods
          credits.py              # what a parse and a turn cost
          ledger.py               # the credit ledger
          codes.py                # redeem codes
          pay.py                  # the Kano Pay adapter
        admin/                    # the operator console's rules and writes (docs/admin.md)
          permissions.py          # the console's permissions, fixed in code
          deps.py                 # `require_console_session`, `require_permission`
          groups.py               # groups, members, the built-in group and the owner join
          audit.py                # the audit log's one writer
          grants.py               # an operator's grants of credits or days
          codes.py                # creating and disabling redeem codes
          jobs.py                 # a stuck job's requeue, the Kano Proxy and Kano Pay checks
          settings.py             # the settings page: reads and changes
      migrations/                 # Alembic (docs/database.md)
      tests/                      # pytest, every upstream stubbed (docs/testing.md)
      alembic.ini
      pyproject.toml, uv.lock     # uv-managed
      Dockerfile                  # includes poppler-utils and git
    sandbox/                      # the sandbox runner and the egress proxy: one uv project, one image (docs/sandbox.md)
      src/sightread_sandbox/
        main.py                   # the runner's HTTP contract
        __main__.py               # `python -m sightread_sandbox`: starts the runner
        backends.py               # where a shell runs: one Docker container per user
        shell.py                  # a persistent shell session
        config.py                 # the SANDBOX_* settings
        egress.py                 # the egress proxy: the VMs' one way out
      tests/
      pyproject.toml, uv.lock
      Dockerfile
    web/                          # the Nuxt web app (docs/web.md)
      app/
        app.vue, error.vue        # the root component and the error page
        pages/                    # one file per route; pages/admin/ the console's
        layouts/                  # default.vue: the signed-in shell and the console's nav
        components/               # Vue components; admin/, agents/, chat/, settings/, ui/ by area
        composables/              # shared reactive state (useAuth, useAdminConsole, useConversationStream…)
        lib/                      # framework-free logic with vitest tests; api.ts the typed API client
        middleware/               # route guards: authed.ts, admin.ts
        plugins/                  # client start-up: the session, the theme, the build update
        types/                    # page-meta.d.ts: the `adminArea` page meta
        assets/css/               # global styles
      i18n/locales/               # en.ts, zh-TW.ts: the UI catalog
      shared/                     # seo.ts: framework-free code nuxt.config.ts, the app and server/ all read
      server/routes/              # robots.txt and sitemap.xml, prerendered; no API (docs/web.md § Search engines)
      public/                     # icons and the web manifest
      nuxt.config.ts, package.json, Dockerfile
  deploy/
    caddy/Caddyfile               # production routing + TLS
    caddy/Caddyfile.local         # the local stack's single origin, plain HTTP
    sandbox-image/                # a user's VM image: Dockerfile, matplotlibrc (Noto fonts first), pip (uv's pip)
  docs/                           # the specification; start at index.md
  docker-compose.yaml             # the local full stack, plain HTTP, ports exposed
  docker-compose.production.yaml  # production, standalone (-f): Caddy/TLS, backups; not an override merge
  docker-compose.only-container-network.yaml  # an overlay onto production for a host behind a front proxy
  .env.example                    # every local env var, placeholder values
  .env.production.example         # every production env var, placeholder values
  backups/                        # gitignored; the production backup service writes its dumps here
  .rule                           # the agent rules; AGENTS.md and CLAUDE.md link to it
  README.md, README.zh-TW.md
  LICENSE                         # MIT
```

## Boundaries

### Entry points

- `routes/` and `mcp/` are thin: validation, auth and calls into the service packages; no business logic lives in them.
- `mcp/server.py` is a shell over the service layer: no parsing decision, no model choice, no queue knowledge and no workspace write lives in it, so an MCP spec change touches that file alone.
- `jobs/intake.py` (`submit_parse`) is the one intake sequence — store, hash, probe, dedup, enqueue — that REST, MCP and the library's upload all run, so the three cannot drift apart.
- MCP's `parse` reaches it through `POST /v1/parse`, where its upload lands.
- `routes/library.py` files the job that sequence returns under a name and a folder, nothing more.
- The service packages — `jobs/`, `workspace/`, `parsing/`, `agents/`, `billing/`, `upstream/`, `sandbox/`, `admin/` — import neither FastAPI nor Starlette, because the worker and the CLI run the same code with no request.
- The one exception is `admin/deps.py`, the console's FastAPI dependencies; a route hands an upload to `jobs/intake.py` as a `ChunkReader`, never as a framework type.
- Nothing in `jobs/` or `workspace/` imports from `routes/`, because the worker never imports a route.
- What a route and the worker both run lives in `workspace/`, and the routes import it from there.
- `trash.py` holds *Empty Trash* and the Trash's expiry (`empty_trash`) with the locks and the commit-then-discard they share with the routes (`lock_user`, `refuse_while_answering`, `commit_then_discard`).
- `filing.py` holds the Files page's operations, which the library routes and the agent's library tools both run.
- The Files page's operations sit in `filing.py`, not `trash.py`, because `filing.py` imports `trash.py`'s locks and the Trash's half needs `filing.py`'s names: one module, no cycle.
- The routes keep what is theirs: the payloads, the bodies' validation, *Delete Immediately*, and the workspace lock taken around each call.

### PDFs and the upstream

- `parsing/poppler.py` is the only module that spawns Poppler; everything else consumes its typed results.
- The application imports no PDF library, because PyMuPDF is AGPL and the only PDF engine is Poppler as a separate program ([parsing.md](./parsing.md)).
- The tests' fixture PDFs are built with `reportlab`, a dev dependency (BSD) the application never imports ([testing.md](./testing.md) § The suite).
- `upstream/` is the only package that calls the model upstream, Kano Proxy on the operator's key: `client.py` for vision calls and the catalog, `anthropic.py` for every Messages API call.
- `kano_connection` in `upstream/client.py` is the only code that reads the value of `KANO_PROXY_API_KEY`; its callers hold the `Connection` it returns and hand it back to `upstream/`.
- Elsewhere only whether the key is set is checked: `config.py`, and the console's settings page, which shows a secret as set or unset.

### The workspace and the sandbox

- `workspace/` is the only package that touches `WORKSPACES_DIR`: everything else reaches a user's files through its functions, because every open there goes through `safe_io.py`, which follows no link a shell could have planted.
- `workspace/` is the only package that runs git.
- `sandbox/client.py` is the only module that calls the sandbox runner.
- The runner (`apps/sandbox`) is the only process with the Docker socket.
- The egress proxy reads one file the worker writes (`sandbox/egress.py`) and talks to nothing of the service: it holds no secret and no setting of the database.

### Agents, the queue and billing

- `agents/` defines agents and nothing else: no HTTP, no database; the runners and the settings consume its definitions ([agents.md](./agents.md)).
- `jobs/queue.py` is the only module that knows the queue is PostgreSQL: the claim, the requeue and the worker lock.
- `billing/plans.py` and `billing/credits.py` are pure: no HTTP, no database session; the rows and the clock are passed in.
- `billing/pay.py` is the only writer of `subscriptions`, from Kano Pay's canonical answer, never from a notification's body.

### The operator console

- `admin/` is the only package that decides a console permission or writes `groups`, `group_members`, `audit_log` and `runtime_settings`; `routes/admin.py` is thin over it.
- The Google callback and the dev login call `join_owner_group` in `admin/groups.py` for the owner join.
- Every console mutation writes its audit row through `admin/audit.py`, in the caller's transaction, before the route's one commit, so neither exists without the other.
- Nothing outside `admin/` compares a group's name: a group counts by its permissions and its `builtin` flag.
- Whether the worker runs and what a requeue does stay in `jobs/queue.py`: `admin/jobs.py` calls it and reads the worker lock only through it.

### Web

- Pages and components stay thin: a rule that can be pure is a framework-free module in `app/lib/` with a vitest test, and shared reactive state is a composable.
- All API access goes through the one typed client, `app/lib/api.ts`.
- The console's permission logic is the pure `app/lib/admin.ts`, so it is tested without a DOM; the console reads permissions from `useAdminConsole` only.
