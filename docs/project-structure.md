# Project structure

```text
agent-sightread/
  apps/
    api/                      # FastAPI app + worker (one uv project, one image)
      src/sightread/
        main.py               # FastAPI wiring: /v1, /api, /oauth, /mcp, /.well-known
        config.py             # env settings (pydantic-settings)
        db/                   # engine, models, session helpers
        auth/                 # oidc.py, sessions.py, api_keys.py, oauth_as.py, crypto.py
        routes/               # v1.py (data plane), control.py (/api, incl. /api/agents),
                              # kano.py (/api/kano — the Kano Proxy connect flow),
                              # library.py (/api/library — the web file library),
                              # chat.py (/api/conversations, /api/sandbox),
                              # attachments.py (/api/attachments), extensions.py
                              # (/api/skills, /api/subagents), oauth.py
        agents/               # the agent registry (docs/agents.md); parse.py holds the
                              # parse agent's definition: default prompt, pipeline version;
                              # chat.py the chat agent's system prompt and tool definitions;
                              # skills.py and subagents.py the built-in skills and subagents
        parsing/              # poppler.py (subprocess wrappers),
                              # images.py (heic/exif/downscale), markdown.py, figures.py,
                              # attachments.py (the chat composer's files, prepared)
        workspace/            # the per-user file tree and its git revisions
                              # (docs/workspace.md): layout.py, git.py, documents.py,
                              # reconcile.py
        sandbox/              # client.py: the HTTP client of the sandbox runner
        upstream/             # anthropic.py (Messages API over Kano Proxy, streaming,
                              # tools — the chat agent's client, docs/chat.md);
                              # client.py (httpx client for Kano Proxy and user-defined
                              # OpenAI-compatible connections: vision calls, model
                              # catalogs, usage capture, backoff); kano.py (the connect
                              # flow's authorize URL and token exchange)
        jobs/                 # queue.py (claim/enqueue), intake.py (store/dedup/enqueue),
                              # runner.py, sweeper.py, events.py (SSE + event pairs),
                              # chat_runner.py (the chat agent's tool loop),
                              # chat_subagent.py (a subagent's nested loop),
                              # chat_tools.py, chat_events.py (the conversation stream)
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
      package.json
      Dockerfile
  deploy/
    caddy/Caddyfile           # production routing + TLS
    sandbox-image/Dockerfile  # what runs inside a user's VM: uv, poppler, rg, jq, pandoc…
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
- `upstream/` is the only package that talks to an upstream (Kano Proxy or a user-defined OpenAI-compatible connection): `client.py` for vision calls and catalogs, `anthropic.py` for the chat agent's Messages API calls — together the only modules that ever decrypt a stored user key — and `kano.py` for the connect flow, which receives a Kano Proxy key in plaintext and hands it straight to encryption.
- `workspace/` is the only package that touches `WORKSPACES_DIR` and the only one that runs git; `sandbox/client.py` is the only module that calls the runner. The runner (`apps/sandbox`) is the only process with the Docker socket.
- `agents/` defines agents and nothing else: no HTTP, no database. Runners and settings consume its definitions ([agents.md](./agents.md)).
- `jobs/queue.py` is the only module that knows the queue is Postgres.
- Web: pages thin, logic in composables; API access through one typed client module.
