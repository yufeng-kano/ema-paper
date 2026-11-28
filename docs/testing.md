# Testing

## Cost safety — users' upstream keys are real money

- Tests **never** call Kano Proxy or any live LLM.
- All upstream HTTP is stubbed (`respx` over httpx):
  - vision calls;
  - Messages API streams;
  - the model catalog;
  - every Kano Pay call ([billing.md](./billing.md) § Kano Pay);
  - the chat agent's web tools — Brave, OpenAlex, every URL it reads or downloads ([chat.md](./chat.md) § Tools). The tests resolve a host to a fixed public address and stub the connection to it.
- The sandbox runner is stubbed the same way from the API's side. It is tested on its own with a local bash stand-in for its Docker backend (`apps/sandbox/tests/local_backend.py`, a bash process in a temp dir, injected into the app by the tests), never with Docker.
  - The runner's tests remove every `SANDBOX_*` variable and `APP_ENV` from the environment before each test (`apps/sandbox/tests/conftest.py`).
- The suite must pass with no network beyond its PostgreSQL test database.
- A live smoke test requires the operator's explicit per-instance approval: one tiny document, one cheap model, never a loop or sweep.

## Backend (`apps/api`)

### The suite

- `TEST_DATABASE_URL=postgresql+asyncpg://… uv run pytest`. pytest + pytest-asyncio.
- The suite runs on the PostgreSQL database `TEST_DATABASE_URL` names (a `postgresql+asyncpg://` URL).
- It is the only variable the suite reads to choose a database; the app's `DATABASE_URL` is never read.
- Without it the run stops before collecting anything, with one line saying how to set it (a pytest usage error, exit code 4).
- `TEST_DATABASE_URL` names a dedicated test database, never the app's own: its tables are dropped.
- Its role may create databases: the migration tests make scratch databases on the same server (§ Migrations).
- On the compose `pg` server, make `sightread_test` once and run against it:
  - `docker compose up -d pg`
  - `docker compose exec pg createdb -U sightread sightread_test`
  - `TEST_DATABASE_URL=postgresql+asyncpg://sightread:sightread@127.0.0.1:5432/sightread_test uv run pytest` (the host port is `PG_PORT`).
- Or on a one-off server, removed when it stops:
  - `docker run --rm -d --name sightread-test-pg -e POSTGRES_USER=sightread -e POSTGRES_PASSWORD=sightread -e POSTGRES_DB=sightread_test -p 127.0.0.1:55432:5432 postgres:16`
  - `TEST_DATABASE_URL=postgresql+asyncpg://sightread:sightread@127.0.0.1:55432/sightread_test uv run pytest`
  - `docker stop sightread-test-pg`
- Two runs at once need two databases: a run empties the tables the other is using.
- A run makes the schema once, from a clean database (`Base.metadata.drop_all` then `create_all`), and drops it at the run's end.
- Before each test every table is truncated (`TRUNCATE ... RESTART IDENTITY CASCADE`) and `worker_generation_seq` restarted, so every test starts on empty tables with ids counting from 1.
- The test engine is built with the app's own `sightread.db.session.create_engine`, so the tests have production's session settings (the UTC pin day buckets rely on, `hide_parameters`).
- The test apps' `database_url` is `TEST_DATABASE_URL` too, so their event streams LISTEN for NOTIFY as production's do.
- The test `Settings` put uploads, figures and workspaces under the test's `tmp_path`; no test touches `/data`.
- No test reads a setting from the developer's environment. An autouse fixture in `tests/conftest.py` (`hermetic_settings`) removes every environment variable that names a `Settings` field (matched without regard to case) before each test and clears the `get_settings()` cache.
- So a shell that exported `.env`, or the compose api container's `env_file`, changes no result; each test passes the settings it needs.
- `TEST_DATABASE_URL` is not a `Settings` field; it is read once, at import, before that fixture runs.
- What several test modules share lives in `tests/conftest.py` and the plain helper modules beside it (`helpers.py`, `factories.py` and one `*_helpers.py` per area); no test module imports from another.
- `tests/__init__.py` registers the helper modules for pytest's assertion rewriting, so a failing assert in one shows its values as a test module's does.
- Fixture documents in `tests/fixtures/`: a small text-layer PDF, a scanned-style PDF (rendered-only), a multi-page mix, one corrupt file, tiny jpg/png/webp/heic images. Generate or vendor tiny files — keep fixtures under a few hundred KB each.
- The fixture PDFs are built with `reportlab`, a dev dependency (BSD). The application imports no PDF library ([parsing.md](./parsing.md) § PDF engine: Poppler via subprocess — this is a hard rule).
- Poppler is a real dependency of the test environment. Subprocess wrappers are tested against real fixtures, not mocked.
- The reference environment is a Linux container: `ghcr.io/astral-sh/uv:python3.12-bookworm` with `poppler-utils` and `git` installed. Linux because the suite needs `os.O_PATH` and `/proc/self/fd`. Every run is bounded by `timeout`.
- In that container `TEST_DATABASE_URL` names the server as the container reaches it: the server container's name on a Docker network both join (`docker run --network <network>`), or `host.docker.internal` for a server published on the host (Docker Desktop).

