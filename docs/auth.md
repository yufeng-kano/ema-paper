# Auth

Six credential kinds, strictly separated. No password login anywhere.

## 1. Web sessions — Google OIDC only

- `GET /api/auth/login` → Google (Authorization Code + PKCE). The callback creates or updates the `users` row and creates a server-side session row.
  - The `users` row is keyed by Google `sub`; email, name and avatar `picture` URL are refreshed on each sign-in.
  - The cookie is `HttpOnly; Secure; SameSite=Lax`. Its value is a random token stored **hashed** in `sessions`.
- **The locale rides along.** `?locale=` is parked in the transient signed cookie, and the callback returns the visitor to `WEB_URL/<locale>` — `/en` or `/zh-TW`, every locale having a prefix ([web.md](./web.md) § Search engines, *The locale in the URL*).
  - The web app's locale otherwise lives in its URL; Google is the one leg that drops it.
  - The locale is honoured only when it is one of the web app's locales, `en` or `zh-TW` (`WEB_LOCALES` in `apps/api/src/sightread/config.py`, mirroring `PAGE_LOCALES` in `apps/web/shared/seo.ts`). That is an allowlist, not a path fragment, so a crafted `?locale=` cannot become an open redirect.
  - Anything else returns the visitor to `WEB_URL` itself, whose bare root picks the language from the browser.
  - A connector flow's parked `/oauth/authorize` still wins over it.
- Logout deletes the session row.
- Sessions expire (30 d) and are revocable server-side.
- Deleting the account deletes every session of it (§ Deleting the account).
- **Local dev only:** `AUTH_DEV_MODE=true` **and** `APP_ENV=local` enables a `POST /api/auth/dev-login` that signs in as `dev@localhost` (returns `{user: {id, email}}`).
  - The route must hard-refuse to exist when `APP_ENV != local`.
  - It is CSRF-guarded like every mutation, which lets the web app probe for it without creating a session: no header → 403; absent → 404.
- **Owners join at sign-in** ([admin.md](./admin.md) § Groups). When the callback's ID token says `email_verified: true` and the address — trimmed, in any letter case — is one of `OWNER_EMAILS`, the account joins the operator console's built-in owner group, with its audit row.
  - The join happens in the transaction that creates the session: one commit for the user, the session and the membership.
  - A join already made changes nothing, and two sign-ins at once never fail.
  - An unverified address never joins. Any Google account may still sign in; the check is the owner join's alone.
  - The dev login does the same for `dev@localhost`, counted as verified — it exists only with `APP_ENV=local` — so `OWNER_EMAILS=dev@localhost` makes the local user an owner.
  - Taking an address out of `OWNER_EMAILS` revokes nothing.
- **The operator console takes the session and nothing else** ([admin.md](./admin.md) § Signing in).
  - Every `/api/admin` route answers **401** to a request that carries an `Authorization` header — an API key, an OAuth access token, an upload ticket — even beside a valid session cookie.
  - Every `/api/admin` route but `GET /api/admin/me` answers **403** `reauth_required` once the session is older than `ADMIN_REAUTH_HOURS` (12). `GET /api/admin/me` answers instead, with `reauth_required: true` in its body.
  - The age is measured from `sessions.created_at`, which the sign-in sets and nothing renews.
  - Signing in again creates a new session and starts the window over. The old session keeps working everywhere else until its own expiry, unless the web app signs it out first, which its *Sign in again* does.
  - Google is not asked to prompt again, so the window bounds what an old cookie can do, not what a stolen Google account can.
- `GET /api/me` carries `console`: true while the console is on and the account holds any of its permissions, with no window. That is all the account menu needs to offer it.

## 2. Project API keys (data plane)

- Format `sr_<32 random url-safe chars>`.
- Shown exactly once at creation.
- Stored as SHA-256 hash + display prefix (`sr_...abc4`).
- Sent as `Authorization: Bearer sr_...` on `/v1/*`. Looked up by its SHA-256 hash.
- Revocation = soft delete.

## 3. The upstream key — the operator's, never a user's

