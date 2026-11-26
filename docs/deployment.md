# Deployment

Single server, docker-compose, Caddy in front. Environments: **local** and **production** only. The step-by-step setup is [self-hosting.md](./self-hosting.md); this page is the reference behind it.

Production host/DNS specifics stay out of tracked files (use `.local/` notes if needed; this repo is public). The README's links to the public site are the one exception: the site's address is what the product is reached at, not a detail of how it is deployed.

## Compose files

The two *full stacks* are standalone: each is complete and readable on its own and is never merged with the other, because Compose `-f` merge semantics surprise people. The single exception is `docker-compose.only-container-network.yaml`, an overlay **by design**: it is meaningless on its own and exists precisely to be merged onto the production file.

**Every container is named**, with the same names in both full stacks:

- `ema-paper-pg`, `ema-paper-api`, `ema-paper-worker`, `ema-paper-web`, `ema-paper-caddy`, `ema-paper-sandbox`, `ema-paper-sandbox-egress`, and `ema-paper-backup` in production.
- So `docker logs -f ema-paper-worker` and `docker exec -it ema-paper-api sh` are one command on a laptop and on the server, instead of depending on a generated `-1` suffix that also moves when the checkout directory is renamed.
- The cost, stated once: a container name is unique per host, so the local and production stacks cannot run side by side (bring one `down` first), and no service can be `--scale`d. Neither is something this stack does: it is one server, one instance of each service.

**The project name is pinned too** (`name: ema-paper` in both full stacks). Compose would otherwise take it from the checkout directory, and the named volumes (`ema-paper_pgdata`, `ema-paper_figures`, …) and the default network are prefixed with it: a renamed checkout would come up on fresh, empty volumes beside its own data. An install whose containers are named `sightread-*` ran on files without the pin and moves its data first ([self-hosting.md](./self-hosting.md) § Keep it running).

### `docker-compose.yaml` — local

- Services:
  - `pg`, `api`, `worker`, `web`.
  - `caddy`: plain HTTP on `8080` via `deploy/caddy/Caddyfile.local`, mirroring the production routing so the built web image reaches the API on one origin.
  - `sandbox`: the runner that owns the Docker socket and starts the per-user VMs ([sandbox.md](./sandbox.md)).
  - `sandbox-egress`: the proxy that is the VMs' one way out once `SANDBOX_NETWORK` names its network ([sandbox.md](./sandbox.md) § Network).
- Locally `SANDBOX_RUNTIME=runc` works without KVM and gives no VM isolation; production refuses it.
- Direct ports stay exposed too, all localhost only: web `3000`, api `8000`, pg `${PG_PORT:-5432}`. **Use http://localhost:8080 for the full experience.**
- `env_file: .env` (copy from `.env.example`); `APP_ENV=local`. `AUTH_DEV_MODE=true` works here, so the stack is demoable without Google credentials.
- Set `APP_URL`/`WEB_URL` to `http://localhost:8080` so OAuth discovery documents point at the joined origin.
- Volumes:
  - `pgdata`.
  - `uploads`, shared by api + worker.
  - `figures`.
  - `sandbox_egress`: the list of sites the worker writes and the egress proxy reads.
  - The bind mount `${WORKSPACES_HOST_DIR:-./workspaces}`, shared by api, worker and sandbox. It is a bind mount rather than a named volume because the runner passes the same host path to `docker run`.
- With `WORKSPACES_HOST_DIR` left empty, run compose from the checkout's root: the runner is handed `${PWD}/workspaces`, the invoking shell's directory, while compose mounts `./workspaces` of the project. Otherwise set an absolute `WORKSPACES_HOST_DIR`.
- Day-to-day dev runs `uv run uvicorn` / `pnpm dev` natively for hot reload; the compose file is for integration runs.

### `docker-compose.production.yaml` — used as `docker compose -f docker-compose.production.yaml up -d`

- `caddy` publishes ports 80 and 443 (443 over UDP too), with automatic TLS via ACME (locally 8080, plain HTTP).
- `api`/`web` are not published on the host; they are reachable only on the compose network.
- `APP_ENV=production` (dev login hard-disabled), `AUTH_DEV_MODE` absent.
- `api` gets `FORWARDED_ALLOW_IPS: "*"`, so uvicorn takes the client's address from the `X-Forwarded-For` Caddy sets; the operator console's audit log records it ([admin.md](./admin.md) § The audit log).
  - Trusting every peer is safe only because nothing but the compose network reaches `api`.
  - Caddy replaces any `X-Forwarded-For` a client sent with the address that connected to it.
  - The scheme then comes from `X-Forwarded-Proto` too (`https`). This changes nothing the API answers: every absolute URL it builds comes from `APP_URL`.
- Volumes:
  - `pgdata`, `uploads`.
  - `figures`: stored figure crops, durable result data ([parsing.md](./parsing.md) § Figure crops).
  - `caddy_data` (certs), `caddy_config`.
  - `sandbox_egress`.
  - The `${WORKSPACES_HOST_DIR}` bind mount: every user's files and revisions, primary data ([workspace.md](./workspace.md)).
- The `sandbox` service needs `/var/run/docker.sock` and a host with `/dev/kvm` and the Kata runtime installed and registered with Docker as `kata` ([sandbox.md](./sandbox.md) § Operations).
  - Build the sandbox image (`docker build -t ema-paper-sandbox deploy/sandbox-image`) and name it in `SANDBOX_IMAGE`.
  - Build it again when `deploy/sandbox-image/` changes. A running VM keeps its image until it stops, and the next start takes the new one, since the runner removes a stopped VM rather than restarting it.
- The `sandbox-egress` service is the runner's image run as `python -m sightread_sandbox.egress`, as an unprivileged user on a read-only root with every capability dropped.
  - It is on two networks of its own and never `default`:
    - `sandbox`: named `ema-paper-sandbox`, internal, with no IPv4 address of the host's on it and IPv6 turned off on its bridge by a host step ([sandbox.md](./sandbox.md) § Operations); on `SANDBOX_SUBNET`, the proxy at `SANDBOX_EGRESS_ADDRESS`.
    - `egress`: its way out.
  - It has no `env_file`: compose hands it its port and limits alone.
  - It mounts the `sandbox_egress` volume read-only, where the worker writes the list ([sandbox.md](./sandbox.md) § Network).
  - The runner gets `SANDBOX_PROXY_URL` from the same two values.
  - The VMs join the network only once `.env` sets `SANDBOX_NETWORK=ema-paper-sandbox`.
