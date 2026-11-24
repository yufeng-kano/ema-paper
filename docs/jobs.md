# Jobs: queue, concurrency, dedup, retention

## Queue = PostgreSQL, nothing else

Jobs are rows in `jobs`. The worker claims work with:

```sql
UPDATE jobs SET status='running', started_at=now(), worker_generation=:generation
WHERE id = (
  SELECT id FROM jobs WHERE status='queued'
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED LIMIT 1
) RETURNING *;
```

- Enqueue happens in the **same transaction** as job-row creation — no dual-write problem, no broker.
- Claiming lives behind the small `queue` module interface; nothing else may know how claiming works, so a different queue would replace that module and nothing else.

The worker is the same codebase as the API, launched as its own process/container (`python -m sightread.worker`). Rendering runs in Poppler subprocesses (multi-core comes free). Vision calls fan out per page under a per-job asyncio concurrency limit (`VisionBudget`).

### One worker per deployment

One worker process runs one job at a time. Concurrency inside a job is the page fan-out (`RENDER_WORKERS`, `VISION_CONCURRENCY_PER_JOB`), not more worker processes.

There is **one worker per deployment**. The worker enforces it by holding a PostgreSQL session-level advisory lock (`pg_try_advisory_lock`, one fixed key) for its whole life.

- A second worker process waits for the lock rather than starting. In a rolling restart the one being replaced releases it as it stops. The reason: the recovery a worker runs at start treats every `running` row as a dead worker's, which is only true of a singleton.
- The lock is the session's. PostgreSQL releases it with the session (a restart, a dropped connection) and hands it to the waiting worker. So the holder pings its lock session every few seconds and, the moment the session is gone, stops and cancels the job or turn in flight rather than finish a parse beside the worker that took it over.

### Generations

The loss of the lock is noticed seconds late. In those seconds the replacement has already requeued — and may have claimed — the job the stale worker is still running. So every worker takes a **generation** with the lock:

- The generation is the next value of the `worker_generation_seq` sequence. The SQLite fallback takes the highest on record plus one.
- The worker writes it onto every job it claims (`jobs.worker_generation`, the `:generation` above).
- It carries it on every write it makes to a running job, each fenced on the row still holding that generation: a page recorded, the terminal state and the retention that follows it, the requeue on shutdown, and the endpoint snapshot a run updates at its start. A run whose snapshot update fences out ends there.
- A requeue clears it.

A stale worker's writes therefore land nowhere:

- The page it finishes is not recorded. The upstream calls it made are still logged as usage, since the operator's key paid for them.
- Its result is not written over the replacement's.
- Its shutdown requeue does not put back a job the replacement owns.

### Attempt directories

A stale worker's files are its own as well:

- Every attempt renders pages into a work directory of its own and crops figures into `FIGURES_DIR/{job_id}.{attempt}/`.
- The crops are published as `FIGURES_DIR/{job_id}/` only by the finish that passed the fence, under the same shared hold of the backup lock as the terminal commit. So a backup never carries a succeeded result whose crops still sit in an attempt directory ([parsing.md](./parsing.md) § Figure crops, [workspace.md](./workspace.md) § Backups).
- A stale attempt's discard — on a cancel, or when its finish was fenced out — takes only its own crops, never the replacement's, and nothing of the two attempts mixes.
- A worker starting removes every attempt directory of a job it requeues with the published one.
- A cancelled page's image preparation, which runs in a thread, is waited for before the work directory is removed and the job requeued, so no thread writes into the next attempt's directory.

### The fence on a chat turn

A chat turn is fenced by its terminal state instead of a generation:

- The replacement finishes the stale worker's turn as an orphan.
- The stale worker's loop stops at its next check when it finds the turn finished, as on a cancel.
- The stale worker's own terminal write leaves a finished turn as it is.
- A message it would store — or the model change a fallback answer would record first — is written in one transaction with that check, under the conversation's lock, so a finished turn takes no more history.

### Interrupted and failed jobs

- **Shutdown.** On `SIGTERM` the worker stops claiming. A job caught mid-run goes back to `queued` keeping the pages it finished and nothing else of the attempt — its failed pages' rows and its crops go — so the next attempt transcribes only what is missing (§ Retrying failed pages). A page already paid for is never transcribed twice for its job. A page whose record is being written when the cancel lands is recorded first, its calls in `usage_log`; a succeeded one is kept.
- **A task that raised.** A job whose task raised past the runner's own handling (a database error while the terminal state was being written) is marked `failed` with `internal error` by the worker loop, as a chat turn is. It is never requeued, since the parse ran and billed. So no row stays `running` with nobody driving it, against its user's cap.
- **A parse a chat turn drove.** A parse a chat turn drove itself ([chat.md](./chat.md) § Tools) that is still `running` under the worker's generation once the turn has ended — its requeue raised on a database error, say — is put back on the queue by the worker right after the turn, keeping its succeeded pages, as on a shutdown. The worker runs one thing at a time, so once the turn is over nothing drives it. Left `running` under the live generation it would wait for the next restart, which the console cannot hasten (it requeues only a stuck job).

### Start-up recovery

1. A worker that starts puts every job still `running` back on the queue, its crops discarded and its failed pages forgotten, as on a shutdown. There is one worker per deployment, so a job nobody is driving is one a worker that was killed outright held.
2. Then it finishes the orphaned turns (§ Turns a dead worker left).
3. After the orphaned turns, the worker sweeps the writing markers still standing — shell commands the runner could not confirm over in turns that ended ([workspace.md](./workspace.md) § Revisions):
   - It restarts each one's shell and records the tree.
   - A marker that cannot be read names no shell, so the VM is stopped whole instead.
   - A user whose runner does not answer is left for the next pass. Meanwhile no parse of theirs runs, and no chat turn of any user (§ Turns a dead worker left).
   - A worker with no runner configured (`SANDBOX_URL` or `SANDBOX_TOKEN` not set in its environment) treats a standing marker the same way. It has nothing to stop the shell with, so the orphaned turn and the marker wait, and the user's workspace stays closed, rather than being declared safe over a VM that may still run the command.

