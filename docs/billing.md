# Billing — plans, credits and Kano Pay

Ema Paper sells a subscription that **includes the model spend**: the operator runs every parse and every chat turn on one Kano Proxy account of its own, and what a user may run is measured in **credits** (點數) granted per period. Users no longer connect an upstream or pick a model from a catalog; they pick a plan, and inside it a chat model from the few the operator offers. This doc is the contract for all of that: what a plan grants, how credits are priced and kept, how a subscription is bought and known, and the order in which the service moves there from the bring-your-own-upstream model of [product.md](./product.md). Decisions were taken on 2026-10-23; the numbers below are the operator's defaults, every one of them configuration.

## Plans

Two plans. Free is a platform policy, never a product at the payment provider; a paid plan is a row of `KANO_PAY_PLANS_JSON` joined to a real price in Kano Pay's catalog.

| | Free | Pro |
|---|---|---|
| Price | 0 | Kano Pay's catalog (never written into code or copy) |
| Credits per period | `FREE_CREDITS_PER_PERIOD` (150) | the plan's `credits` (5,000 in the launch configuration) |
| Period | the UTC calendar month | the subscription's paid period, as Pay reports it |
| Sandbox (`bash` in chat) | no | yes, the VM stops after 10 idle minutes (`SANDBOX_IDLE_SECONDS=600` on that deployment) |
| Workspace storage | `FREE_STORAGE_BYTES` (512 MB) | the plan's `storageBytes` (10 GB) |
| Parses at once | `FREE_MAX_JOBS` (1) | the plan's `maxJobs` (3) |
| Files | kept for as long as the account exists — **both** plans — save what the reader moves to the Trash, deleted for good 30 days later ([workspace.md](./workspace.md) § The Trash). Nothing is deleted when a period ends or a subscription lapses; a Free user over the storage cap can read and delete, not add ([workspace.md](./workspace.md) § Quota) |
| REST API and MCP | yes | yes |