- `POSTGRES_PASSWORD`, `WORKSPACES_HOST_DIR`, `WEB_URL`, `DOMAIN` and `ACME_EMAIL` have no default here: the stack refuses to start without them.
- Migrations run from the api image's start command: `apps/api/Dockerfile`'s `CMD`, `alembic upgrade head` and then `exec uvicorn`. Neither compose file overrides it; the worker's own `command:` replaces it with `python -m sightread.worker`.
- The worker gates on the api's **healthcheck** (`/healthz`), not merely on its container starting. Healthy means migrations finished and the server is up, so a worker can never claim a queued job against a half-migrated schema, bill the user, and then fail on a missing column.

#### Backups

A `backup` service (the `postgres:16` image) loops once a day into `./backups` (mounted, gitignored), keeping the newest seven stamps. The schedule and the rotation are a visible shell loop in the compose file on purpose.

Each stamp is a directory `backups/<stamp>/` holding:

- `sightread.dump`: `pg_dump --format=custom`.
- `figures.tar.gz`: the `figures` volume, mounted read-only. A restored database with the crops gone would keep every figure's metadata while losing its image, and a parse's source is deleted when its job ends unless a library document keeps it as its original ([jobs.md](./jobs.md) § Retention).
- `workspaces.tar.gz`: the workspaces, without each user's `.cache` — the package cache, the default Python environment and the shell's state, which a reset deletes and an install makes again ([workspace.md](./workspace.md) § Backups).

The rules of a pass:

- **Published together, or not at all.** The three are written into a staging directory and published by one rename of that directory. A stamp that exists under `backups/` is complete by construction, whatever stopped the host in between. Rotation deletes stamps whole.
- **One snapshot.** The three are captured under the workspace backup lock (`{WORKSPACES_DIR}/.repos/backup.lock`), held exclusively for the whole pass; every workspace write takes it shared. So the dump and the tree agree ([workspace.md](./workspace.md) § Backups).
- **Unconfirmed writes.** A `.repos/<user>.writing` marker is a shell command the worker could not confirm over, whose VM may still be writing. A pass that finds one waits up to `BACKUP_WRITING_WAIT_SECONDS` (3600) for it to clear, then archives regardless and lists the markers in the stamp's `unconfirmed-writes.txt`. A restore then knows which trees may hold a file mid-write; their revision history is intact either way.

Restore is all three of one stamp:

- The dump, with `worker` and `api` stopped (`docker compose -f … stop worker api`): `docker compose -f … exec -T backup sh -c 'dropdb "$PGDATABASE" && createdb "$PGDATABASE" && pg_restore -d "$PGDATABASE" /backups/<stamp>/sightread.dump'`, the database the service's environment names, its `/backups` being `./backups`.
- The figures archive untarred back into the volume: `tar -xzf figures.tar.gz -C /data` inside a container mounting it.
- The workspaces archive untarred into `WORKSPACES_HOST_DIR`: `tar -xzf workspaces.tar.gz --strip-components=1 -C <WORKSPACES_HOST_DIR>`. This restores into the directory whatever its name, since the archive's entries start with `workspaces/`.

The workspaces archive carries every user's edits, notes, originals and revision history, and every file of any kind the reader keeps, for which the archive is the only other copy there is ([workspace.md](./workspace.md) § Files). Without it, rows come back as fresh parse copies, and a file's row as nothing. Test all three before launch.

Nothing is taken out of a stamp once it is published. What a reader deletes — an item from the Trash, a whole account ([auth.md](./auth.md) § Deleting the account) — leaves the backups as the stamps holding it rotate out, about a week later, which is what the Privacy page says:

- A stamp goes when its seventh successor is published.
- Successive stamps are a day plus a pass apart, a failed pass adding a day.

### `docker-compose.only-container-network.yaml` — production behind a front proxy

For hosts where something else already owns `:443`, typically an SNI router fronting several unrelated stacks. Merged onto the production file:

```bash
docker compose -f docker-compose.production.yaml -f docker-compose.only-container-network.yaml up -d
```

- Drops Caddy's published ports (`ports: !reset []`), so the whole stack claims **no host port at all**. Caddy still listens on 443 *inside* the container. `!reset` on `ports` needs a recent Compose (verified on 2.40.1).
- Joins Caddy to the external network `proxy` with the alias `ema-paper-caddy`.
  - That alias is how the front router reaches it. It is prefixed because `caddy` alone would collide with every other stack on a shared network.
  - Create the network once: `docker network create proxy`.
  - `api`, `worker` and `pg` stay on the default network and remain unreachable from it.
- **Caddy still terminates TLS and still owns its certificate.** The front router must do SNI passthrough (raw TCP, no TLS termination). Terminating TLS upstream is not supported here: it breaks this stack's ACME, and the certificate is deliberately the service's own responsibility.
- Port 80 is not published either, and need not be: Caddy validates its certificate over the passed-through 443 (§ Caddy routing), so the front router only has to forward 443.
- The overlay carries no host-specific values: the domain still comes from `DOMAIN` in `.env`. It is tracked on purpose: anyone fronting this stack with their own proxy needs it.

## Caddy routing (`deploy/caddy/Caddyfile`)

```text
<domain> {
  tls { issuer acme { disable_http_challenge } }   # validate over 443 (TLS-ALPN-01), never :80
  header { defer; Strict-Transport-Security …; X-Content-Type-Options …; … }   # § Security headers
  @notfile not path /api/library/documents/*/file /api/library/documents/*/original   # the file routes carry their own policy
  header @notfile { defer; Content-Security-Policy …; X-Frame-Options DENY }
  @upload path /api/library/documents    # the web upload: a file of any kind
  request_body @upload { max_size 2048MB }  # FILE_MAX_BYTES plus the multipart envelope
  @rest not path /api/library/documents
  request_body @rest { max_size 180MB }  # UPLOAD_MAX_BYTES in base64 plus 64 KiB: /v1/parse's JSON form
  @api path /healthz /v1/* /api/* /oauth/* /mcp /mcp/* /.well-known/*
  handle @api { reverse_proxy api:8000 }
  handle      { reverse_proxy web:3000 }
}
```

