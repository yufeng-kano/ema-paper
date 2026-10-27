# Product

## What it is

Ema Paper is a hosted, multi-user document-understanding service: PDF and image in, markdown with figure coordinate placeholders out. It descends from the "sightread MCP" spec (vision-based extraction, coordinates-not-crops) but is deliberately a **platform**, not a stateless tool. Since 2026-10-25 it is also where the reader keeps the rest of their files: a file of any kind is uploaded once and stored once, in the same tree the chat agent works in, and converted into a document when it is a PDF or an image the reader wants read ([workspace.md](./workspace.md) § Files).

## Name

The product is **Ema Paper** (renamed from Sightread on 2026-10-23). The name is what users see: the web app, the sign-in page's SEO title, the MCP server name, the agent prompts and these docs. The identifiers underneath keep the old word on purpose, because renaming them would break clients and data without changing anything a user sees: the repository `sightread`, the Python package `sightread`, the database and compose service names, browser storage keys, and the `sightread://` figure placeholder scheme, which is part of the `/v1/parse` contract ([api.md](./api.md)).

## Deliberate deviations from the origin spec

The origin spec (v0.1) is a reference, not a contract. Decisions that supersede it:

1. **Not stateless.** Jobs, results, and usage live in PostgreSQL. Parse results (markdown + metadata) are kept while a file in the reader's library names them, and otherwise `PARSE_RETENTION_DAYS` (30) days after the parse finishes; **source files are deleted immediately** when a job reaches a terminal state (see [jobs.md](./jobs.md) § Retention).
2. **REST is the core; MCP is a thin shell** over the same service layer — a single `parse` tool that mints an upload ticket and returns `curl` commands; file bytes never travel through MCP. Requires the agent to have shell + network access ([mcp.md](./mcp.md)).
3. **No `file path` source.** Hosted service accepts uploads (multipart) or base64 only — server paths would be an LFI.
4. **Models are the operator's choice.** The operator fixes the parse model and offers a short list of chat models, each at its own credit rate ([billing.md](./billing.md) § Models, [agents.md](./agents.md)); the chat list is checked against Kano Proxy's live catalog, and bbox quality is the operator's responsibility ([parsing.md](./parsing.md)).
5. **bbox format is declared per response** (`bbox_format` field), not assumed globally.
6. **Coordinates are the contract; crops are a stored extra.** The response carries everything an API caller needs to crop for themselves (page dims, bbox format, method, model) and the data plane still ships no image bytes — but the pipeline now also saves each figure's crop from the page it already rendered, so the web result viewer can show real figures ([parsing.md](./parsing.md) § Figure crops). Crop delivery is a control-plane read for the viewer, not part of `/v1/*`.
7. **Poppler, not PyMuPDF.** PyMuPDF is AGPL; Poppler is used via subprocess (crash isolation for free). This repo is MIT-licensed open source.

## Tenants and trust model

- **One upstream, the operator's.** Every model call runs on the operator's own Kano Proxy account ([auth.md](./auth.md) § 3); users bring no key. Under the subscription ([billing.md](./billing.md)) a user's spend is bounded by the credits their plan grants per period: Free (a small monthly grant, no sandbox, 512 MB) and Pro (a larger grant, the sandbox, 10 GB), paid through Kano Pay. Files are kept on both plans for as long as the account exists, save what the reader moves to the Trash, deleted 30 days later; the reader deletes the account themselves, at once ([auth.md](./auth.md) § Deleting the account).
- Documents are untrusted input: parsing runs in short-lived subprocesses with timeouts; uploads are size- and page-capped.
- Document content is sensitive: source files are short-lived, prompts/completions are never logged (nor are a failed database statement's bound values, which hold the same text), results are visible only to their owner.
- **Operators see metadata, never content** (since 2026-10-27, [admin.md](./admin.md) § What an operator sees). The operator, and whoever the operator gives the console to, can see an account's address and name, plan, credits, storage and what it ran — file names, pages, status, usage — and change only through audited routes; no route shows them a document, a note or a conversation, and none signs them in as a user. The Privacy page says so.

## Non-goals

- No password auth. No staging environment. No multi-server orchestration.
- No built-in OCR models or self-hosted VLMs — inference runs only on Kano Proxy, on the operator's account.
- No cross-user dedup cache (per-user only; global sharing would leak parse history and spend).
