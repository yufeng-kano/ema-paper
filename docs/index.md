# Ema Paper documentation

Hosted, multi-user **vision document parsing** service. Users sign in with Google, connect their own Kano Proxy account (or an OpenAI-compatible endpoint of their own — every vision call runs on the user's account, never the operator's), and parse PDFs/images into markdown with figure bounding boxes via REST or a hosted MCP endpoint (Claude Connectors compatible).

## Docs map

| Doc | Summary |
|-----|---------|
| [product.md](./product.md) | Goals, non-goals, origin spec deviations, tenants |
| [api.md](./api.md) | REST data plane (`/v1/*`), control plane (`/api/*`), partial results, figure crop routes, SSE, limits, errors |
| [auth.md](./auth.md) | Google OIDC sessions, hashed API keys, encrypted upstream keys, OAuth 2.1 AS for Claude Connectors, single-use upload tickets, the Kano Proxy connect flow |
| [agents.md](./agents.md) | The agent registry (`parse`, `chat` and `check`), per-agent upstream/model/prompt/effort settings, the parse agent's fallback model, the chat agent's instructions, languages and memory switches, the check agent's switch and instructions, enqueue rules |
| [chat.md](./chat.md) | The `chat` agent: the Messages API over Kano Proxy, tools (bash, editor, page and figure views, skills, subagents, memory), the reader's instructions appended to the prompt (whole-prompt replacement as the advanced option), answer and translation languages, memory (snapshot per conversation, the `memory` tool), prompt caching breakpoints, append-only history, translation (`/translate` — the document, a selection or sections), commands kept to the passage or sections they are sent with, what a user message carries (replies, sections, commands, attachments), conversations/turns/events |
| [workspace.md](./workspace.md) | The per-user file tree: document directories, markdown documents, inserted images, the Trash (move out of `files/`, Put Back, crash repair), copies, git revisions, reconciliation with the library, quota, backups |
| [sandbox.md](./sandbox.md) | One Kata microVM per user: the runner service, its HTTP contract, isolation, lifecycle, the image, operations |
| [parsing.md](./parsing.md) | Poppler rendering, vision-only conversion (every page or nothing; blocked and cut-off pages), the fallback model, the format check (program joins across pages, a checker agent on what they cannot settle, text kept, a reading copy), page markers, bbox contract, figure crop persistence, the figure check (edge rules on every box, the page's own model on a flagged page, two rounds, unverified figures marked), prompts, coordinate conventions per model family, models, image normalization |
| [jobs.md](./jobs.md) | PG job queue (SKIP LOCKED), concurrency caps, SSE progress, retrying failed pages (succeeded pages kept with their figure checks, the format check taken again), re-converting a document (a new parse of its original, swapped in as one revision once it succeeds), dedup cache, file retention |
| [database.md](./database.md) | PostgreSQL schema, Alembic migrations, secrets handling, the Trash as `trashed_at` (live rows computed, names unique among them) |
| [mcp.md](./mcp.md) | One `parse` tool minting upload tickets + curl commands, streamable HTTP, Claude Connectors flow |
| [web.md](./web.md) | Nuxt control plane: pages, the settings sheet behind the account menu (theme and language, the account, API keys, the MCP connector, Usage's three tabs) and its Agent section (instructions, languages, what the agent receives, memory, the advanced prompt dialog, skills, subagents, the parse agent's failed pages), the file library (Finder's model: selection, its keys with Ctrl off Apple platforms, inline naming, copy and paste, the Trash tab, undo; uploads, new and uploaded markdown documents, the retry dialog of a failed parse, re-converting a parsed document), filing parses from History, the Graphite design system, the result viewer (the outline, collapse and the way back after a jump; editing — the block handle and menu, conversions, moves and drag, keyboard block selection and its undo, the selection toolbar, the slash menu, images; notes — excerpts, section links, locate, backlink markers; the agent panel — step groups, answer blocks and their drag, replies, context chips, asking from the document, section links, memory lines, commands, attachments), links to a block, i18n |
| [project-structure.md](./project-structure.md) | Monorepo layout and module boundaries |
| [deployment.md](./deployment.md) | docker-compose (local, production, and an overlay for hosts with a front proxy), Caddy, TLS, env vars |
| [testing.md](./testing.md) | Test strategy, cost-safety rules, commands |

## Stack (fixed)

- **API / worker:** Python 3.12+, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, httpx, Authlib, official `mcp` SDK. Managed with **uv**.
- **PDF engine:** Poppler CLI (`pdftoppm`, `pdfinfo`) via subprocess — never a linked PDF library. See [parsing.md](./parsing.md).
- **Web:** Nuxt (latest stable) + TypeScript + `@nuxtjs/i18n` (en, zh-TW), pnpm.
- **Data:** PostgreSQL only — relational state **and** job queue (`FOR UPDATE SKIP LOCKED`). No Redis, no message broker.
- **Upstream:** Kano Proxy by default (`KANO_PROXY_BASE_URL`), with the key each user obtains through Kano Proxy's connect flow ([auth.md](./auth.md) § 6); model list fetched live from `{KANO_PROXY_BASE_URL}/openai/v1/models` with that key — never hard-coded. Users may instead route the parse agent's calls through a **provider connection**: their own OpenAI-compatible Chat Completions endpoint (base URL + key), models listed live from that endpoint's `/models` ([api.md](./api.md) § Upstreams). The chat agent speaks the Anthropic Messages API through Kano Proxy's `/anthropic/v1` route only ([chat.md](./chat.md)).
- **Sandbox:** one Kata microVM per user, started by a separate runner service that alone holds the Docker socket; the user's workspace directory is the VM's `/workspace` ([sandbox.md](./sandbox.md), [workspace.md](./workspace.md)).
- **Deploy:** docker-compose on a single server, Caddy for TLS + routing. Environments: local + production only.

## Product one-liner

Sign in with Google, connect Kano Proxy and pick a model, get an API key (or connect Claude via the MCP connector), then `POST /v1/parse` a PDF or image — back comes markdown with `<!-- page: N -->` markers, `sightread://` figure placeholders, and the exact model/bbox-format used, run on your own Kano Proxy account.