### Migrations

- Migrations are tested as real upgrades (`tests/test_migrations.py`).
- Each test runs on scratch databases it creates on the `TEST_DATABASE_URL` server, and drops them.
- `alembic upgrade head` on one and `Base.metadata.create_all` on another give the same catalog both ways ([database.md](./database.md) § Migrations):
  - every column's type, nullability and default;
  - every constraint by name, through `pg_get_constraintdef`;
  - every index, through `pg_indexes.indexdef`;
  - the sequences.
- The upgraded head holds the seed rows a fresh database needs (the built-in `owner` group), and refuses what its constraints forbid.
- `alembic downgrade base` leaves no table, sequence or type.
- A migration that rewrites existing rows adds a test there. The test upgrades from the previous head with synthetic existing rows in place and checks what it left. It goes at the next squash.

### Must-cover: database connections

[api.md](./api.md) § Database connections.

- A file whose reader stops halfway through its body: with a pool of one connection, the next request still answers.
- No pooled connection checked out by the first byte of a body, on either plane, for a file, an original and a JSON answer.
- No pooled connection checked out halfway through a request body, for the routes that read their own: the Files upload, an inserted image, a chat attachment, `POST /v1/parse` in both forms, the `doc.md` and `notes.md` saves and `POST /oauth/register`. Kano Pay's webhook is not among them.

### Must-cover: the chat agent and the workspace

- The Messages stream parser: text, thinking, tool_use with partial JSON, usage with cache tokens, refusal.
- The tool loop ending on `end_turn`.
- The tool-call cap.
- A cancel, the call it cut short still billed.
- The editor tool refusing a path outside the user's directory and the read-only names.
- A save with a stale `base_revision` answering 409.
- A revert producing a new revision.
- Reconciliation after a rename, a move and a delete in the tree.
- The turn queue's per-user cap.
- The SSE replay from `after`.
- Usage rows carrying `agent`, `turn_id` and the cache tokens.

### Must-cover: retention and deletion

- The Trash's expiry:
  - deleting only what expired;
  - a folder with everything in it, entries, originals and images;
  - a refused user skipped while the others go;
  - nothing at `TRASH_RETENTION_DAYS=0`.
- The parse purge:
  - deleting what the keep rule does not keep, crops and results copy first;
  - keeping a document's parse, a trashed document's, a re-convert in flight, an earlier parse of a document's original, a young parse and a queued or running one;
  - `usage_log` rows and ledger entries left with `job_id` NULL, `spent` unchanged;
  - `/v1/jobs/{id}` 404 after;
  - nothing at `PARSE_RETENTION_DAYS=0`.
- The dedup margin.
- Account deletion:
  - its rows and files gone, and another user's untouched;
  - its refusals: a renewing subscription, Pay unreachable, running work, CSRF;
  - a queued job deleted;
  - the sweep finishing from a marker;
  - Pay's webhook acknowledging a deleted subject.

### Must-cover: the operator console

[admin.md](./admin.md) § Acceptance.

