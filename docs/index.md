# agent-sightread documentation

Hosted, multi-user **vision document parsing** service. Users sign in with Google, store their own OpenRouter API key (BYO — every vision call bills the user's key, never the operator's), and parse PDFs/images into markdown with figure bounding boxes via REST or a hosted MCP endpoint (Claude Connectors compatible).

## Docs map

| Doc | Summary |
|-----|---------|
| [product.md](./product.md) | Goals, non-goals, origin spec deviations, tenants |
| [api.md](./api.md) | REST data plane (`/v1/*`), control plane (`/api/*`), partial results, figure crop routes, SSE, limits, errors |
| [auth.md](./auth.md) | Google OIDC sessions, hashed API keys, encrypted OpenRouter keys, OAuth 2.1 AS for Claude Connectors, single-use upload tickets |
| [parsing.md](./parsing.md) | Poppler rendering, vision-only conversion, page markers, bbox contract, figure crop persistence, prompts, profiles, image normalization |
| [jobs.md](./jobs.md) | PG job queue (SKIP LOCKED), concurrency caps, SSE progress, dedup cache, file retention |
| [database.md](./database.md) | PostgreSQL schema, Alembic migrations, secrets handling |
| [mcp.md](./mcp.md) | One `parse` tool minting upload tickets + curl commands, streamable HTTP, Claude Connectors flow |
| [web.md](./web.md) | Nuxt control plane: pages, the file library, filing parses from History, the Graphite design system, the result viewer, i18n |
| [project-structure.md](./project-structure.md) | Monorepo layout and module boundaries |
| [deployment.md](./deployment.md) | docker-compose (local, production, and an overlay for hosts with a front proxy), Caddy, TLS, env vars |
| [testing.md](./testing.md) | Test strategy, cost-safety rules, commands |

## Stack (fixed)

- **API / worker:** Python 3.12+, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, httpx, Authlib, official `mcp` SDK. Managed with **uv**.
- **PDF engine:** Poppler CLI (`pdftoppm`, `pdfinfo`) via subprocess — never a linked PDF library. See [parsing.md](./parsing.md).
- **Web:** Nuxt (latest stable) + TypeScript + `@nuxtjs/i18n` (en, zh-TW), pnpm.
- **Data:** PostgreSQL only — relational state **and** job queue (`FOR UPDATE SKIP LOCKED`). No Redis, no message broker.
- **Upstream:** OpenRouter by default, with each user's own key; model list fetched live from `/api/v1/models` filtered to image-input models — never hard-coded. Users may instead route vision calls through a **provider connection**: their own OpenAI-compatible Chat Completions endpoint (base URL + key, e.g. a kano-proxy `/openai/v1` base), models listed live from that endpoint's `/models` ([api.md](./api.md) § Upstreams).
- **Deploy:** docker-compose on a single server, Caddy for TLS + routing. Environments: local + production only.

## Product one-liner

Sign in with Google, save your OpenRouter key, get an API key (or connect Claude via the MCP connector), then `POST /v1/parse` a PDF or image — back comes markdown with `<!-- page: N -->` markers, `sightread://` figure placeholders, and the exact model/bbox-format used, billed to your own OpenRouter account.