### From the operator console

The queue's side of [admin.md](./admin.md) § Overview, jobs and usage.

Whether the worker runs is read from `pg_locks` alone:

- The lock above shows there as a granted `advisory` lock in the current database whose `classid` is the key's high 32 bits, `objid` its low 32 bits and `objsubid` 1 — the form PostgreSQL gives a lock taken on one bigint.
- The live generation is `worker_generation_seq`'s last value, read without taking one.
- Nothing in the API ever calls `pg_try_advisory_lock` or `nextval` for it. A probe that tried the lock while the worker was down would hold it, and a session lock left on a pooled API connection would keep every worker from starting.

A `running` job is **stuck** when no worker holds the lock, or the one that does runs with another generation than the job's. Nobody drives it until a worker starts and requeues it.

An operator with `jobs.write` can requeue a stuck job before then. The requeue is the start-up's:

- It runs under the job's row lock.
- Its crops are discarded — every attempt directory, before the commit — so a worker that claims the job a moment later cannot lose a new attempt's crops.
- `requeue_job` puts it back keeping its succeeded pages, without committing, so the change and its audit record commit together.
- Its credits stay reserved.

A job the live worker runs is never requeued from the console, nor is a queued or finished one, and a chat turn never is.

## Concurrency caps (defaults)

- `MAX_JOBS_PER_USER` = 2 running jobs, the ceiling.
  - Intake caps a user at their plan's `maxJobs` under it (Free: `FREE_MAX_JOBS` = 1, [billing.md](./billing.md) § Plans).
  - Excess `POST /v1/parse` gets 429 + `Retry-After: PARSE_RETRY_AFTER_SECONDS` (30; fail closed, never crash).
  - The cap is asked only of an upload about to start a parse, after the dedup cache and the in-flight join. One the cache answers, or that joins its own parse still running, starts nothing and is never refused by it.
  - The web library's upload runs the same intake and hits the same cap, so a multi-file drop is a client-side queue that treats the 429 as back-pressure and retries ([web.md](./web.md) § Files). The cap is not relaxed for a browser.
- `VISION_CONCURRENCY_PER_JOB` = 8 concurrent upstream calls.
- `RENDER_WORKERS` = CPU count (env-overridable) concurrent Poppler subprocesses per worker.

Fairness:

- The claim query above is FIFO.
- The per-user cap is enforced at claim time too: the claim skips a queued job whose user already has their cap running.
- The claim reads the cap from the user's `subscriptions` row as it stands then, or — with no paid period running — from the open granted period of their `plan_grants` row ([billing.md](./billing.md) § Periods, *Granted days*). A paid or granted period that lapsed between intake and claim runs the queued jobs one at a time, as Free does — under `MAX_JOBS_PER_USER`.
- The plans' `maxJobs` and `FREE_MAX_JOBS` the claim applies come from the settings the worker takes before each claim, the database's overrides on `.env` ([admin.md](./admin.md) § Settings). A plan changed on the console's Settings page counts from the worker's next read of them, within `RUNTIME_SETTINGS_CACHE_SECONDS`. The claimed job then runs on that same snapshot to its end.

## Progress

Worker updates `jobs.pages_done` and `job_pages` rows as pages finish. SSE endpoints read from PG (LISTEN/NOTIFY for wakeup, poll fallback). Terminal jobs replay their final event on reconnect.

A stream holds no connection of the request's. The route closes its session before streaming (`POST /v1/parse` and `GET /v1/jobs/{id}/events` alike). The stream reads on short sessions of its own and listens on a connection outside the pool, so streams that last a whole parse never use up the API's pool.

A running job has a readable partial result:

- Each finished page's markdown is stored on its `job_pages` row (`markdown`, NULL for a failed page).
- The control-plane result route assembles the finished pages with the same `assemble` the final result uses, and answers 200 with `meta.partial: true` while the job is queued or running ([api.md](./api.md) § Partial results).
- The final `results` row is still written whole at terminal state, and is the only thing the dedup cache ever serves.
- `requeue_job` keeps the succeeded rows, their markdown included, and deletes the failed ones; `pages_done` restarts at the kept count.
- A failed job keeps all its rows: they are what a retry starts from.

A job that carries a format check (`jobs.check_model`) is still `running` while it is checked, after its last page ([parsing.md](./parsing.md) § Format check). The library payload says `checking` for a running job with a check whose every page is done ([api.md](./api.md)).

## Retrying failed pages

A parse with a failed page fails whole ([parsing.md](./parsing.md) § Vision-only conversion): it has no result to open, only its original ([web.md](./web.md) § Files, *A failed parse opens on its original*), and it is retried.

`POST /api/library/documents/{id}/retry` ([api.md](./api.md)) puts the job behind the document — or the document's failed re-convert (§ Re-converting a document) — back on the queue, keeping every page that succeeded:

- The job goes back to `queued`, its error, start and finish cleared.
- The failed pages' rows are deleted and `pages_done` restarts at the kept count, in the transaction that requeues it.
- That transaction is decided under the user's workspace lock and the job's row lock (the one order, [workspace.md](./workspace.md) § Revisions), and only from `failed`, so two retries cannot both put it back.
- The same transaction takes the job's credits back ([billing.md](./billing.md) § A parse): under the user's row lock, taken between the workspace's and the job's, the entry its failure released is reserved again from the current period. A balance that cannot cover it answers **402** with nothing requeued.

The run that claims it:

