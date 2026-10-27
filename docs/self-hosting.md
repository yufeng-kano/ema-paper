# Self-hosting

How to run your own Ema Paper: on a laptop to try it, then on a server. This page is the walkthrough. [deployment.md](./deployment.md) is the reference behind each step: the compose files, Caddy, the security headers and every environment variable.

## What you need

- **Docker with Compose.** A recent Compose: the front-proxy overlay (§ Behind a front proxy) was verified on 2.40.1.
- **A Kano Proxy and an API key from it.** Every model call goes there, on that one key ([auth.md](./auth.md) § 3). Run your own from [kano-proxy](https://github.com/yufeng-kano/kano-proxy), or use an account on one someone else runs. Whoever owns the key pays for the models; users bring no key of their own.
- **A Google OAuth client** for sign-in. Production only: locally a dev sign-in stands in.
- **A domain** pointing at the server, for production. Caddy obtains and renews the certificate itself.

Paid plans and the chat agent's sandbox are optional, and neither is needed to run the service (§ Plans, credits and the sandbox).

## Try it on your machine

1. Copy the local configuration:

   ```bash
   cp .env.example .env
   ```

2. In `.env`, change these lines:

   | Variable | Value |
   |---|---|
   | `APP_URL`, `WEB_URL` | `http://localhost:8080`, the one origin Caddy serves locally |
   | `SECRET_KEY`, `SANDBOX_TOKEN` | long random strings, e.g. `openssl rand -hex 32` |
   | `KANO_PROXY_BASE_URL`, `KANO_PROXY_API_KEY` | your Kano Proxy's origin and key; leave both empty to run without models |
   | `PARSE_MODEL`, `MODEL_PRICES_JSON` | the parse model and the price table (§ Models and prices) |

3. Start the stack:

   ```bash
   docker compose up --build
   ```

4. Open http://localhost:8080 and choose **Dev sign-in**. It exists only while `APP_ENV=local` and `AUTH_DEV_MODE=true`.

Without the Kano Proxy values the app still runs: Files, markdown documents, notes and editing work, and parsing and chat answer 503.

## Models and prices

`PARSE_MODEL` is the vision model every parse runs on, an id from your Kano Proxy's catalog. `MODEL_PRICES_JSON` holds list prices in USD per million tokens, for the parse model and for every chat model you want to offer:

```bash
PARSE_MODEL=<vision-model-id>
MODEL_PRICES_JSON='{"<vision-model-id>": {"input": <usd>, "cache_read": <usd>, "cache_write": <usd>, "output": <usd>}, "<chat-model-id>": {...}}'
```

- The chat agent offers the catalog's models that the table prices, in the table's order. The first is the default.
- The API and the worker refuse to start while a model they would run has no price.
- Credits are derived from these prices and `CREDIT_USD` ([billing.md](./billing.md) § Credits, § Models).

## Run it on a server

1. Clone the repository on the server, then copy the production configuration. It is a standalone file: never carry the local `.env` to a server.

   ```bash
   cp .env.production.example .env
   ```

2. Fill in every placeholder. These come first:

   | Variable | Value |
   |---|---|
   | `DOMAIN`, `ACME_EMAIL` | the host Caddy serves, and the Let's Encrypt contact |
   | `APP_URL`, `WEB_URL` | `https://<domain>`, both. `WEB_URL` is built into the public pages, so changing it later needs `--build` |
   | `SECRET_KEY` | a long random string, e.g. `openssl rand -hex 32`. Never the local one |
   | `POSTGRES_PASSWORD` | a new password, also written into `DATABASE_URL` |
   | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | an OAuth client with redirect URI `https://<domain>/api/auth/callback` and origin `https://<domain>` |
   | `KANO_PROXY_BASE_URL`, `KANO_PROXY_API_KEY`, `PARSE_MODEL`, `MODEL_PRICES_JSON` | § Models and prices |
   | `WORKSPACES_HOST_DIR` | an absolute host path for every user's files. This is primary data: back it up |
   | `SUPPORT_EMAIL`, `OPERATOR_NAME` | who runs the service, printed on the Terms, Privacy and Refund pages |
   | `KANO_PAY_URL`, `KANO_PAY_APP_ID`, `KANO_PAY_API_KEY`, `KANO_PAY_WEBHOOK_SECRET` | empty, unless you run Kano Pay (§ Plans, credits and the sandbox) |

3. Point the domain's DNS at the server, open ports 80 and 443, and start:

   ```bash
   docker compose -f docker-compose.production.yaml up -d --build
   ```

   The API runs the database migrations as it starts, and the worker waits until it is healthy.

4. Check that `https://<domain>/healthz` answers, then sign in with Google.

### Behind a front proxy

When something else already owns ports 80 and 443, typically an SNI router in front of several stacks, add the overlay. Create the shared network once, then keep the second `-f` on every later command:

```bash
docker network create proxy
docker compose -f docker-compose.production.yaml -f docker-compose.only-container-network.yaml up -d --build
```

The router must pass TLS through untouched (SNI passthrough). Caddy still terminates TLS and owns its certificate, which it validates over 443 alone ([deployment.md](./deployment.md) § `docker-compose.only-container-network.yaml`).

## Plans, credits and the sandbox

- **Every account is on Free** until it buys a paid plan. Free grants `FREE_CREDITS_PER_PERIOD` credits each UTC month, `FREE_STORAGE_BYTES` of storage and `FREE_MAX_JOBS` parses at once. Raise them for a private deployment, within `WORKSPACE_QUOTA_BYTES` and `MAX_JOBS_PER_USER`, or the API refuses to start.
- **Paid plans go through Kano Pay**, a separate payment service ([billing.md](./billing.md) § Kano Pay). With the four `KANO_PAY_*` values empty, nobody can check out and Free keeps working.
- **The sandbox comes with a paid plan.** It gives the chat agent `bash` in one Kata microVM per user, and needs a host with `/dev/kvm` and the Kata runtime ([sandbox.md](./sandbox.md) § Operations). A deployment without paid plans sets `SANDBOX_URL=` (empty): the agent then works without `bash`, and everything else stays.

### Redeem codes

A redeem code gives an account credits, or days of a paid plan, without Kano Pay ([billing.md](./billing.md) § Redeem codes) — a gift to a user, a launch campaign, or the way a private deployment hands out a paid plan's storage and sandbox with Pay left unconfigured. Codes are made on the server, in the api container; there is no page for them:

```sh
# 100 codes of 500 credits, valid a year once redeemed, redeemable until the end of 2026
docker compose exec api python -m sightread.codes create --credits 500 --valid-days 365 \
    --count 100 --redeem-by 2026-12-31 --label "launch"
# one code anyone may use 200 times, 30 days of the plan "pro" each
docker compose exec api python -m sightread.codes create --plan pro --days 30 --uses 200 --label "beta"
docker compose exec api python -m sightread.codes list            # live codes and their uses
docker compose exec api python -m sightread.codes list --all      # disabled and exhausted ones too
docker compose exec api python -m sightread.codes disable EMA-XXXX-XXXX-XXXX
```

`create` prints each code once, as a user types it (`EMA-XXXX-XXXX-XXXX`; the prefix is `REDEEM_CODE_PREFIX`); `--uses` is how many accounts may redeem one code (1 unless given), and a plan must be one of `KANO_PAY_PLANS_JSON` or the command refuses. A user redeems a code in the settings dialog's Usage section. Granted days are spent only while no subscription pays for the account, and a code's credits are never refunded.

## The operator console

The console is your own pages inside the app — today *Groups*, to give other people access, and *Audit*, the record of every change made there ([admin.md](./admin.md)). It does not exist until you name its first owner. Put your Google address in `.env`:

```sh
OWNER_EMAILS=you@example.com    # comma-separated for several; empty = no console
```

Then recreate the api container so it reads the file again — `restart` would keep the old environment:

```bash
docker compose -f docker-compose.production.yaml up -d --no-deps api
# behind a front proxy: add -f docker-compose.only-container-network.yaml, as always
```

Sign in with that Google account (or sign out and in again): the account menu now has *Admin console*. The address joins the built-in `owner` group, which holds every permission, at a sign-in whose address Google has verified; taking it out of `OWNER_EMAILS` later removes no one — an owner does that on the Groups page. The console asks for a fresh sign-in once a session is older than `ADMIN_REAUTH_HOURS` (12). Locally, `OWNER_EMAILS=dev@localhost` makes the dev login an owner.

Redeem codes are still made with `python -m sightread.codes` (§ Redeem codes); the CLI needs no console and keeps working beside it.

## Connect your tools

- **REST API.** Create a key in Settings → API keys, then:

  ```bash
  curl -X POST https://<domain>/v1/parse \
    -H "Authorization: Bearer <api-key>" \
    -F "file=@paper.pdf"
  ```

  The answer is a job to follow, or the result itself when the same file was parsed before ([api.md](./api.md)).

- **Claude.** Add `https://<domain>/mcp` as a custom connector in Claude's settings, sign in, and approve. A client that takes a header instead can use an API key:

  ```json
  {
    "mcpServers": {
      "emapaper": {
        "command": "npx",
        "args": ["-y", "mcp-remote", "https://<domain>/mcp", "--header", "Authorization: Bearer <api-key>"]
      }
    }
  }
  ```

  The connector's one tool hands back an upload ticket and `curl` commands, so the client needs a shell with network access ([mcp.md](./mcp.md)).

## Keep it running

- **Update:** `git pull`, then the same `docker compose -f … up -d --build` you started with. A changed Caddyfile also needs `docker compose -f … restart caddy` ([deployment.md](./deployment.md) § Release flow).
- **Back up:** the `backup` service writes the database dump, the figure crops and every workspace into `./backups/<stamp>/` once a day and keeps the newest seven. Copy them off the server, and test a restore before you rely on it ([deployment.md](./deployment.md) § Compose files).
- **Logs:** `docker logs -f sightread-api`, and likewise `sightread-worker`, `sightread-web` and `sightread-caddy`.

## Develop

Day-to-day work runs the API (`uv run uvicorn`) and the web app (`pnpm dev`) natively for hot reload. Layout and module boundaries are in [project-structure.md](./project-structure.md); tests and the rule that they never call a paid model are in [testing.md](./testing.md).
