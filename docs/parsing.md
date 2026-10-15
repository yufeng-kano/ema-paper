# Parsing pipeline

## PDF engine: Poppler via subprocess — this is a hard rule

All PDF work goes through Poppler CLI tools in short-lived subprocesses:

- `pdfinfo` — page count + page dimensions (points).
- `pdftoppm -png -r <dpi>` — page rendering for vision calls, scaled so the long edge ≤ **2000 px** (large images destabilize VLM detection).

Why subprocess, not a linked library: crash isolation (a malicious PDF kills the child, not the worker), free multi-core parallelism, and licensing (Poppler is GPL — fine to invoke as a separate program; PyMuPDF is AGPL and banned here). Every subprocess gets a timeout (default 60 s/page) and runs against the job's temp directory only.

## Vision-only conversion

Every page is converted the same way — **one path, no routing**:

1. Render the page (`pdftoppm`).
2. Send the rendered image to the model chosen for the `parse` agent ([agents.md](./agents.md)), on its upstream (Kano Proxy or a provider connection), with the transcription prompt. The model returns the page's markdown with figure placeholders inline. `method: "vision"` on every page.

There is no PDF-text-layer conversion path: the product converts what the page *looks like*, and a single pipeline means every page obeys the same prompt, the same figure contract and the same failure rules. The PDF text layer is never extracted — `pdfinfo` and `pdftoppm` are the only Poppler calls.

A page that cannot be rendered or transcribed → entry in `errors`, parsing continues. Whole document unreadable → job fails, no partial markdown; the failure message names the per-page reasons.

## Page markers

The assembled markdown carries one marker line per page, immediately before that page's content:

```markdown
<!-- page: 4 -->
```

Markers are how a caller maps any passage back to its page (citations, cross-checking figures). They are HTML comments, so ordinary markdown renderers hide them.

## Coordinate contract

- Figures: `bbox` = `[ymin, xmin, ymax, xmax]`, normalized 0–1000, origin top-left (`yxyx_norm1000`, Gemini-native). This is what every result declares; what a model is *asked* for depends on its family and is converted into this space before storage (§ Coordinate conventions).
- The service **never converts coordinates**; the response declares `meta.bbox_format` and the receiver does the one and only conversion at crop time.
- Placeholder: `![fig{n}](sightread://p{page}/{ymin},{xmin},{ymax},{xmax})`, caption verbatim on the next line.
- The box bounds the graphic itself — the plotted area, drawing or photograph — and **excludes the caption line and surrounding body text**. The caption already travels on the placeholder's next line, so a box that included it would print it twice: once baked into the crop, once as text. The prompt states this explicitly.
- Figure ids are document-wide (`fig1`, `fig2`, …) and the page number is **ours**, not the model's: the model emits the placeholder in place and the assembler renumbers it. Boxes are clamped to 0–1000; a box that is still degenerate (zero or negative area) is dropped, never guessed at.

## Figure crops

The page image a vision call already rendered is also where figures are cropped from, at the one moment it exists ([jobs.md](./jobs.md) deletes rendered pages immediately after the call):

- After a page transcribes, every placeholder in that page's markdown is cropped from the rendered PNG with Pillow (raster work on our own render — not PDF work, so the Poppler-only rule is untouched) and written to `FIGURES_DIR/{job_id}/p{page}_{ymin}_{xmin}_{ymax}_{xmax}.png`.
- The filename is the cleaned bbox under **our** page number — the same clamping and renumbering `assemble` applies — so a crop is addressable from any placeholder without waiting for document-wide figure ids.
- Crops get a 0.8% margin per side (8 bbox units), clamped to the page — enough to forgive a slightly tight box, small enough not to pull the caption the prompt just excluded back into the crop. (The API docs suggest self-cropping callers add ~2% when they prefer safety over tightness; that is their trade to make.)
- A crop that fails is logged and skipped; it never fails the page. A figure whose crop is missing (old results, failed crop) simply renders as a placeholder in the viewer.
- Writes are atomic (encode to a staging name, rename): a partial result can point the viewer at a crop mid-encode, and an existing name must never stream as a truncated PNG. The figure routes answer with `Cache-Control: private, no-store` — a browser cache keys on the URL, not the session, and a cached crop must not outlive a sign-out.
- At most `FIGURES_PER_PAGE_MAX` (default 40) crops per page, in reading order; the excess is logged and skipped. The upstream response body is already capped, but a malicious or malfunctioning user-defined endpoint could otherwise turn one small response into unbounded PNG encodes and durable disk under the shared figures volume.
- Crop work runs in a thread the event loop cannot cancel, so writing and discarding are serialized under one per-job lock: a failed or interrupted job's cleanup waits out any in-flight crop and marks the job's figures discarded, after which a late crop thread writes nothing — the directory of an abandoned attempt cannot be recreated behind the cleanup.
- Crops persist with the result (kept indefinitely, like `results`), under `FIGURES_DIR` — outside the upload dir, so the sweeper never touches them. They are served to the signed-in owner by control-plane routes only ([api.md](./api.md)); the data plane still ships coordinates, not image bytes.

