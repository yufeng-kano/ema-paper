# Auth

Six credential kinds, strictly separated. No password login anywhere.

## 1. Web sessions — Google OIDC only

- `GET /api/auth/login` → Google (Authorization Code + PKCE); callback creates/updates the `users` row (keyed by Google `sub`; email, name and avatar `picture` URL are refreshed on each sign-in) and a server-side session row; cookie is `HttpOnly; Secure; SameSite=Lax`, value is a random token stored **hashed** in `sessions`.
- The locale rides along: `?locale=` is parked in the transient signed cookie and the callback returns the visitor to `WEB_URL/<prefix>`. The web app's locale otherwise lives in its URL, and Google is the one leg that drops it. The prefix is only honoured when it is in `WEB_LOCALE_PREFIXES` — an allowlist, not a path fragment, so a crafted `?locale=` cannot become an open redirect. A connector flow's parked `/oauth/authorize` still wins over it.
- Logout deletes the session row. Sessions expire (30 d) and are revocable server-side.
- Local dev only: `AUTH_DEV_MODE=true` **and** `APP_ENV=local` enables a `POST /api/auth/dev-login` that signs in as `dev@localhost` (returns `{user: {id, email}}`) — the route must hard-refuse to exist when `APP_ENV != local`. It is CSRF-guarded like every mutation, which lets the web app probe for it without creating a session (no header → 403; absent → 404).

## 2. Project API keys (data plane)

- Format `sr_<32 random url-safe chars>`; shown exactly once at creation; stored as SHA-256 hash + display prefix (`sr_...abc4`).
- Sent as `Authorization: Bearer sr_...` on `/v1/*`. Constant-time lookup by hash. Revocation = soft delete.

## 3. User's upstream keys — encrypted, never hashed (we must use them)

Two flavours, same rules: the Kano Proxy key (`kano_proxy_keys`, one per user, obtained by the connect flow in § 6 — never typed in) and the API key of each user-defined provider connection (`provider_connections`, any OpenAI-compatible endpoint — [api.md](./api.md) § Upstreams).

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
- Lifetime `UPLOAD_TICKET_TTL_SECONDS` (default 3600). One TTL covers the upload and the reads; recovery from an expired ticket is minting a new one and re-uploading — dedup makes that free ([jobs.md](./jobs.md) § Dedup).
- **Scope.** An unspent ticket authenticates `POST /v1/parse` once. When the upload creates a job (or hits the dedup cache), the ticket binds to that `job_id` and is marked spent; from then on it authenticates only `GET /v1/jobs/{id}`, `/result`, `/events` for that one job. A request that fails before a job exists (413, 400, 429) does **not** spend the ticket. Every other endpoint — `/mcp` included — refuses `srt_` tokens.
- **Rejection copy is part of the contract.** A spent/expired/unknown ticket gets 401 with: `Upload ticket expired or already spent — call the parse tool again for a fresh ticket; re-uploading the same file returns the cached result instantly.` That message is the agent's only recovery hint, so it lives in the response, not just here.
- **Mint rate limit:** `UPLOAD_TICKET_RATE_PER_HOUR` (default 30) per user; exceeding it is a 429-shaped tool error. Minting also deletes that user's expired tickets (opportunistic cleanup — no sweeper involvement, the table stays small because every mint tidies up).
- Never in query strings, like every other bearer.

## 6. Kano Proxy connect — authorization code + PKCE, sightread as the client

Kano Proxy is the built-in upstream ([api.md](./api.md) § Upstreams). Its key is not pasted: the user approves sightread on Kano Proxy and sightread receives a key for that user and app. The contract is shared with kano-proxy-cloud, which implements the other side, and is followed exactly.

Configuration: `KANO_PROXY_BASE_URL` (the origin, e.g. `https://kano-proxy.example.com`), `KANO_PROXY_CLIENT_ID`, `KANO_PROXY_CLIENT_SECRET` ([deployment.md](./deployment.md)). With any of the three unset, Kano Proxy is unavailable: `GET /api/kano` says so, connect answers `?kano=error`, and enqueueing on it is refused.

1. **`GET /api/kano/connect`** (session; optional `?locale=` as on sign-in, § 1). Creates a connect state in `kano_connect_states`, bound to the signed-in user: a random `state` stored as its SHA-256 hash, and a random PKCE `code_verifier` stored AES-GCM (HKDF context `kano-connect-verifier-v1` — it is replayed at the token step, so it cannot be hashed), expiring after `KANO_CONNECT_TTL_SECONDS` (default 600). The user's expired states are deleted on the way. Then 302 to
   `{KANO_PROXY_BASE_URL}/connect/authorize?response_type=code&client_id=<KANO_PROXY_CLIENT_ID>&redirect_uri=<APP_URL>/api/kano/callback&state=<state>&code_challenge=<base64url(sha256(verifier)), no padding>&code_challenge_method=S256`.
   `redirect_uri` is always exactly `APP_URL + /api/kano/callback`, and the same value is sent again at the token step.
2. **`GET /api/kano/callback?code=&state=`** (session). The state must exist, be unexpired and belong to the user of the current session. It is consumed by deleting its row in the same statement that finds it, so a replay finds nothing (single use) and two concurrent callbacks cannot both win. A state bound to another user is refused and left alone. Every failure ends in a 302 to the web settings page with a non-secret status: `error=access_denied` → `?kano=denied`; no session, an unknown, expired, foreign or used state, a missing code, or a failed token exchange → `?kano=error`. Nothing answers JSON to a browser in the middle of a redirect chain.
3. **Token exchange**, server to server: `POST {KANO_PROXY_BASE_URL}/api/connect/token`, `application/x-www-form-urlencoded`, body `grant_type=authorization_code&code=…&redirect_uri=…&client_id=…&client_secret=…&code_verifier=…`. Success is 200 `{"access_token": "sk-kano-proxy-…", "token_type": "bearer"}` — no expiry, no refresh token. Errors are 400 `{"error": "invalid_request|invalid_grant|unsupported_grant_type"}` and 401 `{"error": "invalid_client"}`; only the error code is logged.
4. **Store or replace** the user's `kano_proxy_keys` row with the new key (encrypted, masked), then 302 to `/settings?kano=connected`, which also takes the code out of the address bar. Reconnecting replaces the stored key; Kano Proxy itself revokes the previous key for the same user and app.

`GET /api/kano` answers `{available, connected, masked}`. **`DELETE /api/kano`** (session + the CSRF header) deletes the stored key locally only. The key stays listed on Kano Proxy's Access page, where the user deletes it; the settings UI says so.

The settings page the callback returns to is `WEB_URL/settings`, prefixed with the locale parked by `/api/kano/connect` when that locale is in `WEB_LOCALE_PREFIXES` (the allowlist rule of § 1). The locale rides in the same short-lived signed cookie sign-in uses; the state itself is server-side.

## Logging rule

Log key **ids/prefixes** and auth outcomes, never credential material — no session tokens, no API keys, no Kano Proxy or connection keys, no OAuth codes/tokens, no connect `code`, `state` or `code_verifier`, no client secret, in logs or exceptions. The connect flow logs the user id and the outcome (`connected`, `denied`, `state rejected`, `exchange failed: <error code>`), nothing else.