- Access:
  - every `/api/admin` path 404 with `OWNER_EMAILS` empty;
  - 401 for an API key, an OAuth access token and an upload ticket, even beside a session cookie;
  - 403 `permission_denied` without the route's permission, and for each escalation rule;
  - 403 `reauth_required` for a session older than `ADMIN_REAUTH_HOURS`, after the permission check;
  - 409 `admin_conflict` for the built-in group's rules and its last member.
- The owner join:
  - from a verified Google address (the callback driven with its Authlib client patched, no network);
  - never from an unverified one;
  - idempotent;
  - from the dev login.
- One audit row for every mutation, and none for a no-op.
- CSRF on every console mutation.
- The users list's search, plan filter and paging.
- An account's detail, with no content in it.
- The grants:
  - a credit pack spent after the period, with the other packs by expiry;
  - days banked untouched under a paid subscription until it lapses;
  - another plan's days refused;
  - a repeated request id making one pack, or banking the days once.
- The settings in the database:
  - the reading layer's identity without rows, its cache and its fallbacks;
  - a refused change's 422 holding no secret in the answer or the log;
  - the impact confirmation;
  - the worker's snapshot per claim, and a turn's prices kept to its end;
  - the codes CLI reading the database's plans.
- Account deletion removing memberships and keeping audit rows with `actor_id` NULL.
- Redeem codes:
  - the plaintext only in the create answer — never in the list, a record or a log line;
  - the batch rules, `REDEEM_CODES_MAX_COUNT` among them, refused alike by the console (422) and the CLI (exit 2).
- Its jobs and usage:
  - the worker's state read from `pg_locks` against a real advisory lock held by another connection, and never taken;
  - the requeue of a stuck job only, 409 for any other, audited;
  - the jobs list's address only with `users.read`;
  - usage per UTC day and model across a midnight.
- The overview:
  - its connections against stubbed Kano Proxy and Kano Pay — up, down, and answering what their clients do not expect;
  - its audit records only with `audit.read`.

### Must-cover: parsing, the queue, OAuth and MCP