## Prompts

The transcription prompt is a template with three tokens, substituted per call by plain string replacement (a template with stray braces must not break a job): `{page}` (the real page number), `{bbox_instructions}` (the coordinate paragraph for the model's family — the placeholder to emit and what its integers mean, § Coordinate conventions) and `{bbox_format}` (that convention's id).

Resolution order for a job's prompt, decided at enqueue time and stored on the job row:

1. **The user's selected prompt preset for the agent** (`agent_settings.prompt_preset_id` → `prompt_presets.text`, managed on the web Agent page) when one is selected. It replaces the template entirely, and figure/bbox quality is then the user's own responsibility.
2. The agent's default template — for `parse`, `DEFAULT_PROMPT_TEMPLATE`, part of the agent's definition in code and the starting text of a new preset on the Agent page ([agents.md](./agents.md)).

What the default template asks for beyond the markdown itself: the page's **furniture is left out** — a page or slide number, and a running header or footer that repeats across pages. A deck numbers every slide and a paper numbers every page; neither is what a reader of a transcription came for, and the viewer already knows which page a passage came from (web.md § Result viewer). The instruction then says, explicitly, that a number inside a sentence, a figure or table label, a citation marker, a numbered heading or a table **is** content: a model told to drop numbers will otherwise drop the ones that carry meaning. Changing the template changes results, so `PIPELINE_VERSION` moves with it and cached results parsed under the old wording stop answering new uploads.

`jobs.prompt` holds the effective template verbatim (reproducibility); `jobs.prompt_sha256` is part of the dedup cache key ([jobs.md](./jobs.md)), so editing a preset's text invalidates cached results parsed with the old one.

## Coordinate conventions

The stored contract never changes: every result declares `yxyx_norm1000`, and every figure crop, the viewer and every API caller read that one space. What changes per model is the convention the model is **asked** for. Vision models are trained on different coordinate conventions, and asking for the wrong one costs box accuracy, so the pipeline picks the convention from the model id's family, converts the model's answer into the stored contract before anything downstream sees it, and the Agent page only shows the choice ([web.md](./web.md)). Nothing here is a user setting.