Every upstream call runs on the operator's own Kano Proxy key, `KANO_PROXY_API_KEY` in the server's `.env` ([billing.md](./billing.md) § Models and the operator's upstream, [deployment.md](./deployment.md)).

- The API and the worker read it from configuration and present it as `Authorization: Bearer`. A call to the Anthropic-format route (the `chat` and `check` agents) carries it as `x-api-key` as well.
- It is never stored in the database, never returned by a route, never logged.
- Users hold no upstream credential of their own.
- `auth/crypto.py` holds only two things:
  - the token hashing every stored credential uses (sessions, API keys, OAuth grant tokens, upload and document tickets);
  - the minting of the prefixed `sr_` / `srt_` / `srd_` tokens.

## 4. OAuth 2.1 authorization server — for Claude Connectors

Claude custom connectors assume OAuth 2.1 on remote MCP servers and attempt Dynamic Client Registration. Tokens in query strings are prohibited. So this app is also a minimal OAuth AS (Authlib):

- Discovery: `/.well-known/oauth-authorization-server` (RFC 8414) and `/.well-known/oauth-protected-resource` (RFC 9728, pointing at `/mcp`). The latter is also served under its path-suffixed form `/.well-known/oauth-protected-resource/mcp`, where a client that starts from `<origin>/mcp` looks first.
- `POST /oauth/register` — open DCR (RFC 7591). Redirect URIs are restricted to `https://`, plus `http://localhost` and `http://127.0.0.1` when `APP_ENV=local`.
- `GET /oauth/authorize` — requires a web session (redirects to Google login and back if absent), renders the consent page, and issues a short-lived code bound to PKCE S256.
  - The consent page is server-rendered by FastAPI, not Nuxt.
  - It is one card carrying its own styles, mark and icon links inline. It may open in a stripped-down connector webview, so it makes no external request and cannot half-load while asking for account access.
  - Its palette is a narrowed copy of the web app's tokens ([web.md](./web.md)) — duplicated deliberately, since FastAPI cannot import the Nuxt stylesheet and consent must not depend on the web app being up.
  - English only; it is the one user-facing surface outside the i18n catalog.
- `POST /oauth/token` — code + PKCE → access token (opaque random, stored hashed, 1 h) + refresh token (30 d, rotating).
  - Access tokens authenticate `/mcp` and `/v1/*` like an API key, resolved to the same user.
  - Every live grant holds all three scopes (below), so a token reaches what an API key reaches, and no request is checked against a scope.

Result: adding the connector in Claude is "paste URL → Google login → consent" — no manual key copying.

Implementation notes:

- **Public clients only.** DCR issues no client secret, and `token_endpoint_auth_method` is `none`.
  - PKCE S256 is the only client authentication. That is what Claude does anyway, and it leaves no shared secret to leak.
  - Authlib supplies the PKCE primitives. Its Flask/Django authorization-server framework is sync, so the endpoints themselves are plain FastAPI routes over `oauth_grants`.
- **Three scopes, `parse`, `library` and `organize`.**
  - `parse` is the `parse` tool and the job routes.
  - `library` is reading the Files library and saving a document's text and notes — the library tools of the connector ([mcp.md](./mcp.md)) and `/v1/library*` ([api.md](./api.md) § The library on the data plane).
  - Filing is part of `library` ([mcp.md](./mcp.md) § Filing into Files): it adds documents and folders to the reader's own library and removes or overwrites nothing.
  - `organize` is putting the Files library in order: making folders, moving, renaming and copying items, moving them to the Trash and putting them back — the connector's organizing tools and `POST /v1/library/<tool>` ([mcp.md](./mcp.md) § The organizing tools). It is a scope of its own because it moves the reader's items and sends them to the Trash, which `library` promised it would not. Nothing it does deletes for good.
  - A grant is made for all three, whatever the client asks: `scopes_supported` lists them, and the token answer names what was granted. The reason is that the consent page says all three: *It will be able to submit documents and file them into your Files folders, read your Files library, save the text and notes of its documents, and organize your Files: make folders, move, rename and copy items, move them to the Trash and put them back. It cannot delete anything for good, or reach your settings, API keys or billing.*
  - A refresh keeps the scope of the grant it rotates, so every live grant holds all three scopes. A scope is never widened without the consent page.
  - So a grant approved before `organize` existed is revoked, with its codes, access and refresh tokens, by a data-only migration ([database.md](./database.md) § Migrations): its connector is answered 401 and goes through the consent page again, once. The grants approved before `library` existed went the same way.
  - An API key carries all three, as the account's own.
- **Error shapes.** `/oauth/register` and `/oauth/token` answer in RFC form (`{"error": "...", "error_description": "..."}`), because OAuth clients parse that.
  - The app's own error envelope stays on `/v1` and `/api`.
  - `/oauth/authorize` answers a browser, so it renders HTML or redirects.
- **Codes** live 5 min and are single use.
  - Redeeming a code or a refresh token locks its row, so two requests racing with one get one token pair; the other is answered `invalid_grant`.
  - A code is bound to client, user, PKCE challenge and the redirect URI it was issued for (`oauth_grants.redirect_uri`). A token request that sends `redirect_uri` must send that one; one that omits it is not checked against it.
  - A refused client or redirect URI never redirects — it renders an error page.
- **Consent CSRF**: the consent form carries a one-time token stored in the short-lived signed cookie; a cross-site form post cannot produce it.
  - That same cookie parks the authorize request while the user signs in with Google.
  - Only a path under `/oauth/authorize` is resumed.
- **Without Google**: with no Google client configured, `/oauth/authorize` answers 401 with a page instead of redirecting to a Google client that does not exist. Where the dev login exists (`AUTH_DEV_MODE=true` and `APP_ENV=local`), the page tells the operator to sign in through the web app's dev login first; anywhere else it says sign-in is unavailable.
- **`resource` (RFC 8707)** is accepted and ignored: this AS protects exactly one resource, `APP_URL + /mcp`.

## 5. Upload tickets — single-use, job-scoped (the MCP `parse` tool)

A ticket lets an agent do exactly one upload — a parse, or the retry of a failed one — and then read the job that upload created, and nothing else. Only the MCP `parse` tool mints one ([mcp.md](./mcp.md)). The point is that an OAuth-connector agent never holds a durable credential, and a leaked ticket is worth one upload to one account for an hour, not an API key.

- Format `srt_<32 random url-safe chars>`; stored as a SHA-256 hash in `upload_tickets` ([database.md](./database.md)). Returned in the tool result exactly once.
- **Lifetime** `UPLOAD_TICKET_TTL_SECONDS` (default 3600). One TTL covers the upload and the reads.
  - Recovery from an expired ticket is minting a new one and re-uploading.
  - Dedup makes that free while the parse runs or is kept ([jobs.md](./jobs.md) § Dedup cache, [jobs.md](./jobs.md) § Dedup margin).
  - A failed parse is resumed by the retry upload ([jobs.md](./jobs.md) § Retrying failed pages).
- **Scope.**
  - An unspent ticket authenticates `POST /v1/parse` once.
  - When the upload creates a job (or hits the dedup cache, joins a parse in flight, or retries a failed one), the ticket binds to that `job_id` and is marked spent.
  - Two uploads racing on one ticket: the first to spend it gets its parse; the other gets the 401 below, with nothing queued, charged or kept.
  - From then on it authenticates only `GET /v1/jobs/{id}`, `/result`, `/result.md`, `/events` and `/figures/…` for that one job, and the `last` forms of all but `/events` (`/v1/jobs/last`, `…/result`, `…/result.md`, `…/figures/…`).
  - A request that fails before a job exists (413, 400, 402, 409, 429) does **not** spend the ticket.
  - `last` on an unspent ticket answers **409**: the ticket has created no parse yet (its upload still arriving, refused, or never sent), and a refused or unsent one can still upload. It does not answer with the expired copy below, which would send the agent to mint again for an upload that is still refused.
  - Every other endpoint — `/mcp` included — refuses `srt_` tokens.
- **Rejection copy is part of the contract.** A spent/expired/unknown ticket gets 401 with: `Upload ticket expired or already spent — call the parse tool again for a fresh ticket. Re-uploading the same file joins its parse while it runs and returns it once it has succeeded; a failed parse is resumed with the retry command.` That message is the agent's only recovery hint, so it lives in the response, not just here.
- **Mint rate limit:** `UPLOAD_TICKET_RATE_PER_HOUR` (default 30) per user; exceeding it is a 429-shaped tool error. Minting also deletes that user's expired tickets older than the rate window, so an expired ticket still counts for its hour: opportunistic cleanup, no sweeper involvement — the table stays small because every mint tidies up.
- **Where the upload goes.**
  - When the `parse` tool was given a `folder`, the ticket records that the upload is filed into Files (`into_library`) and where (`folder_id`, NULL the top level).
  - The destination is chosen at the mint, so the upload's own form cannot widen it: `folder` sent with a ticket is **400**.
  - Every ticket's `retry` retries a failed Files document where it is ([api.md](./api.md) § POST /v1/parse).
  - A ticket with `into_library` takes no `retry` (**400**, unspent), since a retry files nothing.
  - A filing refused after the intake gives the ticket back, unspent and unbound, unless another upload spent it on its own job meanwhile.
  - The folder is a foreign key `ON DELETE SET NULL`. A folder deleted for good before the upload leaves the ticket filing at the top level. A folder in the Trash refuses the upload with **404**, the ticket unspent.
- Never in query strings, like every other bearer.

## 6. Document tickets — short-lived, one document (the MCP library tools)

The connector's library tools hand the agent `curl` commands, as `parse` does, and the commands need a credential the agent can hold: a **document ticket**, for one document of the caller's. Only the `read_document` and `write_document` tools mint one ([mcp.md](./mcp.md) § The library tools). So an OAuth connector still never holds a durable credential, and a leaked ticket is worth one document for an hour.

- Format `srd_<32 random url-safe chars>`; stored as a SHA-256 hash in `document_tickets` with the `document_id` and the `scope` ([database.md](./database.md)). Returned in the tool result exactly once.
- **Scope.**
  - A `read` ticket authenticates the `GET` routes under `/v1/library/documents/{id}` of its own document: the row, `doc.md`, `notes.md`, the crops, the inserted images, the original and a library file's bytes.
  - A `write` ticket adds the two saves, `PUT …/doc.md` and `PUT …/notes.md`.
  - Neither is spent by use: within its lifetime it serves any number of requests, so a download fetches every crop on one ticket.
  - A ticket that expired, names another document, or is a `read` ticket on a save gets 401 *Document ticket expired or for another document — call read_document (or write_document to save) again for a fresh one.*
  - `GET /v1/library`, `GET /v1/library/trash` and the organizing routes (`POST /v1/library/<tool>`) refuse a ticket with a 401 that says to use an API key or the connector's tools: a ticket never lists the library or changes where anything is.
  - Every other endpoint — the job routes, `/v1/parse`, `/mcp` — refuses `srd_` as any unknown bearer (401; `/v1/jobs/last*` its 400 for anything but an upload ticket).
- **The ticket only names what the account may reach.** Each request still resolves the document as the account's own and live. A document put in the Trash after the mint answers 404. A document deleted for good takes its tickets with its row, and the ticket then gets the 401 above.
- **Lifetime** `DOCUMENT_TICKET_TTL_SECONDS` (default 3600). Recovery from an expired ticket is calling the tool again. The revision a save sends is the file's content hash, not the ticket's, so a copy downloaded on one ticket saves on another.
- **Mint rate limit:** `DOCUMENT_TICKET_RATE_PER_HOUR` (default 120) per user, counted apart from upload tickets; exceeding it is a 429-shaped tool error. Minting also deletes that user's expired document tickets older than the rate window, as upload tickets tidy up.
- Never in query strings, like every other bearer.

## Deleting the account

A user deletes their own account from the settings dialog's *Account* section ([web.md](./web.md) § Deleting the account): `DELETE /api/account` (session + CSRF). It answers **204** with the session cookie cleared, as logout clears it.

Everything of the account is gone at once: its rows, its files, its sessions on every device (their next request answers 401) and its connectors' tokens. There is no grace period and no undo; the confirmation is the dialog's.

One request does all of it, in this order:

1. **Kano Pay is asked first**, before any lock, so no row waits on the network: with Pay configured the subscription is read fresh (`GET /v1/subscriptions/:subjectId`, the canonical fetch the webhook makes).
2. **The workspace lock**, which takes the backup lock shared ([workspace.md](./workspace.md) § Backups), so no write of the user's lands in between and a running backup is waited for. A workspace recovering from an unconfirmed shell command answers its 409, as every workspace route does.
3. **The user row, `FOR UPDATE`.**
4. **A subscription that still renews refuses**: **409** `subscription_active`, *Cancel your subscription first*.
   - A subscription renews while its `status` is not `canceled`, `expired` or `failed` and `cancel_at_period_end` is false. One set to end with its period is no obstacle.
   - The reason: Kano Pay has no cancel endpoint, the portal opens only from the account ([billing.md](./billing.md) § Kano Pay), and a later account is a new subject, so a subscription left renewing could never be cancelled from the app again. The reader cancels in the portal first.
   - Pay's answer from step 1 is written back as the webhook writes it — the newer version wins — and the stored row then decides. So a cancellation made a minute ago counts, and so does a notification written while the request waited for the locks.
   - Pay answering no subscription leaves the stored row as it is.
   - With Pay unreachable, a user who has a `subscriptions` or a `checkouts` row is answered **503** `pay_unavailable` — nothing can say the subscription ended — and one who has neither goes on.
   - With Pay unconfigured, the stored row decides.
5. **A checkout that can still be paid refuses**: **409** `checkout_open`, *A checkout you started can still be paid*.
   - It refuses with Pay configured, when the user's `checkouts` row is not completed and younger than `KANO_PAY_CHECKOUT_LIFETIME_SECONDS` (86400, Pay's checkout session lifetime).
   - The reason: paid after the account is gone, it would start a subscription for a subject no one can sign in as again.
   - Pay's answer in step 4 marks a paid checkout completed ([billing.md](./billing.md) § Kano Pay), so a paid one refuses only as its subscription does.
   - A `pack_checkouts` row refuses nothing: a pack paid after the account is gone renews nothing ([billing.md](./billing.md) § Kano Pay).
