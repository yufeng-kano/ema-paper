# Jobs: queue, concurrency, dedup, retention

## Queue = PostgreSQL, nothing else

Jobs are rows in `jobs`. The worker claims work with:

```sql
UPDATE jobs SET status='running', started_at=now()
WHERE id = (
  SELECT id FROM jobs WHERE status='queued'
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED LIMIT 1
) RETURNING *;
```

Enqueue happens in the **same transaction** as job-row creation — no dual-write problem, no broker. If a queue swap (e.g. RabbitMQ) ever happens, it happens behind the small `queue` module interface; nothing else may know how claiming works.

The worker is the same codebase as the API, launched as its own process/container (`python -m sightread.worker`). Rendering runs in Poppler subprocesses (multi-core comes free); vision calls fan out per page with an asyncio semaphore.

One worker process runs one job at a time — concurrency inside a job is the page fan-out, concurrency across jobs is more worker processes. On `SIGTERM` the worker stops claiming; a job caught mid-run goes back to `queued` with its partial progress deleted, so a restart reparses it cleanly rather than resuming half a result.

## Concurrency caps (defaults)

- `MAX_JOBS_PER_USER` = 2 running jobs; excess `POST /v1/parse` gets 429 + `Retry-After` (fail closed, never crash). The web library's upload runs the same intake and hits the same cap, so a multi-file drop is a client-side queue that treats the 429 as back-pressure and retries ([web.md](./web.md) § Files) — the cap is not relaxed for a browser.
- `VISION_CONCURRENCY_PER_JOB` = 8 concurrent upstream calls.
- `RENDER_WORKERS` = CPU count (env-overridable) concurrent Poppler subprocesses per worker.

Fairness: the claim query above is FIFO; per-user cap is enforced at claim time too (skip a queued job whose user already has `MAX_JOBS_PER_USER` running).

## Progress

Worker updates `jobs.pages_done` and `job_pages` rows as pages finish; SSE endpoints read from PG (LISTEN/NOTIFY for wakeup, poll fallback). Terminal jobs replay their final event on reconnect.

Each finished page's markdown is stored on its `job_pages` row (`markdown`, NULL for a failed page), so a running job has a readable partial result: the control-plane result routes assemble the finished pages with the same `assemble` the final result uses and answer 200 with `meta.partial: true` while the job is queued or running ([api.md](./api.md)). The final `results` row is still written whole at terminal state and is the only thing the dedup cache ever serves; `requeue_job` deletes `job_pages` rows as before, partial markdown included, so an interrupted job reparses from scratch.

## Dedup cache

Key: `(user_id, agent, sha256, model, connection_id, connection_base_url, pages_spec, prompt_sha256, pipeline_version)` — `agent` because one agent's output never answers another's request ([agents.md](./agents.md)), `connection_id` because the same model id on two different endpoints is not the same model, and `connection_base_url` (the endpoint snapshot) because *editing* a connection's URL repoints what that id means: results parsed through the old endpoint must not answer uploads aimed at the new one. `pipeline_version` is the agent's own (`parse`: `PIPELINE_VERSION`).

The built-in upstream snapshots `KANO_PROXY_BASE_URL` as its `connection_base_url` (with `connection_id` NULL). Historical OpenRouter jobs have both NULL, which no new job ever has, so a result OpenRouter produced can never answer a new upload; and moving the deployment to another Kano Proxy origin likewise stops old results answering. The snapshot is taken at enqueue and **reconciled at claim time**: if the connection's URL (or `KANO_PROXY_BASE_URL`) changed while the job sat queued, the worker updates the job's snapshot to the endpoint it actually calls, so the cached result is keyed by the URL that truly produced it. A key rotation alone — editing a connection's key, or reconnecting Kano Proxy — does not invalidate the cache: same endpoint, same model, same output.