| Family (model id) | Asked for | Image sent | Basis |
|---|---|---|---|
| `google/…`, `gemini-…` | `[ymin, xmin, ymax, xmax]`, 0–1000 — `yxyx_norm1000`, the native convention; no conversion | the render as rendered (long edge ≤ 2000 px) | Gemini docs |
| `anthropic/…`, `claude-…` | `[x1, y1, x2, y2]` in pixels of the image sent — `xyxy_px` | the render **pre-resized to the size Claude would resize it to** (standard tier: no edge over 1568 px and `⌈w/28⌉ × ⌈h/28⌉ ≤ 1568` visual tokens; `agents/bbox.py` carries the published formula), so the pixel answer maps one-to-one onto the image we hold | Claude's vision-coordinates docs: pixels, never 0–1000 |
| `openai/…`, `gpt-…` | `[x1, y1, x2, y2]` in pixels of the image sent — `xyxy_px` | the render as rendered | **Third-party only** (Roboflow's GPT-6 tests report pixel boxes on the image sent, inputs kept ≤ 2048 px). Not confirmed against OpenAI's own docs |
| anything else | `yxyx_norm1000` | as rendered | the default, and the convention most open models share |

Conversion (`canonicalize` in `agents/bbox.py`): a pixel box `[x1, y1, x2, y2]` on a `W × H` image becomes `[round(y1 / H × 1000), round(x1 / W × 1000), round(y2 / H × 1000), round(x2 / W × 1000)]`, then the usual clamp-or-reject of `clean_bbox`. The rewrite happens on the page's markdown right after the model answers, so crops, assembly and dedup never meet a pixel box.

The prompt carries the convention through the `{bbox_instructions}` token: it expands to the convention's own paragraph — the placeholder shape to emit and what its four integers mean, including the sent image's pixel size for the pixel conventions — and `{bbox_format}` expands to the convention's id. A custom prompt that keeps `{bbox_instructions}` therefore works on every family. A custom prompt that spells out a convention of its own is still converted as if it followed the model's — that is the user's responsibility, as with every custom prompt.

The convention is a function of the model, which is already part of the dedup key. The default template changed to carry the token, so `PIPELINE_VERSION` moved.

## Models

There are no preset profiles. The user picks any model from the live catalog of the agent's upstream (`GET /api/agents/parse/models`, [api.md](./api.md)); nothing is recommended or hard-coded, and nothing filters the catalog to vision models yet ([agents.md](./agents.md) — `parse` declares `vision` but the declaration is not enforced). Every model's result declares the same contract (`yxyx_norm1000`); the convention the model is asked for follows its family (§ Coordinate conventions), and bbox quality depends on the model the user chose.

`PIPELINE_VERSION` (part of `parse`'s definition) and the prompt hash are part of the dedup cache key ([jobs.md](./jobs.md)), so prompt/pipeline improvements invalidate old cached results.

## Image input

Accepted: jpg, png, webp, heic. Normalization before the vision call, in this order:

1. HEIC → JPEG (pillow-heif).
2. Apply EXIF orientation.
3. Downscale to long edge ≤ 2000 px.

Single page; `width_pt`/`height_pt` are the pixel dimensions of the (original) input image; bbox space is still 0–1000 normalized.

## Upstream usage (Kano Proxy or an OpenAI-compatible connection)

Vision calls run against the job's upstream ([api.md](./api.md) § Upstreams): Kano Proxy's `{KANO_PROXY_BASE_URL}/openai/v1/chat/completions` with the user's Kano Proxy key when `jobs.connection_id` is NULL, otherwise that provider connection's `{base_url}/chat/completions` with the connection's key. One wire format everywhere — OpenAI Chat Completions with an `image_url` data-URI part, no extension fields.

- One request per page, fanned out with an asyncio semaphore (`VISION_CONCURRENCY_PER_JOB`, default 8); 429 → exponential backoff + reduced concurrency for that job.
- **A transient failure is retried before the page is given up.** One upstream call gets up to `VISION_MAX_ATTEMPTS` (4) tries with exponential backoff. Transient means the same request may well succeed a moment later: the endpoint was unreachable or timed out, it answered 408 or 5xx (as a status or as an error payload inside a 200), its body was not JSON, or the completion came back empty. Unlike a 429 these do not shrink the job's concurrency — the upstream is flaky, not throttling. What is **not** retried is what will fail the same way again: any other 4xx, a response over the size cap, 402 and a rejected key. A page that still fails after the last attempt is recorded as failed with the same reasons as before, and the worker logs the upstream's own failure message (a status code or a fixed phrase, never credential material or document content) so a failed page can be diagnosed afterwards.
- **A blank page is a page, not a failure.** The prompt tells the model to leave out page furniture, so a page that carries nothing but its number — the blank verso before a book chapter — honestly transcribes to nothing, and some servers say "nothing" as `content: null` or as no choice at all rather than as an empty string. A `null` content on a choice that finished with `stop` is the model's complete answer and is an empty page at once. An empty completion in any other shape is ambiguous, so it is retried as transient first; if every attempt comes back empty, the page is recorded as succeeded and empty rather than failed. A completion cut short (`finish_reason` `length`, `content_filter`, …) is never read as blank. Empty pages are dropped from the assembled markdown as they always were, and do not appear in `errors`.
- Every response's `usage` object is written to `usage_log` per call, and every row says where its cost came from (`usage_log.cost_source`, [database.md](./database.md)):
  - **`estimated`** — the built-in Kano Proxy answered with a numeric `usage.cost`, or a provider connection with **Use the cost this endpoint reports** turned on (`provider_connections.report_cost`, off by default) did. Recorded as reported, and only ever shown as an estimate: Kano Proxy reports what the call would have cost at list prices, including for subscription traffic nobody is charged per call for — the endpoint priced the call, it is not a bill. A JSON number only, finite, not negative, and within the column's range; anything else is `unknown`.
  - **`unknown`** — a connection without the flag, or an upstream whose response carried no usable cost (missing, `null`, a string, negative, out of range). The tokens are recorded and the cost is NULL, never 0: a call nobody priced must not read as a free one.
  - **`openrouter`** — historical rows only, from when OpenRouter was the built-in upstream: its own billing for the call. No new row is written with it; the old ones stay readable and are what Usage calls *billed*.

  Usage totals keep the three apart ([api.md](./api.md) § `GET /api/usage`): billed cost is never summed with estimates, and unpriced calls are counted, not priced. Kano Proxy's cost is always recorded (sightread knows what it reports); for a connection the flag is opt-in and never inferred from the URL — Sightread does not know which other endpoints price honestly, the user does. It is read from the connection when the job runs, like its URL and key ([database.md](./database.md) § Rules). Never maintain a local price table and never price a call locally.
- Upstream response bodies are read with a size cap (`UPSTREAM_RESPONSE_MAX_BYTES`, default 32 MB) — a user-controlled endpoint must not be able to exhaust worker/API memory with an unbounded body; over the cap the call fails as an upstream error.
- 402 → mark the page failed with reason `payment`, continue remaining pages only if the error is page-scoped; abort the job when the key is clearly dead. 401/403 (a rejected key — e.g. a Kano Proxy key revoked on its Access page) aborts the job at once.