The domain comes from `{$DOMAIN}` and the ACME contact from `{$ACME_EMAIL}`, both in `.env` and passed to the caddy service.

The two body caps are matched routes, not one site-wide line, because a Caddy `request_body` limit wraps the body for every request it matches and two limits on one request leave the smaller in force.

- `@upload`: the web upload (`POST /api/library/documents`, [api.md](./api.md)) may carry a file of any kind up to `FILE_MAX_BYTES` ([workspace.md](./workspace.md) § Files).
- `@rest`: every other route is held to the largest body the app accepts on any of them, `POST /v1/parse`'s JSON form: `UPLOAD_MAX_BYTES` in base64 (four bytes per three) plus 64 KiB of envelope, 179,022,508 bytes at the default — hence 180MB (Caddy's `MB` is 10^6 bytes, not 2^20).
- Both caps are literal values in the Caddyfile: when `.env` raises `UPLOAD_MAX_BYTES` or `FILE_MAX_BYTES`, raise the matching `max_size` in `Caddyfile` and `Caddyfile.local` too.
- The app enforces the finer caps itself (a PDF under `UPLOAD_MAX_BYTES`, markdown under `CONTENT_MAX_BYTES`).

Proxying:

- `/healthz` is in the `@api` matcher so an external monitor hits the API rather than the Nuxt 404 page.
- Uploads and SSE go straight through Caddy to FastAPI — never through the Nuxt/Node server.
- The API proxy disables response buffering (`flush_interval -1`), for SSE and MCP streams.
- Long-lived connections need generous idle timeouts: the API proxy sets 30-minute read/write timeouts.

Certificates are always this stack's own: the site block carries an explicit `tls { issuer acme { disable_http_challenge } }`, so validation is TLS-ALPN-01 (RFC 8737) over 443 and never HTTP-01 over 80.

- That holds for both deployments: standalone, where Caddy publishes 80 and 443 itself, and behind a front proxy, where only a passed-through 443 reaches it.
- Port 80 is therefore only ever an HTTP→HTTPS convenience, never a dependency.

## Security headers

Caddy sends the same security headers on every answer of the site, the API's and the web app's alike, so they are defined in one place: the `header` blocks of `deploy/caddy/Caddyfile`.