6. **Running work refuses**: **409** `invalid_request`, *A parse or an answer is still running; try again when it finishes*.
   - Running work is any job of the user `running`, or any chat turn of the user unfinished and not `queued` (`finished_at` NULL with `running`, or `cancelled` and not yet wound down by the worker).
   - The reason: it would go on writing files and usage as the account goes.
   - The user's `queued` jobs and turns are locked `FOR UPDATE` — the claim's `SKIP LOCKED` then passes them by — and go with the account, their credit reservations with the ledger.
7. **The sandbox VM is stopped** when a runner is configured (`POST /sandboxes/{user_id}/stop`, [sandbox.md](./sandbox.md)), best effort: a failure is logged and the deletion goes on.
8. **The marker.** The user's job ids and chat-attachment ids are collected, and `{WORKSPACES_DIR}/.repos/{user_id}.deleted` is written holding the job ids (JSON).
   - It is written whole, flushed to disk and renamed into place, its directory flushed after it, since the commit that follows is durable.
   - So a process that dies after the commit leaves the worker's sweep what it needs to finish ([workspace.md](./workspace.md) § Layout).
9. **The `users` row is deleted and the transaction committed.**
   - Every table with a `user_id` cascades from it, its group memberships of the operator console included.
   - `pay_events` keep their rows with `user_id` NULL, the console's audit rows keep theirs with `actor_id` NULL, and `oauth_clients`, shared, stay ([database.md](./database.md) § Rules).
   - Membership refuses nothing: deleting the last owner's account leaves the owner group empty until an `OWNER_EMAILS` address signs in again ([admin.md](./admin.md) § Groups).
