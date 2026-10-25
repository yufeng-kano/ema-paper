# Auth

Six credential kinds, strictly separated. No password login anywhere.

## 1. Web sessions — Google OIDC only

- `GET /api/auth/login` → Google (Authorization Code + PKCE); callback creates/updates the `users` row (keyed by Google `sub`; email, name and avatar `picture` URL are refreshed on each sign-in) and a server-side session row; cookie is `HttpOnly; Secure; SameSite=Lax`, value is a random token stored **hashed** in `sessions`.
- The locale rides along: `?locale=` is parked in the transient signed cookie and the callback returns the visitor to `WEB_URL/<prefix>`. The web app's locale otherwise lives in its URL, and Google is the one leg that drops it. The prefix is only honoured when it is in `WEB_LOCALE_PREFIXES` — an allowlist, not a path fragment, so a crafted `?locale=` cannot become an open redirect. A connector flow's parked `/oauth/authorize` still wins over it.
- Logout deletes the session row. Sessions expire (30 d) and are revocable server-side. Deleting the account deletes every session of it (§ Deleting the account).
- Local dev only: `AUTH_DEV_MODE=true` **and** `APP_ENV=local` enables a `POST /api/auth/dev-login` that signs in as `dev@localhost` (returns `{user: {id, email}}`) — the route must hard-refuse to exist when `APP_ENV != local`. It is CSRF-guarded like every mutation, which lets the web app probe for it without creating a session (no header → 403; absent → 404).

## 2. Project API keys (data plane)

- Format `sr_<32 random url-safe chars>`; shown exactly once at creation; stored as SHA-256 hash + display prefix (`sr_...abc4`).
- Sent as `Authorization: Bearer sr_...` on `/v1/*`. Constant-time lookup by hash. Revocation = soft delete.

## 3. The upstream key — the operator's, never a user's

