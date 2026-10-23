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
| Workspace storage | `FREE_STORAGE_BYTES` (50 MB) | the plan's `storageBytes` (10 GiB) |
| Parses at once | `FREE_MAX_JOBS` (1) | the plan's `maxJobs` (3) |
| Files | kept for as long as the account exists — **both** plans. Nothing is deleted when a period ends or a subscription lapses; a Free user over the storage cap can read and delete, not add ([workspace.md](./workspace.md) § Quota) |
| REST API and MCP | yes | yes |

- **Every user has Free**, from the moment the `users` row exists, with no row of its own: a user with no active subscription *is* on Free. Empty Kano Pay configuration must not break Free.
- **Entitlements come from the plan the current period belongs to** (§ Periods): the storage cap, the concurrency cap and whether the chat agent gets `bash` are read from it wherever today's code reads `WORKSPACE_QUOTA_BYTES`, `MAX_JOBS_PER_USER` and `sandbox_configured`. A deployment with no runner (`SANDBOX_URL` empty) gives nobody `bash`, whatever the plan says.
- **Without a sandbox the chat agent runs as it does today without one** (`TOOLS_WITHOUT_BASH`, [chat.md](./chat.md) § Tools): page and figure views, the editor, skills, subagents, memory and translation stay; only `bash` goes. Which of those Free should keep is still the operator's open question; the contract here is the current set.
- A paid plan whose `planId` Pay does not know, or whose price is missing from the catalog, cannot be bought and grants nothing: `KANO_PAY_PLANS_JSON` says what a plan *is* (credits, storage, sandbox, concurrency), the catalog says what it *costs*, and only the intersection is on sale ([kano-proxy-cloud `billing.md`](https://github.com/yufeng-kano/kano-proxy-cloud) follows the same join).
- No top-up packs and no third tier in this version. The ledger leaves room for both (`credit_entries.kind`), and Kano Pay would need a one-time purchase for packs; neither is designed here.

## Credits

**One credit is `CREDIT_USD` (US$0.002) of model cost at list price.** The rates below were set from production's own usage over the 60 days before 2026-10-23 (parse ≈ 5,150 input + 1,440 output tokens per page on `gpt-6-luna`, format and figure checks included; chat ≈ 115K input per turn, nine tenths of it prompt-cache reads, plus ≈ 3K output). The operator's Kano Proxy account runs on subscription pools, so list price is the pricing reference, not the operator's cost.

### A parse

- A job costs **`PARSE_CREDITS_PER_PAGE` (1) per page it parses** — the pages its `pages` spec selects, not the document's page count — whatever the model, since the operator picks it. That covers the figure check and the format check, which are not charged on their own.
- Charged when the job is **queued**, not when it finishes: the credits are reserved then, so the Files page can say "20 pages, 20 credits" before the upload and a user without enough credits never queues a parse (`402 insufficient_credits`, § Refusals). A reservation becomes consumed when the job ends `succeeded` and is **released** when it ends `failed` — the user got nothing, and the retry is free either way.
- **A dedup hit costs nothing** — no model ran ([jobs.md](./jobs.md) § Dedup) — and neither does a second upload of bytes already on their way (`reuse_in_flight`). **Retrying failed pages costs nothing** ([jobs.md](./jobs.md) § Retrying failed pages): the job keeps its one entry. **Re-converting** is a new job and is charged like one ([jobs.md](./jobs.md) § Re-converting a document). A parse on a page spec that is a subset of an earlier one is a new job too.
- The worker releases a reservation in the same transaction that marks the job `failed`, and consumes it in the one that marks it `succeeded`; a job requeued after a crash keeps its reservation. Nothing refunds a `succeeded` job.

### A chat turn

- A turn is charged **by the tokens its model calls really used**, every call of the turn together (subagent calls carry the turn's `turn_id` and count with it): `input × price.input + cache_read × price.cache_read + cache_write × price.cache_write + output × price.output`, prices per token from `MODEL_PRICES_JSON` (§ Models), summed in USD and **rounded up to whole credits once per turn**, never per call, with a floor of 1 credit. A `/translate` turn is a turn like any other.
- A turn needs a balance of **at least `CHAT_MIN_CREDITS` (1)** to be queued (`402` otherwise) — its cost is not known in advance. The runner settles the turn's entry **after every model call** (the entry's `amount` becomes the rounded-up total so far, the period's `spent` moves by the difference), and when the balance is gone mid-turn — the settled remaining balance is zero or below, a subagent's call included — the loop ends with `turn_error` `out_of_credits` after the call that spent it, before any further tool or model call: the turn's output so far stays in the thread, nothing is refunded, and the period may end a few credits in the red. That overdraft is not carried into the next period. A turn the worker finishes on its own (`fail_turn`: a shutdown, a restart, an escaped error) closes the entry at what it was charged so far, like any other terminal state.
- The worker records every call's `usage_log` row as it does now ([chat.md](./chat.md)); credits are computed from those same token counts, so Usage and the ledger agree by construction.

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
| A chat send with a balance under `CHAT_MIN_CREDITS` | **402** `insufficient_credits`, same body (`need` is the minimum) |
| A turn whose balance runs out mid-way | the turn ends with `turn_error` `out_of_credits` (§ A chat turn) |
| A checkout for a plan not on sale, or while Pay says checkout is closed | **409** `checkout_unavailable` |
| Pay unreachable while a user asks for a checkout or the portal | **503** `pay_unavailable`; existing subscription rows are never changed by an outage |

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
                 amount, job_id FK NULL UNIQUE, turn_id FK SET NULL NULL UNIQUE, model NULL,
                 created_at, settled_at NULL
```

- **`credit_periods.spent` is the balance's fast path and `credit_entries` its record**; the two agree because every write touches both in one transaction. `spent` counts the reserved and consumed entries of the period; a release subtracts its amount. `remaining = granted − spent` and may be negative only through a chat overdraft.
- **Reserving a parse** is one statement under the user's row lock (`SELECT … FOR UPDATE` on `users`, which `submit_parse` and the chat send already take for their caps): read or create the period row, then `UPDATE credit_periods SET spent = spent + :amount WHERE user_id = :u AND period_start = :p AND granted − spent ≥ :amount`; no row updated is the 402. The entry is inserted in the same transaction as the job, with `state = reserved`.
- **Settling** is idempotent: the entry's `state` is the guard. A job finishing twice (a requeue that raced a finish) moves `reserved → consumed` once; a release of an entry already consumed does nothing and is logged. The chat entry is updated by its `turn_id`, and the period's `spent` by the difference between the new and the old `amount`, so a repeated settlement of the same usage moves nothing.
- **Deleting a user** takes the ledger with them, as it takes `usage_log` today. Nothing else deletes a row: the ledger is the billing record, kept as long as the account. Deleting a conversation takes its turns and sets the entries' `turn_id` to NULL, as it does for `usage_log`; the amounts stay in the period and in the usage breakdown.
- **Periods are small** — one row per user per month or paid period — and are never swept. Entries are one per job and one per turn.

What the web app reads: `GET /api/billing` (§ Routes) for the plan, the period and its balance; the Usage tab's credits per day and per agent come from `credit_entries` grouped like `usage_log` is today ([web.md](./web.md) § Pages).

## Models and the operator's upstream

- **One Kano Proxy key for the whole deployment:** `KANO_PROXY_API_KEY`, in the server's `.env` ([deployment.md](./deployment.md)). The worker and the API present it on every upstream call. It replaces the per-user key the connect flow obtained ([auth.md](./auth.md) § 6), which goes away with the flow (§ Transition).
- **The parse model is fixed:** `PARSE_MODEL` (`gpt-6-luna`), one id, the same for every user; its bbox convention is resolved as today ([parsing.md](./parsing.md) § Coordinate conventions). The format check runs on the parse model unless `CHECK_MODEL` names another. The parse agent's fallback model, if the operator sets one, is `PARSE_FALLBACK_MODEL`; a user has no model settings for `parse` or `check` any more — their prompt, instructions and switches stay.
- **The chat models are a short list:** `CHAT_MODELS` (comma-separated ids: `claude-sonnet-5-5, claude-opus-5-5, gpt-6-luna, gpt-5.6-terra, gpt-6-sol`), each shown with its credit rates. `GET /api/agents/chat/models` answers that list **intersected with the live catalog** of the operator's Kano Proxy, as the catalog is fetched today, so a model the proxy no longer serves is not offered; a stored choice the catalog dropped is still shown as the current one and refused at enqueue, as a vanished model is today. `agent_settings.model` for `chat` must be one of `CHAT_MODELS`; the default for a user who chose none is its first entry.
- **`MODEL_PRICES_JSON` must price every model the deployment offers** — `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL` and every `CHAT_MODELS` entry — as `{"<model>": {"input": <USD per 1M>, "cache_read": …, "cache_write": …, "output": …}}`. The API and the worker **refuse to start** with an offered model unpriced: a model nobody priced must never run for free. A chat turn whose model has no price (the list changed under a queued turn) fails with `turn_error` `model_unpriced` before its first call.
- **Nothing in `MODEL_PRICES_JSON` is product data the UI shows as a price**: the web app shows credits per 1K tokens and per page, derived from it and `CREDIT_USD` by the API (`GET /api/billing`), never typed into the frontend.

## Kano Pay

Payment is through **Kano Pay** (`pay.yuufeng.com`), the operator's payment service shared with Kano Proxy, and the contract is the one its own docs define (`kano-pay/docs/integrating-apps.md`): an App registered in Pay's `APPS_JSON` with a service key, a return URL, a webhook URL and its plans; `POST /v1/checkouts`, `GET /v1/subscriptions/:subjectId`, `POST /v1/portal`, `GET /v1/catalog`; signed notifications. Pay is moving its own backend from Paddle to **Dodo Payments**, which is the merchant of record (tax, invoices, refunds, the customer portal); the App contract does not change with it, so nothing here names the provider. Ema Paper is the second App on Pay after Kano Proxy, whose adapter (`kano-proxy-cloud/docs/billing.md` § Kano Pay adapter) this one copies.

- **Subject id** is the `users.id` as a decimal string. It never changes for an account, so a subscription found at Pay for a subject is that user's.
- **Checkout.** `POST /api/billing/checkout {plan_id}` (session + CSRF) refuses a plan not on sale (§ Plans) and, with Pay's `checkoutEnabled` false, answers 409. It then takes the user's `checkouts` row under their row lock: a row for the same plan reuses its `idempotency_key`; another plan replaces the row with a fresh key. The key is written **before** Pay is called, so a retry after an ambiguous timeout repeats the same request and Pay returns the same checkout rather than a second one. Pay's `checkoutUrl` is validated (https, Pay's own host) and returned as `{url}`; the browser goes there. Nothing is granted from the return redirect: the user comes back to `/files?settings=billing` and the page shows whatever `GET /api/billing` says, which is what the webhook or a refresh has written.
- **Webhook.** `POST /api/billing/pay-webhook` carries no session: it verifies `Kano-Pay-Signature` (`ts=<unix seconds>;h1=<hex HMAC-SHA256>` over `<ts>.<raw body>` with `KANO_PAY_WEBHOOK_SECRET`, constant-time; a timestamp more than 5 minutes off is refused), reads the body only for the event `id`, `type` and the subject, then **fetches the canonical subscription from Pay** (`GET /v1/subscriptions/:subjectId`) rather than trusting the body. In one transaction: insert the `pay_events` row (a duplicate id is a 200 and nothing else), and upsert `subscriptions` **only when the fetched `version` is newer** than the stored one. The subscription's plan must be in `KANO_PAY_PLANS_JSON` to grant anything; an unknown plan is stored with its id and grants Free. 2xx is answered only once the rows are committed. A body naming another App's id, or a subject that is not a user, is refused and logged by id only.
- **Refresh.** `GET /api/billing` reconciles with Pay at most once per `BILLING_REFRESH_SECONDS` (120) per user — the same fetch the webhook makes — so a missed notification is recovered on the next visit; a Pay outage leaves the stored row as it is and the response says `pay_reachable: false`. Nothing is reset or revoked because Pay could not be reached.
- **Portal.** `POST /api/billing/portal` returns Pay's fresh `{url}`; it is never cached and never shown to a user without an active or lapsed subscription of their own.
- **Catalog.** `GET /api/billing/catalog` is public, answers `{available, plans: [{plan_id, name, amount, currency, cycle, credits, storage_bytes, sandbox}]}` for the plans on sale, cached in memory for five minutes with `Cache-Control: public, max-age=300`; with Pay unconfigured or unreachable it answers `{available: false, plans: []}` with `no-store`, never an error and never a number the operator did not configure at Pay. The public pricing page and the billing section both read it ([web.md](./web.md)).
- **Secrets:** `KANO_PAY_API_KEY` and `KANO_PAY_WEBHOOK_SECRET` live in `.env` only, are never logged, and their presence is the only thing the API ever says about them. No customer or payment payload is logged or stored beyond the fields above.

## Routes

| Route | Auth | Answer |
|---|---|---|
| `GET /api/billing` | session | `{plan: {id, name, credits, storage_bytes, sandbox, max_jobs}, subscription: {status, plan_id, period_start, period_end, cancel_at_period_end} \| null, period: {start, end, granted, spent, remaining}, rates: {parse_per_page, chat: {<model>: {input, cache_read, cache_write, output}}}, checkout_available, pay_reachable}` — `rates.chat` is credits per 1K tokens for each offered chat model |
| `GET /api/billing/catalog` | none | § Kano Pay |
| `POST /api/billing/checkout` | session + CSRF | `{url}` |
| `POST /api/billing/portal` | session + CSRF | `{url}` |
| `POST /api/billing/pay-webhook` | `Kano-Pay-Signature` | `{}` |
| `GET /api/billing/entries?days=30` | session | `{days, per_day: [{date, parse, chat}], per_agent: [{agent, credits}]}` — the Usage tab's credits view, from `credit_entries` |

`GET /api/me` grows `plan` (`free` or the plan id) and `credits_remaining`, so the shell can show the balance without a second request; `POST /api/library/documents` and `POST /v1/parse` answer 402 as above; the `jobs` and `chat_turns` payloads grow `credits` (the entry's amount, `null` while a reservation is pending its settlement), so History and the thread can show what a parse or a turn cost ([web.md](./web.md) § The agent panel).

## Web

The screens are specified in [web.md](./web.md) and the design in the project's design canvas; the contract they render is this doc's. The settings dialog gains a **Billing** section: the plan, the period's credits as a meter with its reset date, the rates, *Upgrade* (a checkout) or the portal's three actions (payment method, invoices, cancel), and the sandbox and storage the plan gives. Credits appear in three more places: the upload confirmation ("20 pages, 20 credits — 130 left"), the 402 state of an upload or a send (with *Upgrade*), and under every answer in the thread (its credits). A public `/pricing` page reads the catalog. Copy goes through the i18n catalog as always; prices and credits come from the API.

## Configuration

| Var | Meaning |
|---|---|
| `KANO_PROXY_API_KEY` | the operator's Kano Proxy key, presented on every upstream call. Empty = no upstream: parses and turns are refused with 503, as an unconfigured Kano Proxy is today |
| `PARSE_MODEL`, `CHECK_MODEL`, `PARSE_FALLBACK_MODEL` | the parse pipeline's models (`CHECK_MODEL` empty = the parse model; `PARSE_FALLBACK_MODEL` empty = no fallback) |
| `CHAT_MODELS` | comma-separated ids the chat agent offers; the first is the default |
| `MODEL_PRICES_JSON` | list prices, USD per 1M tokens, for every model above (§ Models) |
| `CREDIT_USD` | what one credit is worth in list-price USD (0.002) |
| `PARSE_CREDITS_PER_PAGE` | credits per parsed page (1) |
| `CHAT_MIN_CREDITS` | the balance a chat send needs (1) |
| `FREE_CREDITS_PER_PERIOD`, `FREE_STORAGE_BYTES`, `FREE_MAX_JOBS` | the Free plan (150, 52428800, 1) |
| `KANO_PAY_URL`, `KANO_PAY_APP_ID`, `KANO_PAY_API_KEY`, `KANO_PAY_WEBHOOK_SECRET` | the Pay registration; all four set = Pay configured, otherwise every user is on Free and the billing section says checkout is not available |
| `KANO_PAY_PLANS_JSON` | `[{"planId": "pro", "credits": 5000, "storageBytes": 10737418240, "sandbox": true, "maxJobs": 3}]` — what a paid plan grants; the price is Pay's |
| `BILLING_REFRESH_SECONDS` | how often `GET /api/billing` may reconcile with Pay per user (120) |

`WORKSPACE_QUOTA_BYTES` and `MAX_JOBS_PER_USER` are the ceilings: a plan, Free included, configured past either refuses to start, so no plan promises what the deployment cannot carry.

## Transition

The service runs today on each user's own Kano Proxy key. The move is made in steps, each a PR with its docs, each leaving the service runnable, in this order; the `docs/` of a step are rewritten by that step, and until a step lands the docs it will touch describe what runs, with a pointer here where the two will differ.

1. **This doc and the schema** — the five tables, empty, their ORM models, the configuration above with its validation, and the credit arithmetic as a pure module. No behaviour changes: nothing reads the tables yet.
2. **The ledger** — reservation at `submit_parse`, settlement in the worker's terminal commit, the chat runner's per-call settlement and `out_of_credits`, `GET /api/billing` and `/entries`, 402s, `credits` on jobs and turns. Every existing user is on Free from this step; a user who had connected their own Kano Proxy still runs on it (step 4 ends that), charged in credits all the same, and a chat model they chose must be priced in `MODEL_PRICES_JSON` or its turn fails `model_unpriced`. Until step 3, `GET /api/billing` answers `checkout_available: false` and `pay_reachable: null`, and the plan's storage, concurrency and sandbox entitlements are not yet enforced (today's `WORKSPACE_QUOTA_BYTES`, `MAX_JOBS_PER_USER` and `sandbox_configured` still are): they take effect with the step that makes a paid plan buyable.
3. **Kano Pay** — Pay's own switch to Dodo first (in the kano-pay repository: docs, adapter, test mode, one real purchase and refund), then Ema Paper's registration in `APPS_JSON`, the checkout, webhook, portal and catalog routes here, `subscriptions` written for real.
4. **The operator's upstream** — `KANO_PROXY_API_KEY`, `PARSE_MODEL`, `CHAT_MODELS`; the connect flow, `kano_proxy_keys`, `kano_connect_states`, `provider_connections` and `agent_settings.connection_id` removed with their routes and UI ([auth.md](./auth.md) § 6, [api.md](./api.md) § Upstreams, [agents.md](./agents.md)); the model dropdowns replaced by the offered list. Jobs already queued on a user's own key finish on it: the worker resolves the key at run time, as today, and a key that is gone fails the job as a missing key does.
5. **The web app** — the Billing section, the credits moments, the public pricing page, and the plan's storage and sandbox in the Usage tab.
6. **Public pages** — Terms, Privacy, Refund, support address (Dodo's site review needs them).
7. **Domain and DNS**, then **deploy**: the migration, the new `.env` variables, one monitored real purchase.

Existing users land on Free with that month's grant; a trial of Pro for them is the operator's call and would be a `subscriptions` row written by hand — nothing in the code invents one.

## Acceptance

Unit tests, stubbed HTTP, no Pay or model call ever ([testing.md](./testing.md)):

- Credit arithmetic: a parse's credits from a pages spec; a turn's from token counts and prices, rounded up once over the turn, floored at 1; an unpriced model refused; configuration validation (every offered model priced, plans well-formed, ceilings respected).
- Periods: Free's month and a paid period from a subscription row and a clock; a lapsed row falls back; a plan change raises and never lowers `granted`; a period row created once under concurrent first use (PostgreSQL).
- The ledger (PostgreSQL): two parses racing for the last credits — one reserves, one gets 402; settlement idempotent (consume twice, release after consume); a release restores the balance; a chat overdraft ends the turn and is not carried over.
- The webhook: a wrong or stale signature refused; a duplicate event id answered 200 without a second write; a stale `version` ignored; an unknown plan grants Free; the canonical fetch is what is written, not the body.
- Checkout: the idempotency key stable across a retry; a plan not on sale refused; checkout closed refused.
- The migration: applies on a database holding existing users, jobs and usage rows, which it does not touch; the downgrade drops the five tables and nothing else.