- **transcribes only the pages without a succeeded row** (`job_pages`), on the model the retry runs:
  - the model the retry named (`jobs.retry_model`);
  - else the job's own `model` while the deployment still offers it, one of the parse agent's two (`jobs.retry_model` NULL);
  - else `PARSE_MODEL`, written as `jobs.retry_model`;
- **sends a page that model blocks to the job's fallback**, if it has one the deployment still offers ([parsing.md](./parsing.md) § Fallback model). A fallback it no longer offers is cleared from the job (`jobs.fallback_model` NULL) by the retry, so the page fails instead;
- **renders the kept pages that carry figures again**, to crop them from their stored markdown: a render is ours to pay for, a transcription is the user's, and an attempt's crops come from that attempt alone ([parsing.md](./parsing.md) § Figure crops). Their boxes were checked when they were transcribed, and their statuses come with them: a kept page is not checked again ([parsing.md](./parsing.md) § Figure check);
- **assembles every page, kept and new,** and checks the document when the job carries a check, then commits the result like any result. A page that fails again, or a check that fails again, fails the job again, with nothing lost.

**A job whose `connection_base_url` is NULL (it ran on OpenRouter or on a user's provider connection) is never run on Kano Proxy.**

- A retry of it is accepted — its credits reserved again, as any retry's.
- The worker fails it when it takes it, through its normal finish, the reservation released and the original kept, with *this parse ran on an upstream this deployment no longer has; upload the file again*.
- Kano Proxy would otherwise transcribe only the pages the other upstream failed, and the mixed result, re-stamped with `KANO_PROXY_BASE_URL` at claim, would answer Kano Proxy dedup lookups.

**The retry takes the format check again from the settings of its moment**, as an upload would ([agents.md](./agents.md) § Enqueue rules):

- A check that failed may have failed on its model, and a reader who turned the switch off retries without one.
- A check with no model of its own follows the parse's, which on a retry is the model the retry runs (the one it named, else the job's own while offered, else `PARSE_MODEL`). So a retry that moves a blocked page to another model does not send its text back to the first one in the check.
- A parse that failed only its check has every page kept, so its retry sends no page to a model at all.

**A retry runs on the document's original** ([workspace.md](./workspace.md)), because the upload was deleted when the job failed:

- The route copies the original into the upload directory as the job's source (`source_path` set, `source_deleted_at` cleared).
- The run's retention deletes that copy again — every recipient already holds its original.
- A requeue whose commit raises is read back on a connection of its own, as a filing is ([workspace.md](./workspace.md) § Revisions):
  - landed: the retry stands on its copy and is answered as queued;
  - rolled back: the copy is removed and the answer says nothing changed;
  - unreadable: the copy stays — a retry that landed needs it, and the upload sweeper takes it otherwise — and the answer says the retry may have been queued.

**From the library, only a failed `parse` behind a document with an original is retried:**

- A document whose original was not kept — the workspace was at its quota — answers 409.
- So does one whose original is still being filed: the moment after the failure, until its retention has run (§ Retention). Retried a moment later, it runs.
- A parse made on the data plane — the REST API's or the connector's — kept no original either, so its retry brings the file again (below).

**A retry is not an upload:**

- No per-user cap refuses it. The claim applies the cap to it like to any queued job.
- It changes nothing of the dedup key: the job keeps its model, prompt and endpoint snapshot, and a result it finishes answers that key like any other.

**The chat agent retries a library parse as the route does.** `convert_file` with `retry` ([chat.md](./chat.md) § Tools) calls the function the route calls (`intake.retry_library_parse`): the same locks, a copy of the same original, the credits reserved again, the check from the settings of the moment. It runs on the route's default with no `model`: the job's own model while the deployment offers it, else `PARSE_MODEL`. It then claims the job and drives it inside the turn as a conversion is driven (§ Converting a file). It retries only a document's own failed parse: a failed re-convert is the reader's, retried or dismissed from Files.

