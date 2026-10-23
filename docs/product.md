# Product

## What it is

Ema Paper is a hosted, multi-user document-understanding service: PDF and image in, markdown with figure coordinate placeholders out. It descends from the "sightread MCP" spec (vision-based extraction, coordinates-not-crops) but is deliberately a **platform**, not a stateless tool.

## Name

The product is **Ema Paper** (renamed from Sightread on 2026-10-23). The name is what users see: the web app, the sign-in page's SEO title, the MCP server name, the agent prompts and these docs. The identifiers underneath keep the old word on purpose, because renaming them would break clients and data without changing anything a user sees: the repository `sightread`, the Python package `sightread`, the database and compose service names, browser storage keys, and the `sightread://` figure placeholder scheme, which is part of the `/v1/parse` contract ([api.md](./api.md)).

## Deliberate deviations from the origin spec

The origin spec (v0.1) is a reference, not a contract. Decisions that supersede it:

1. **Not stateless.** Jobs, results, and usage live in PostgreSQL. Parse results (markdown + metadata) are kept indefinitely; **source files are deleted immediately** when a job reaches a terminal state (see [jobs.md](./jobs.md)).
2. **REST is the core; MCP is a thin shell** over the same service layer — a single `parse` tool that mints an upload ticket and returns `curl` commands; file bytes never travel through MCP. Requires the agent to have shell + network access ([mcp.md](./mcp.md)).
3. **No `file path` source.** Hosted service accepts uploads (multipart) or base64 only — server paths would be an LFI.
4. **Model is user-chosen.** Users pick any model from the live catalog of their upstream — Kano Proxy, or a provider connection of their own — per agent ([agents.md](./agents.md)). Nothing is preset or recommended; bbox quality is the user's responsibility ([parsing.md](./parsing.md)).
5. **bbox format is declared per response** (`bbox_format` field), not assumed globally.
6. **Coordinates are the contract; crops are a stored extra.** The response carries everything an API caller needs to crop for themselves (page dims, bbox format, method, model) and the data plane still ships no image bytes — but the pipeline now also saves each figure's crop from the page it already rendered, so the web result viewer can show real figures ([parsing.md](./parsing.md) § Figure crops). Crop delivery is a control-plane read for the viewer, not part of `/v1/*`.
7. **Poppler, not PyMuPDF.** PyMuPDF is AGPL; Poppler is used via subprocess (crash isolation for free). This repo is MIT-licensed open source.

## Tenants and trust model

- Every user brings their **own upstream**: their Kano Proxy account, connected once through Kano Proxy's consent screen ([auth.md](./auth.md) § 6), or an OpenAI-compatible endpoint and key of their own. Vision spend is theirs. The operator pays only for CPU/disk, protected by per-user concurrency caps.
- Documents are untrusted input: parsing runs in short-lived subprocesses with timeouts; uploads are size- and page-capped.
- Document content is sensitive: source files are short-lived, prompts/completions are never logged (nor are a failed database statement's bound values, which hold the same text), results are visible only to their owner.

## Non-goals

- No password auth. No staging environment. No multi-server orchestration.
- No built-in OCR models or self-hosted VLMs — inference runs only on Kano Proxy or a user's own provider connection.
- No cross-user dedup cache (per-user only; global sharing would leak parse history and spend).