- The operator's key on every upstream call, and never in a response.
- A model outside the deployment's list refused at enqueue and at a send, and never run by a retry (a retry naming none runs on `PARSE_MODEL` when the job's model was withdrawn, and drops a withdrawn fallback).
- Dedup never crossing agents.
- Dedup never answering from a row with no endpoint snapshot (`connection_base_url` NULL: OpenRouter and provider-connection history), and such a row failing at claim rather than running on Kano Proxy.
- An unconfigured Kano Proxy or parse model refused at enqueue.
- Bbox placeholder emission.
- Page markers.
- Dedup key matching + `force`.
- Per-user job cap 429.
- SKIP LOCKED claim under two concurrent workers.
- Source-file deletion at terminal state.
- Sweeper.
- OAuth AS happy path (DCR → PKCE → token → `/mcp` auth).
- 402/429 upstream handling.
- The connector's parity with a Files upload:
  - a `/v1/parse` checked when the switch is on, and `markdown`/`result.md` the reading copy;
  - the figure crop routes, by id and by `last`;
  - `retry` keeping succeeded pages, answered by a newer parse of the file (a Files upload's included), and its 409/400/402 refusals;
  - a second upload joining a parse in flight, even at the plan's cap;
  - a multipart flag that is no bool refused;
  - `last` on an unspent ticket answering 409;
  - the connector's shell commands run in bash, dash and zsh against a stub server when they change.
- MCP tools end to end through the official SDK client over ASGI. The app lifespan does not run under `ASGITransport`, so `/mcp` tests start the SDK session manager themselves.

### Must-cover: the library on the data plane and its tools

[api.md](./api.md) § The library on the data plane, [mcp.md](./mcp.md) § The library tools.

- The live library only: the Trash and what is under a trashed folder never listed or served.
- Another account's document 404.
- A document ticket reading only its own document, and refused on `GET /v1/library`, the job routes, `/v1/parse` and `/mcp`.
- A `read` ticket refused on a save.
- A new grant holding all three scopes, and a refresh keeping the scope it had.
- The upgrade revoking a grant without `library` and deleting an unspent ticket minted under one, and the upgrade revoking a grant without `organize` (tests/test_migrations.py).
- A save:
  - without `If-Match`: 428;
  - with a stale one: 409 `revision_conflict`, the file untouched;
  - with a good one: a revision authored `api`, with the new ETag.
- The **download** and **save** commands, when they change, run in bash, dash and zsh against a running app:
  - crops and inserted images landing under `figures/`;
  - a save following a save;
  - a conflict leaving the local file and its ETag as they were.

### Must-cover: organizing the library through the connector and the data plane

[mcp.md](./mcp.md) § The organizing tools, [api.md](./api.md) § The library on the data plane.

- Each tool through the SDK client, and its route, doing what the Files page does:
  - a move into a folder, a taken name suffixed, the undo record naming both names;
  - a rename, a collision refused;
  - a new folder with the folders on its way, one already there reported;
  - a copy;
  - a trash and a put back by the path the Trash listing gives.
- A move into a folder that does not exist refused, nothing made.
- A batch doing what it can: one item refused, the others done, the call not an error; every item refused, an error (a tool error, the first refusal's status on the route).
- `undo` of a suffixed move putting the item back under its old name, of a trash putting it back, of a new folder trashing it; `redo` doing the step again; a record that is not one refused with nothing changed.
- Another account's items unreachable, by path and by an undo record's ids.
- A document ticket refused on `GET /v1/library/trash` and the organizing routes.
- The commits authored `api`.
- The Files Agent's library tools answering as before (tests/test_chat_library.py).

### Must-cover: filing a data-plane parse into Files

[mcp.md](./mcp.md) § Filing into Files.

- The `parse` tool's `folder`:
  - found as listed;
  - the missing names made and reported;
  - a name a document holds refused;
  - `/` the top level.
- The ticket filing into its folder, with the original kept and a document row as the Files upload makes.
- A file parsed without a folder, filed by a cache hit with nothing charged.
- No twin for the same parse in the same folder.
- `pages` refused.
- `folder` with a ticket refused.
- A durable credential's `folder`.
- A trashed destination refused, with the ticket unspent.
- A filed parse's failure retried from its original through a fresh ticket.
- `retry` on a filing ticket refused, with the ticket unspent.
- A second retry of a document already put back answered with it.
- A filing refused after a cache hit (`storage_full`) leaving the ticket unspent, so the read fails.
- A path with `..` or past `FOLDER_PATH_DEPTH_MAX` refused, with nothing made.
- A rate-refused `parse` making no folder.

## Frontend (`apps/web`)

- `pnpm test` (vitest) covers the framework-free modules (`app/**/*.test.ts`, `shared/**/*.test.ts`): no browser, no Nuxt runtime, so no composable is unit tested.
- `pnpm typecheck` and `pnpm lint` are part of done-criteria.
- The en and zh-TW catalogs are checked whole (`app/lib/catalog.test.ts`):
  - every leaf is a string;
  - both have the same keys;
  - each key has the same `{placeholder}` names in both (plural forms included);
  - no zh-TW value holds a Simplified-only character.
- Domain tests (`lib/admin.test.ts` and the like) check that the words their lists need exist in English; the parity check carries that to zh-TW.
- No browser-automation UI verification: correctness is judged by reading code and types.
- The PDF viewer (`app/lib/pdf/`, [pdf-viewer.md](./pdf-viewer.md) § Testing):
  - what runs without a browser is unit tested like any other module — for the device check, `probe.test.ts`: the limits kept, the pixel comparison, what a read says of a drawing, the copied report's exact lines, and a label for every check, every note and every way a canvas is drawn;
  - the stage 1 viewer's pure modules, each with its test beside it: `zoom.test.ts` (the clamp, the ladder, fit width and fit page from a page size, the opening zoom, a phone's multiples, the anchored scroll of a zoom commit, the checked preset), `layout.test.ts` (the page boxes, the current page and its tie, the render window, the correction for a size change above the viewport), `budget.test.ts` (the cap, the backing size, the detail canvas's part, what the total budget releases), `page-input.test.ts` (parsing, clamping, commit and cancel for Enter and for leaving), `errors.test.ts` (a failure classed by its exception and status), `links.test.ts` (the URL filter, a link's box, an explicit destination) and `support.test.ts` (the build probe);
  - the stage 3 scene's pure modules in `app/lib/pdf/scene/`, each with its test beside it:
    - `ops.test.ts`: the codes against the stored fixtures and the installed version;
    - `matrix.test.ts`;
    - `clips.test.ts`: interning and boxes;
    - `state.test.ts`;
    - `text.test.ts`: glyph origins for each text-state operator;
    - `scene.test.ts`: the layout, interning and the tree;
    - `coverage.test.ts`: every op classed;
    - `adapter.test.ts`: each fixture page well formed, deterministic and the same in slices;
    - `slice.test.ts`;
    - `check.test.ts`: the grid and text comparison, with pdf.js's own boxes and text in each fixture as the oracle, and a mutation that must fail;
    - `fixture.test.ts`;
  - stored operator-list fixtures (`app/lib/pdf/scene/fixtures/`):
    - they are made by `pnpm fixtures:oplists`, a developer script that runs pdf.js in Node 24 or later (it refuses an older one, whose glyph outlines differ in precision) on PDFs written by `scripts/pdf-fixtures/`;
    - no test runs pdf.js or reads a PDF;
    - they are captured again after a `pdfjs-dist` upgrade, and `ops.test.ts` fails until they are;
  - `pnpm scene:check <file.pdf>` runs the same comparison on any PDF on disk, with pdf.js in Node ([pdf-viewer.md](./pdf-viewer.md) § Stage 3: the scene check). It is a developer script, not a test;
  - none of them imports `pdfjs-dist`, and no test reaches the network or fetches or reads a PDF;
  - the licence check (`app/lib/`), which reads the installed tree of the viewer's libraries and fails on a licence [pdf-viewer.md](./pdf-viewer.md) § Licences does not allow. It also reads each `scene/` file whose header says *Ported from*, and checks that it names an allowed source, its licence, the revision, what changed and its section of `THIRD_PARTY_NOTICES`;
  - the catalog check above covers the `viewer.pdf` keys with no test of their own;
  - what the stage 1 viewer does in a browser is checked by hand on the owner's Mac and iPhone with [pdf-viewer.md](./pdf-viewer.md) § Stage 1: the Canvas 2D viewer, *The device test list*. The rule above, no browser automation, stands;
  - what needs a GPU cannot run in vitest. It is checked by hand on real devices, Safari on a Mac and on an iPhone first, through the device check at `/pdf-probe`.
- The media viewer (`app/lib/media/`, [media-viewer.md](./media-viewer.md) § Testing):
  - its pure modules, each with its test beside it: `image-zoom.test.ts` (fit to window from a pane and an image, never above 100%; the range on desktop and on a phone; a menu choice and a phone multiple; the ladder from a fit between rungs; a pinch clamped to the range; a double click and a double tap; a resize at fit and at a custom zoom; which choice is checked; the image box, its place on the stage and the scroll that keeps a point under the anchor), `player.test.ts` (the video box in a landscape and a portrait pane, with and without a gutter, and a seek kept inside the video), `tracks.test.ts` (an MP4 or QuickTime box tree built in the test: a video track found or not, a movie header at the end asking for the next read, a header too large, a WebM refused) and `failure.test.ts` (a failure classed by the one-byte read's status, its rejection, the browser being offline, the `MediaError` code and whether the metadata had come);
  - the helpers it adds have their tests where their modules' are: `mediaKey` in `shortcuts.test.ts`, `formatDuration` in `format.test.ts`, `probeFileStatus` and `readFileBytes` with a stubbed `fetch` in `api.test.ts`, the Tab trap's selector naming `video[controls]` and `audio[controls]` in `focus.test.ts` (the suite has no DOM, so the selector is what it can test), and an SVG's kind in `library.test.ts`;
  - no test loads an image or a video, or reaches the network;
  - the catalog check above covers the `viewer.image`, `viewer.video`, `viewer.audio` and `viewer.media` keys;
  - what the image view and the player do in a browser is checked by hand on the owner's Mac and iPhone with [media-viewer.md](./media-viewer.md) § The device test list. The rule above, no browser automation, stands.

## Lint/format

- Python: `uv run ruff check` + `ruff format --check`.
- TypeScript: eslint with the `@nuxt/eslint` defaults (`eslint.config.mjs` is `withNuxt()`); the web app has no Prettier.