**A data-plane parse is retried by sending its file again** (so the connector gets the web's retry, [mcp.md](./mcp.md)): `POST /v1/parse` with `retry` ([api.md](./api.md)).

1. The upload streams to disk and is hashed as any upload is.
2. Then, instead of the cache, it looks for the newest parse of the caller's — by `created_at` — with the same `sha256` and `pages` that the retention still keeps (§ Retention), whether a document names it (`job_id` or `reconvert_job_id`) or not.
3. The newest is the one that counts:
   - **None** — **409**, the upload deleted and a ticket not spent.
   - **Queued or running** (a retry already sent) **or succeeded** (the file was parsed again since the failure, on the data plane or in Files) — the retry is answered with it, the upload deleted and nothing charged, so an older failure the caller no longer needs is never run and billed again.
   - **Failed, and a document names it** — the library's to retry, from its original. Every caller has it retried so, and a parse the connector filed into Files is retried where it is ([mcp.md](./mcp.md) § Filing into Files):
     - through `retry_filed_parse`, under the workspace lock taken after the upload's transaction ends;
     - on the live document whose own parse it is (the lowest id when one parse was filed twice);
     - under the same dedup key's hold a plain upload takes;
     - the upload deleted and a ticket spent on the job;
     - a second retry that finds it queued by then is answered with it;
     - a failed re-convert or a parse only the Trash names gets **409** saying the library retries it.
   - **Failed, and no document names it** — put back as above:
     - under the dedup key's hold that a plain upload of the same bytes takes (so the two never both start a parse), then the user's row lock and the job's;
     - from `failed` only;
     - its credits reserved again (**402** otherwise, nothing requeued);
     - the check taken from the settings of the moment;
     - the failed pages run on the `model` the upload names — one of the parse agent's two, the fallback for a page the first one blocked — or else the job's own while the deployment offers it, else `PARSE_MODEL`;
     - the upload becomes the job's source under the job's own name (`{job_id}.retry-<random>.<ext>`, so whatever removes a job's uploads by its id finds it), and the run's retention deletes it as any data-plane source;
     - a requeue that raises before its commit removes it; one whose commit raises leaves it — a retry that landed runs on it, and the upload sweeper takes it otherwise;
     - a ticket is spent on the job, so `last` reads it.

Two retries of one parse cannot both put it back: the second finds it queued and joins it, as an upload joins a parse in flight (§ Dedup cache). Like the library's, the retry is no upload: the per-user cap does not refuse it, and nothing of the dedup key changes. `retry` with `force` is 400.

## Re-converting a document

A document whose parse succeeded can be parsed again, whole, on a model the reader picks: `POST /api/library/documents/{id}/reconvert` ([api.md](./api.md)). It is for a parse that came out wrong in a way only a new run fixes: a better prompt or pipeline since, or a model that boxed figures badly. **The document keeps the version it has until the new parse succeeds**, so it never stands unopenable or half-replaced.

Only a library document that meets all of these is re-converted:

- Its own parse succeeded. A failed parse is retried instead, a running one is waited for (409).
- It kept an original. A document that kept no original is uploaded again (409). One whose original is still being filed, the moment after its parse ended, is re-converted a moment later (409, § Retention).
- No re-convert is queued, running, or succeeded and still being taken (409).

A markdown document has no parse (404).

- **It is a new parse of the document's original**, a new job like any other.
  - It runs as a Files upload of that file would, with the parse agent's settings of its moment ([agents.md](./agents.md) § Enqueue rules): the model the reader named, else the agent's own; its prompt; its upstream; its fallback model; the format check.
  - It is never answered from the dedup cache or joined to a parse in flight: the cached answer is the one being replaced.
  - The per-user cap refuses it as it refuses an upload (429).
  - The route streams the original into the upload directory as the job's source, under the workspace lock, as a retry copies it. The run's retention deletes that copy: the document already keeps its original.
- **While it runs, the document points at it** (`documents.reconvert_job_id`, [database.md](./database.md)).
  - The pointer is set in the commit that queues the job, so no re-convert runs that its document would not take.
  - Nothing else about the document changes: it opens, its text and notes save, the agent works on it, all on the version it has.
  - A copy made meanwhile is a copy of that version, and the re-convert stays the document's own.
  - The library row carries the new parse's live state as `reconvert` ([api.md](./api.md)).
- **When it succeeds, it becomes the document's parse.** Under the workspace lock:
  1. The document's directory as it stands is recorded first.
  2. The directory takes the new parse as one revision, `reconvert <path>`: `doc.md` from the new result as materialization writes it (the reading copy when the parse was checked) and `.meta.json` for the new job. `notes.md` is not touched.
  3. The row's `job_id` names the new job and the pointer clears.
  4. The new crops are copied in as for any parse. The earlier parse's crops that the new one does not hold leave the directory's `figures/`: the shell's copies only. The crops themselves stay with that parse, where the figure route still finds them (below).
  - Files before the row: a commit that fails leaves the pointer standing over files already in place, and the next taker finishes it.
  - A swap that cannot write or record its two files puts both back as they were: the document opens on the version it has, and the next read tries again.
  - An edit made while the parse ran is replaced too, and stays in the document's revisions like every text before it ([workspace.md](./workspace.md) § Revisions).
  - The earlier parse stays, its result still the record of what it produced. It is a parse of the document's original, which the retention keeps for as long as the document stands (§ Retention), and its crops are what the figure route falls back on (below).
- **Who takes it.** The worker does this right after the parse's terminal commit, where it materializes a new upload's directory.
  - The document's `content` and `result` routes do it first when they find a succeeded re-convert still pointed at: a worker that stopped between the two. One they cannot take leaves the document opening as it is.
  - A document in the Trash when its re-convert succeeds keeps its version: the pointer clears, and the new parse, a parse of the same original, is kept as long as the document is (§ Retention).
- **When it fails, the document keeps its version**, and the pointer stays, so the row says so.
  - A failed re-convert is retried like a failed parse (§ Retrying failed pages): the retry route sends its failed pages and keeps the rest, and a retry that succeeds swaps the document as above.
  - Or it is dismissed (`DELETE …/reconvert`): the pointer clears, and the failed parse, of the same original, is kept with the document like the others (§ Retention).
  - A new re-convert replaces a failed one the same way.

A figure placeholder from before the swap — in a reverted text or quoted in the notes — keeps its crop: the figure route looks for a crop the document's parse does not hold among the user's other succeeded parses of the same original ([parsing.md](./parsing.md) § Figure crops).

## Converting a file

A file kept in the library ([workspace.md](./workspace.md) § Files) that intake would parse — a PDF or an image — becomes a document on request: `POST /api/library/documents/{id}/convert` ([api.md](./api.md)). It is requested:

- from the file's menu, or by *Markdown* chosen in its viewer ([web.md](./web.md) § Files);
- on arrival, while the parse agent's `auto_convert` is on (off unless the reader turns it on, [agents.md](./agents.md)): the same thing done by intake before the bytes ever land in the tree.

It is an upload's parse run late:

- **The file becomes the job's source.** Under the workspace lock the route:
  1. reads the file in place and copies it into the upload directory, hashed on the way as an upload is and named `{job_id}.<ext>`; a file over `UPLOAD_MAX_BYTES` answers 413 and nothing is queued;
  2. queues the parse with the parse agent's settings of its moment: its model; its prompt; its fallback; the format check ([agents.md](./agents.md) § Enqueue rules);
  3. moves the file out of the tree, aside as a delete moves a directory;
  4. turns the row into a parse naming the job: `kind` `parse`, `job_id` set, `media_type` and `size_bytes` cleared, `view` set to the one the request named, if any (the result viewer names `markdown`, [api.md](./api.md)).
  - The parse is committed as an upload's is, and the row in a commit of its own. A row that does not commit brings the file back from aside; once it has committed, the copy aside is discarded.
  - Its id, name and place do not change, so a link to the file opens the document.
  - The credits are reserved first, as for an upload: a balance that cannot cover them (402) changes nothing ([billing.md](./billing.md) § A parse).
  - The per-user cap and the dedup cache apply as to an upload. A cap hit answers 429 with the file still a file. A cache hit is `succeeded` at once, and the route files the file's fresh copy as the document's original right away, instead of the worker (§ Retention).
- **When it ends, the worker files the source** as the document's original, a failed parse's too, so the file is never lost to the error (§ Retention).
  - Once the parse has succeeded, the worker has `source.<ext>` copied into the directory it materializes at the file's path, as for any library upload (§ Retention).
  - The filing runs to its end whatever cancels the run meanwhile.
  - It is not refused by the quota: the file was counted in the tree.
  - The source waits for it in the upload directory however long the parse is queued (§ Retention).
  - The document then opens, or, failed, opens on its original with the retry there (§ Retrying failed pages).
- Converting is not undone from the Files page: the file has become the document's original, and *Re-convert* is what runs it again.
- A file intake would not parse answers 409 `not_convertible`; a document answers 404.
- **From the chat agent.** `convert_file` ([chat.md](./chat.md) § Tools) runs this same conversion on a file of the library the agent names: the same intake, credits, cap and cache, through the one function the route calls (`intake.convert_kept_file`).
  - Since the worker is busy with the turn that asked, the tool claims the job itself (`queue.claim_job`, with the worker's generation) and drives it with `run_job` inside the turn, on the turn's settings snapshot.
  - A Stop, the tool's timeout or the end of the turn requeues it keeping its finished pages, and the worker takes it next. A job is not claimed once one of them has come.
  - A parse that reaches its end meanwhile, failed or succeeded, files its original before the tool answers ([chat.md](./chat.md) § Tools, *A conversion runs inside the turn*).
  - A conversion refused — not a file, not convertible, credits, cap — changes nothing, as the route's refusals do.
  - With `retry`, it retries a document whose own parse failed the same way (§ Retrying failed pages).

## Credits

A parse is charged in credits when it is queued and settled when it ends ([billing.md](./billing.md) § A parse):

- The reservation is written in the commit that creates the job.
- `succeeded` consumes it; `failed` releases it.
- A retry reserves it again (§ Retrying failed pages).
- A dedup hit or an in-flight reuse costs nothing.

The worker settles in the same commit that writes the terminal state, idempotently, so a requeue that raced a finish cannot charge twice.

The user's row is locked before the job is inserted, not after: the insert's foreign-key check already holds a share lock on that row, and two uploads taking the exclusive lock after it would deadlock instead of queueing one behind the other.

A chat turn's entry is closed by whichever path finishes the turn, the runner's own or `fail_turn` ([billing.md](./billing.md) § A chat turn).

What a job is charged and which models it runs on are fixed when it is queued — the reservation, and the job's model, check model and fallback — so a settings change made on the console afterwards ([admin.md](./admin.md) § Settings) does not reach a job already queued.

## Dedup cache

Key: `(user_id, agent, sha256, model, connection_base_url, pages_spec, prompt_sha256, pipeline_version)`.

- `agent`: one agent's output never answers another's request ([agents.md](./agents.md)).
- `connection_base_url` (the endpoint snapshot, below): moving the deployment to another Kano Proxy origin repoints what a model id means, so results parsed through the old endpoint must not answer uploads aimed at the new one.
- `pipeline_version`: the parse pipeline's (`agents/parse.py` `PIPELINE_VERSION`); only `parse` has jobs.

The endpoint snapshot:

- Every job snapshots `KANO_PROXY_BASE_URL` as its `connection_base_url`.
- A job from an upstream the deployment no longer has — OpenRouter, or a user's provider connection — has `connection_base_url` NULL, which no new job has, so its result never answers a new upload.
- The snapshot is taken at enqueue and **reconciled at claim time**: if `KANO_PROXY_BASE_URL` changed while the job sat queued, the worker updates the job's snapshot to the endpoint it actually calls, so the cached result is keyed by the URL that truly produced it.
- A rotation of `KANO_PROXY_API_KEY` alone does not invalidate the cache: same endpoint, same model, same output.

The rules:

- On `POST /v1/parse`, hash while streaming to disk. If a **succeeded** job matches the full key — one the retention keeps for at least another day (§ Dedup margin) — and `force` is not set: return its result immediately (200, `meta.cached: true`), delete the fresh upload, create no job.
- A result some of whose pages a fallback or a retry's model wrote is complete, and answers its key ([parsing.md](./parsing.md) § Fallback model).
- **A cache hit records no usage.** No upstream call runs, so no `usage_log` row is written. The original job's rows are the only record of what that result cost, with the `cost_source` they were recorded under.
- **Per-user only.** Never dedup across users: it would hand one user's paid output to another and leak that a document was parsed before.
- `force: true` always reparses: a new job, with a result row of its own.
- **A retry must not buy a second parse.** The cache answers with *finished* work only, which leaves one gap: a browser cannot tell a lost response from a lost request, so it retries an upload that may already have landed, and the same bytes become a second job that charges the user's credits twice for one document.
  - Every upload that is not `force` (the web library, `/v1/parse`, a chat conversion) answers such a retry with the job already queued or running for that exact key instead of starting another: the in-flight join.
  - The lookup is serialized by a transaction-level advisory lock on the dedup key (`hold_dedup_key`, PostgreSQL only), because reading before inserting protects a retry that arrives *after* the first upload committed, but not two that overlap: both would find nothing and both would enqueue.
  - A retry that arrives after the first request's job has already finished — failed, or succeeded outside the cache's retention — is answered with that job too, when it finished within `UPLOAD_RETRY_WINDOW_SECONDS` (60) of the retry. The browser retries within seconds, and the same bytes filed twice in that window are one upload whatever became of it.
  - Past the window the normal rule holds, and the same bytes parse again.
  - `/v1/parse` joins as the library does, though an API client posting the same file twice might mean it: the connector re-runs a command the shell cut off, and its notes tell it to upload the same file again after a spent ticket.
  - `force`, which skips the cache and this lookup alike, is how a caller asks for a second parse.

## Retention — two layers, sweeper is the guarantee

1. **Immediate:** source file (and rendered page images) deleted the moment a job reaches `succeeded`/`failed`.
   - Figure crops are taken from the rendered page *before* it is deleted ([parsing.md](./parsing.md) § Figure crops).
   - After that, reparsing requires re-upload — accepted trade-off, documented here on purpose.
   - **Exception: a library document keeps its original.** When the finished job is the parse behind a `documents` row, the worker files the source as that document's original — `{WORKSPACES_DIR}/.repos/{user_id}.sources/{document_id}.<ext>`, outside the VM's mount — and puts a copy of it in the document's workspace directory as `source.<ext>` for the shell, instead of deleting it, so the chat agent can re-render a page ([workspace.md](./workspace.md)).
     - Whatever its terminal state: a failed parse's row keeps its original as a finished one does, so the upload is not lost with the error, and a row that lands after a failed parse takes the upload the same way.
     - The `source.<ext>` copy goes only into a directory already materialized, one that holds `.meta.json`. A failed parse's has none until a retry succeeds, nor has a parse's whose materialization has not run yet. A lone `source.<ext>` there would read as a folder's file, renamed by the reconciliation and counted twice ([workspace.md](./workspace.md) § Quota). The materialization that writes `.meta.json` makes the copy from the original.
     - This holds for every filing of an original: the worker's, the sweep's and a row's that lands after its parse (`keep_source`).
     - `source_deleted_at` is still set, since the upload directory no longer holds it.
   - **Retention runs *after* the terminal state is committed.**
     - It runs under a shared hold of the backup lock taken before that commit and kept until the original and the copies are in place, so a backup never captures the finished rows without them ([workspace.md](./workspace.md) § Backups).
     - The whole finalization of a committed terminal state — a result's or a failure's: retention and the document copies — runs to its end under a worker shutdown rather than being cut by it, since a cancel landing between the commit and the copies would release the locks under a copy still being made, and the requeue that follows the cancel leaves a job already `succeeded` as it is, crops included.
     - It is best effort: a copy that fails (the workspace vanished, the disk filled) is logged, and the job is `succeeded` either way.
     - Only once every copy is done is the upload removed and `source_deleted_at` set.
   - **Until then, the upload answers as the original of the row it is filed for** — the last row naming the parse that holds no original, which it is moved to whatever the quota. The library's `original` and `…/original` read it ([api.md](./api.md)).
     - So the moment between a parse's end and its filing never reads as an original lost.
     - A retry or a re-convert, which copies the original, waits for it (409 meanwhile, § Retrying failed pages).
   - **The rows that receive a copy are read under a lock on the job's row:** in the terminal commit itself, before it decides whether the upload is kept for a row or deleted, and again in retention, after the user's workspace lock ([workspace.md](./workspace.md) § Revisions, one order everywhere).
     - Filing a parse (an upload landing, a copy of a document whose parse is still running) takes the same job lock before it inserts. So a row filed while the parse finishes either is among the recipients or is filed after, as a finished parse — never filed in the gap and left without its original.
     - A row filed after the terminal commit but before retention holds the workspace lock finds no original yet and takes the upload as its own. Retention does not then find the upload gone and keep nothing: a recipient that already holds an original is left alone, and the rest take a copy of the original any recipient holds. So the ones found at the terminal commit keep theirs whichever side got to the upload first.
   - **The Files page's upload marks its job `retain_source` when it queues it**, because its `documents` row lands only after the parse is queued. A worker that finishes first finds no row and leaves the upload where it is, and the route takes it into the document directory as soon as the row exists.
     - When the upload is a dedup hit — the parse already exists — the fresh file is kept for that row as well instead of being discarded, so a document filed through the Files page always has its original.
2. **Sweeper:** periodic task inside the worker (every 15 min). The sweeper is the guarantee; immediate deletion is an optimization.
   - **First, it finishes the retention a finished library parse never got** — the worker died between the terminal commit and the copies, or a copy raised. A `succeeded` or `failed` job a document names whose source is still in the upload directory with `source_deleted_at` NULL has its retention run then, as the worker would have run it, so the original lands instead of aging out (a converted file has no other copy).
   - **Then it deletes anything under the upload dir older than `UPLOAD_MAX_AGE_SECONDS`** (86400, a day), except:
     - what is named for a job still `queued` or `running` (`{job_id}.<ext>`, its retry copies, its render directories): that is the job's source, a converted file's only copy, and a parse that waits longer than a day for the worker still runs on it;
     - what is named for a finished job whose retention is still to run — a document names it and `source_deleted_at` is NULL: one whose retention failed in this very pass, or that finished after the pass looked — which the next sweep files.
   - So it catches crashed jobs, abandoned uploads, bugs, and the chat agent's attachments that were uploaded but never sent (`chat-attachment-*`, [chat.md](./chat.md) § What a user message carries). A send embeds and deletes its own, and the attachments route counts and drops their rows by the same age.
   - `FIGURES_DIR` is outside the upload dir precisely so the sweeper cannot eat stored crops.
   - The same pass also:
     - packs each workspace's git history once it holds `WORKSPACE_GIT_PACK_LOOSE_OBJECTS` loose objects, under that workspace's lock ([workspace.md](./workspace.md) § Revisions);
     - removes from each workspace the staging file of a file upload the API process died in — one at the top of `files/` whose lock no upload holds ([workspace.md](./workspace.md) § Files) — with no workspace lock, the file's own being the arbiter;
     - deletes for good what has been in the Trash longer than `TRASH_RETENTION_DAYS` ([workspace.md](./workspace.md) § The Trash);
     - deletes the finished parses no file names once they are `PARSE_RETENTION_DAYS` old (below);
     - finishes the account deletions a request left half done, from their markers ([auth.md](./auth.md) § Deleting the account).
   - The Trash's and the parses' passes go user by user, each user under their workspace lock (which takes the backup lock shared, [workspace.md](./workspace.md) § Backups).
     - A user whose pass is refused — a conversation about a file it would delete still answering (409), a workspace recovering from a shell command it could not confirm finished — is skipped, logged and tried again at the next sweep.
     - Any other error in one user's pass is logged and the sweep goes on to the next user.
   - A sweep that raises past all of that is logged as well, and the next one runs on schedule: one failing pass never ends the sweeper for the rest of the worker's life.
   - The sweeper reaches the database through the worker's own sessionmaker and settings.

**A finished parse is kept `PARSE_RETENTION_DAYS` (30) days, or as long as a file needs it.** The rule is written once, as one SQL condition in `jobs/queue.py` that the purge below and the dedup cache (§ Dedup margin) both use. A parse is **kept** while:

- it is queued or running;
- it finished less than `PARSE_RETENTION_DAYS` days ago;
- any `documents` row of the same user — live or in the Trash — names a parse (`job_id` or `reconvert_job_id`) of the same original (`jobs.sha256`);
- an upload ticket that has not expired names it (`upload_tickets.job_id`, [auth.md](./auth.md) § 5).

The document clause keeps a document's own parse, its re-convert, and the earlier parses of the same original that the figure route reads for placeholders from before a re-convert ([parsing.md](./parsing.md) § Figure crops). The ticket clause keeps what a spent ticket still reads through `/v1/jobs/last` — a cache hit's included, which a document alone would keep only until that document is deleted for good.

Everything else is deleted by the worker's sweep: the parses of the REST API and the connector, which no file names unless they were filed into Files ([mcp.md](./mcp.md) § Filing into Files), and a library parse once no document of its original remains.

- **The purge**, every sweep, skipped while `PARSE_RETENTION_DAYS` is 0 — which keeps every finished parse for ever.
  1. It takes the users with a finished (`succeeded` or `failed`) job that is not kept, one at a time, under the user's workspace lock — the lock every filing, copy, retry and re-convert takes, so none of them can name a job while it goes.
  2. It locks the candidate job rows (`FOR UPDATE`) and reads the rule again under the lock.
  3. It removes the **files first**: the job's crop directories, published and attempt alike (`discard_job_figures`, [parsing.md](./parsing.md) § Figure crops), its workspace copy `.sightread/results/{job_id}/` ([workspace.md](./workspace.md) § Layout), and anything in the upload directory named for the job (an upload is stored under the id of the job it becomes, `{job_id}.<ext>`, so this finds a queued job's original too).
  4. Then it deletes the job rows and commits.
  - Files first, because nothing reads a parse past its retention: a commit that fails leaves rows the next sweep deletes, never crops no row can find.
  - A job whose crop directories will not go (`discard_job_figures` answers false, logged) keeps its row, its results copy and its uploads for the next sweep, since its row is the only thing that names them.
- **What goes with the row, and what stays.** `results` and `job_pages` cascade with the job. `usage_log`, `upload_tickets` and `credit_entries` keep their rows with `job_id` NULL: usage and the ledger are the billing record of something that really ran, and the period's `spent` does not move ([billing.md](./billing.md) § The ledger).
- **After it, the job does not exist.** The data plane answers 404 `No such job` for it — `/v1/jobs/{id}`, `/result`, `/result.md`, `/events` ([api.md](./api.md)) — as for a job that never was. The same bytes uploaded again parse, and are charged, again.
- **Deleting a document deletes the entry only** — *Delete Immediately*, *Empty Trash* or the Trash's expiry ([workspace.md](./workspace.md) § The Trash) — and its parse then falls under this rule: one that finished more than `PARSE_RETENTION_DAYS` ago, that no other document of the same original names and that no upload ticket still reads goes at the next sweep.

Crops live exactly as long as something references them:

- A job that ends `failed` has its crop directory removed with the result it never wrote.
- A shutdown requeue removes it too. The next attempt crops every page again, the kept ones from their stored markdown ([parsing.md](./parsing.md) § Figure crops), so nothing stale from the first attempt can answer a figure route.
- A parse the purge deletes has its crops removed before its rows (above).

A file of the library ([workspace.md](./workspace.md) § Files) is under none of this: it has no parse and no copy in the upload directory, and only the Trash — its expiry after `TRASH_RETENTION_DAYS` (30) days, *Delete Immediately* or *Empty Trash* ([workspace.md](./workspace.md) § The Trash) — and the account's deletion remove one.

- Results (markdown and metadata in PG) and figure crops (`FIGURES_DIR`) live as long as their parse, by the rule above.
- Usage rows outlive their job — they are the billing history — and go only with the account ([auth.md](./auth.md) § Deleting the account).
- The sweep removes from a workspace only what a rule names: an item past its time in the Trash, the results copy of a purged parse, and the files of a deleted account whose marker still stands — never anything by inference.
- Packing a history removes only what no revision names ([workspace.md](./workspace.md)).

## Dedup margin

**The cache answers only with a parse that stays kept for at least another day.** The lookup asks the retention rule with a day's margin (`parse_kept(…, margin=1 day)`). A succeeded parse answers when it is:

- kept by the document clause (§ Retention);
- finished less than `PARSE_RETENTION_DAYS − 1` days ago — with `PARSE_RETENTION_DAYS` at 1, never by its age alone; at 0, any succeeded parse answers;
- named by an upload ticket with a day left, which a ticket of `UPLOAD_TICKET_TTL_SECONDS` (an hour) never has.

So a result the cache hands out is not purged under its use — to a library upload about to file a row naming it, or to an upload ticket that reads it back through `/v1/jobs/last` ([auth.md](./auth.md) § 5). The ticket the cache hit spends names the job and keeps it for its own lifetime, even when the document that kept it is deleted for good meanwhile.

The document clause has no margin, so a hit only a file kept can still go in the moment between the lookup and the use, when that file is deleted for good and a sweep runs right then:

- **A library upload** checks for its job under the workspace lock, which the purge takes too. It refuses plainly (409 *The earlier parse of this file was just deleted; upload it again*) rather than file a row naming nothing. Uploaded again, the file parses afresh.
- **A ticket** whose hit is purged in the instant before its spend commits gets a 500 from the job's foreign key. The ticket stays unspent, for the same command to run again.

A parse in the last day of its retention answers no lookup: the upload parses again, as a new job with a retention of its own.

The in-flight reuse (§ Dedup cache, *A retry must not buy a second parse*) takes no margin: it answers only with a job queued, running or finished within `UPLOAD_RETRY_WINDOW_SECONDS`, which the retention keeps anyway.

## Chat turns

The `chat` agent has its own queue, `chat_turns` ([chat.md](./chat.md)).

- One row per user message, or per compaction, its `kind` saying which ([chat.md](./chat.md) § Compaction). An edited message is a message of its own, with a turn of its own on a branch of its own ([chat.md](./chat.md) § History is append-only).
- Turns are claimed by the same worker loop as jobs, with the same `FOR UPDATE SKIP LOCKED` query shape.
- A per-user cap of `MAX_CHAT_TURNS_PER_USER` running turns applies.
- The worker alternates between the two queues — a parse never starves a conversation and the reverse — and runs one claimed item at a time per process, exactly as for jobs.

### A turn the worker cannot finish

A turn the worker cannot finish is never requeued: its tool calls may have run, and replaying them is not safe.

- **Shutdown.** A turn interrupted by shutdown is marked `failed` with `worker stopped`.
- **Raised past the runner.** A turn whose task raised past the runner's own handling (a database error before the first event, say) is marked `failed` with `internal error` by the worker loop itself, so no turn stays `running` with nobody driving it.
- Either way the turn is stamped finished and given its terminal event. A turn the user had already cancelled keeps `cancelled`.

### Turns a dead worker left

A worker that was killed outright cannot do either. So a worker that starts finishes every turn still `running` as `failed` with `worker restarted`, and every turn cancelled and not yet wound down as `cancelled`, each with its terminal event. There is one worker process per deployment, so a turn nobody is driving is exactly one a dead worker left.

Before such a turn is finished:

1. Its conversation's shell is restarted on the runner, which kills the command inside the VM first ([sandbox.md](./sandbox.md) § The runner). The dead worker may have had a command running, and the worker's end did not end it, so it must not go on writing the workspace after the terminal event. A runner that does not answer leaves that turn unfinished for the next pass, rather than finishing it over a command that may still run.
2. Whatever the turn changed and never recorded — the worker died between a tool's write and its commit, so the tree is dirty — is committed as one revision in the turn's name (`bash: interrupted by a worker restart`, author `agent:<conversation id>`), under the shell and workspace locks the tool held. The library follows the tree as after a command ([workspace.md](./workspace.md) § Revisions). The changes stay, revertible, and the next revision of any tool carries only its own. A tree that cannot be committed leaves the turn for the next pass too.

The worker retries the whole cleanup every `ORPHAN_RETRY_SECONDS` (30) until it goes through, so a database or runner blip at start does not leave turns unfinished — and their conversations busy, and their users at the turn cap — until the next restart. Meanwhile:

- Until the first pass has completed at all (a database blip at start), the worker claims nothing, parses included: the users whose turns wait are not known yet.
- Once it has, and until the cleanup has gone through, the worker claims no chat turn at all and no parse of a user whose turn is still waiting: a command the dead worker left running holds no lock any more, and a new turn or a finishing parse writing the same user's tree beside it would corrupt the tree and its revisions.

The sweeper does not touch turns.

### Idle compaction

**Idle compaction** is the worker's other periodic task, beside the sweep. Every `CHAT_IDLE_COMPACT_SWEEP_SECONDS` (60) it enqueues a compaction turn — a `chat_turns` row of kind `idle_compact`, claimed and run like any turn — for each conversation whose prompt cache is about to expire with a large context behind it, under the checks and locks a reader's send takes ([chat.md](./chat.md) § Compaction). A pass that fails is logged and the next runs on schedule, as a sweep's does.

### The settings a turn runs on

A claimed turn runs on the settings the worker took for its claim — prices, `CREDIT_USD`, `CHAT_MIN_CREDITS` — from its first model call to its last settlement, whatever the console's Settings page saves meanwhile ([billing.md](./billing.md) § A chat turn).

- A claimed turn whose conversation is on a model that snapshot does not price reads the settings again, once, before it runs: the API offers a model priced on the page at once, and the worker's copy may be `RUNTIME_SETTINGS_CACHE_SECONDS` old ([admin.md](./admin.md) § Settings).
- The idle compaction sweep ([chat.md](./chat.md) § Compaction) reads them once per pass, through the same cache.