Every upstream call runs on the operator's own Kano Proxy key, `KANO_PROXY_API_KEY` in the server's `.env` ([billing.md](./billing.md) § Models and the operator's upstream, [deployment.md](./deployment.md)): the API and the worker read it from configuration and present it as `Authorization: Bearer`; it is never stored in the database, never returned by a route, never logged. Users hold no upstream credential of their own any more — the per-user Kano Proxy key the connect flow used to obtain and the user-defined provider connections were removed by step 4 of [billing.md](./billing.md) § Transition, their tables with them. The encryption helpers went with them: `auth/crypto.py` keeps only the token hashing sessions, API keys and upload tickets use.

The rules the removed keys followed, kept here for the record because the next secret that must be *replayed* (not merely verified) follows them again:

- AES-256-GCM, key derived from `SECRET_KEY` via HKDF (context strings `kano-proxy-key-v1` and `provider-connection-key-v1`), random nonce per encryption.
- The Kano Proxy key is not validated separately: it arrives from Kano Proxy's own token endpoint in exchange for a code the user just approved. A connection's key is validated at save time with `GET {base_url}/models` on its own endpoint; invalid → 400, nothing stored. A connection's `base_url` must be `http(s)`, must never carry userinfo (`user:pass@` — the URL is stored and displayed in plaintext), and outside `APP_ENV=local` must be `https` and must not denote a non-public address (the app fetches it server-side). Host literals are canonicalized before the check — the shortened/decimal/hex IPv4 spellings the resolver accepts (`127.1`, `2130706433`, `0x7f000001`) are judged by the address they denote, not their text — and a hostname is **resolved** at save time (`getaddrinfo`), every resolved address required to be global, which rejects `/etc/hosts` aliases (`ip6-localhost`) and attacker domains pointing into internal space. Residual, accepted: a domain whose DNS answer changes *after* validation (rebinding) is not re-checked at call time — full protection needs request-time resolve-and-pin, revisit if the threat model hardens. A malformed URL (bad IPv6 bracket syntax included) is a 400, never a 500.
- API/UI only ever return the masked form (`sk-kano-…abcd`). The plaintext exists in memory only for the duration of an upstream call (and, for the Kano Proxy key, between the token response and its encryption). **Never logged, never in error messages.**

## 4. OAuth 2.1 authorization server — for Claude Connectors

Claude custom connectors assume OAuth 2.1 on remote MCP servers and attempt Dynamic Client Registration; tokens in query strings are prohibited. So this app is also a minimal OAuth AS (Authlib):

- Discovery: `/.well-known/oauth-authorization-server` (RFC 8414) and `/.well-known/oauth-protected-resource` (RFC 9728, pointing at `/mcp`).
- `POST /oauth/register` — open DCR (RFC 7591), redirect URIs restricted to `https://` (plus `http://localhost` for local).
- `GET /oauth/authorize` — requires a web session (redirects to Google login and back if absent), renders the consent page (server-rendered by FastAPI, not Nuxt), issues a short-lived code bound to PKCE S256. The page is one card carrying its own styles, mark and icon links inline: it may open in a stripped-down connector webview, so it makes no external request and cannot half-load while asking for account access. Its palette is a narrowed copy of the web app's tokens ([web.md](./web.md)) — duplicated deliberately, since FastAPI cannot import the Nuxt stylesheet and consent must not depend on the web app being up. English only for now; it is the one user-facing surface outside the i18n catalog.
- `POST /oauth/token` — code + PKCE → access token (opaque random, stored hashed, 1 h) + refresh token (30 d, rotating). Access tokens authenticate `/mcp` and `/v1/*` exactly like an API key, resolved to the same user.

Result: adding the connector in Claude is "paste URL → Google login → consent" — no manual key copying.

Implementation notes (as built):

- **Public clients only.** DCR issues no client secret and `token_endpoint_auth_method` is `none`; PKCE S256 is the only client authentication, which is what Claude does anyway and leaves no shared secret to leak. One scope, `parse`. Authlib supplies the PKCE primitives; its Flask/Django authorization-server framework is sync, so the endpoints themselves are plain FastAPI routes over `oauth_grants`.
- **Error shapes.** `/oauth/register` and `/oauth/token` answer in RFC form (`{"error": "...", "error_description": "..."}`) because OAuth clients parse that; the app's own error envelope stays on `/v1` and `/api`. `/oauth/authorize` answers a browser, so it renders HTML or redirects.
- **Codes** live 5 min, are single use, and are bound to client, user, PKCE challenge and the redirect URI they were issued for (`oauth_grants.redirect_uri`). A refused client or redirect URI never redirects — it renders an error page.
- **Consent CSRF**: the consent form carries a one-time token stored in the short-lived signed cookie; a cross-site form post cannot produce it. That same cookie parks the authorize request while the user signs in with Google, and only a path under `/oauth/authorize` is resumed.
- **Local without Google**: `/oauth/authorize` answers 401 with a page telling the operator to sign in through the web app's dev login first, instead of redirecting to a Google client that does not exist.
- **`resource` (RFC 8707)** is accepted and ignored: this AS protects exactly one resource, `APP_URL + /mcp`.

## 5. Upload tickets — single-use, job-scoped (the MCP `parse` tool)

A ticket lets an agent do exactly one upload and then read the job that upload created — nothing else. Minted only by the MCP `parse` tool ([mcp.md](./mcp.md)); the point is that an OAuth-connector agent never holds a durable credential, and a leaked ticket is worth one upload to one account for an hour, not an API key.

- Format `srt_<32 random url-safe chars>`; stored as SHA-256 hash + display prefix (`srt_...abc4`) in `upload_tickets` ([database.md](./database.md)). Returned in the tool result exactly once.
- Lifetime `UPLOAD_TICKET_TTL_SECONDS` (default 3600). One TTL covers the upload and the reads; recovery from an expired ticket is minting a new one and re-uploading — dedup makes that free while the parse is kept ([jobs.md](./jobs.md) § Dedup margin).
- **Scope.** An unspent ticket authenticates `POST /v1/parse` once. When the upload creates a job (or hits the dedup cache), the ticket binds to that `job_id` and is marked spent; from then on it authenticates only `GET /v1/jobs/{id}`, `/result`, `/events` for that one job. A request that fails before a job exists (413, 400, 429) does **not** spend the ticket. Every other endpoint — `/mcp` included — refuses `srt_` tokens.
- **Rejection copy is part of the contract.** A spent/expired/unknown ticket gets 401 with: `Upload ticket expired or already spent — call the parse tool again for a fresh ticket; re-uploading the same file returns the cached result instantly.` That message is the agent's only recovery hint, so it lives in the response, not just here.
- **Mint rate limit:** `UPLOAD_TICKET_RATE_PER_HOUR` (default 30) per user; exceeding it is a 429-shaped tool error. Minting also deletes that user's expired tickets (opportunistic cleanup — no sweeper involvement, the table stays small because every mint tidies up).
- Never in query strings, like every other bearer.

## 6. Kano Proxy connect — removed

The authorization-code flow by which a user approved Ema Paper on Kano Proxy and the app received a key for them (`GET /api/kano/connect`, `GET /api/kano/callback`, `GET/DELETE /api/kano`, the `kano_proxy_keys` and `kano_connect_states` tables, `KANO_PROXY_CLIENT_ID` / `KANO_PROXY_CLIENT_SECRET` / `KANO_CONNECT_TTL_SECONDS`) was removed by step 4 of [billing.md](./billing.md) § Transition: every user runs on the operator's one Kano Proxy account (§ 3), paid for in credits. Kano Proxy's side of the contract (kano-proxy-cloud's connect endpoints) is untouched by this; Ema Paper is simply no longer a client of it.

## Deleting the account

A user deletes their own account from the settings dialog's *Account* section ([web.md](./web.md) § Deleting the account): `DELETE /api/account` (session + CSRF) answers **204** with the session cookie cleared, as logout clears it, and everything of the account is gone at once — its rows, its files, its sessions on every device (their next request answers 401) and its connectors' tokens. There is no grace period and no undo; the confirmation is the dialog's. One request does all of it, in this order:

1. **The workspace lock**, which takes the backup lock shared ([workspace.md](./workspace.md) § Backups), so no write of the user's lands in between and a running backup is waited for; a workspace recovering from an unconfirmed shell command answers its 409, as every workspace route does.
2. **The user row, `FOR UPDATE`.**
3. **A subscription that still renews refuses**: **409** `subscription_active`, *Cancel your subscription first*. A subscription renews while its `status` is not `canceled`, `expired` or `failed` and `cancel_at_period_end` is false; one set to end with its period is no obstacle. Kano Pay has no cancel endpoint, the portal opens only from the account ([billing.md](./billing.md) § Kano Pay), and a later account is a new subject, so a subscription left renewing could never be cancelled from the app again: the reader cancels in the portal first. With Pay configured the subscription is read fresh from Pay (`GET /v1/subscriptions/:subjectId`, the canonical fetch the webhook makes) and written back as the webhook writes it, so a cancellation made a minute ago counts; with Pay unreachable, a user who has a `subscriptions` or a `checkouts` row is answered **503** `pay_unavailable` — nothing can say the subscription ended — and one who has neither goes on; with Pay unconfigured, the stored row decides.
4. **Running work refuses**: **409** `invalid_request`, *A parse or an answer is still running; try again when it finishes* — any job of the user `running`, or any chat turn of the user unfinished and not `queued` (`finished_at` NULL with `running`, or `cancelled` and not yet wound down by the worker): it would go on writing files and usage as the account goes. The user's `queued` jobs and turns are locked `FOR UPDATE` — the claim's `SKIP LOCKED` then passes them by — and go with the account, their credit reservations with the ledger.
5. **The sandbox VM is stopped** when a runner is configured (`POST /sandboxes/{user_id}/stop`, [sandbox.md](./sandbox.md)), best effort: a failure is logged and the deletion goes on.
6. **The marker.** The user's job ids and chat-attachment ids are collected, and `{WORKSPACES_DIR}/.repos/{user_id}.deleted` is written holding the job ids (JSON), so a process that dies after the commit leaves the worker's sweep what it needs to finish ([workspace.md](./workspace.md) § Layout).
7. **The `users` row is deleted and the transaction committed.** Every table with a `user_id` cascades from it; `pay_events` keep their rows with `user_id` NULL, and `oauth_clients`, shared, stay ([database.md](./database.md) § Rules).
8. **The files go**: the user's tree `{WORKSPACES_DIR}/{user_id}`, the git directory `.repos/{user_id}`, the originals `.repos/{user_id}.sources`, the images `.repos/{user_id}.images`, the lock files, the crop directories of every collected job (`FIGURES_DIR`) and the upload-directory files of those jobs and of the chat attachments; then the marker. Every removal goes through `safe_io`'s descriptors and follows no link ([workspace.md](./workspace.md) § Revisions). A failure is logged, not answered: the rows are gone, and the sweep finishes the files.
9. **The user's Pay refresh stamp is dropped** from the API's memory ([billing.md](./billing.md) § Kano Pay, *Refresh*), and the answer is 204 with the cookie cleared.

- **The sweep finishes an interrupted deletion.** Every sweep ([jobs.md](./jobs.md) § Retention) lists `.repos/*.deleted`. For a marker whose user row is gone it removes what step 8 removes, the crops and uploads of the job ids the marker names included, and then the marker; a marker whose user row still exists — the commit never landed — is removed alone, and the account stands. Nothing else in the sweeper deletes a user's files by inference; a chat attachment's file left behind is the upload directory's, which the same sweep empties of anything older than `UPLOAD_MAX_AGE_SECONDS`.
- **Kano Pay goes on writing.** A deleted account's subscription still sends its ending events, and Pay retries a refused delivery for ever, so the webhook acknowledges an event whose subject has no account (200 `{}`), logged at info, and records nothing ([billing.md](./billing.md) § Kano Pay).
- **Signing in again** with the same Google account later makes a new, empty account — a new `users.id`, never the old one — on Free from zero.
- **Backups.** The production backups keep the newest seven daily stamps ([deployment.md](./deployment.md)); the account's data leaves them as they rotate, within seven days.

## Logging rule

Log key **ids/prefixes** and auth outcomes, never credential material — no session tokens, no API keys, no Kano Proxy or connection keys, no OAuth codes/tokens, no connect `code`, `state` or `code_verifier`, no client secret, in logs or exceptions. The connect flow logs the user id and the outcome (`connected`, `denied`, `state rejected`, `exchange failed: <error code>`), nothing else.
