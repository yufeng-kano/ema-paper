# Product

## What it is

Ema Paper is a hosted, multi-user document-understanding service: PDF and image in, markdown with figure coordinate placeholders out. It is deliberately a **platform**, not a stateless tool.

It is also where the reader keeps the rest of their files. A file of any kind is uploaded once and stored once, in the same tree the chat agent works in. It is converted into a document when it is a PDF or an image the reader wants read ([workspace.md](./workspace.md) § Files).

## Name

The product is **Ema Paper**. The name is what users see: the web app, the sign-in page's SEO title, the MCP server name, the agent prompts and these docs.

The identifiers underneath keep the word `sightread` on purpose, because renaming them would break clients and data without changing anything a user sees:

- the Python package `sightread`;
- the database name;
- browser storage keys;
- the `sightread://` figure placeholder scheme, which is part of the `/v1/parse` contract ([api.md](./api.md)).

## Deliberate deviations from the origin spec

The origin spec is the "sightread MCP" spec (v0.1): vision-based extraction, coordinates-not-crops. It is a reference, not a contract. Decisions that supersede it:

1. **Not stateless.** Jobs, results, and usage live in PostgreSQL ([jobs.md](./jobs.md) § Retention).
   - Parse results (markdown + metadata) are kept while a file in the reader's library names them, and otherwise `PARSE_RETENTION_DAYS` (30) days after the parse finishes.
   - **Source files are deleted immediately** when a job reaches a terminal state. The exception is a library document's original, which the worker files with the document instead.
2. **REST is the core; MCP is a thin shell** over the same service layer ([mcp.md](./mcp.md)).
   - A `parse` tool mints an upload ticket and returns `curl` commands.
   - The library tools (`list_library`, `read_document`, `write_document`) let Claude read the Files library and save a document's text and notes the same way.
   - File bytes never travel through MCP.
   - The MCP shell requires the agent to have shell + network access.
3. **No `file path` source.** The hosted service accepts uploads (multipart) or base64 only, because server paths would be an LFI.
4. **Models are the operator's choice.**
   - The operator fixes the parse model and offers a short list of chat models, each at its own credit rate ([billing.md](./billing.md) § Models, [agents.md](./agents.md)).
   - The chat list is checked against Kano Proxy's live catalog.
   - bbox quality is the operator's responsibility ([parsing.md](./parsing.md)).
5. **bbox format is declared per response** (`bbox_format` field), not assumed globally.
6. **Coordinates are the contract; crops are a stored extra.**
   - The response carries everything an API caller needs to crop for themselves: page dims, bbox format, method, model.
   - The pipeline also saves each figure's crop from the page it already rendered ([parsing.md](./parsing.md) § Figure crops).
   - The web result viewer shows those crops, and `/v1/jobs/{id}/figures/…` serves the same ones, so the connector gets the figures the web reader shows ([mcp.md](./mcp.md)).
   - The result JSON itself carries no image bytes.
7. **Poppler, not PyMuPDF.** PyMuPDF is AGPL; Poppler is used via subprocess, which gives crash isolation for free. This repo is MIT-licensed open source.
   - That is the server's PDF work. The browser draws a PDF with the web app's own viewer, which takes permissively licensed code only ([pdf-viewer.md](./pdf-viewer.md) § Licences).

## Tenants and trust model

- **One upstream, the operator's.** Every model call runs on the operator's own Kano Proxy account ([auth.md](./auth.md) § 3). Users bring no key.
- **Spend is bounded by the plan.** Under the subscription ([billing.md](./billing.md)) a user's spend is bounded by the credits their plan grants per period. Plans are paid through Kano Pay.
  - Free: a small monthly grant, no sandbox, 512 MB.
  - Pro: a larger grant, the sandbox, 10 GB.
- **Files are kept on both plans** for as long as the account exists, save what the reader moves to the Trash: that is deleted 30 days later. The reader deletes the account themselves, at once ([auth.md](./auth.md) § Deleting the account).
- **Documents are untrusted input.** Parsing runs in short-lived subprocesses with timeouts. Uploads are size- and page-capped.
- **Document content is sensitive.**
  - Source files are short-lived.
  - Prompts/completions are never logged, nor are a failed database statement's bound values, which hold the same text.
  - Results are visible only to their owner.
- **Operators see metadata, never content** ([admin.md](./admin.md) § What an operator sees).
  - The operator, and whoever the operator gives the console to, can see an account's address and name, plan, credits, storage and what it ran — file names, pages, status, usage.
  - They can change things only through audited routes.
  - No route shows them a document, a note or a conversation, and none signs them in as a user.
  - The Privacy page says so.

## Non-goals

- No password auth.
- No staging environment.
- No multi-server orchestration.
- No built-in OCR models or self-hosted VLMs — inference runs only on Kano Proxy, on the operator's account.
- No cross-user dedup cache (per-user only; global sharing would leak parse history and spend).