10. **The files go**: the user's tree `{WORKSPACES_DIR}/{user_id}`, the git directory `.repos/{user_id}`, the originals `.repos/{user_id}.sources`, the images `.repos/{user_id}.images`, the lock files, the crop directories of every collected job (`FIGURES_DIR`) and the upload-directory files of those jobs and of the chat attachments; then the marker.
    - Every removal goes through `safe_io`'s descriptors and follows no link ([workspace.md](./workspace.md) § Revisions).
    - A failure — a crop directory that will not go included — is logged, not answered, and keeps the marker: the rows are gone, and the sweep finishes the files.
11. **The user's Pay refresh stamp is dropped** from the API's memory ([billing.md](./billing.md) § Kano Pay, *Refresh*), and the answer is 204 with the cookie cleared.

- **The sweep finishes an interrupted deletion.** Every sweep ([jobs.md](./jobs.md) § Retention) lists `.repos/*.deleted`.
  - For a marker whose user row is gone, it removes what step 10 removes, the crops and uploads of the job ids the marker names included, and then the marker.
  - A marker whose user row still exists — the commit never landed — is removed alone, and the account stands.
  - A marker that cannot be read (empty, or not a JSON list of ids) is logged and read as naming no jobs: the account's directories still go, and the crops of its jobs are left for the operator.
  - Nothing else in the sweeper deletes a user's files by inference. So a process that was already waiting for the user's workspace lock when the account went — a sweep that listed the user before the commit — creates an empty `.repos/{user_id}.lock` again as it takes the lock, and that file stays.
  - A chat attachment's file left behind is the upload directory's, which the same sweep empties of anything older than `UPLOAD_MAX_AGE_SECONDS`.
- **Kano Pay goes on writing.** A deleted account's subscription still sends its ending events, and Pay retries a refused delivery for ever. So the webhook acknowledges an event whose subject has no account (200 `{}`) and records nothing. It is logged as a warning, since a deleted account's subscription that still bills is the operator's to cancel or refund in Pay ([billing.md](./billing.md) § Kano Pay).
- **Signing in again** with the same Google account later makes a new, empty account — a new `users.id`, never the old one — on Free from zero.
- **Backups.** The production backups keep the newest seven daily stamps ([deployment.md](./deployment.md)). The account's data leaves them as they rotate out, about a week later: a stamp goes when its seventh successor is published, and successive stamps are a day plus a pass apart.

## Logging rule

Log key **ids/prefixes** and auth outcomes, never credential material. None of these appears in logs or exceptions:

- session tokens;
- API keys;
- OAuth codes and tokens;
- upload and document tickets;
- client secrets;
- the operator's `KANO_PROXY_API_KEY`.