- The blocks are deferred, so each value replaces whatever the upstream sent (h3's defaults on a Nuxt error, for one) and every header goes out once.
- The site-wide block also drops `Server` (uvicorn) and `X-Powered-By` (Nuxt).

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
connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'
```

- `script-src 'unsafe-inline'`: Nuxt writes inline scripts into every page — the import map and `window.__NUXT__.config` — and their content changes with each build, so a hash list here would break on the next build. Scripts still load from this origin only.
- `style-src 'unsafe-inline'`: KaTeX sets math with `style` attributes, Nuxt inlines component styles, and the OAuth consent page carries its own `<style>`.
- `font-src 'self' data:`: the app sets type in the system's own face ([web.md](./web.md) § Design system — light glass), so no font host is listed. KaTeX's fonts are bundled.
- `img-src`:
  - `https://*.googleusercontent.com` is the account's Google profile picture (Settings → Account), which loads from Google's own host.
  - `blob:` is the composer's preview of a pasted or attached image (an object URL).
  - `data:` is inline images.
  - A remote image in a document is not rendered (it stays text), so no other host is listed.
- `connect-src 'self'`: the app calls only its own origin — `/api/*`, `/v1/*` and their SSE streams.
- `worker-src 'self'`: the PDF viewer's workers are chunks of the build, served from this origin ([pdf-viewer.md](./pdf-viewer.md)). No worker starts from a `blob:` or `data:` URL. It is named, not left to fall back to `script-src`, so the rule for workers is read in one place.
- No `'wasm-unsafe-eval'` in `script-src`: nothing in the app compiles WebAssembly, the PDF viewer included ([pdf-viewer.md](./pdf-viewer.md) § Who does what).
- No `form-action`: the consent screen's form posts to `/oauth/authorize`, which redirects to the connector's `redirect_uri` — another origin, or `http://localhost` for a CLI client — and browsers hold that redirect to `form-action` too. Checkout and the portal are full-page navigations to Kano Pay, which no directive restricts.
- No `Cross-Origin-Opener-Policy` or `Cross-Origin-Resource-Policy`: a connector opens the consent screen in a popup and keeps its handle, and a connector's page loads this site's icon from its own origin.

Two routes keep a policy of their own: `GET /api/library/documents/{id}/file` and `GET /api/library/documents/{id}/original`, which serve the reader's files and a parsed document's original inline ([api.md](./api.md)).

- They answer with `Content-Security-Policy: sandbox` and `X-Frame-Options: DENY`, so that whatever a file is, it runs nothing with the session. An HTML or SVG file is sent as an attachment, but the headers stand on every answer.
- A PDF served inline is the one exception, since a browser's PDF viewer refuses a sandboxed frame: that answer carries `frame-ancestors 'self'` and `SAMEORIGIN`, so the result viewer's Original frame on this origin may show it and no other page may.
- The site's policy allows inline scripts and its frame header forbids every frame, and both would replace the routes', so the Caddyfile sends those two from a block with a matcher that leaves both paths out (`header @notfile`, `@notfile not path /api/library/documents/*/file /api/library/documents/*/original`).
- The other headers stay on every answer, the file routes' included.

`deploy/caddy/Caddyfile.local` sends the same headers except HSTS, which a browser ignores over plain HTTP, so a policy violation shows up in a local integration run before production. Change both together.

## Environment variables (`.env.example` / `.env.production.example` are the authoritative lists)

- Two standalone example files, same reasoning as the compose files: `.env.example` is local, `.env.production.example` is production, and neither is a diff against the other.
- Deploying means copying the production one and filling it in — never carrying the local `.env` to a server.
- `AUTH_DEV_MODE` deliberately does not appear in the production example at all.
- Secrets live in `.env` (gitignored) on the server; tracked files carry placeholders only.
- Never print secret values — only whether one exists.

### The app and sign-in

| Var | Notes |
|-----|-------|
| `APP_ENV` | `local` / `production` |
| `APP_URL` | public origin, used in OAuth metadata + OIDC redirect; a trailing slash is ignored |
| `WEB_URL` | the web app's public origin; a trailing slash is ignored, by the API and by the web build (below) |
| `SECRET_KEY` | signs the short-lived `sr_oidc` cookie (10 minutes): the Google sign-in state (OIDC state and PKCE verifier), the page a sign-in returns to and a connector's consent token; generate a long random value per environment (`openssl rand -hex 32`) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OIDC |
| `AUTH_DEV_MODE` | local only; absent from `.env.production.example` |
| `SUPPORT_EMAIL`, `OPERATOR_NAME` | the address the public Terms, Privacy and Refund pages name, and the name of who operates the service (below) |

- **`WEB_URL`** is:
  - where sign-in returns the visitor: `WEB_URL/<locale>` for one of the web app's locales (`en`, `zh-TW`: `WEB_LOCALES` in `apps/api/src/sightread/config.py`, mirroring `PAGE_LOCALES` in `apps/web/shared/seo.ts`), `WEB_URL` itself for any other value, whose bare root redirects by the browser's language ([auth.md](./auth.md) § 1, [web.md](./web.md) § Search engines);
  - the origin of the public pages' canonical, `hreflang` and `og:url` links, `robots.txt`'s `Sitemap` line and the sitemap's URLs ([web.md](./web.md) § Search engines), passed by `docker-compose.production.yaml` to the web image's build; the pages are prerendered, so a changed value needs the web image rebuilt;
  - the address the chat agent's web tools name in their User-Agent, `EmaPaper-agent/1.0 (+<WEB_URL>)` ([chat.md](./chat.md) § Tools).
- **`SECRET_KEY`**: sessions are random tokens stored hashed (SHA-256, no key), so rotating it signs nobody out and makes nothing unreadable: it only fails a sign-in or a connector authorization in progress.
- **`SUPPORT_EMAIL`, `OPERATOR_NAME`**:
  - The address is the one named for questions, deletion requests and refund requests.
  - An account is deleted by its reader ([auth.md](./auth.md) § Deleting the account).
  - Both are served by `GET /api/site`; either empty = the pages print no such line ([web.md](./web.md) § Pages).
  - Dodo's site review needs the address.

### Upstream and models

| Var | Notes |
|-----|-------|
| `KANO_PROXY_BASE_URL` | Kano Proxy's origin (e.g. `https://kano-proxy.example.com`, no path). Empty = no upstream (below) |
| `KANO_PROXY_API_KEY` | the operator's own Kano Proxy key, presented on every upstream call ([auth.md](./auth.md) § 3). Lives only in `.env`. Empty = no upstream, as above |
| `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL`, `MODEL_PRICES_JSON` | the models the deployment offers, with their list prices ([billing.md](./billing.md) § Models); overridable in the database |
| `SUMMARY_MODEL`, `SUMMARY_MAX_OUTPUT_TOKENS`, `SUMMARY_TIMEOUT_SECONDS`, `TITLE_MESSAGE_MAX_CHARS` | the summary model and the limits of its calls ([chat.md](./chat.md) § Conversations, turns, events); `.env`'s alone, never overridden from the console |

- **Kano Proxy**:
  - Vision calls and the catalog go to `{origin}/openai/v1`, the chat and check agents to `{origin}/anthropic/v1`.
  - No upstream: parses and turns are refused with 503.
- **The models**:
  - The chat agent offers the live catalog's models the table prices, in its order.
  - The API and the worker refuse to start with an offered model unpriced.
  - Parses are refused with 503 while `PARSE_MODEL` is empty.
  - All four may be overridden in the database (§ Settings in the database).
- **The summary model**:
  - `SUMMARY_MODEL`: the small model the title `/rename` asks for is written on. Empty = the conversation's own model. One named here must be priced in `MODEL_PRICES_JSON`, or the API and the worker refuse to start.
  - `SUMMARY_MAX_OUTPUT_TOKENS`: the most one of its calls may write (1024).
  - `SUMMARY_TIMEOUT_SECONDS`: how long one of its calls may take (60).
  - `TITLE_MESSAGE_MAX_CHARS`: how much of each message of the two exchanges it reads (1500).

### Credits, plans and payments

| Var | Notes |
|-----|-------|
| `CREDIT_USD`, `PARSE_CREDITS_PER_PAGE`, `CHAT_MIN_CREDITS`, `FREE_CREDITS_PER_PERIOD`, `FREE_STORAGE_BYTES`, `FREE_MAX_JOBS` | credits and the Free plan ([billing.md](./billing.md) § Credits, § Plans); overridable in the database |
| `KANO_PAY_URL`, `KANO_PAY_APP_ID`, `KANO_PAY_API_KEY`, `KANO_PAY_WEBHOOK_SECRET`, `KANO_PAY_PLANS_JSON`, `KANO_PAY_PACKS_JSON`, `BILLING_REFRESH_SECONDS` | the Kano Pay registration, the paid plans and the top-up packs ([billing.md](./billing.md) § Kano Pay, § Top-up packs) |
| `REDEEM_CODE_PREFIX`, `REDEEM_CODE_CHARS`, `GRANT_PERIOD_DAYS`, `REDEEM_MAX_FAILURES`, `REDEEM_FAILURE_WINDOW_SECONDS` | redeem codes ([billing.md](./billing.md) § Redeem codes) |
| `REDEEM_CODES_MAX_COUNT` | the most redeem codes one create makes, on the console's page and with `python -m sightread.codes` (1000; from 1 to 10,000) ([admin.md](./admin.md) § Redeem codes) |

- **Credits and the Free plan**:
  - `FREE_STORAGE_BYTES` and `FREE_MAX_JOBS` are at least 0 and within `WORKSPACE_QUOTA_BYTES` and `MAX_JOBS_PER_USER`.
  - The three credit counts, and a plan's or a pack's `credits`, are at most 2,147,483,647 (the ledger's 32-bit integers).
  - All six may be overridden in the database (§ Settings in the database).
- **Kano Pay**:
  - The key and the secret live only in `.env`.
  - Any of `KANO_PAY_URL`, `KANO_PAY_APP_ID`, `KANO_PAY_API_KEY` and `KANO_PAY_WEBHOOK_SECRET` unset = every user on Free.
  - `KANO_PAY_PACKS_JSON` empty = no pack sold.
  - `KANO_PAY_PLANS_JSON` and `KANO_PAY_PACKS_JSON` may be overridden in the database (§ Settings in the database); the rest may not.
- **Redeem codes**:
  - `REDEEM_CODE_PREFIX`: the prefix codes are printed and typed with (`EMA`).
  - `REDEEM_CODE_CHARS`: how many random characters follow it (12).
  - `GRANT_PERIOD_DAYS`: how many days one granted period of a paid plan runs (30).
  - `REDEEM_MAX_FAILURES`, `REDEEM_FAILURE_WINDOW_SECONDS`: the refused attempts a user may make in the window before 429 (10 in 3600 s).
- **`REDEEM_CODES_MAX_COUNT`**: outside 1 to 10,000, the API, the worker and the CLI refuse to start. One batch is checked in one query of a parameter per code, which the PostgreSQL driver caps at 32,767.

### The operator console

| Var | Notes |
|-----|-------|
| `OWNER_EMAILS` | the operator console ([admin.md](./admin.md)): the Google addresses that join its built-in owner group (below). Empty (the default) = no console |
| `ADMIN_REAUTH_HOURS`, `ADMIN_PAGE_SIZE` | the console's re-login window and the rows one page of its lists holds ([admin.md](./admin.md) § Configuration) |
| `RUNTIME_SETTINGS_CACHE_SECONDS` | how long the API, the worker and the CLI keep the settings the database overrides before reading them again (30; above 0, or they refuse to start) (§ Settings in the database) |
| `ADMIN_GRANT_VALID_DAYS`, `ADMIN_GRANT_MAX_CREDITS`, `ADMIN_GRANT_MAX_DAYS` | the console's grants to an account ([admin.md](./admin.md) § Users and grants) |
| `ADMIN_PROBE_TIMEOUT_SECONDS`, `ADMIN_USAGE_MAX_DAYS`, `ADMIN_FAILED_WINDOW_HOURS` | the console's overview and usage ([admin.md](./admin.md) § Configuration) |
| `FORWARDED_ALLOW_IPS` | uvicorn's: the peers whose `X-Forwarded-For` it trusts for a request's client address. Not in `.env` (below) |

- **`OWNER_EMAILS`**:
  - The addresses join the owner group when they sign in with the address verified; comma-separated, in any letter case.
  - Empty: every `/api/admin` path answers 404, `GET /api/me` says `console: false`.
  - Taking an address out removes no one.
  - Read by the API only; a change needs the api container recreated (§ Release flow).
- **The re-login window and the page size**:
  - `ADMIN_REAUTH_HOURS`: how many hours after a sign-in a session may still use the console (12; above 0).
  - `ADMIN_PAGE_SIZE`: the rows one page of its lists holds (50; 1–500).
  - Outside either range the API refuses to start, and so does the worker, which checks every setting it loads.
- **Grants**:
  - `ADMIN_GRANT_VALID_DAYS`: how many days granted credits are valid unless the operator chooses another number (365; 1 to `ADMIN_GRANT_MAX_DAYS`).
  - `ADMIN_GRANT_MAX_CREDITS`: the most credits one grant may give (1000000; 1 to 2,147,483,647).
  - `ADMIN_GRANT_MAX_DAYS`: the most days one grant may give, of a plan or of a credit grant's validity (3650; 1 to 36,500).
  - Out of range, the API and the worker refuse to start.
- **Overview and usage**:
  - `ADMIN_PROBE_TIMEOUT_SECONDS`: how long the overview waits for each of Kano Proxy's and Kano Pay's catalogs, which it asks at once on every read (5; above 0).
  - `ADMIN_USAGE_MAX_DAYS`: the most days one usage read may span (366; at least 90, the Usage page's longest range).
  - `ADMIN_FAILED_WINDOW_HOURS`: how many hours back the overview counts failed jobs (24; 1–8760, a year).
  - Outside any range the API and the worker refuse to start.
- **`FORWARDED_ALLOW_IPS`**:
  - `docker-compose.production.yaml` sets it to `*` on `api` (§ Compose files); locally it keeps uvicorn's default (`127.0.0.1`).
  - Behind a front proxy that passes TLS through by SNI without the PROXY protocol, the address that connects to Caddy is the router's, so that is the address the console's audit log records for every request.

### Uploads, files and workspaces

| Var | Notes |
|-----|-------|
| `UPLOAD_MAX_BYTES`, `PAGE_CAP`, `MAX_JOBS_PER_USER`, `UPLOAD_RETRY_WINDOW_SECONDS`, `VISION_CONCURRENCY_PER_JOB`, `VISION_MAX_OUTPUT_TOKENS`, `RENDER_WORKERS` | defaults in [api.md](./api.md) / [jobs.md](./jobs.md) |
| `FILE_MAX_BYTES` | the most a file of any kind may be when uploaded to the library (2000000000, 2 GB) (below) |
| `REVISION_MAX_FILE_BYTES` | the largest file a workspace revision records (1048576, 1 MiB); a larger one, or a binary one, is kept in the tree and left out of the history ([workspace.md](./workspace.md) § Revisions) |
| `UPLOAD_DIR`, `FIGURES_DIR` | the uploads (`/data/uploads`) and the stored figure crops (`/data/figures`): set by compose on api and worker; a value in `.env` is ignored |
| `WORKSPACES_HOST_DIR` / `WORKSPACES_DIR` | the per-user workspaces: the host directory compose bind-mounts, and where the containers see it (`/data/workspaces`) (below) |
| `WORKSPACE_QUOTA_BYTES` | the ceiling every plan's storage, Free's included, must fit under (default 10 GiB); the UI shows and the API enforces the user's plan's cap ([billing.md](./billing.md) § Plans) |
| `UPLOAD_MAX_AGE_SECONDS` | how old an upload-directory entry may grow before the worker's sweeper takes it, an unsent chat attachment's included (86400) ([jobs.md](./jobs.md) § Retention) |
| `TRASH_RETENTION_DAYS` | how many days an item stays in the Trash before the worker's sweep deletes it for good (30) (below) |
| `PARSE_RETENTION_DAYS` | how many days a finished parse no file names is kept before the sweep deletes it with its result, crops and workspace copy (30); must be ≥ 0, and 0 = finished parses are kept forever ([jobs.md](./jobs.md) § Retention) |
| `WORKSPACE_GIT_TIMEOUT_SECONDS` | how long any one git call the host makes may take (60) ([workspace.md](./workspace.md) § Revisions); the API image installs the `git` CLI beside Poppler for these calls |
| `WORKSPACE_GIT_PACK_LOOSE_OBJECTS` | how many loose objects a workspace's git history may hold before the worker's sweep packs it (100) ([workspace.md](./workspace.md) § Revisions) |

- **`FILE_MAX_BYTES`**: enforced in the app and, on that one route, in Caddy (§ Caddy routing, [workspace.md](./workspace.md) § Files). `GET /api/me` passes it to the web app as `limits.file_max_bytes`.
- **`WORKSPACES_HOST_DIR`**:
  - The runner is given the host path (`SANDBOX_WORKSPACES_DIR`) because it hands it to `docker run` ([workspace.md](./workspace.md), [sandbox.md](./sandbox.md)).
  - Compose gives the runner `SANDBOX_WORKSPACES_DIR` from `WORKSPACES_HOST_DIR` in both stacks, so the two cannot differ.
  - Empty locally means `./workspaces`, correct only when compose runs from the checkout's root.
  - `WORKSPACES_HOST_DIR` is the one `.env` sets. Compose sets `WORKSPACES_DIR` (`/data/workspaces`) and the runner's `SANDBOX_WORKSPACES_MOUNT` (`/data/workspaces`) and `SANDBOX_WORKSPACES_DIR`; a value of theirs in `.env` is ignored.
- **`TRASH_RETENTION_DAYS`**: must be ≥ 0, and 0 = the Trash is never emptied automatically ([workspace.md](./workspace.md) § The Trash). `GET /api/me` passes it to the web app as `limits.trash_retention_days`, for the Trash tab's line.

### The sandbox

| Var | Notes |
|-----|-------|
| `SANDBOX_URL` / `SANDBOX_TOKEN` | the runner's address on the compose network and the shared token api and worker present to it; empty `SANDBOX_URL` = no sandbox, the chat agent runs without `bash` |
| `SANDBOX_PORT` | the port the runner listens on (8100) (below) |
| `SANDBOX_RUNTIME`, `SANDBOX_VM_RUNTIMES`, `SANDBOX_IMAGE`, `SANDBOX_NETWORK`, `SANDBOX_INDEX_URL`, `SANDBOX_CPUS`, `SANDBOX_MEMORY`, `SANDBOX_PIDS`, `SANDBOX_TMP_SIZE`, `SANDBOX_IDLE_SECONDS`, `SANDBOX_DOCKER_TIMEOUT_SECONDS`, `SANDBOX_OUTPUT_MAX_BYTES` | the runner's own settings ([sandbox.md](./sandbox.md)); `SANDBOX_NETWORK` is read by the worker and the API as well (below) |
| `SANDBOX_EGRESS_HOSTS` | the sites the sandbox may reach over HTTPS (`pypi.org,files.pythonhosted.org`), one of the settings the database may override (§ Settings in the database; [sandbox.md](./sandbox.md) § Network) |
| `SANDBOX_SUBNET`, `SANDBOX_VM_RANGE`, `SANDBOX_EGRESS_ADDRESS`, `SANDBOX_EGRESS_PORT` | compose's own: the internal network's subnet, the VMs' part of it, the egress proxy's fixed address on it and its port (below) |
| `SANDBOX_EGRESS_CONNECT_TIMEOUT_SECONDS`, `SANDBOX_EGRESS_IDLE_SECONDS`, `SANDBOX_EGRESS_MAX_TUNNELS` | the egress proxy's limits (below) |
| `SANDBOX_EGRESS_HOSTS_FILE` | where the worker writes the list in effect and the proxy reads it: set by compose on a volume of their own (`/data/sandbox-egress/hosts`); a value in `.env` is ignored; empty, the worker writes nothing |
| `SANDBOX_CONTROL_TIMEOUT_SECONDS` | how long api and worker give the runner's stop, reset, restart and status calls (180) (below) |
| `SANDBOX_START_TIMEOUT_SECONDS`, `SANDBOX_EXEC_GRACE_SECONDS` | what a shell command may take beyond its own timeout before api and worker give up on it (below) |

- **`SANDBOX_PORT`**: the runner's health check in both compose files probes it from the container's own environment, so no fixed port is wired anywhere. `SANDBOX_URL` in `.env` must name the same port, in both stacks.
- **`SANDBOX_NETWORK`**:
  - `none` or `ema-paper-sandbox`, the internal network of the egress proxy ([sandbox.md](./sandbox.md) § Network).
  - Read by the worker, so the chat agent's prompt says what the shell can reach ([chat.md](./chat.md) § System prompt).
  - Read by the API, whose Settings page warns while it is `none` that the list of sites applies to nothing.
- **The internal network**:
  - `SANDBOX_SUBNET`: the internal network's subnet (`10.250.250.0/24`); it must not overlap another network of the host.
  - `SANDBOX_VM_RANGE`: the part of it the VMs' addresses come from (`10.250.250.128/25`).
  - `SANDBOX_EGRESS_ADDRESS`: the egress proxy's fixed address on it, outside that part (`10.250.250.2`).
  - `SANDBOX_EGRESS_PORT`: the proxy's port (3128).
  - From the address and the port compose gives the runner `SANDBOX_PROXY_URL` — the proxy VMs are pointed at — and the proxy its port.
- **The egress proxy's limits**:
  - `SANDBOX_EGRESS_CONNECT_TIMEOUT_SECONDS`: the request, the connection and the TLS hello each within 10 seconds.
  - `SANDBOX_EGRESS_IDLE_SECONDS`: a tunnel quiet both ways is closed after 300.
  - `SANDBOX_EGRESS_MAX_TUNNELS`: at most 64 tunnels per VM.
  - Compose passes the proxy these and its port alone, never `.env` whole.
- **`SANDBOX_CONTROL_TIMEOUT_SECONDS`**: must cover the runner's `SANDBOX_DOCKER_TIMEOUT_SECONDS` plus its inspection and a reset's cache deletion ([sandbox.md](./sandbox.md)).
- **`SANDBOX_START_TIMEOUT_SECONDS`**: the VM's cold start on a stopped sandbox (180; the runner gives `docker run` the same).
- **`SANDBOX_EXEC_GRACE_SECONDS`**: the grace for the reply (60).

### The chat agent

| Var | Notes |
|-----|-------|
| `CHAT_WEB_ACCESS`, `BRAVE_SEARCH_API_KEY`, `OPENALEX_API_KEY`, `BRAVE_SEARCH_URL`, `OPENALEX_WORKS_URL`, `WEB_SEARCH_CREDITS` | the chat agent's web tools ([chat.md](./chat.md) § Tools, [billing.md](./billing.md) § A chat turn) (below) |
| `MAX_CHAT_TURNS_PER_USER`, `CHAT_MAX_OUTPUT_TOKENS`, `CHAT_MAX_TOOL_CALLS_PER_TURN`, `CHAT_OVERDRAFT_MAX_TOOL_CALLS`, `CHAT_TURN_TIMEOUT_SECONDS`, `CHAT_STREAM_TIMEOUT_SECONDS`, `CHAT_WRITER_GRACE_SECONDS`, `REVERT_MAX_BYTES`, `REVERT_MAX_PATHS`, `WORKSPACE_LOG_MAX_BYTES`, `CHAT_TOOL_RESULT_MAX_CHARS`, `CHAT_IMAGE_ROUND_MAX_BYTES`, `CHAT_IMAGE_TURN_MAX_BYTES`, `CHAT_PAGE_IMAGE_MAX_PIXELS`, `CHAT_HISTORY_MAX_BYTES`, `CHAT_MESSAGE_MAX_CHARS`, `CHAT_SELECTION_MAX_CHARS`, `WEB_SEARCH_MAX_PER_TURN`, `WEB_SEARCH_MAX_PER_DAY`, `PAPER_SEARCH_MAX_PER_TURN`, `PAPER_SEARCH_MAX_PER_DAY`, `READ_URL_MAX_PER_TURN`, `READ_URL_MAX_BYTES`, `DOWNLOAD_MAX_BYTES`, `DOWNLOAD_MAX_PER_DAY`, `WEB_TOOL_TIMEOUT_SECONDS`, `DOWNLOAD_TIMEOUT_SECONDS` | the chat agent's limits ([chat.md](./chat.md)) |
| `CONVERT_FILE_MAX_PER_TURN`, `CONVERT_FILE_TIMEOUT_SECONDS` | the chat agent's limits ([chat.md](./chat.md)) that bound `convert_file`, the conversion of a kept file the agent runs inside its turn: 5 per turn, 900 seconds before it is put back on the queue |
| `CHAT_ATTACHMENT_MAX_BYTES`, `CHAT_ATTACHMENTS_PER_MESSAGE`, `CHAT_ATTACHMENT_TEXT_MAX_CHARS`, `CHAT_ATTACHMENT_IMAGE_LONG_EDGE`, `CHAT_ATTACHMENTS_PENDING_MAX`, `CHAT_ATTACHMENT_CACHE_SECONDS`, `CHAT_ATTACHMENT_PDF_TIMEOUT_SECONDS`, `CHAT_SKILLS_MAX`, `CHAT_SUBAGENTS_MAX`, `CHAT_SUBAGENT_MAX_TOOL_CALLS`, `CHAT_INSTRUCTIONS_MAX_CHARS`, `CHAT_MEMORY_MAX_CHARS`, `CHAT_MEMORIES_MAX`, `CHAT_MEMORY_BLOCK_MAX_CHARS`, `CHAT_SECTIONS_PER_MESSAGE`, `CHAT_SECTION_HEADING_MAX_CHARS` | the composer's attachments and sections, a user's own skills and subagents, the chat agent's instructions and memories (defaults in [api.md](./api.md); [chat.md](./chat.md) § What a user message carries, § Skills, § Subagents, § Instructions, § Memory) |
| `CHAT_LIBRARY_ITEMS_PER_MESSAGE`, `CHAT_LIBRARY_MAX_PATHS`, `CHAT_LIST_FILES_MAX_ENTRIES` | a library conversation's limits ([chat.md](./chat.md) § Library conversations) (below) |

- **The web tools**:
  - `CHAT_WEB_ACCESS`: the switch (true).
  - `BRAVE_SEARCH_API_KEY`, `OPENALEX_API_KEY`: the keys `web_search` and `search_papers` run on; each tool is absent while its key is empty. Both keys live only in `.env`.
  - `BRAVE_SEARCH_URL`, `OPENALEX_WORKS_URL`: the endpoints the two searches call (Brave's and OpenAlex's own by default).
  - `WEB_SEARCH_CREDITS`: what a web search costs the reader (3 credits).
- **A library conversation's limits**:
  - `CHAT_LIBRARY_ITEMS_PER_MESSAGE`: the Files-page items one message may carry (200); a send past it is refused with 400.
  - `CHAT_LIBRARY_MAX_PATHS`: the paths one library tool call may name (100).
  - `CHAT_LIST_FILES_MAX_ENTRIES`: the lines one `list_files` answers before it says how many it left out (500).

### The database and Caddy

| Var | Notes |
|-----|-------|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `PG_PORT` | local compose only: host port for `pg` (default 5432); change it when that port is taken |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | the `pg` container's credentials; the password is required in production |
| `DOMAIN` / `ACME_EMAIL` | production only, required: the host Caddy serves, and the Let's Encrypt contact address |

### Google and Kano Proxy in production

- **Google OAuth client**: the authorized redirect URI is `https://<domain>/api/auth/callback`, and the authorized origin is `https://<domain>`. Nothing else in the app needs registering with Google — the connector flow uses this app's own authorization server.
- **Kano Proxy**: the operator's own project API key goes into `KANO_PROXY_API_KEY`; nothing else is registered on Kano Proxy. The compose files pass `.env` through whole (`env_file`) to `api`, `worker` and `sandbox`, so nothing else changes there.

### Settings in the database

Thirteen of the settings above may be changed on the operator console's Settings page, which stores them in the `runtime_settings` table over `.env` ([admin.md](./admin.md) § Settings): `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL`, `MODEL_PRICES_JSON`, `CREDIT_USD`, `PARSE_CREDITS_PER_PAGE`, `CHAT_MIN_CREDITS`, `FREE_CREDITS_PER_PERIOD`, `FREE_STORAGE_BYTES`, `FREE_MAX_JOBS`, `SANDBOX_EGRESS_HOSTS`, `KANO_PAY_PLANS_JSON` and `KANO_PAY_PACKS_JSON`.

`.env` stays the default. A key without a row is `.env`'s, and *Restore to .env* deletes a row.

No other setting can be overridden. These are `.env`'s alone: secrets, connections, paths, the ceilings `WORKSPACE_QUOTA_BYTES` and `MAX_JOBS_PER_USER`, `WEB_SEARCH_CREDITS`, `GRANT_PERIOD_DAYS`, the console's own settings and `RUNTIME_SETTINGS_CACHE_SECONDS`.

- **No restart.** The API applies a change at once. The worker and the CLI read the rows again every `RUNTIME_SETTINGS_CACHE_SECONDS` (30), and the egress proxy follows the file the worker writes on the same beat ([sandbox.md](./sandbox.md) § Network). The containers start from `.env` and check it alone, so a container starts whatever the database holds.
- **`.env` still decides where the database is silent, and still counts when it speaks.** A stored value is checked against the `.env` around it when it is saved and each time the rows are read. When an edit of `.env` — a ceiling lowered, a model priced only by `.env`'s table — makes the stored values fail the start checks, the API and the worker run on `.env` alone, and the Settings page says so in a banner until the values are changed or restored ([admin.md](./admin.md) § Settings, *When the database's values no longer fit*).
- **Editing `.env` for a key the database overrides changes nothing in effect** until that key is restored to `.env`. The Settings page shows `.env`'s text beside the database's.
- **Not within the 30 seconds** ([admin.md](./admin.md) § Settings, *What does not follow*):
  - the public catalog's browser cache (`KANO_PAY_CATALOG_TTL_SECONDS`);
  - a period already running when its plan's credits are lowered;
  - work already queued or started.

## Release flow

On the server: `git pull && docker compose -f docker-compose.production.yaml up -d --build`.

- On hosts with a front proxy, append `-f docker-compose.only-container-network.yaml`, and keep appending it, since leaving it off republishes 80/443 and collides with whatever owns them.

Before the pull:

- Note what a rollback returns to: the commit, `git rev-parse HEAD`, and the database's revision, `docker compose -f docker-compose.production.yaml exec api alembic current` (same `-f` list). After the pull an api that cannot migrate restarts over and over, and `exec` refuses a restarting container.
- Take a dump: `docker compose -f … exec -T backup sh -c 'pg_dump --format=custom --file=/backups/pre-$(date -u +%Y%m%dT%H%M%SZ).dump'`. The `backup` service's environment names the database, and its `/backups` is `./backups`. Rotation deletes only the `backups/[0-9]*/` stamp directories, so the file stays until it is removed by hand.
- Start no update while a backup pass is dumping, which `docker compose -f … top backup` shows by listing a `pg_dump` process: the dump holds a lock on every table, and the migrations wait behind it.
- A `backups/.tmp-<stamp>` directory is the staging directory of a pass that is running or of one a stop cut short. Nothing removes one left by a stopped pass: delete it by hand while `top backup` lists neither `pg_dump` nor `tar`.
- An install whose containers are named `sightread-*` first moves its data to the current names ([self-hosting.md](./self-hosting.md) § Keep it running).

The api runs `alembic upgrade head` as it starts (§ Compose files). `migrations/env.py` runs every revision of one upgrade in one transaction, so they commit together or not at all.

The update has finished when `docker compose -f … ps -a` shows `api` *healthy* and `worker` *Up*.

- The api's health check has a budget: an api still migrating about 210 s after its start (`start_period` 60 s, then 30 probes 5 s apart) turns unhealthy. `up` then exits with *dependency failed to start* and leaves the new worker created but not started, and no restart policy starts it.
- Wait until `docker compose -f … ps api` shows the api *healthy*, then run the same `up -d` again, which starts the worker.

Going back:

- An `up` that failed inside the migration changed nothing in the database: check out the noted commit and run the same `up -d --build`.
- After a migration that finished:
  1. `docker compose -f … stop worker api`.
  2. While the new checkout and its images are still in place, `docker compose -f … run --rm --no-deps api alembic downgrade <the noted revision>`.
  3. `git checkout <the noted commit>`, then the same `up -d --build`. The next update starts with `git checkout main`.
- A downgrade restores the structure only: a dropped column comes back empty, and a deleted row does not come back. A downgrade that could undo its change only by deleting rows a user made refuses while they exist ([database.md](./database.md) § Migrations).
- The dump restores the data as it was before the update, without what was written since. In place of step 2, with `worker` and `api` stopped: `docker compose -f … exec -T backup sh -c 'dropdb "$PGDATABASE" && createdb "$PGDATABASE" && pg_restore -d "$PGDATABASE" /backups/pre-<stamp>.dump'`. The dump is at the noted revision, so no downgrade follows.

The migration chain starts at the baseline `4449f68533c0` ([database.md](./database.md) § Migrations).

- A database on an older revision — a server not updated since before the baseline, or a backup of one restored — is first updated once with commit `945b2e9`, the last release that carries the earlier revisions, and then with the current one.
- Such a database updated with the current release alone stops the api's `alembic upgrade head` at *Can't locate revision*: the api never turns healthy, the worker does not start, and nothing in the database is changed.

A change to `.env` alone — `OWNER_EMAILS` to turn the operator console on, say — needs no build, but it reaches a container only when compose recreates it.

- `docker compose -f docker-compose.production.yaml up -d --no-deps api` (same `-f` list) recreates `api` with the new file and leaves `pg` and the worker running.
- `docker compose … restart api` keeps the environment the container was created with, so the change would not apply.
- `.env` is also the `env_file` of `worker` and `sandbox`, so the next plain `up -d` recreates those too.

A Caddyfile change takes `docker compose … restart caddy` (same `-f` list), which mounts the new file. The restart drops open connections, SSE streams included.

Neither a plain `up -d` nor `caddy reload` picks up the change:

- `up -d` leaves an unchanged `caddy` container running.
- The Caddyfile is bind-mounted as a single file: `git pull` writes it as a new file the running container never sees, so `caddy reload` would read the old one.
