# Testing

## Cost safety — users' upstream keys are real money

- Tests **never** call Kano Proxy, a provider connection, or any live LLM. All upstream HTTP — vision calls, Messages API streams, the model catalog and every Kano Pay call ([billing.md](./billing.md) § Kano Pay) — is stubbed (`respx` over httpx). The sandbox runner is stubbed the same way from the API's side, and tested on its own with the `local` backend (a bash process in a temp dir), never with Docker. CI must pass with no network.
- A live smoke test requires the operator's explicit per-instance approval: one tiny document, one cheap model, never a loop or sweep.

## Backend (`apps/api`)

- `uv run pytest`. pytest + pytest-asyncio; DB tests against a throwaway Postgres (compose `pg` or testcontainers), each test in a rolled-back transaction where possible.
- Fixture documents in `tests/fixtures/`: a small text-layer PDF, a scanned-style PDF (rendered-only), a multi-page mix, one corrupt file, tiny jpg/png/webp/heic images. Generate or vendor tiny files — keep fixtures under a few hundred KB each.
- Migration backfills are tested as real upgrades (`tests/test_migrations.py`): each test creates a scratch database on the `TEST_DATABASE_URL` server, upgrades to the revision before the change, writes synthetic existing rows, upgrades, checks the backfill and the constraints, and drops the database. PostgreSQL only — the module skips on the SQLite fallback, so run it with `TEST_DATABASE_URL` set before shipping a migration.
- Poppler is a real dependency of the test environment (CI image installs `poppler-utils`); subprocess wrappers are tested against real fixtures, not mocked.
- Must-cover for the chat agent and the workspace: the Messages stream parser (text, thinking, tool_use with partial JSON, usage with cache tokens, refusal), the tool loop ending on `end_turn`, the tool-call cap and a cancel (the call it cut short still billed); the editor tool refusing a path outside the user's directory and the read-only names; a save with a stale `base_revision` answering 409 and a revert producing a new revision; reconciliation after a rename, a move and a delete in the tree; the turn queue's per-user cap; the SSE replay from `after`; usage rows carrying `agent`, `turn_id` and the cache tokens.
- Must-cover for retention and deletion: the Trash's expiry deleting only what expired, a folder with everything in it, entries, originals and images, a refused user skipped while the others go, and nothing at `TRASH_RETENTION_DAYS=0`; the parse purge deleting what the keep rule does not keep, crops and results copy first, and keeping a document's parse, a trashed document's, a re-convert in flight, an earlier parse of a document's original, a young parse and a queued or running one, with `usage_log` rows and ledger entries left with `job_id` NULL, `spent` unchanged, `/v1/jobs/{id}` 404 after, and nothing at `PARSE_RETENTION_DAYS=0`; the dedup margin; account deletion's rows and files gone and another user's untouched, its refusals (a renewing subscription, Pay unreachable, running work, CSRF), a queued job deleted, the sweep finishing from a marker, and Pay's webhook acknowledging a deleted subject.
- Must-cover: the operator's key on every upstream call and never in a response, a model outside the deployment's list refused at enqueue and at a send, dedup never crossing agents or answering from historical OpenRouter or provider-connection rows, an unconfigured Kano Proxy or parse model refused at enqueue, bbox placeholder emission, page markers, dedup key matching + `force` + degraded results never served from cache, per-user job cap 429, SKIP LOCKED claim under two concurrent workers, source-file deletion at terminal state, sweeper, OAuth AS happy path (DCR → PKCE → token → `/mcp` auth), 402/429 upstream handling, MCP tools end to end through the official SDK client over ASGI (the app lifespan does not run under `ASGITransport`, so `/mcp` tests start the SDK session manager themselves).

## Frontend (`apps/web`)

- `pnpm test` (vitest) for composables/utils; `pnpm typecheck` and lint are part of done-criteria. No browser-automation UI verification — correctness is judged by reading code and types.

## Lint/format

- Python: `uv run ruff check` + `ruff format --check`. TypeScript: eslint + prettier per Nuxt defaults.