- On `POST /v1/parse`, hash while streaming to disk; if a **succeeded** job matches the full key and `force` is not set → return its result immediately (200, `meta.cached: true`), delete the fresh upload, create no job.
- **Only complete results are cache hits.** A result carrying page errors (a transient upstream failure, an unreadable page) is returned to its own job but never reused — the same upload reparses instead of replaying a degraded result forever.
- **A cache hit records no usage.** No upstream call runs, so no `usage_log` row is written — the original job's rows are the only record of what that result cost, with the `cost_source` they were recorded under. `report_cost` is not part of the key: it changes what gets recorded about a call, never the output.
- **Per-user only.** Never dedup across users: it would hand one user's paid output to another and leak that a document was parsed before.
- `force: true` always reparses (result row is replaced).
- **A retry must not buy a second parse.** The cache answers with *finished* work only, which leaves one gap: a browser cannot tell a lost response from a lost request, so it retries an upload that may already have landed, and the same bytes become a second job that bills the user's key twice for one document. `submit_parse(reuse_in_flight=True)` — asked for by the web library and by nothing else — answers such a retry with the job already queued or running for that exact key instead of starting another. The lookup is serialized by a transaction-level advisory lock on the dedup key (`hold_dedup_key`, PostgreSQL only), because reading before inserting protects a retry that arrives *after* the first upload committed but not two that overlap: both would find nothing and both would enqueue. `/v1/parse` does not do this: an API client that posts the same file twice means it, and has `force` besides.

## Retention — two layers, sweeper is the guarantee

1. **Immediate:** source file (and rendered page images) deleted the moment a job reaches `succeeded`/`failed` — figure crops are taken from the rendered page *before* it is deleted ([parsing.md](./parsing.md) § Figure crops). After that, reparsing requires re-upload — accepted trade-off, documented here on purpose. **Exception: a library document keeps its original.** When the finished job is the parse behind a `documents` row, the worker moves the source into that document's workspace directory as `source.<ext>` instead of deleting it, so the chat agent can re-render a page ([workspace.md](./workspace.md)); `source_deleted_at` is still set, since the upload directory no longer holds it. Retention runs *after* the terminal state is committed and is best effort: a copy that fails (the workspace vanished, the disk filled) is logged, and the job is `succeeded` either way — only once every copy is done is the upload removed and `source_deleted_at` set. The rows that receive a copy are read under a lock on the job's row, taken after the user's workspace lock ([workspace.md](./workspace.md) § Revisions, one order everywhere), and filing a parse (an upload landing, *Add to Files*) takes the same job lock before it inserts, so a row filed while the parse finishes either is among the recipients or is filed after, as a finished parse. The Files page's upload marks its job `retain_source` when it queues it, because its `documents` row lands only after the parse is queued: a worker that finishes first finds no row and leaves the upload where it is, and the route takes it into the document directory as soon as the row exists. When the upload is a dedup hit — the parse already exists — the fresh file is kept for that row as well instead of being discarded, so a document filed through the Files page always has its original.
2. **Sweeper:** periodic task inside the worker (every 15 min) trashes anything under the upload dir older than 24 h — catches crashed jobs, abandoned uploads, bugs. The sweeper is the guarantee; immediate deletion is an optimization. `FIGURES_DIR` is outside the upload dir precisely so the sweeper cannot eat stored crops.

Crops live exactly as long as something references them: a job that ends `failed` has its crop directory removed with the result it never wrote, and a shutdown requeue removes it with the rest of the partial progress — the reparse writes fresh crops, so nothing stale from the first attempt can answer a figure route.

Results (markdown + metadata in PG) and figure crops (`FIGURES_DIR`) are kept **indefinitely** for now (low traffic); revisit when storage says otherwise. Usage rows are permanent — they are the billing history. Workspaces are never swept ([workspace.md](./workspace.md)).

## Chat turns

The `chat` agent has its own queue, `chat_turns` ([chat.md](./chat.md)): one row per user message, claimed by the same worker loop with the same `FOR UPDATE SKIP LOCKED` query shape and a per-user cap of `MAX_CHAT_TURNS_PER_USER` running turns. The worker alternates between the two queues — a parse never starves a conversation and the reverse — and runs one claimed item at a time per process, exactly as for jobs; a turn interrupted by shutdown is marked `failed` with `worker stopped` (a turn the user had already cancelled keeps `cancelled`), stamped finished and given its terminal event, never requeued: its tool calls may have run, and replaying them is not safe. A turn whose task raised past the runner's own handling (a database error before the first event, say) is marked `failed` with `internal error` by the worker loop itself, so no turn stays `running` with nobody driving it. A worker that was killed outright cannot do either, so a worker that starts finishes every turn still `running` (or cancelled and not yet wound down) with `worker restarted` and its terminal event — there is one worker process per deployment, so a turn nobody is driving is exactly one a dead worker left. The sweeper does not touch turns.
