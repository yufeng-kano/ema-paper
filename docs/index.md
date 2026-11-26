# Ema Paper documentation

Ema Paper is a hosted, multi-user **vision document parsing** service.

- A reader signs in with Google and uploads a PDF or an image; a script posts it to the REST API with an API key; Claude hands it over through the hosted MCP endpoint (Claude Connectors compatible).
- Back comes markdown with page markers and figure bounding boxes.
- In the web app the reader reads and edits it with a tool-using chat agent, and keeps files of any kind beside it, in the one tree the agent works in ([workspace.md](./workspace.md) § Files).
- Every model call runs on the operator's Kano Proxy key. Readers pay in credits: Free each month, more on a paid plan ([billing.md](./billing.md)).

These docs are the specification the code is held to. Read the ones a task touches before the work, and change them before the code (§ How the docs are written).

## Docs map

In the order a newcomer should meet them. Each entry says what the doc is the home of, the tasks that send you to it, and, where two docs share a border, what is held on the other side.

### Orientation

- **[index.md](./index.md)** — this page: the map, the fixed stack and the conventions of the docs.
  - Read: first, for any task.
- **[product.md](./product.md)** — what Ema Paper is and is not: its scope, its name, where it departs from the origin spec, and the trust model between readers, operators and documents.
  - Read: a feature that changes the product's scope, or who can see what.
- **[project-structure.md](./project-structure.md)** — where the code lives: the monorepo's layout, each module's job, and the boundaries that say which module alone may do what and what the code never imports.
  - Read: adding or moving a module, or deciding where a change belongs.

### The API and auth

- **[api.md](./api.md)** — every HTTP route of both planes: the data plane `/v1/*` and the control plane `/api/*`, with their payloads, the one error shape, SSE and the request limits.
  - Read: any change to a route, a payload, an error type or a limit.
  - Not here: the console's routes (admin.md) and the billing routes' answers (billing.md); api.md lists them and points there.
- **[auth.md](./auth.md)** — the credential kinds and how each is issued, stored and checked: Google sessions, API keys, the operator's upstream key, the OAuth 2.1 server for Claude Connectors and its scopes, upload and document tickets; and deleting the account.
  - Read: anything that signs a caller in, issues or checks a credential, or deletes an account.
  - Not here: who may use the operator console (admin.md).
- **[mcp.md](./mcp.md)** — the hosted MCP server: its transport, the `parse` tool and the commands it hands back, filing into Files, the library tools, and the organizing tools that put Files in order within the call.
  - Read: any change to a tool, its description or what a connector parse does.
  - Not here: the REST routes the commands call (api.md) and the tickets they carry (auth.md).

### The data

- **[database.md](./database.md)** — the PostgreSQL schema, the Alembic migration rules, and the storage rules (credentials hashed, settings kept as the text `.env` holds).
  - Read: any new or changed table, column, index or migration, before the code.

### The pipeline and jobs

- **[parsing.md](./parsing.md)** — the parse from file to markdown: Poppler rendering, the vision call per page, the format check, figure boxes and crops, page markers and the coordinate contract.
  - Read: anything that renders, converts, crops or checks a page, or changes what a parse returns.
- **[jobs.md](./jobs.md)** — the PostgreSQL queue and its one worker: claiming and recovery, concurrency, progress, retries and conversions, dedup, retention, and the chat turns the worker runs.
  - Read: anything that enqueues, claims or retries work, or changes what is kept and for how long.
  - Not here: what a parse does inside its job (parsing.md), what a turn does (chat.md).

### The agents and chat

- **[agents.md](./agents.md)** — the agent registry (`parse`, `chat`, `check`), each agent's per-user settings, and the enqueue rules that turn those settings into a job.
  - Read: adding an agent or a setting, or changing which model, prompt or switch a job runs with.
  - Not here: the chat agent's behaviour (chat.md), the Agent section that edits the settings (web.md).
- **[chat.md](./chat.md)** — the `chat` agent: its Messages API calls, tools, system prompt, memory, AGENTS.md, skills and subagents, translation, conversations and turns, and library conversations.
  - Read: any change to what the agent can do, sees, remembers or keeps in its history.
  - Not here: the agent panel that shows it (web.md), the credits a turn costs (billing.md).

### The workspace and sandbox

- **[workspace.md](./workspace.md)** — each reader's file tree on disk: its layout, documents, files, images, the Trash, git revisions, how the tree and the library's rows stay one, quota and backups.
  - Read: anything that writes, moves, deletes or counts a file of the library, or makes a revision.
  - Not here: the Files page (web.md), its routes (api.md).
- **[sandbox.md](./sandbox.md)** — the per-user microVM the chat agent's `bash` runs in: the runner service and its contract, isolation, the egress proxy, lifecycle and the image.
  - Read: anything that runs inside the VM, reaches out of it, or is installed in its image.

