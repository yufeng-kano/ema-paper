# Project structure

```text
agent-sightread/
  apps/
    api/                      # FastAPI app + worker (one uv project, one image)
      src/sightread/
        main.py               # FastAPI wiring: /v1, /api, /oauth, /mcp, /.well-known
                              # (/api/admin mounted only while OWNER_EMAILS names one)
        config.py             # env settings (pydantic-settings)
        db/                   # engine, models, session helpers
        auth/                 # oidc.py, sessions.py, api_keys.py, oauth_as.py, crypto.py
        routes/               # v1.py (data plane), control.py (/api, incl. /api/agents),
                              # library.py (/api/library — the web file library),
                              # chat.py (/api/conversations, /api/sandbox),
                              # attachments.py (/api/attachments), extensions.py
                              # (/api/skills, /api/subagents), memories.py
                              # (/api/memories), oauth.py, admin.py (/api/admin —
                              # the operator console, docs/admin.md)
        agents/               # the agent registry (docs/agents.md); parse.py holds the
                              # parse agent's definition: default prompt, pipeline version;
                              # chat.py the chat agent's system prompt and tool definitions;
                              # skills.py and subagents.py the built-in skills and subagents;
                              # memory.py the memory block, its selection and text cleaning;
                              # check.py the check agent's prompt and tools
        admin/                # the operator console (docs/admin.md): permissions.py
                              # (ALL_PERMISSIONS, the areas, normalizing a group's list),
                              # deps.py (require_console_session, require_permission),
                              # groups.py (the built-in group, the sign-in owner join,
                              # members, the no-escalation checks), audit.py (the one
                              # helper that writes an audit row in the caller's transaction)
        billing/              # plans, periods and credit arithmetic (docs/billing.md):
                              # credits.py prices a parse and a turn, plans.py reads the
                              # plans and decides the current period; pure, no HTTP
        parsing/              # poppler.py (subprocess wrappers),
                              # images.py (heic/exif/downscale), markdown.py, figures.py,
                              # joins.py (the format check's program rules),
                              # attachments.py (the chat composer's files, prepared)
        workspace/            # the per-user file tree and its git revisions
                              # (docs/workspace.md): layout.py, git.py, documents.py,
                              # reconcile.py, trash.py (deleting for good: Empty Trash
                              # and the Trash's expiry, one function), accounts.py (a
                              # deleted account's marker and files), filing.py (names,
                              # the suffixed insert and the filing of a file — what the
                              # upload route and the agent's download share)
        sandbox/              # client.py: the HTTP client of the sandbox runner
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
                              # chat_web.py (the web tools: Brave search, OpenAlex, the
                              # guarded fetch, reading a URL, downloading into Files)
        mcp/                  # server.py: thin shell over the service layer (docs/mcp.md)
        worker.py             # `python -m sightread.worker`
      migrations/             # Alembic
      tests/
      pyproject.toml          # uv-managed
      Dockerfile              # includes poppler-utils
    sandbox/                  # the sandbox runner (docs/sandbox.md): one uv project, its
                              # own image; owns the Docker socket, nothing else does
    web/                      # Nuxt control plane (docs/web.md)
      app/ or src/            # Nuxt conventions; i18n/ catalog en + zh-TW
                              # the operator console (docs/admin.md): pages/admin/,
                              # middleware/admin.ts, composables/useAdminConsole.ts,
                              # lib/admin.ts (pure: areas, nav, permission checks; vitest),
                              # components/admin/
      server/routes/          # robots.txt and sitemap.xml, prerendered; no API (docs/web.md § Search engines)
      shared/                 # framework-free code nuxt.config.ts, the app and server/ all read
      package.json
      Dockerfile
  deploy/
    caddy/Caddyfile           # production routing + TLS
    sandbox-image/Dockerfile  # what runs inside a user's VM: uv, poppler, rg, jq, pandoc…
    deploy.sh                 # the release, run on the server (POSIX sh): dump, fast-forward
                              # to origin/main, build, wait for running work, swap, health
                              # checks, rollback printed; run again, it finishes an unfinished
                              # deploy (docs/deployment.md § Release flow)
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
- Nothing in `jobs/` or `workspace/` imports from `routes/`: what a route and the worker both run — *Empty Trash* and the Trash's expiry, the locks and the commit-then-discard they share — lives in `workspace/`, and the routes import it from there.
- `workspace/` is the only package that touches `WORKSPACES_DIR` and the only one that runs git; `sandbox/client.py` is the only module that calls the runner. The runner (`apps/sandbox`) is the only process with the Docker socket.
- `agents/` defines agents and nothing else: no HTTP, no database. Runners and settings consume its definitions ([agents.md](./agents.md)).
- `jobs/queue.py` is the only module that knows the queue is Postgres.
- `admin/` is the only package that decides a console permission or writes `groups`, `group_members` and `audit_log`; `routes/admin.py` is thin over it, and the Google callback and the dev login call `admin/groups.py` for the owner join. Every console mutation writes its audit row through `admin/audit.py` before the route's one commit. Nothing outside it compares a group's name.
- Web: pages thin, logic in composables; API access through one typed client module. The console's permission logic is the pure `app/lib/admin.ts`, so it is tested without a DOM; its pages read permissions from `useAdminConsole` only.