- **Storage is set in decimal bytes.** The web app prints sizes 1000-based, as Finder does (`formatBytes`, [web.md](./web.md)), so a cap meant to read as a round number is one in decimal: `512000000` reads as *512 MB* and `10000000000` as *10 GB*, where `524288000` (500 MiB) would read as *524 MB* and `10737418240` (10 GiB) as *10.7 GB*. `WORKSPACE_QUOTA_BYTES` is only the ceiling a plan must fit under and no page prints it, so its default stays 10 GiB.
- **Every user has Free**, from the moment the `users` row exists, with no row of its own: a user with no active subscription *is* on Free. Empty Kano Pay configuration must not break Free.
- **Entitlements come from the plan the current period belongs to** (§ Periods): the storage cap, the concurrency cap and whether the chat agent gets `bash` are read from it wherever today's code reads `WORKSPACE_QUOTA_BYTES`, `MAX_JOBS_PER_USER` and `sandbox_configured`. A deployment with no runner (`SANDBOX_URL` empty) gives nobody `bash`, whatever the plan says.
- **Without a sandbox the chat agent runs as it does today without one** (`TOOLS_WITHOUT_BASH`, [chat.md](./chat.md) § Tools): page and figure views, the editor, skills, subagents, memory and translation stay; only `bash` goes. Which of those Free should keep is still the operator's open question; the contract here is the current set.
- A paid plan whose `planId` Pay does not know, or whose price is missing from the catalog, cannot be bought and grants nothing: `KANO_PAY_PLANS_JSON` says what a plan *is* (credits, storage, sandbox, concurrency), the catalog says what it *costs*, and only the intersection is on sale ([kano-proxy-cloud `billing.md`](https://github.com/yufeng-kano/kano-proxy-cloud) follows the same join).
- No top-up packs and no third tier in this version. The ledger leaves room for both (`credit_entries.kind`), and Kano Pay would need a one-time purchase for packs; neither is designed here.

## Credits

**One credit is `CREDIT_USD` (US$0.002) of model cost at list price.** The rates below were set from production's own usage over the 60 days before 2026-10-23 (parse ≈ 5,150 input + 1,440 output tokens per page on `gpt-6-luna`, format and figure checks included; chat ≈ 115K input per turn, nine tenths of it prompt-cache reads, plus ≈ 3K output). The operator's Kano Proxy account runs on subscription pools, so list price is the pricing reference, not the operator's cost.

### A parse

- A job costs **`PARSE_CREDITS_PER_PAGE` (1) per page it parses** — the pages its `pages` spec selects, not the document's page count — whatever the model, since the operator picks it. That covers the figure check and the format check, which are not charged on their own.
- Charged when the job is **queued**, not when it finishes: the credits are reserved then, so a user without enough credits never queues a parse — the refusal names the pages' cost and the balance (`402 insufficient_credits`, § Refusals). A reservation becomes consumed when the job ends `succeeded` and is **released** when it ends `failed` — the user got nothing.
- **A dedup hit costs nothing** — no model ran ([jobs.md](./jobs.md) § Dedup) — and neither does a second upload of bytes already on their way (`reuse_in_flight`). **A retry reserves the job's credits again** ([jobs.md](./jobs.md) § Retrying failed pages): the job keeps its one entry, and the retry takes it back from the current period — dated then, and refused with `402 insufficient_credits` when the balance cannot cover it, nothing requeued — to be consumed or released by the retry's own end, as the first run's was. A document is charged once, by the run that delivers it, and a job that never succeeds costs nothing however often it is retried. A job queued before the ledger existed has no entry, and its retry reserves nothing, as its first run did. Until 2026-10-25 a retry reserved nothing, so a job that failed and then succeeded on a retry was never charged — a reader could make the format check fail on purpose, its prompt being theirs to write, and retry with the check off. **Re-converting** is a new job and is charged like one ([jobs.md](./jobs.md) § Re-converting a document). A parse on a page spec that is a subset of an earlier one is a new job too.
- The worker releases a reservation in the same transaction that marks the job `failed`, and consumes it in the one that marks it `succeeded`; a job requeued after a crash keeps its reservation. Nothing refunds a `succeeded` job.

### A chat turn

- A turn is charged **by the tokens its model calls really used**, every call of the turn together (subagent calls carry the turn's `turn_id` and count with it): `input × price.input + cache_read × price.cache_read + cache_write × price.cache_write + output × price.output`, prices per token from `MODEL_PRICES_JSON` (§ Models), summed in USD and **rounded up to whole credits once per turn**, never per call, with a floor of 1 credit. A `/translate` turn is a turn like any other.
- A turn needs a balance of **at least `CHAT_MIN_CREDITS` (1)** to be queued (`402` otherwise) — its cost is not known in advance. The runner settles the turn's entry **after every model call** (the entry's `amount` becomes the rounded-up total so far, the period's `spent` moves by the difference), and **when the balance is gone mid-turn** — the settled remaining balance is zero or below, a subagent's call included — **the turn still finishes, within a bound**: from then on at most `CHAT_OVERDRAFT_MAX_TOOL_CALLS` (25) more tool calls run, the turn's own and its subagents' together, each counted as it starts. A call past them answers an error result instead of running (a write already running finishes and commits as it would), and a subagent about to make another model call with none left stops there and reports what it has. Once they are used, the turn's next model call is its last: it goes with `tool_choice: {"type": "none"}`, so the model answers with what it has, and the turn ends `succeeded` when it does. Should that call still stop on a tool call, its tool calls are dropped, its text kept, and the turn ends with `turn_error` `out_of_credits`. Every call is charged as usual, nothing is refunded, and the period ends in the red by what that tail cost. That overdraft is not carried into the next period, and no screen shows a negative balance: an overdrawn period reads 0 left. The bound is separate from `CHAT_MAX_TOOL_CALLS_PER_TURN`, which still caps every turn: a turn that runs out of credits stops at whichever comes first. A turn the worker finishes on its own (`fail_turn`: a shutdown, a restart, an escaped error) closes the entry at what it was charged so far, like any other terminal state.
- The worker records every call's `usage_log` row as it does now ([chat.md](./chat.md)); credits are computed from those same token counts, so Usage and the ledger agree by construction. A call cut short — a Stop, the turn's timeout, a stream that broke — is recorded and charged for what it streamed ([chat.md](./chat.md) § Conversations, turns, events): stopping an answer does not make it free.

### Rates at the launch configuration

Per 1K tokens, and what the measured average turn comes to; the parse rate is the flat 1 credit per page (≈ 0.7 credits of list-price cost on `gpt-6-luna`).

| Model | input | cache read | cache write | output | ≈ per turn |
|---|---|---|---|---|---|
| `gpt-6-luna` | 0.05 | 0.005 | 0.05 | 0.25 | 2 |
| `claude-sonnet-5-5` | 1 | 0.05 | 1.25 | 5 | 34 |
| `gpt-6-sol` | 1 | 0.1 | 1 | 5 | 36 |
| `gpt-5.6-terra` | 1 | 0.1 | 1 | 6 | 39 |
| `claude-opus-5-5` | 2 | 0.1 | 2.5 | 10 | 68 |

Those are `MODEL_PRICES_JSON` values divided by `CREDIT_USD`; the table is an illustration, the configuration is the truth. OpenAI bills input past 272K tokens at twice the rate; the proxy's usage report does not say which tokens those were, so this service prices every token at the base rate and the operator carries the difference on the rare long turn.

### Periods

A user's **current period** is decided at every charge, from the subscription row and the clock:

1. A subscription whose `status` is `active` or `trialing`, whose `period_start ≤ now < period_end`, and whose `plan_id` is in `KANO_PAY_PLANS_JSON` → the period is `[period_start, period_end)` on that plan.
2. Otherwise → Free: the period is the UTC calendar month containing `now`.

Each period has one `credit_periods` row per user, created on first use with `granted` = the plan's credits at that moment, and `spent` moving as entries are reserved, consumed and released. A period that was never used has no row and costs nothing. Rules that follow from this:

- **Credits do not carry over.** A new period is a new row with a fresh grant; whatever was left is left.
- **Upgrading mid-month** starts the paid period at Pay's `period_start` with the plan's full grant. The Free month's row stays as it is and is current again, remainder and all, if the paid period ends inside that month.
- **A plan change inside a period** (a later `KANO_PAY_PLANS_JSON` edit, or Pay reporting another plan for the same period) raises the row's `granted` to the new plan's credits when that is higher and never lowers it: credits already shown as available are not taken back.
- **A lapsed subscription** (`past_due`, `canceled`, `paused`, or a period that ended) puts the user on Free's month at the next charge, with that month's row — which may already exist, from before the upgrade. `cancel_at_period_end` changes nothing until the period ends. Every charge re-reads the clock; nothing depends on a webhook arriving to expire access.
- **Reservations live where they were made.** A parse reserved in one period and finished in the next consumes or releases that period's row: the entry carries its `period_start`.

### Refusals

| Situation | Answer |
|---|---|
| A parse whose pages cost more than the current balance | **402** `insufficient_credits`, body `{need, remaining, period_end}` — the web app's upload confirmation and the API caller's hint. The upload is not stored and an upload ticket is not spent |
| A retry of a failed parse whose credits are more than the current balance | **402** `insufficient_credits`, same body; the retry is not queued |
| A chat send with a balance under `CHAT_MIN_CREDITS` | **402** `insufficient_credits`, same body (`need` is the minimum) |
| A turn whose balance runs out mid-way | the turn ends with `turn_error` `out_of_credits` (§ A chat turn) |
| A checkout for a plan not on sale, or while Pay says checkout is closed | **409** `checkout_unavailable` |
| Pay unreachable while a user asks for a checkout or the portal | **503** `pay_unavailable`; existing subscription rows are never changed by an outage |
| Deleting the account while a subscription still renews | **409** `subscription_active` — cancelled in the portal first (§ Kano Pay, *Deleting the account*); **503** `pay_unavailable` when Pay cannot be reached to say and the user has a `subscriptions` or `checkouts` row |

402 is new to the API; its envelope is the standard one ([api.md](./api.md) § Errors). No refusal costs the caller anything, and none of them ever consults the clock loosely: the balance is read under the user's row lock in the same transaction that writes the entry.

## The ledger

Four tables, all `ON DELETE CASCADE` from `users` ([database.md](./database.md)):

```text
subscriptions    user_id PK/FK, subscription_id UNIQUE, status, plan_id,
                 period_start, period_end, cancel_at_period_end, version, updated_at
checkouts        user_id PK/FK, plan_id, idempotency_key UNIQUE, transaction_id UNIQUE NULL,
                 created_at, completed_at NULL
pay_events       id PK (Pay's event id), type, user_id FK NULL, received_at
credit_periods   (user_id FK, period_start) PK, period_end, plan_id NULL, granted, spent,
                 created_at, updated_at
credit_entries   id PK, user_id FK, period_start, kind parse|chat, state reserved|consumed|released,
                 amount, job_id FK SET NULL NULL UNIQUE, turn_id FK SET NULL NULL UNIQUE, model NULL,
                 created_at, settled_at NULL
```

- **`credit_periods.spent` is the balance's fast path and `credit_entries` its record**; the two agree because every write touches both in one transaction. `spent` counts the reserved and consumed entries of the period; a release subtracts its amount. `remaining = granted − spent` and may be negative only through a chat overdraft.
- **Reserving a parse** is one statement under the user's row lock (`SELECT … FOR UPDATE` on `users`, which `submit_parse` and the chat send already take for their caps): read or create the period row, then `UPDATE credit_periods SET spent = spent + :amount WHERE user_id = :u AND period_start = :p AND granted − spent ≥ :amount`; no row updated is the 402. The entry is inserted in the same transaction as the job, with `state = reserved`. A retry runs the same statement for the job's released entry, in the transaction that requeues the job, and moves the entry to the current period with `state = reserved` again; an entry in any other state is left as it is.
- **Settling** is idempotent: the entry's `state` is the guard. A job finishing twice (a requeue that raced a finish) moves `reserved → consumed` once; a release of an entry already consumed does nothing and is logged. The chat entry is updated by its `turn_id`, and the period's `spent` by the difference between the new and the old `amount`, so a repeated settlement of the same usage moves nothing.
- **Deleting the account** ([auth.md](./auth.md) § Deleting the account) takes the ledger with it, as it takes `usage_log`. Nothing else deletes a row: the ledger is the billing record, kept as long as the account. Deleting a conversation takes its turns and sets the entries' `turn_id` to NULL, as it does for `usage_log`; a parse the retention deletes ([jobs.md](./jobs.md) § Retention) sets its entry's `job_id` to NULL the same way. Either way the amounts stay in the period, `spent` unchanged, and in the usage breakdown.
- **Periods are small** — one row per user per month or paid period — and are never swept. Entries are one per job and one per turn.

What the web app reads: `GET /api/billing` (§ Routes) for the plan, the period and its balance; the Usage tab's credits per day and per agent come from `credit_entries` grouped like `usage_log` is today ([web.md](./web.md) § Pages).

## Models and the operator's upstream

- **One Kano Proxy key for the whole deployment:** `KANO_PROXY_API_KEY`, in the server's `.env` ([deployment.md](./deployment.md)). The worker and the API present it on every upstream call. It replaced the per-user key the connect flow used to obtain; the flow, the stored keys and the provider connections went with it (§ Transition, step 4).
- **The parse model is fixed:** `PARSE_MODEL` (`gpt-6-luna`), one id, the same for every user; its bbox convention is resolved as today ([parsing.md](./parsing.md) § Coordinate conventions). The format check runs on the parse model unless `CHECK_MODEL` names another. The parse agent's fallback model, if the operator sets one, is `PARSE_FALLBACK_MODEL`; a user has no model settings for `parse` or `check` any more — their prompt, instructions and switches stay.
- **The chat models are the live catalog's priced ones, with no list of their own:** the models of the operator's Kano Proxy catalog (`GET {KANO_PROXY_BASE_URL}/openai/v1/models`, fetched live, no cache — on a group endpoint, exactly the group's model names) that `MODEL_PRICES_JSON` prices, **in `MODEL_PRICES_JSON`'s order**; the first is the default for a user who chose none. Kano Proxy decides what exists and the price table what is sold, so a model is added or withdrawn in one of those two places and nowhere else: a catalog model nobody priced is not offered, and a priced model the catalog does not list (a fallback target, a model the group dropped) is not either. `GET /api/agents/chat/models` answers that list. `agent_settings.model` for `chat` must be on it when it is saved (400 otherwise); a stored choice the list later loses is still shown as the current one and refused at enqueue, as a vanished model always was. (`CHAT_MODELS`, the comma-separated list this replaced, is ignored if still set.)
- **`MODEL_PRICES_JSON` must price every model the deployment runs on its own** — `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL` — and is what makes a catalog model a chat model, as `{"<model>": {"input": <USD per 1M>, "cache_read": …, "cache_write": …, "output": …}}`. The API and the worker **refuse to start** with an offered model unpriced: a model nobody priced must never run for free. A chat turn whose model has no price (the list changed under a queued turn) fails with `turn_error` `model_unpriced` before its first call, and a subagent whose own `model` has no price answers an error result before its first call instead of running — its calls would otherwise be charged as nothing.
- **Nothing in `MODEL_PRICES_JSON` is product data the UI shows as a price**: the web app shows credits per 1K tokens and per page, derived from it and `CREDIT_USD` by the API (`GET /api/billing`), never typed into the frontend.

## Kano Pay

Payment is through **Kano Pay** (`pay.yuufeng.com`), the operator's payment service shared with Kano Proxy, and the contract is the one its own docs define (`kano-pay/docs/integrating-apps.md`): an App registered in Pay's `APPS_JSON` with a service key, a return URL, a webhook URL and its plans; `POST /v1/checkouts`, `GET /v1/subscriptions/:subjectId`, `POST /v1/portal`, `GET /v1/catalog`; signed notifications. Pay is moving its own backend from Paddle to **Dodo Payments**, which is the merchant of record (tax, invoices, refunds, the customer portal); the App contract does not change with it, so nothing here names the provider. Ema Paper is the second App on Pay after Kano Proxy, whose adapter (`kano-proxy-cloud/docs/billing.md` § Kano Pay adapter) this one copies.

- **Subject id** is the `users.id` as a decimal string. It never changes for an account, so a subscription found at Pay for a subject is that user's.
- **Checkout.** `POST /api/billing/checkout {plan_id}` (session + CSRF) refuses a plan not on sale (§ Plans) and, with Pay's `checkoutEnabled` false, answers 409. It then takes the user's `checkouts` row under their row lock: a row for the same plan reuses its `idempotency_key`; another plan replaces the row with a fresh key. The key is written **before** Pay is called, so a retry after an ambiguous timeout repeats the same request and Pay returns the same checkout rather than a second one. Pay's `checkoutUrl` is validated (https, Pay's own host) and returned as `{url}`; the browser goes there. Nothing is granted from the return redirect: the user comes back to `/files?settings=billing` (the settings dialog's Usage section) and the page shows whatever `GET /api/billing` says, which is what the webhook or a refresh has written.
- **Webhook.** `POST /api/billing/pay-webhook` carries no session: it verifies `Kano-Pay-Signature` (`ts=<unix seconds>;h1=<hex HMAC-SHA256>` over `<ts>.<raw body>` with `KANO_PAY_WEBHOOK_SECRET`, constant-time; a timestamp more than `KANO_PAY_SIGNATURE_TOLERANCE_SECONDS` off is refused), reads the body only for the event `id`, `type` and the subject, then **fetches the canonical subscription from Pay** (`GET /v1/subscriptions/:subjectId`) rather than trusting the body. In one transaction, under the user's row lock: insert the `pay_events` row (a duplicate id is a 200 and nothing else), upsert `subscriptions` **only when the fetched `version` is newer** than the stored one, and mark the user's open `checkouts` row completed when the fetched subscription is paid and its `version` is later than the row's `created_at` — the purchase that checkout started. The subscription's plan must be in `KANO_PAY_PLANS_JSON` to grant anything; an unknown plan is stored with its id and grants Free. 2xx is answered only once the rows are committed. A body naming another App's id is refused and logged by id only. A subject that is not a user — an account deleted while its subscription still sends its ending events — is acknowledged (200 `{}`) and logged at info, with nothing fetched or recorded: Pay retries a refused delivery for ever.
- **Refresh.** `GET /api/billing` reconciles with Pay at most once per `BILLING_REFRESH_SECONDS` (120) per user — the same fetch the webhook makes — so a missed notification is recovered on the next visit, the `checkouts` row completed with it as the webhook would; a Pay outage leaves the stored row as it is and the response says `pay_reachable: false`. Nothing is reset or revoked because Pay could not be reached.
- **Portal.** `POST /api/billing/portal` returns Pay's fresh `{url}`; it is never cached and never shown to a user without an active or lapsed subscription of their own — a user with no `subscriptions` row gets 404 `invalid_request`.
- **Refunds.** Nothing here refunds. A refund is asked for at the support address, decided by the operator and made by Dodo to the original payment method; the subscription it ends reaches the app as any change does — the webhook or the refresh — and the period's grant is not clawed back from the ledger. The policy a user is promised is the public page's (§ Transition, step 6).
- **Deleting the account** ([auth.md](./auth.md) § Deleting the account) is refused while a subscription still renews — `status` not `canceled`, `expired` or `failed`, and `cancel_at_period_end` false — with **409** `subscription_active`. Pay has no cancel endpoint, and once the account is gone its subscription could be reached from the app no more: the portal opens only for a user with a `subscriptions` row, and the same Google account signing in again is a new subject. So the reader cancels in the portal first; a subscription set to end with its period deletes. The deletion reads the subscription fresh from Pay, as the webhook does, and writes back what it reads; with Pay unreachable, a user with a `subscriptions` or `checkouts` row is answered 503 `pay_unavailable`, and with Pay unconfigured the stored row decides.
- **Errors.** Checkout and portal answer 503 `pay_unavailable` when Pay is unconfigured, unreachable, answers 5xx or answers something that does not validate (a checkout URL off Pay's origin, a portal URL that is not https); checkout answers 409 `checkout_unavailable` when the plan is not on sale, Pay's checkout is closed, or Pay refuses with a 4xx of its own (a subscription or an open checkout already stands). The webhook answers 401 `auth` for a bad or stale signature, 400 for a body it cannot read, 413 past 64 KB, and 503 when the canonical fetch fails or Pay has no subscription for the subject yet — Pay retries, and nothing is recorded. Every answer that is not 2xx leaves the rows untouched.
- **Catalog.** `GET /api/billing/catalog` is public, answers `{available, plans: [{plan_id, name, amount, currency, cycle, credits, storage_bytes, sandbox, max_jobs}], free: {credits, storage_bytes, sandbox, max_jobs}, rates}` — the plans on sale, the Free plan as this deployment configures it and the rates of `GET /api/billing` (§ Routes), so the pricing page needs no second request — cached in memory for `KANO_PAY_CATALOG_TTL_SECONDS` with `Cache-Control: public, max-age=<that>`; with Pay unconfigured or unreachable it answers `available: false` with no plans, `free` and `rates` still, with `no-store`, never an error and never a number the operator did not configure at Pay. The public pricing page and the billing section both read it ([web.md](./web.md)).
- **Secrets:** `KANO_PAY_API_KEY` and `KANO_PAY_WEBHOOK_SECRET` live in `.env` only, are never logged, and their presence is the only thing the API ever says about them. No customer or payment payload is logged or stored beyond the fields above.

## Routes

| Route | Auth | Answer |
|---|---|---|
| `GET /api/billing` | session | `{plan: {id, name, credits, storage_bytes, sandbox, max_jobs}, subscription: {status, plan_id, period_start, period_end, cancel_at_period_end} \| null, period: {start, end, granted, spent, remaining}, rates: {parse_per_page, chat: {<model>: {input, cache_read, cache_write, output}}}, checkout_available, pay_reachable}` — `rates.chat` is credits per 1K tokens for each model `MODEL_PRICES_JSON` prices, in its order — the chat models among them, whichever the live catalog serves (§ Models) |
| `GET /api/billing/catalog` | none | § Kano Pay |
| `POST /api/billing/checkout` | session + CSRF | `{url}` |
| `POST /api/billing/portal` | session + CSRF | `{url}` |
| `POST /api/billing/pay-webhook` | `Kano-Pay-Signature` | `{}` |
| `GET /api/billing/entries?days=30` | session | `{days, per_day: [{date, parse, chat}], per_agent: [{agent, credits}]}` — the Usage tab's credits view, from `credit_entries` |

`GET /api/me` grows `plan` (`free` or the plan id), `credits_remaining` and `chat_min_credits` (`CHAT_MIN_CREDITS`), so the shell can show the balance, and the agent panel say a send would be refused, without a second request; `POST /api/library/documents` and `POST /v1/parse` answer 402 as above; the job (`GET /v1/jobs/{id}`), library document and `chat_turns` payloads grow `credits` (the entry's amount, `null` while a reservation is pending its settlement), so an API caller can see what a parse cost and the thread what a turn did ([web.md](./web.md) § The agent panel).

## Web

The screens are specified in [web.md](./web.md) and the design in the project's design canvas; the contract they render is this doc's. The settings dialog has a **Usage** section (called *Billing* until 2026-10-24; `?settings=billing` still opens it): the plan, the period's credits and the workspace's storage as bars with the reset date, the rates, *Upgrade* (a checkout) or *Manage subscription* (Pay's portal, where the payment method, the invoices and cancelling live), and the sandbox the plan gives. Credits appear in two more places: the 402 state of an upload or a send — a dialog naming the cost, the balance and the reset date, with the way to the pricing page — and under every answer in the thread (its credits). A public `/pricing` page reads the catalog — the plans on sale with Pay's prices, the Free plan and the rates from the same answer — and the sign-in page links to it. Copy goes through the i18n catalog as always; prices and credits come from the API, never from the page.

## Configuration

| Var | Meaning |
|---|---|
| `KANO_PROXY_API_KEY` | the operator's Kano Proxy key, presented on every upstream call. Empty = no upstream: parses and turns are refused with 503, as an unconfigured Kano Proxy is today |
| `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL` | the parse pipeline's models (`CHECK_MODEL` empty = the parse model; `PARSE_FALLBACK_MODEL` empty = no fallback) |
| `MODEL_PRICES_JSON` | list prices, USD per 1M tokens, for every model above and every chat model, in the order the chat agent offers them (§ Models) |
| `CREDIT_USD` | what one credit is worth in list-price USD (0.002) |
| `PARSE_CREDITS_PER_PAGE` | credits per parsed page (1) |
| `CHAT_MIN_CREDITS` | the balance a chat send needs (1) |
| `FREE_CREDITS_PER_PERIOD`, `FREE_STORAGE_BYTES`, `FREE_MAX_JOBS` | the Free plan (150, 512000000, 1) |
| `KANO_PAY_URL`, `KANO_PAY_APP_ID`, `KANO_PAY_API_KEY`, `KANO_PAY_WEBHOOK_SECRET` | the Pay registration; all four set = Pay configured, otherwise every user is on Free and the billing section says checkout is not available |
| `KANO_PAY_PLANS_JSON` | `[{"planId": "pro", "credits": 5000, "storageBytes": 10000000000, "sandbox": true, "maxJobs": 3}]` — what a paid plan grants; the price is Pay's |
| `BILLING_REFRESH_SECONDS` | how often `GET /api/billing` may reconcile with Pay per user (120) |
| `KANO_PAY_TIMEOUT_SECONDS`, `KANO_PAY_SIGNATURE_TOLERANCE_SECONDS`, `KANO_PAY_CATALOG_TTL_SECONDS`, `KANO_PAY_EVENT_MAX_BYTES` | each call to Pay (15), a notification's replay window (300), the catalog's cache and `max-age` (300), the most a notification may carry (65536) |

`WORKSPACE_QUOTA_BYTES` and `MAX_JOBS_PER_USER` are the ceilings: a plan, Free included, configured past either refuses to start, so no plan promises what the deployment cannot carry.

## Transition

The service runs today on each user's own Kano Proxy key. The move is made in steps, each a PR with its docs, each leaving the service runnable, in this order; the `docs/` of a step are rewritten by that step, and until a step lands the docs it will touch describe what runs, with a pointer here where the two will differ.

1. **This doc and the schema** — the five tables, empty, their ORM models, the configuration above with its validation, and the credit arithmetic as a pure module. No behaviour changes: nothing reads the tables yet.
2. **The ledger** — reservation at `submit_parse`, settlement in the worker's terminal commit, the chat runner's per-call settlement and `out_of_credits`, `GET /api/billing` and `/entries`, 402s, `credits` on jobs and turns. Every existing user is on Free from this step; a user who had connected their own Kano Proxy still runs on it (step 4 ends that), charged in credits all the same, and a chat model they chose must be priced in `MODEL_PRICES_JSON` or its turn fails `model_unpriced`. With Pay unconfigured, `GET /api/billing` answers `checkout_available: false` and `pay_reachable: null`.
3. **Kano Pay** — Pay's own switch to Dodo first (in the kano-pay repository: docs, adapter, test mode, one real purchase and refund), then Ema Paper's registration in `APPS_JSON`, the checkout, webhook, portal and catalog routes here (`billing/pay.py`, `routes/billing.py`), `subscriptions` written for real, and the plan's storage, concurrency and sandbox entitlements enforced from this step: `ledger.storage_limit`, `ledger.jobs_limit` and `ledger.sandbox_granted` replace the bare `WORKSPACE_QUOTA_BYTES`, `MAX_JOBS_PER_USER` and `sandbox_configured` reads, and the worker's claim applies the same per-user cap from the `subscriptions` row as it stands at claim time ([jobs.md](./jobs.md) § Concurrency caps). Existing users are on Free, so a deployment whose Free plan is smaller than the old ceilings tightens on deploy: a Free user over `FREE_STORAGE_BYTES` can read and delete, not add, and runs one parse at a time.
4. **The operator's upstream** — `KANO_PROXY_API_KEY`, `PARSE_MODEL`, `CHAT_MODELS`; the connect flow, `kano_proxy_keys`, `kano_connect_states`, `provider_connections`, `agent_settings.connection_id` and `agent_settings.fallback_model` removed with their routes and UI ([auth.md](./auth.md), [api.md](./api.md) § Upstreams, [agents.md](./agents.md)); the model dropdowns replaced by the offered list (`GET /api/agents/{agent}/models`: the parse agent's fixed models, the chat agent's `CHAT_MODELS` against the live catalog). A job queued on a user's own Kano Proxy key runs on the operator's — the same proxy — and a job queued on a provider connection fails when the worker takes it, as a job whose connection was deleted always did (`jobs.connection_id` stays as history, [database.md](./database.md) § Rules). A `parse` or `check` model a user had chosen is cleared by the migration; a `chat` model outside `CHAT_MODELS` stays stored, is shown as the current choice, and is refused at the next send.
5. **The web app** — the Billing section (now *Usage*, with the plan's storage and sandbox), the credits moments, the public pricing page.
6. **Public pages** — `/terms`, `/privacy`, `/refund`, prerendered in both locales, linked from the sign-in and pricing footers, with the support address and the operator's name read from `GET /api/site` (`SUPPORT_EMAIL`, `OPERATOR_NAME`; [web.md](./web.md) § Pages, [deployment.md](./deployment.md)). Dodo's site review needs them. The refund policy the page states — a first subscription refunded in full on request within 14 days of the purchase, a renewal not, a failed parse never charged, an outage credited or refunded pro rata — is the operator's; Dodo makes the refund to the original payment method and the app learns of it as it learns of any subscription change, from Pay (§ Kano Pay).
7. **Domain and DNS**, then **deploy**: the migration, the new `.env` variables, one monitored real purchase.

Existing users land on Free with that month's grant; a trial of Pro for them is the operator's call and would be a `subscriptions` row written by hand — nothing in the code invents one.

## Acceptance

Unit tests, stubbed HTTP, no Pay or model call ever ([testing.md](./testing.md)):

- Credit arithmetic: a parse's credits from a pages spec; a turn's from token counts and prices, rounded up once over the turn, floored at 1; an unpriced model refused; configuration validation (every offered model priced, plans well-formed, ceilings respected).
- Periods: Free's month and a paid period from a subscription row and a clock; a lapsed row falls back; a plan change raises and never lowers `granted`; a period row created once under concurrent first use (PostgreSQL).
- The ledger (PostgreSQL): two parses racing for the last credits — one reserves, one gets 402; settlement idempotent (consume twice, release after consume); a release restores the balance; a chat overdraft ends the turn and is not carried over; a call stopped mid-stream is charged for the input it reported and the output that streamed; a retried parse reserves again, is refused 402 on a balance that cannot cover it, and is charged once, by the run that succeeds.
- The webhook: a wrong or stale signature refused; a duplicate event id answered 200 without a second write; a stale `version` ignored; an unknown plan grants Free; the canonical fetch is what is written, not the body; a subject with no account acknowledged with 200 and nothing written.
- Deleting the account: refused 409 while a subscription renews, allowed once it is set to end with its period, 503 with Pay unreachable and a subscription on record. A purged parse: its entry kept with `job_id` NULL, the period's `spent` unchanged.
- Checkout: the idempotency key stable across a retry; a plan not on sale refused; checkout closed refused.
- The migration: applies on a database holding existing users, jobs and usage rows, which it does not touch; the downgrade drops the five tables and nothing else.