### The web and the console

- **[web.md](./web.md)** — the Nuxt app: its stack, pages, settings dialog, design system, the Files library, the result viewer with its editor, notes and agent panel, phones and tablets, and search engines.
  - Read: any screen, component, interaction or piece of copy.
  - Not here: the console's pages (admin.md); the contract the Usage section renders (billing.md).
- **[admin.md](./admin.md)** — the operator console: permissions and groups, signing in, the audit log, what an operator sees, the settings the database may override, and the console's routes and pages.
  - Read: anything under `/api/admin` or `/admin`, a new permission, or a setting the console may change.
  - Not here: the rules of the grants and codes it makes (billing.md).

### Billing

- **[billing.md](./billing.md)** — plans, credits and their prices, top-up packs, redeem codes and operator grants, the ledger, the models the operator offers, and Kano Pay.
  - Read: anything that charges, grants or refunds credits, changes a plan, a price or an offered model, or talks to Kano Pay.

### Running it

- **[deployment.md](./deployment.md)** — the reference for the running stack: the compose files, Caddy, the security headers, backups, every environment variable and the release flow.
  - Read: a new environment variable, container, route through Caddy or header; anything that changes how the stack is started or updated.
- **[self-hosting.md](./self-hosting.md)** — the walkthrough for an operator running their own Ema Paper, from a local trial to a production server.
  - Read: any change an operator must make differently to install, configure or upgrade.
  - Not here: the reference behind each step (deployment.md).
- **[testing.md](./testing.md)** — how the code is verified: the cost-safety rules, the backend and frontend suites, what they must cover, and the commands.
  - Read: before writing or running tests, and before delivering any change.

## Stack (fixed)

- **API / worker:** Python 3.12+, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, httpx, Authlib, the official `mcp` SDK. Managed with **uv**.
- **PDF engine:** Poppler CLI (`pdfinfo`, `pdftoppm`, `pdftotext`) via subprocess — never a linked PDF library. See [parsing.md](./parsing.md).
- **Web:** Nuxt (latest stable) + TypeScript + `@nuxtjs/i18n` (en, zh-TW), pnpm ([web.md](./web.md) § Stack).
- **Data:** PostgreSQL only — relational state **and** job queue (`FOR UPDATE SKIP LOCKED`). No Redis, no message broker.
- **Upstream:** Kano Proxy (`KANO_PROXY_BASE_URL`) on the operator's own key (`KANO_PROXY_API_KEY`, [auth.md](./auth.md) § 3); users connect none of their own.
  - The models are the deployment's (`PARSE_MODEL`, `MODEL_PRICES_JSON`, …, [billing.md](./billing.md) § Models and the operator's upstream), never hard-coded.
  - The chat list is the live catalog at `{KANO_PROXY_BASE_URL}/openai/v1/models`, kept to the priced models.
  - The `chat` and `check` agents speak the Anthropic Messages API through Kano Proxy's `/anthropic/v1` route ([chat.md](./chat.md)).
- **Web tools:** Brave Search and OpenAlex, called from the worker ([chat.md](./chat.md) § Tools).
- **Payments:** Kano Pay ([billing.md](./billing.md) § Kano Pay).
- **Sandbox:** one Kata microVM per user, started by a separate runner service (`apps/sandbox`) that alone holds the Docker socket; the user's workspace directory is the VM's `/workspace`; an egress proxy is the VMs' one way out ([sandbox.md](./sandbox.md), [workspace.md](./workspace.md)).
- **Deploy:** docker-compose on a single server, Caddy for TLS + routing. Environments: local + production only.

## Product one-liner

Sign in with Google and upload a PDF or a scan — or `POST /v1/parse` it with an API key, or hand it over through the MCP connector — and back comes markdown with `<!-- page: N -->` markers, `sightread://` figure placeholders and the exact model and bbox format used; in the web app, read it as clean text, translate it, keep notes beside it and ask an agent that sees the pages and searches the web, with every file kept and revised in one tree — paid for in credits, the model included.

## How the docs are written

For whoever edits them next.

- **The present tense.** A doc states the system as it is. No history, no dates of changes, no "since" or "no longer" about what the system was: git has the history.
- **One fact per sentence.** Independent rules are a list. A table's cells stay short, and the detail sits beneath the table.
- **One home per fact.** Each fact is stated in the doc the map names as its home; every other doc links to it instead of restating it.
- **Sections are cited by heading**, as `[chat.md](./chat.md) § Tools`, or by the section sign and the heading alone within one doc; a bold lead-in is cited in italics after the heading. So a heading is a stable name: renaming one means updating every citation of it.
- **Docs first.** A change to a feature, a schema or a behaviour changes its doc before the code, and the map here when a doc's scope changes.
