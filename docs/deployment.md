# Deployment

Single server, docker-compose, Caddy in front. Environments: **local** and **production** only. Production host/DNS specifics stay out of tracked files (use `.local/` notes if needed; this repo is public).

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
- Backups: a `backup` service (the `postgres:16` image) loops once a day into `./backups` (mounted, gitignored), keeping the newest seven of each: `pg_dump --format=custom` for the database, and a `tar.gz` of the `figures` volume (mounted read-only) — a restored database with the crops gone would keep every figure's metadata while losing its image, and the sources that produced them are long deleted. The two are **published as a same-stamp pair, or not at all**: both are written as temporaries and moved into place only when both succeeded, so the newest dump always has its matching figures archive. The workspaces archive rides the same stamp, and the three are captured under the workspace backup lock (`{WORKSPACES_DIR}/.repos/backup.lock`, held exclusively for the whole pass; every workspace write takes it shared), so the dump and the tree agree ([workspace.md](./workspace.md) § Backups). The schedule and the rotation are a visible shell loop in the compose file on purpose. Restore is `pg_restore -d sightread <dump>` plus untarring the same-stamp figures archive back into the volume (`tar -xzf figures-<stamp>.tar.gz -C /data` inside a container mounting it); test both before launch. Leaving managed platforms means backups are ours now.
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
  request_body { max_size 128MB }        # keep in sync with UPLOAD_MAX_BYTES
  @api path /healthz /v1/* /api/* /oauth/* /mcp /mcp/* /.well-known/*
  handle @api { reverse_proxy api:8000 }
  handle      { reverse_proxy web:3000 }
}
```

`/healthz` is listed first so an external monitor hits the API rather than the Nuxt 404 page. Uploads and SSE go straight through Caddy to FastAPI — never through the Nuxt/Node server. Disable proxy buffering for SSE routes; long-lived connections need generous idle timeouts.

As built: the domain comes from `{$DOMAIN}` and the ACME contact from `{$ACME_EMAIL}` (both in `.env`, passed to the caddy service), the API proxy sets `flush_interval -1` (no response buffering, for SSE and MCP streams) and 30-minute read/write timeouts.

Certificates are always this stack's own: the site block carries an explicit `tls { issuer acme { disable_http_challenge } }`, so validation is TLS-ALPN-01 over 443 and never HTTP-01 over 80. That holds for both deployments — standalone, where Caddy publishes 80 and 443 itself, and behind a front proxy, where only a passed-through 443 reaches it. Port 80 is therefore only ever an HTTP→HTTPS convenience, never a dependency.

## Environment variables (`.env.example` / `.env.production.example` are the authoritative lists)

| Var | Notes |
|-----|-------|
| `APP_ENV` | `local` / `production` |
| `APP_URL` | public origin, used in OAuth metadata + OIDC redirect |
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `SECRET_KEY` | sessions + HKDF root for upstream-key encryption (Kano Proxy keys, connection keys, connect verifiers); generate long random |
| `KANO_PROXY_BASE_URL` | Kano Proxy's origin (e.g. `https://kano-proxy.example.com`, no path). Vision calls go to `{origin}/openai/v1`, consent to `{origin}/connect/authorize`, the token exchange to `{origin}/api/connect/token` ([auth.md](./auth.md) § 6). Empty = Kano Proxy unavailable |
| `KANO_PROXY_CLIENT_ID` / `KANO_PROXY_CLIENT_SECRET` | sightread's client registration on Kano Proxy. The redirect URI registered there is `<APP_URL>/api/kano/callback`. The secret lives only in `.env` |
| `KANO_CONNECT_TTL_SECONDS` | lifetime of a pending connect state (default 600) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OIDC |
| `AUTH_DEV_MODE` | local only; absent from `.env.production.example` |
| `UPLOAD_MAX_BYTES`, `PAGE_CAP`, `MAX_JOBS_PER_USER`, `VISION_CONCURRENCY_PER_JOB`, `RENDER_WORKERS` | defaults in [api.md](./api.md) / [jobs.md](./jobs.md) |
| `UPLOAD_DIR` | `/data/uploads` in containers |
| `WORKSPACES_HOST_DIR` / `WORKSPACES_DIR` | the per-user workspaces: the host directory compose bind-mounts, and where the containers see it (`/data/workspaces`). The runner is given the host path (`SANDBOX_WORKSPACES_DIR`) because it hands it to `docker run` ([workspace.md](./workspace.md), [sandbox.md](./sandbox.md)) |
| `WORKSPACE_QUOTA_BYTES` | per-user workspace size the UI shows and the API refuses past (default 10 GiB) |
| `SANDBOX_URL` / `SANDBOX_TOKEN` | the runner's address on the compose network and the shared token api and worker present to it; empty `SANDBOX_URL` = no sandbox, the chat agent runs without `bash` |
| `SANDBOX_RUNTIME`, `SANDBOX_VM_RUNTIMES`, `SANDBOX_IMAGE`, `SANDBOX_NETWORK`, `SANDBOX_INDEX_URL`, `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS`, `SANDBOX_TMP_SIZE`, `SANDBOX_IDLE_SECONDS`, `SANDBOX_DOCKER_TIMEOUT_SECONDS`, `SANDBOX_OUTPUT_MAX_BYTES` | the runner's own settings ([sandbox.md](./sandbox.md)) |
| `MAX_CHAT_TURNS_PER_USER`, `CHAT_MAX_OUTPUT_TOKENS`, `CHAT_MAX_TOOL_CALLS_PER_TURN`, `CHAT_TURN_TIMEOUT_SECONDS`, `CHAT_STREAM_TIMEOUT_SECONDS`, `CHAT_WRITER_GRACE_SECONDS`, `REVERT_MAX_BYTES`, `REVERT_MAX_PATHS`, `CHAT_TOOL_RESULT_MAX_CHARS`, `CHAT_IMAGE_ROUND_MAX_BYTES`, `CHAT_IMAGE_TURN_MAX_BYTES`, `CHAT_MESSAGE_MAX_CHARS`, `CHAT_SELECTION_MAX_CHARS` | the chat agent's limits ([chat.md](./chat.md)) |
| `WORKSPACE_GIT_TIMEOUT_SECONDS` | how long any one git call the host makes may take (60) ([workspace.md](./workspace.md) § Revisions) |
| `SANDBOX_CONTROL_TIMEOUT_SECONDS` | how long api and worker give the runner's stop, reset, restart and status calls (180); must cover the runner's `SANDBOX_DOCKER_TIMEOUT_SECONDS` plus its inspection and a reset's cache deletion ([sandbox.md](./sandbox.md)) |
| `PG_PORT` | local compose only: host port for `pg` (default 5432); change it when that port is taken |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | the `pg` container's credentials; the password is required in production |
| `DOMAIN` / `ACME_EMAIL` | production only: the host Caddy serves, and the Let's Encrypt contact address |

Two standalone example files, same reasoning as the compose files: `.env.example` is local,
`.env.production.example` is production, and neither is a diff against the other. Deploying means
copying the production one and filling it in — never carrying the local `.env` to a server.
`AUTH_DEV_MODE` deliberately does not appear in the production example at all.

Secrets live in `.env` (gitignored) on the server; tracked files carry placeholders only. Never print secret values — only whether one exists.

Google OAuth client (production): the authorized redirect URI is `https://<domain>/api/auth/callback`, and the authorized origin is `https://<domain>`. Nothing else in the app needs registering with Google — the connector flow uses this app's own authorization server.

Kano Proxy client (production): sightread is registered on Kano Proxy as an app with the exact redirect URI `https://<domain>/api/kano/callback`; the client id and secret it was issued go into `KANO_PROXY_CLIENT_ID` / `KANO_PROXY_CLIENT_SECRET`. The compose files pass `.env` through whole (`env_file`), so nothing else changes there.

## Release flow (initial)

`git pull && docker compose -f docker-compose.production.yaml up -d --build` on the server (append
`-f docker-compose.only-container-network.yaml` on hosts with a front proxy — and keep appending it,
since leaving it off republishes 80/443 and collides with whatever owns them). Tagged releases/CI can come later; keep root `package.json` version bumped per SemVer when tagging starts.
