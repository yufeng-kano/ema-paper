# Deployment

Single server, docker-compose, Caddy in front. Environments: **local** and **production** only. The step-by-step setup is [self-hosting.md](./self-hosting.md); this page is the reference behind it.

Production host/DNS specifics stay out of tracked files (use `.local/` notes if needed; this repo is public). The README's links to the public site are the one exception: the site's address is what the product is reached at, not a detail of how it is deployed.

## Compose files

Compose `-f` merge semantics surprise people, so the two *full stacks* are standalone: each is
complete and readable on its own and is never merged with the other. The single exception is
`docker-compose.only-container-network.yaml`, which is an overlay **by design** — it is meaningless
on its own and exists precisely to be merged onto the production file.

**Every container is named.** `sightread-pg`, `sightread-api`, `sightread-worker`,
`sightread-web`, `sightread-caddy`, and `sightread-backup` in production — the same names in
both full stacks, so `docker logs -f sightread-worker` and `docker exec -it sightread-api sh`
are one command on a laptop and on the server instead of depending on a generated `-1` suffix
that also moves when the checkout directory is renamed. The cost, stated once: a container
name is unique per host, so the local and production stacks cannot run side by side (bring one
`down` first) and no service can be `--scale`d. Neither is something this stack does — it is
one server, one instance of each service.

### `docker-compose.yaml` — local

- Services: `pg`, `api`, `worker`, `web`, `caddy` (plain HTTP on `8080` via `deploy/caddy/Caddyfile.local`, mirroring the production routing so the built web image reaches the API on one origin), and `sandbox` — the runner that owns the Docker socket and starts the per-user VMs ([sandbox.md](./sandbox.md)). Locally `SANDBOX_RUNTIME=runc` works without KVM and gives no VM isolation; production refuses it. Direct ports stay exposed too: web `3000`, api `8000`, pg `${PG_PORT:-5432}` (all localhost only). **Use http://localhost:8080 for the full experience.**
- `env_file: .env` (copy from `.env.example`); `APP_ENV=local`, `AUTH_DEV_MODE=true` works here so the stack is demoable without Google credentials. Set `APP_URL`/`WEB_URL` to `http://localhost:8080` so OAuth discovery documents point at the joined origin.
- Volumes: `pgdata`, `uploads` (shared by api + worker), `figures`, and the bind mount `${WORKSPACES_DIR}` (shared by api, worker and sandbox — a bind mount rather than a named volume because the runner passes the same host path to `docker run`).
- Day-to-day dev still runs `uv run uvicorn` / `pnpm dev` natively for hot reload; the compose file is for integration runs.

### `docker-compose.production.yaml` — used as `docker compose -f docker-compose.production.yaml up -d`

- Adds `caddy` (ports 80/443, automatic TLS via ACME); `api`/`web` not published on the host, reachable only on the compose network.
- `APP_ENV=production` (dev login hard-disabled), `AUTH_DEV_MODE` absent.
- Volumes: `pgdata`, `uploads`, `figures` (stored figure crops — durable result data, [parsing.md](./parsing.md) § Figure crops), `caddy_data` (certs), `caddy_config`, and the `${WORKSPACES_DIR}` bind mount (every user's files and revisions — primary data, [workspace.md](./workspace.md)).
- The `sandbox` service needs `/var/run/docker.sock` and a host with `/dev/kvm` and the Kata runtime installed and registered with Docker as `kata` ([sandbox.md](./sandbox.md) § Operations). Build the sandbox image once (`docker build -t sightread-sandbox deploy/sandbox-image`) and name it in `SANDBOX_IMAGE`.
- Backups: a `backup` service (the `postgres:16` image) loops once a day into `./backups` (mounted, gitignored), keeping the newest seven stamps, each a directory `backups/<stamp>/` holding `sightread.dump` (`pg_dump --format=custom`), `figures.tar.gz` (the `figures` volume, mounted read-only) and `workspaces.tar.gz` — a restored database with the crops gone would keep every figure's metadata while losing its image, and the sources that produced them are long deleted. The three are **published together, or not at all**: written into a staging directory and published by one rename of that directory, so a stamp that exists under `backups/` is complete by construction, whatever stopped the host in between, and rotation deletes stamps whole. The workspaces archive rides the same stamp, and the three are captured under the workspace backup lock (`{WORKSPACES_DIR}/.repos/backup.lock`, held exclusively for the whole pass; every workspace write takes it shared), so the dump and the tree agree ([workspace.md](./workspace.md) § Backups). A pass that finds a `.repos/<user>.writing` marker — a shell command the worker could not confirm over, whose VM may still be writing — waits up to `BACKUP_WRITING_WAIT_SECONDS` (3600) for it to clear, then archives regardless and lists the markers in the stamp's `unconfirmed-writes.txt`, so a restore knows which trees may hold a file mid-write (their revision history is intact either way). The schedule and the rotation are a visible shell loop in the compose file on purpose. Restore is all three of one stamp: `pg_restore -d sightread backups/<stamp>/sightread.dump`, the figures archive untarred back into the volume (`tar -xzf figures.tar.gz -C /data` inside a container mounting it), and the workspaces archive untarred into `WORKSPACES_HOST_DIR` (`tar -xzf workspaces.tar.gz -C <parent of WORKSPACES_HOST_DIR>`, which carries every user's edits, notes, originals and revision history, and since 2026-10-25 every file of any kind the reader keeps, for which the archive is the only other copy there is ([workspace.md](./workspace.md) § Files) — rows without it come back as fresh parse copies, and a file's row as nothing); test all three before launch. Leaving managed platforms means backups are ours now. Nothing is taken out of a stamp once it is published: what a reader deletes — an item from the Trash, a whole account ([auth.md](./auth.md) § Deleting the account) — leaves the backups as the stamps holding it rotate out, about a week later — a stamp goes when its seventh successor is published, and successive stamps are a day plus a pass apart, a failed pass adding a day — which is what the Privacy page says.
- `POSTGRES_PASSWORD` has no default here: the stack refuses to start without one. Migrations run from the `api` container's start command (`alembic upgrade head`), and the worker gates on the api's **healthcheck** (`/healthz`), not merely on its container starting — healthy means migrations finished and the server is up, so a worker can never claim a queued job against a half-migrated schema, bill the user's key, and then fail on a missing column.

### `docker-compose.only-container-network.yaml` — production behind a front proxy

For hosts where something else already owns `:443` — typically an SNI router fronting several
unrelated stacks. Merged onto the production file:

```bash
docker compose -f docker-compose.production.yaml -f docker-compose.only-container-network.yaml up -d
```

- Drops Caddy's published ports (`ports: !reset []`) so the whole stack claims **no host port at
  all**; Caddy still listens on 443 *inside* the container. `!reset` on `ports` needs a recent
  Compose (verified on 2.40.1).
- Joins Caddy to the external network `proxy` with the alias `sightread-caddy` — that alias is how
  the front router reaches it, and it is prefixed because `caddy` alone would collide with every
  other stack on a shared network. Create the network once: `docker network create proxy`. `api`,
  `worker` and `pg` stay on the default network and remain unreachable from it.
- **Caddy still terminates TLS and still owns its certificate.** The front router must do SNI
  passthrough (raw TCP, no TLS termination). Terminating TLS upstream is not supported here: it
  breaks this stack's ACME, and the certificate is deliberately the service's own responsibility.
- Port 80 is not published either, so the HTTP-01 challenge cannot reach Caddy. The Caddyfile
  disables it and validates with TLS-ALPN-01 (RFC 8737) over the 443 that is passed through, so the
  front router only has to forward 443.
- The overlay carries no host-specific values — the domain still comes from `DOMAIN` in `.env`.
  It is tracked on purpose: anyone fronting this stack with their own proxy needs it.

## Caddy routing (`deploy/caddy/Caddyfile`)

```text
<domain> {
  tls { issuer acme { disable_http_challenge } }   # validate over 443 (TLS-ALPN-01), never :80
  header { defer; Strict-Transport-Security …; Content-Security-Policy …; … }   # § Security headers
  @upload path /api/library/documents    # the web upload: a file of any kind
  request_body @upload { max_size 2GB }  # keep in sync with FILE_MAX_BYTES
  @rest not path /api/library/documents
  request_body @rest { max_size 128MB }  # keep in sync with UPLOAD_MAX_BYTES
  @api path /healthz /v1/* /api/* /oauth/* /mcp /mcp/* /.well-known/*
  handle @api { reverse_proxy api:8000 }
  handle      { reverse_proxy web:3000 }
}
```

The two body caps are matched routes, not one site-wide line, because a Caddy `request_body` limit wraps the body for every request it matches and two limits on one request leave the smaller in force: the web upload (`POST /api/library/documents`, [api.md](./api.md)) may carry a file of any kind up to `FILE_MAX_BYTES` ([workspace.md](./workspace.md) § Files), and every other route keeps `UPLOAD_MAX_BYTES`; the app enforces the finer caps itself (a PDF under `UPLOAD_MAX_BYTES`, markdown under `CONTENT_MAX_BYTES`). `/healthz` is listed first so an external monitor hits the API rather than the Nuxt 404 page. Uploads and SSE go straight through Caddy to FastAPI — never through the Nuxt/Node server. Disable proxy buffering for SSE routes; long-lived connections need generous idle timeouts.

As built: the domain comes from `{$DOMAIN}` and the ACME contact from `{$ACME_EMAIL}` (both in `.env`, passed to the caddy service), the API proxy sets `flush_interval -1` (no response buffering, for SSE and MCP streams) and 30-minute read/write timeouts.

Certificates are always this stack's own: the site block carries an explicit `tls { issuer acme { disable_http_challenge } }`, so validation is TLS-ALPN-01 over 443 and never HTTP-01 over 80. That holds for both deployments — standalone, where Caddy publishes 80 and 443 itself, and behind a front proxy, where only a passed-through 443 reaches it. Port 80 is therefore only ever an HTTP→HTTPS convenience, never a dependency.

## Security headers

Caddy sends the same security headers on every answer of the site, the API's and the web app's alike, so they are defined in one place: the `header` block of `deploy/caddy/Caddyfile`. The block is deferred, so each value replaces whatever the upstream sent (h3's defaults on a Nuxt error, for one) and every header goes out once. It also drops `Server` (uvicorn) and `X-Powered-By` (Nuxt).

| Header | Value | Why |
|--------|-------|-----|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | The site is HTTPS only. No `preload`: that commits the parent domain and is slow to undo |
| `Content-Security-Policy` | below | |
| `X-Frame-Options` | `DENY` | No page is meant to be framed, the connector consent screen least of all (clickjacking). Older browsers' form of `frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` | Every file the API serves goes out with its declared type, never sniffed into script or HTML |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Another site sees the origin only, never a path or query of this one |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` | Features the app never uses. The clipboard stays allowed: the viewer and chat copy |

The policy, one line in the Caddyfile:

```text
default-src 'self'; script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline'; font-src 'self' data:;
img-src 'self' data: blob: https://*.googleusercontent.com;
connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'
```

- `script-src 'unsafe-inline'`: Nuxt writes inline scripts into every page — the import map and `window.__NUXT__.config` — and their content changes with each build, so a hash list here would break on the next build. Scripts still load from this origin only. A strict policy (nonces from the Nitro renderer, hashes for the prerendered pages) is a separate step.
- `style-src 'unsafe-inline'`: KaTeX sets math with `style` attributes, Nuxt inlines component styles, and the OAuth consent page carries its own `<style>`.
- `font-src 'self' data:`: the app sets type in the system's own face ([web.md](./web.md) § Design system — light glass), so no font host is listed. KaTeX's fonts are bundled.
- `img-src`: `https://*.googleusercontent.com` is the account's Google profile picture (Settings → Account), which loads from Google's own host. `blob:` is the composer's preview of a pasted or attached image (an object URL), `data:` inline images. A remote image in a document is not rendered (it stays text), so no other host is listed.
- `connect-src 'self'`: the app calls only its own origin — `/api/*`, `/v1/*` and their SSE streams.
- No `form-action`: the consent screen's form posts to `/oauth/authorize`, which redirects to the connector's `redirect_uri` — another origin, or `http://localhost` for a CLI client — and browsers hold that redirect to `form-action` too. Checkout and the portal are full-page navigations to Kano Pay, which no directive restricts.
- No `Cross-Origin-Opener-Policy` or `Cross-Origin-Resource-Policy`: a connector opens the consent screen in a popup and keeps its handle, and a connector's page loads this site's icon from its own origin.

One route keeps a policy of its own: `GET /api/library/documents/{id}/file`, which serves the reader's files inline ([api.md](./api.md)), answers with `Content-Security-Policy: sandbox`, so that whatever a file is — an HTML or SVG file is sent as an attachment, but the header stands on every answer — it runs nothing with the session. The site's policy allows inline scripts and would replace it, so the Caddyfile's policy line carries a matcher that leaves that path out (`@notfile not path /api/library/documents/*/file`); the other headers of the block stay on every answer, the file route's included.

`deploy/caddy/Caddyfile.local` sends the same headers except HSTS, which a browser ignores over plain HTTP, so a policy violation shows up in a local integration run before production. Change both together.

## Environment variables (`.env.example` / `.env.production.example` are the authoritative lists)

| Var | Notes |
|-----|-------|
| `APP_ENV` | `local` / `production` |
| `APP_URL` | public origin, used in OAuth metadata + OIDC redirect |
| `WEB_URL` | the web app's public origin: where sign-in returns the visitor, and — passed by `docker-compose.production.yaml` to the web image's build — the origin of the public pages' canonical, `hreflang` and `og:url` links, `robots.txt`'s `Sitemap` line and the sitemap's URLs ([web.md](./web.md) § Search engines). The pages are prerendered, so a changed value needs the web image rebuilt |
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `SECRET_KEY` | sessions and the signed cookies; generate long random |
| `KANO_PROXY_BASE_URL` | Kano Proxy's origin (e.g. `https://kano-proxy.example.com`, no path). Vision calls and the catalog go to `{origin}/openai/v1`, the chat and check agents to `{origin}/anthropic/v1`. Empty = no upstream: parses and turns are refused with 503 |
| `KANO_PROXY_API_KEY` | the operator's own Kano Proxy key, presented on every upstream call ([auth.md](./auth.md) § 3). Lives only in `.env`. Empty = no upstream, as above |
| `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL`, `MODEL_PRICES_JSON` | the models the deployment offers, with their list prices; the chat agent offers the live catalog's models the table prices, in its order ([billing.md](./billing.md) § Models). The API and the worker refuse to start with an offered model unpriced; parses are refused with 503 while `PARSE_MODEL` is empty |
| `CREDIT_USD`, `PARSE_CREDITS_PER_PAGE`, `CHAT_MIN_CREDITS`, `FREE_CREDITS_PER_PERIOD`, `FREE_STORAGE_BYTES`, `FREE_MAX_JOBS` | credits and the Free plan ([billing.md](./billing.md) § Credits, § Plans) |
| `KANO_PAY_URL`, `KANO_PAY_APP_ID`, `KANO_PAY_API_KEY`, `KANO_PAY_WEBHOOK_SECRET`, `KANO_PAY_PLANS_JSON`, `BILLING_REFRESH_SECONDS` | the Kano Pay registration and the paid plans ([billing.md](./billing.md) § Kano Pay). The key and the secret live only in `.env`; all four unset = every user on Free |
| `SUPPORT_EMAIL`, `OPERATOR_NAME` | the address the public Terms, Privacy and Refund pages name for questions and refund requests (an account is deleted by its reader, [auth.md](./auth.md) § Deleting the account), and the name of who operates the service; both served by `GET /api/site`, either empty = the pages print no such line ([web.md](./web.md) § Pages). Dodo's site review needs the address |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OIDC |
| `AUTH_DEV_MODE` | local only; absent from `.env.production.example` |
| `UPLOAD_MAX_BYTES`, `PAGE_CAP`, `MAX_JOBS_PER_USER`, `UPLOAD_RETRY_WINDOW_SECONDS`, `VISION_CONCURRENCY_PER_JOB`, `VISION_MAX_OUTPUT_TOKENS`, `RENDER_WORKERS` | defaults in [api.md](./api.md) / [jobs.md](./jobs.md) |
| `FILE_MAX_BYTES` | the most a file of any kind may be when uploaded to the library (2000000000, 2 GB); enforced in the app and, on that one route, in Caddy (§ Caddy routing, [workspace.md](./workspace.md) § Files). `GET /api/me` passes it to the web app as `limits.file_max_bytes` |
| `REVISION_MAX_FILE_BYTES` | the largest file a workspace revision records (1048576, 1 MiB); a larger one, or a binary one, is kept in the tree and left out of the history ([workspace.md](./workspace.md) § Revisions) |
| `UPLOAD_DIR` | `/data/uploads` in containers |
| `WORKSPACES_HOST_DIR` / `WORKSPACES_DIR` | the per-user workspaces: the host directory compose bind-mounts, and where the containers see it (`/data/workspaces`). The runner is given the host path (`SANDBOX_WORKSPACES_DIR`) because it hands it to `docker run` ([workspace.md](./workspace.md), [sandbox.md](./sandbox.md)) |
| `WORKSPACE_QUOTA_BYTES` | the ceiling every plan's storage, Free's included, must fit under (default 10 GiB); the UI shows and the API enforces the user's plan's cap ([billing.md](./billing.md) § Plans) |
| `SANDBOX_URL` / `SANDBOX_TOKEN` | the runner's address on the compose network and the shared token api and worker present to it; empty `SANDBOX_URL` = no sandbox, the chat agent runs without `bash` |
| `SANDBOX_PORT` | the port the runner listens on (8100). The local compose file derives api's and worker's `SANDBOX_URL` from it when `.env` leaves `SANDBOX_URL` unset (an empty `SANDBOX_URL=` in `.env` still disables the sandbox: compose takes the derived value for an unset variable only, never for an empty one), and the runner's health check in both compose files probes it from the container's own environment, so no fixed port is wired anywhere; in production `SANDBOX_URL` in `.env` must name the same port |
| `SANDBOX_RUNTIME`, `SANDBOX_VM_RUNTIMES`, `SANDBOX_IMAGE`, `SANDBOX_NETWORK`, `SANDBOX_INDEX_URL`, `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS`, `SANDBOX_TMP_SIZE`, `SANDBOX_IDLE_SECONDS`, `SANDBOX_DOCKER_TIMEOUT_SECONDS`, `SANDBOX_OUTPUT_MAX_BYTES` | the runner's own settings ([sandbox.md](./sandbox.md)); `SANDBOX_NETWORK` is read by the worker as well, so the chat agent's prompt says whether `uv add` can reach an index ([chat.md](./chat.md) § System prompt) |
| `MAX_CHAT_TURNS_PER_USER`, `CHAT_MAX_OUTPUT_TOKENS`, `CHAT_MAX_TOOL_CALLS_PER_TURN`, `CHAT_OVERDRAFT_MAX_TOOL_CALLS`, `CHAT_TURN_TIMEOUT_SECONDS`, `CHAT_STREAM_TIMEOUT_SECONDS`, `CHAT_WRITER_GRACE_SECONDS`, `REVERT_MAX_BYTES`, `REVERT_MAX_PATHS`, `WORKSPACE_LOG_MAX_BYTES`, `WORKSPACE_LOG_FILES_MAX`, `CHAT_TOOL_RESULT_MAX_CHARS`, `CHAT_IMAGE_ROUND_MAX_BYTES`, `CHAT_IMAGE_TURN_MAX_BYTES`, `CHAT_PAGE_IMAGE_MAX_PIXELS`, `CHAT_HISTORY_MAX_BYTES`, `CHAT_MESSAGE_MAX_CHARS`, `CHAT_SELECTION_MAX_CHARS` | the chat agent's limits ([chat.md](./chat.md)) |
| `CHAT_ATTACHMENT_MAX_BYTES`, `CHAT_ATTACHMENTS_PER_MESSAGE`, `CHAT_ATTACHMENT_TEXT_MAX_CHARS`, `CHAT_ATTACHMENT_IMAGE_LONG_EDGE`, `CHAT_ATTACHMENTS_PENDING_MAX`, `CHAT_ATTACHMENT_CACHE_SECONDS`, `CHAT_ATTACHMENT_PDF_TIMEOUT_SECONDS`, `CHAT_SKILLS_MAX`, `CHAT_SUBAGENTS_MAX`, `CHAT_SUBAGENT_MAX_TOOL_CALLS`, `CHAT_INSTRUCTIONS_MAX_CHARS`, `CHAT_MEMORY_MAX_CHARS`, `CHAT_MEMORIES_MAX`, `CHAT_MEMORY_BLOCK_MAX_CHARS`, `CHAT_SECTIONS_PER_MESSAGE`, `CHAT_SECTION_HEADING_MAX_CHARS` | the composer's attachments and sections, a user's own skills and subagents, the chat agent's instructions and memories (defaults in [api.md](./api.md); [chat.md](./chat.md) § What a user message carries, § Skills, § Subagents, § Instructions, § Memory) |
| `UPLOAD_MAX_AGE_SECONDS` | how old an upload-directory entry may grow before the worker's sweeper takes it, an unsent chat attachment's included (86400) ([jobs.md](./jobs.md) § Retention) |
| `TRASH_RETENTION_DAYS` | how many days an item stays in the Trash before the worker's sweep deletes it for good (30); must be ≥ 0, and 0 = the Trash is never emptied automatically ([workspace.md](./workspace.md) § The Trash). `GET /api/me` passes it to the web app as `limits.trash_retention_days`, for the Trash tab's line |
| `PARSE_RETENTION_DAYS` | how many days a finished parse no file names is kept before the sweep deletes it with its result, crops and workspace copy (30); must be ≥ 0, and 0 = finished parses are kept forever ([jobs.md](./jobs.md) § Retention) |
| `WORKSPACE_GIT_TIMEOUT_SECONDS` | how long any one git call the host makes may take (60) ([workspace.md](./workspace.md) § Revisions); the API image installs the `git` CLI beside Poppler for these calls |
| `WORKSPACE_GIT_PACK_LOOSE_OBJECTS` | how many loose objects a workspace's git history may hold before the worker's sweep packs it (100) ([workspace.md](./workspace.md) § Revisions) |
| `SANDBOX_CONTROL_TIMEOUT_SECONDS` | how long api and worker give the runner's stop, reset, restart and status calls (180); must cover the runner's `SANDBOX_DOCKER_TIMEOUT_SECONDS` plus its inspection and a reset's cache deletion ([sandbox.md](./sandbox.md)) |
| `SANDBOX_START_TIMEOUT_SECONDS`, `SANDBOX_EXEC_GRACE_SECONDS` | what a shell command may take beyond its own timeout before api and worker give up on it: the VM's cold start on a stopped sandbox (180; the runner gives `docker run` the same) and the grace for the reply (60) |
| `PG_PORT` | local compose only: host port for `pg` (default 5432); change it when that port is taken |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | the `pg` container's credentials; the password is required in production |
| `DOMAIN` / `ACME_EMAIL` | production only: the host Caddy serves, and the Let's Encrypt contact address |

Two standalone example files, same reasoning as the compose files: `.env.example` is local,
`.env.production.example` is production, and neither is a diff against the other. Deploying means
copying the production one and filling it in — never carrying the local `.env` to a server.
`AUTH_DEV_MODE` deliberately does not appear in the production example at all.

Secrets live in `.env` (gitignored) on the server; tracked files carry placeholders only. Never print secret values — only whether one exists.

Google OAuth client (production): the authorized redirect URI is `https://<domain>/api/auth/callback`, and the authorized origin is `https://<domain>`. Nothing else in the app needs registering with Google — the connector flow uses this app's own authorization server.

Kano Proxy (production): the operator's own project API key goes into `KANO_PROXY_API_KEY`; no app registration on Kano Proxy is needed any more (the connect flow is gone, [auth.md](./auth.md) § 6). The compose files pass `.env` through whole (`env_file`), so nothing else changes there.

## Release flow (initial)

`git pull && docker compose -f docker-compose.production.yaml up -d --build` on the server (append
`-f docker-compose.only-container-network.yaml` on hosts with a front proxy — and keep appending it,
since leaving it off republishes 80/443 and collides with whatever owns them). Tagged releases/CI can come later; keep root `package.json` version bumped per SemVer when tagging starts.

`up -d` leaves an unchanged `caddy` container running, and the Caddyfile is bind-mounted as a single file: `git pull` writes it as a new file the running container never sees, so `caddy reload` would read the old one. A Caddyfile change therefore takes `docker compose … restart caddy` (same `-f` list), which mounts the new file. The restart drops open connections, SSE streams included.
