# Parsing pipeline

## PDF engine: Poppler via subprocess — this is a hard rule

All PDF work of the API and the worker goes through Poppler CLI tools in short-lived subprocesses:

- `pdfinfo` — page count + page dimensions (points).
- `pdftoppm -png -r <dpi>` — page rendering for vision calls, scaled so the long edge ≤ **2000 px** (large images destabilize VLM detection).
- `pdftotext -enc UTF-8 <file> -` — only for the chat agent's PDFs, never for a parse:
  - a PDF attached to a chat message ([chat.md](./chat.md) § What a user message carries), kept to `CHAT_ATTACHMENT_TEXT_MAX_CHARS` characters;
  - a PDF that `read_url` fetches ([chat.md](./chat.md) § Tools), kept to `CHAT_TOOL_RESULT_MAX_CHARS` characters.
  - Its text is read from the pipe, and the child is stopped once four bytes for each character of the cap have come: a character is four bytes at most, so that always holds the cap's characters.
  - A small PDF that holds text far past its own size therefore never has the rest read or written anywhere.
  - The call is killed after `CHAT_ATTACHMENT_PDF_TIMEOUT_SECONDS`.

Why subprocess, not a linked library:

- Crash isolation: a malicious PDF kills the child, not the worker.
- Free multi-core parallelism.
- Licensing: Poppler is GPL — fine to invoke as a separate program; PyMuPDF is AGPL and banned here.

The rule binds the API and the worker, which import no PDF library. The tests build their fixture PDFs with a dev dependency ([testing.md](./testing.md) § The suite).

The browser is the other side of the rule: the web app draws a PDF's pages itself, with libraries under permissive licences only ([pdf-viewer.md](./pdf-viewer.md)).

Every subprocess gets a timeout (default 60 s/page) and runs against the job's temp directory only.

## Vision-only conversion

Every page is converted the same way — **one path, no routing**:

1. Render the page (`pdftoppm`).
2. Send the rendered image to the deployment's parse model (`PARSE_MODEL`, [billing.md](./billing.md) § Models and the operator's upstream) on Kano Proxy, with the transcription prompt. The model returns the page's markdown with figure placeholders inline. `method: "vision"` on every page.
   - An answer wrapped in a fence despite the prompt is unwrapped, and only that: the first line must open a fence (```` ``` ```` or `~~~`) whose info string is `markdown` or `md`, and a bare closing fence of the same kind on the last line goes with it (a missing one is forgiven).
   - A page that starts or ends with a code block of its own — ```` ```python ```` on its first line, a listing closing on its last — keeps every fence it carried: stripping a leading and a trailing fence independently would cut such a block open.
   - A bare ```` ``` ```` wrapper is left as it came: it cannot be told from a page that is one unlabelled listing, and a visible code block loses nothing, while unwrapping a listing would.
3. Check the page's figure boxes (§ Figure check): free rules on every box, and the same model again on a page the rules flag. A box may move; the text never changes. The page's crops are made from the checked boxes (§ Figure crops).

There is no PDF-text-layer conversion path: the product converts what the page *looks like*, and a single pipeline means every page obeys the same prompt, the same figure contract and the same failure rules. A parse never extracts the PDF text layer — `pdfinfo` and `pdftoppm` are its only Poppler calls; `pdftotext` serves the chat agent's PDFs alone.

**Every page or nothing.**

- A page that cannot be rendered or transcribed fails on its own, and the other pages still run to their end. Each one that succeeds is kept on its `job_pages` row ([jobs.md](./jobs.md) § Progress).
- A job with any failed page fails whole: no result is written and no partial markdown is served.
- The failure message counts the failed pages and names their reasons (the first five).
- A reader never opens a document with a page missing. The failed pages are retried instead, alone, with the succeeded ones kept ([jobs.md](./jobs.md) § Retrying failed pages).
- A parse whose pages all succeeded is checked before its result is written, and a failed check fails it the same way (§ Format check).

A page's failure reason is one of a fixed set, stored on its row and named in the job's error:

- `render failed` — Poppler could not render it.
- `the image could not be decoded`.
- `upstream call failed` — every attempt failed (§ Upstream usage).
- `rate limited`.
- `payment`.
- `blocked` and `cut off` — the model stopped its own answer (§ Upstream usage).

A figure check call that fails fails its page with the same reasons (§ Figure check).

## Page markers

The assembled markdown carries one marker line per page, immediately before that page's content; a page whose markdown is empty carries none:

```markdown
<!-- page: 4 -->
```

- Markers are how a caller maps any passage back to its page (citations, cross-checking figures).
- They are HTML comments, so ordinary markdown renderers hide them.
- In the reading copy (§ Format check) a paragraph a page break cut is joined before the next page's marker, so the whole paragraph sits under the page it starts on.

## Coordinate contract

- Figures: `bbox` = `[ymin, xmin, ymax, xmax]`, normalized 0–1000, origin top-left (`yxyx_norm1000`, Gemini-native). This is what every result declares. What a model is *asked* for depends on its family and is converted into this space before storage (§ Coordinate conventions).
- The service never converts coordinates **on the way out**: the response declares `meta.bbox_format` and the receiver does the one conversion into its own pixel space at crop time. The one conversion the service does make is on the way in — a model's native convention into the stored contract, before anything is stored (§ Coordinate conventions).
- Placeholder: `![fig{n}](sightread://p{page}/{ymin},{xmin},{ymax},{xmax})`.
  - The caption follows verbatim on the next line, and only there. A figure without a caption is followed by an empty line, so it claims nothing after it.
  - A result's `figures[].caption` is read the way the result viewer reads it ([web.md](./web.md) § Result viewer): the line right after the placeholder, empty when that line is blank or starts a block of its own (a heading, a table, a list item, another figure, …).
- The box encloses **the whole figure** and **excludes the caption line and surrounding body text**.
  - The whole figure is every panel, and everything drawn as part of it: axes, tick labels, axis titles, legends, colour bars, panel titles and labels, annotations.
  - The caption is excluded because it already travels on the placeholder's next line: a box that included it would print it twice, once baked into the crop, once as text.
  - A figure made of several panels is one figure and one placeholder, unless each panel has a caption of its own.
  - The prompt states all of this.
  - The box is the whole figure, not the plotted area, because a box that bounds only the inside of a chart's axes leaves out axis titles, tick labels or legends — more than a crop margin small enough to keep the caption out can give back (§ Figure crops).
- Figure ids are document-wide (`fig1`, `fig2`, …) and the page number is **ours**, not the model's: the model emits the placeholder in place and the assembler renumbers it.
- Boxes are clamped to 0–1000; a box that is still degenerate (zero or negative area) is dropped, never guessed at.

## Figure crops

Figures are cropped from the page image a vision call already rendered, at the one moment it exists ([jobs.md](./jobs.md) deletes rendered pages immediately after the call):

- **Where a crop is made and written.** After a page transcribes and its boxes are checked (§ Figure check), every placeholder in that page's markdown is cropped from the rendered PNG with Pillow. This is raster work on our own render, not PDF work, so the Poppler-only rule is untouched.
  - A crop is written to `FIGURES_DIR/{job_id}.{attempt}/p{page}_{ymin}_{xmin}_{ymax}_{xmax}.png`, the attempt's own directory.
  - The attempt directory is moved to `FIGURES_DIR/{job_id}/` when the result commits with the worker's generation intact. So an attempt a worker that lost the lock went on with never mixes its crops with the replacement's, or removes them with its own ([jobs.md](./jobs.md) § Queue).
- **Reading a crop while the job runs.** The figure routes read a crop from the attempt directory when the job's own directory does not hold it yet, so the partial result's figures render as their pages finish.
  - Should a stale attempt directory stand beside the replacement's, the newest attempt is read: the same page render and box give the same crop.
  - The lookup and the publication race by design: the rename can land between the look at the job's directory and the look at the attempt's. So the job's directory is looked at once more after the attempts miss.
  - The crop is opened where it is found. A path that vanished under the open (the attempt directory renamed away) is looked up once more.
  - The route streams from that descriptor in chunks off the event loop — never from a path the publication may have moved, and never with the whole file held in memory.
- The filename is the cleaned bbox under **our** page number — the same clamping and renumbering `assemble` applies — so a crop is addressable from any placeholder without waiting for document-wide figure ids.
- Crops get a 0.8% margin per side (8 bbox units), clamped to the page. That is enough to forgive a slightly tight box, and small enough not to pull the caption the prompt just excluded back into the crop. (The API docs suggest self-cropping callers add ~2% when they prefer safety over tightness; that is their trade to make.)
- A crop that fails is logged and skipped; it never fails the page. A figure whose crop is missing (a failed crop) renders as a placeholder in the viewer.
- **A kept page is cropped again.** A page an attempt keeps rather than transcribes — one that succeeded before a requeue or a retry ([jobs.md](./jobs.md) § Retrying failed pages) — is cropped again from its stored markdown, on a fresh render of the page.
  - No upstream call is made, and a page without placeholders is not rendered at all.
  - It is not checked again (§ Figure check).
  - An attempt's crops therefore come from that attempt alone, and publishing it publishes every page's, as for a first run.
- **Writes are atomic** (encode to a staging name, rename): a partial result can point the viewer at a crop mid-encode, and an existing name must never stream as a truncated PNG.
- The figure routes answer with `Cache-Control: private, no-store`, because a browser cache keys on the URL, not the session, and a cached crop must not outlive a sign-out.
- **At most `FIGURES_PER_PAGE_MAX` (default 40) crops per page**, in reading order; the excess is logged and skipped. The upstream response body is already capped, but a malfunctioning upstream, or a prompt that asks for it, could otherwise turn one small response into unbounded PNG encodes and durable disk under the shared figures volume.
- **Writing and discarding are serialized under one per-job lock**, because crop work runs in a thread the event loop cannot cancel. A failed or interrupted job's cleanup waits out any in-flight crop and marks the job's figures discarded, after which a late crop thread writes nothing: the directory of an abandoned attempt cannot be recreated behind the cleanup.
- **Crops persist with the result and go with it.**
  - The parse retention removes a purged parse's crop directories before its rows ([jobs.md](./jobs.md) § Retention).
  - Deleting an account removes those of its jobs ([auth.md](./auth.md) § Deleting the account).
  - They live under `FIGURES_DIR`, outside the upload dir, so the upload sweep never touches them.
- **Crops are served to their owner** by the library's figure route on the control plane, and by `/v1/jobs/{id}/figures/…` on the data plane, which the connector's **figures** command reads ([api.md](./api.md), [mcp.md](./mcp.md)). A caller gets the picture the web reader shows instead of cropping again, and the coordinates still ship in the result for one that does.
- **A library document's figure route reads the crops of the parse the document names.** A crop that parse does not hold is looked for among the user's other succeeded parses of the same original (same `sha256`), newest first.
  - After a re-convert ([jobs.md](./jobs.md) § Re-converting a document), a placeholder from before it — in a reverted text, or quoted in the notes — still finds the crop it was made with.
  - Those earlier parses are kept for exactly this reason: the retention keeps every parse of an original a document of the user names ([jobs.md](./jobs.md) § Retention).

## Figure check

Every page's boxes are checked right after the page is transcribed, before it counts as succeeded and before its crops are made. A box the model drew can still cut its figure: a model that boxes a chart's plotted area leaves the axis titles, tick labels and legend outside, and the crop shows a chart without its labels (§ Coordinate contract).

- The check moves boxes and nothing else: the page's text never changes, only the four numbers of a placeholder.
- It checks the page's distinct boxes in reading order, up to `FIGURES_PER_PAGE_MAX`. A placeholder past the cap has no crop and is `unverified`.

1. **Rules, on every box, with no model call.**
   - A box is read as its crop would be: the box plus its margin, on the page render (§ Figure crops).
   - Ink is a pixel whose luminance differs from the page's background, its most common luminance, by more than 35. A line with fewer than 2 such pixels is blank.
   - Pixels inside the page's other boxes are not ink, so a neighbouring figure inside this crop's margin is not a cut.
   - A side of the crop whose outer 0.1% of the page's long edge holds ink cuts through something. It is pushed outward line by line until a blank run of 0.6% of the long edge; the side then stops a third of that run past the last ink.
   - A side that reaches the page's edge first stops there. A side moves at most 4% of the long edge, and a side that runs into another box, or finds no blank run within that reach, stays where it was.
   - A side on the page's edge is never a cut.
   - A page whose boxes cut nothing is done here: every figure on it is `verified`, and the check cost nothing.
2. **The model, on a page the rules flagged.** A page with any box that cut something, pushed out or not, goes to the model that transcribed it, on the same upstream, as one more call.
   - The call carries the page image as that model was sent it (§ Coordinate conventions), with every box's crop drawn on it as a numbered rectangle, so the model judges what will be cut out; and the check prompt (`FIGURE_CHECK_PROMPT` in `agents/parse.py`).
   - The prompt lists the rectangles in the model's own convention. It asks, for each, whether it encloses its whole figure and nothing else, as the transcription prompt defines it, and for a corrected rectangle when it does not.
   - The answer is JSON: `{"figures": [{"n": 1, "ok": true}, {"n": 2, "ok": false, "box": [x1, y1, x2, y2]}]}`.
   - A corrected rectangle is a crop too. It is converted into the stored contract as a transcribed box is, less the crop's margin on each side, so the new box's crop is what the model drew; then it is cleaned and pushed out by the rules again.
   - A box the model confirms is settled. A box it corrects is drawn again and asked about in the next round, up to `FIGURE_CHECK_ROUNDS` (2) rounds, one call each. There is no next round when nothing was corrected.
3. **Each figure ends with a status** (`figures[].check`, [api.md](./api.md)). `results.meta.figure_check` counts the three.

| Status | When |
|---|---|
| `verified` | Its box never moved, and either its page needed no model or the model confirmed it. |
| `adjusted` | Its box moved, by the rules or the model, and the model then confirmed it. |
| `unverified` | The model still corrected it in the last round (it keeps that last box), or gave it no verdict. |

**An unverified figure never fails the page**: the document opens, the viewer marks the figure, and the parse facts count it ([web.md](./web.md) § Result viewer).

A model's answer gives no verdict for a box when:

- it is not the JSON asked for, an empty answer included;
- its entry for the box is missing or malformed;
- the box it gives cannot be used: degenerate once cleaned, the box it already has, or a box that shares more than half of the smaller of the two with another figure's box (two figures are never merged, and a split figure stays split).

A box without a verdict is not asked about again.

A check call is billed like the page's own calls (`usage_log`, under the model it went to), and each one is logged as soon as it is answered, before the boxes it corrected are worked on: a cancel in that raster work ends the page before it is recorded, as before a fallback (§ Fallback model). A call that fails — past its retries, rate limited, refused for payment, or stopped by the model — fails the page with the same reason a transcription would (§ Vision-only conversion), and the retry transcribes the page again.

The rules flag; the model judges. A box whose crop cuts nothing can still leave out a part set apart by white space, a legend or an axis title, and the rules do not see it. The trade is deliberate: a call for every page with figures would cost about as much as transcribing them again. On the DINO paper the rules flagged every one of GPT-6 Luna's 8 cut figures, the 4 that also left a part outside across white space included, and none of its 6 whole ones. Pages with figures there: 11; flagged: 5, so 5 to 10 calls against 21 transcriptions.

- A page's markdown carries its checked boxes and its row their statuses (`job_pages.figures`, [database.md](./database.md)), so a page an attempt keeps is not checked again ([jobs.md](./jobs.md) § Retrying failed pages).
- The check prompt is part of the pipeline, not a setting: a custom transcription prompt does not change it.
- The check runs on every parse, an API parse too, of `PIPELINE_VERSION` 10 or later. A job queued under an earlier version runs without it, and its figures carry no `check`.

## Prompts

The transcription prompt is a template with three tokens, substituted per call by plain string replacement (a template with stray braces must not break a job):

- `{page}` — the real page number.
- `{bbox_instructions}` — the coordinate paragraph for the model's family: the placeholder shape to emit and what its four integers mean, including the sent image's pixel size for the pixel conventions (§ Coordinate conventions).
- `{bbox_format}` — that convention's id.

Resolution order for a job's prompt, decided at enqueue time and stored on the job row:

1. **The user's own prompt for the agent** (`agent_settings.prompt`) when one is saved. It is edited on the web Agent page, where the dialog opens on the default and offers *Restore default*. It replaces the template entirely, and figure/bbox quality is then the user's own responsibility.
2. Else the agent's default template — for `parse`, `DEFAULT_PROMPT_TEMPLATE`, part of the agent's definition in code and the text the Agent page's prompt dialog opens on ([agents.md](./agents.md)).

What the default template asks for beyond the markdown itself:

- The page's **furniture is left out**: a page or slide number, and a running header or footer that repeats across pages. A deck numbers every slide and a paper numbers every page; neither is what a reader of a transcription came for, and the viewer already knows which page a passage came from ([web.md](./web.md) § Result viewer).
- A number inside a sentence, a figure or table label, a citation marker, a numbered heading or a table **is** content, and the instruction says so explicitly: a model told to drop numbers will otherwise drop the ones that carry meaning.
- Its figure paragraph, after `{bbox_instructions}`, asks for the whole figure in one box and names what belongs to it (§ Coordinate contract).

Changing the template changes results, so `PIPELINE_VERSION` (`agents/parse.py`) moves with it, and cached results parsed under the old wording stop answering new uploads.

- `jobs.prompt` holds the effective template verbatim (reproducibility).
- `PIPELINE_VERSION` and `jobs.prompt_sha256` are both part of the dedup cache key ([jobs.md](./jobs.md)), so a pipeline change or an edited prompt invalidates cached results parsed under the old one.

## Coordinate conventions

The stored contract never changes: every result declares `yxyx_norm1000`, and every figure crop, the viewer and every API caller read that one space. What changes per model is the convention the model is **asked** for.

Vision models are trained on different coordinate conventions, and asking for the wrong one costs box accuracy. So the pipeline picks the convention from the model id's family and converts the model's answer into the stored contract before anything downstream sees it. The Agent page only shows the choice ([web.md](./web.md)). Nothing here is a user setting.

| Family (model id) | Asked for | Image sent | Basis |
|---|---|---|---|
| `google/…`, `gemini-…` | `[ymin, xmin, ymax, xmax]`, 0–1000 — `yxyx_norm1000`, the native convention; no conversion | the render as rendered (long edge ≤ 2000 px) | Gemini docs |
| `anthropic/…`, `claude-…` | `[x1, y1, x2, y2]` in pixels of the image sent — `xyxy_px` | the render **pre-resized to the size Claude would resize it to** (below) | Claude's vision-coordinates docs: pixels, never 0–1000 |
| `openai/…`, `gpt-…` | `[x1, y1, x2, y2]` in pixels of the image sent — `xyxy_px` | the render as rendered | **Third-party only** (below) |
| anything else | `yxyx_norm1000` | as rendered | the default, and the convention most open models share |

- **`anthropic/…`, `claude-…` image.** The pre-resize follows the standard tier: no edge over 1568 px and `⌈w/28⌉ × ⌈h/28⌉ ≤ 1568` visual tokens; `agents/bbox.py` carries the published formula. So the pixel answer maps one-to-one onto the image we hold.
- **`openai/…`, `gpt-…` basis.** Roboflow's GPT-6 tests report pixel boxes on the image sent, inputs kept ≤ 2048 px. Not confirmed against OpenAI's own docs.

Conversion (`canonicalize` in `agents/bbox.py`): a pixel box `[x1, y1, x2, y2]` on a `W × H` image becomes `[round(y1 / H × 1000), round(x1 / W × 1000), round(y2 / H × 1000), round(x2 / W × 1000)]`, then the usual clamp-or-reject of `clean_bbox`. The rewrite happens on the page's markdown right after the model answers, so crops, assembly and dedup never meet a pixel box.

The prompt carries the convention through the `{bbox_instructions}` and `{bbox_format}` tokens (§ Prompts).

A custom prompt that keeps `{bbox_instructions}` therefore works on every family. A custom prompt that spells out a convention of its own is still converted as if it followed the model's — that is the user's responsibility, as with every custom prompt.

The convention is a function of the model, which is already part of the dedup key.

The default template carries the `{bbox_instructions}` token from `PIPELINE_VERSION` 6 on. A job snapshotted under a `PIPELINE_VERSION` below 6 keeps the stored contract as its convention (`convention_for_job`): its prompt still asks every model for `yxyx_norm1000`, and reading that answer as pixels would corrupt it after a paid call.

## Models

The parse model is the deployment's, `PARSE_MODEL`, the same for every user. `PARSE_FALLBACK_MODEL` is the only other model a page may run on ([billing.md](./billing.md) § Models and the operator's upstream; `GET /api/agents/parse/models` lists the two, [api.md](./api.md)). Nothing is hard-coded in code: the ids are configuration, priced in `MODEL_PRICES_JSON`.

## Fallback model

A page can be finished by a model other than the job's own, in two ways. The page always says which model wrote it (`job_pages.model`, `pages[].model` in the result).

- **Automatically, for a blocked page.**
  - Conditions: the deployment names a fallback model (`PARSE_FALLBACK_MODEL`, [billing.md](./billing.md) § Models and the operator's upstream), and the reader's switch *retry blocked pages on it* is on (`auto_fallback`, **off** by default) ([agents.md](./agents.md)).
  - With both, the job snapshots the fallback at enqueue (`jobs.fallback_model`).
  - A retry clears it once the deployment no longer offers it, so a page blocked on that retry fails instead.
  - A page its model blocked (§ Upstream usage) is sent once more, to the fallback, unless the fallback is the model that just blocked it. The render is prepared again for the fallback's convention (§ Coordinate conventions).
  - The page keeps the last answer it got: a second failure is the page's failure.
  - No other failure moves a page to another model: a 402 or a rejected key is the account's, a transient failure was already retried, and a render failure is ours.
- **By hand, for any failed page.** A retry may name the model it sends the failed pages to — one of the parse agent's two ([jobs.md](./jobs.md) § Retrying failed pages). A retry that names none sends them to the job's own model while the deployment offers it, else to `PARSE_MODEL`. The reader picks it in the retry dialog, which offers the fallback first ([web.md](./web.md) § Files — the library).

Billing and logging:

- Every call is billed under the model it went to (`usage_log.model`).
- The blocked call is logged before the fallback is sent: a fallback that aborts the job, or a shutdown while it runs, ends the page before it is recorded, and must not take that call with it.

Neither model is part of the dedup key. They only ever finish pages the job's own model could not, so a result they helped finish still answers what was asked of that model, and its `pages` say which pages another model wrote.

## Format check

Pages are transcribed one at a time, so a paragraph that runs from one page onto the next comes back as two. A model's markup also slips now and then: an emphasis or a formula left open, a numbered heading set in bold. A parse is therefore **checked before it opens**: after every page has succeeded, and before the result is written.

- The check never changes what the document says, only its markup and where the pages join.
- What it writes is a **reading copy** (`results.reading_markdown`) beside the exact transcription (`results.markdown`).
- The reading copy is the document: the library's editable copy starts from it ([workspace.md](./workspace.md)), and the data plane's `markdown` and `result.md` — what the connector lands on disk — are it ([api.md](./api.md) § GET /v1/jobs/{id}/result).
- The exact transcription stays on the result as its record, page by page, and the control plane's `…/result` returns it.

It runs in two steps: the program rules on every checked document, then the check agent only when the rules leave spots.

### Program rules

The program rules run on every checked document, with no model call. At each page boundary they read the last block of one page and the first block of the next.

Blocks are read exactly as the result viewer reads them ([web.md](./web.md) § Result viewer):

- A paragraph runs until a blank line or a line that starts another block, blank line before it or not.
- A line starts another block when it is a heading, a rule, a fence, a display formula, a list item, a quote, a figure, a comment, or a table's header row over its separator row, with or without the outer pipes.
- So the rules and the reader agree on where every block ends: no join reaches past a paragraph, and no paragraph is appended to a heading, code, a formula, a table, a caption or a contents run.

What a page boundary gets:

| Last block of the page | First block of the next page | Outcome |
|---|---|---|
| a paragraph that ends a sentence | anything | left alone |
| a paragraph that ends without closing punctuation | a paragraph that starts with a lowercase letter | **joined**: one paragraph the page cut |
| a paragraph that ends without closing punctuation | a paragraph that does not start with a lowercase letter | spot |
| a list item | a paragraph that starts in lowercase | spot |
| a table | a table | spot |

- Closing punctuation is `.` `!` `?` `:` `;` and their full-width forms, with closing quotes, brackets and emphasis markers after one allowed, as in `**the conclusion.**`.
- A join moves the continuation up before the next page's marker, after a space — or after nothing when the first part ends in a hyphen after a letter, which stays.

A spot is a place the rules cannot settle. Besides the boundaries above, a block is a spot when:

- it has an odd number of `**`;
- it has an odd number of unescaped `$` outside a `$$` block;
- it is a paragraph that is a single bold run starting with a section number (`**3.2 Results**`, a heading set in bold).

Code, a display formula, a comment, a figure and a rule are not looked at for these.

A document with no spot is done here, and costs nothing.

### The check agent

The check agent ([agents.md](./agents.md)) runs only when there are spots.

- It is sent the first `CHECK_SPOTS_MAX` spots in document order, each as a short excerpt; the rest stay as they are.
- It works through three tools:
  - `read_page` — one page's markdown as it now stands.
  - `join_pages` — join a boundary spot by the rules' own join, and only for the cuts a spot can be:
    - a paragraph continuing a paragraph;
    - a paragraph continuing a list item, whose later lines it indents, since the viewer keeps only indented lines in an item;
    - a table continuing a table: the two tables become one, the second's separator row dropped and its header row kept as a row.
  - `str_replace` — replace a passage that occurs exactly once.
- An edit is **refused**, and the agent told why, when it would change the text. Both sides are compared with HTML comments and tags, the LaTeX delimiters `\(` `\)` `\[` `\]`, the markup characters `#` `*` `_` `` ` `` `~` `$` `\` `|` `>` `-` `:` and every whitespace taken out, and must match exactly.
- An edit may not hold a page marker either.
- A refused edit is not a failure.
- The agent is done when it ends its turn, after `CHECK_MAX_CALLS` calls at most, each answer capped at `CHECK_MAX_OUTPUT_TOKENS`.

### A failed check

**A check that fails fails the job**, as a failed page does: a document never opens unchecked. It fails on:

- an upstream call that failed past its retries (§ Upstream usage);
- a rate limit or a payment refusal;
- a rejected key;
- an answer the model blocked or cut off;
- an agent still working at `CHECK_MAX_CALLS`;
- an unexpected error inside the check.

The job's error says so: `the format check failed (<reason>)`.

- Its calls take the vision calls' retry policy (§ Upstream usage), 429 included. The check makes one call at a time, so a 429 reduces no concurrency.
- Every page stays kept, so the retry ([jobs.md](./jobs.md) § Retrying failed pages) transcribes nothing: it renders the pages with figures for their crops, assembles, and checks again.

### What runs the check

- What runs the check is the `check` agent's settings ([agents.md](./agents.md) § Per-agent settings), snapshotted on the job when it is queued or retried (`jobs.check_model`, `check_effort`, `check_prompt`). A NULL `check_model` is a job with no check.
- Every parse is checked: a Files upload, a file converted later, a re-convert, the chat agent's `convert_file`, and a parse through the REST API or the connector ([mcp.md](./mcp.md)).
- That holds while the check's switch is on (`auto_check`, **on** by default) and Kano Proxy is configured: the agent speaks the Messages API there, on Kano Proxy's Anthropic-format route ([chat.md](./chat.md) § Upstream — the Messages API).
- The check is not part of the dedup key, because it never changes `results.markdown`. A cached result is reused as it is, with the reading copy it has or without one.
- Its calls are billed as the `check` agent's (`usage_log.agent`) under the parse's job:
  - A call that breaks off after its stream started (a stream cut short, an `error` event, the size cap, a silent upstream, a cancel) is billed, retried or not, with what it reported so far and cost unknown, as a cut-short chat call is ([chat.md](./chat.md)).
  - A call that never started streaming bills nothing.
- `results.meta.check` records its model and how many joins and edits it made.

## Image input

Accepted: jpg, png, webp, heic.

- The bytes must decode as JPEG, PNG, WebP or HEIC/HEIF (`PARSE_IMAGE_FORMATS` in `parsing/images.py`), whatever the file is named or the request declares.
- Intake opens the upload with those four decoders and no other, so a BMP, TIFF, GIF, PSD, ICO… named `.png` is a **400** "The image could not be decoded", and its upload is deleted before a job exists.
- An image past Pillow's decompression-bomb bound — twice `PIL.Image.MAX_IMAGE_PIXELS`, about 179 million pixels — is refused the same way. A parse has no pixel cap of its own.
- The worker's normalization of a new page uses the same four decoders.
- A retry's restore of a page an earlier attempt kept re-reads the image with every decoder, since that attempt already accepted and billed it.

Normalization before the vision call, in this order:

1. HEIC → JPEG (pillow-heif).
2. Apply EXIF orientation.
3. Bring the image to 8-bit RGB on white — the step the editor's image `view` applies too (`flatten_rgb` in `parsing/images.py`):
   - 16-bit grey (a microscope's, a scan's) goes to 8 bits by its top byte rather than clipped to white;
   - any transparency, a grey or RGB PNG's single key colour included, is flattened onto white.
4. Downscale to long edge ≤ 2000 px.

An image is a single page. Its `width_pt`/`height_pt` are the pixel dimensions of the original input image, oriented by its EXIF data and not downscaled. Bbox space is the same 0–1000 normalized space as a PDF page's.

## Upstream usage

Vision calls run against Kano Proxy's `{KANO_PROXY_BASE_URL}/openai/v1/chat/completions` with the operator's key ([api.md](./api.md) § Upstream). One wire format: OpenAI Chat Completions with an `image_url` data-URI part, no extension fields. A job with no endpoint snapshot (`connection_base_url` NULL: it ran on OpenRouter or a provider connection) fails when the worker takes it ([jobs.md](./jobs.md) § Retrying failed pages).

- **One request per page**, fanned out under a per-job concurrency limit that can shrink while the job runs (`VISION_CONCURRENCY_PER_JOB`, default 8).
- **429** → after the upstream's own `Retry-After` when it sends one in whole seconds (followed as sent, not capped), else exponential backoff; and that job's concurrency is halved (never below 1).
- **A transient failure is retried before the page is given up.**
  - One upstream call gets up to `VISION_MAX_ATTEMPTS` (4) tries with exponential backoff.
  - Transient means the same request may well succeed a moment later:
    - the endpoint was unreachable or timed out;
    - it answered 408 or 5xx, as a status or as an error payload inside a 200;
    - its body was not JSON;
    - the completion came back empty.
  - Unlike a 429, these do not shrink the job's concurrency: the upstream is flaky, not throttling.
  - What is **not** retried is what will fail the same way again: any other 4xx, a response over the size cap, 402 and a rejected key.
  - A page that still fails after the last attempt is recorded as failed with the same reasons as before.
  - The worker logs the upstream's own failure message (a status code or a fixed phrase, never credential material or document content), so a failed page can be diagnosed afterwards.
- **A blank page is a page, not a failure.**
  - The prompt tells the model to leave out page furniture, so a page that carries nothing but its number (the blank verso before a book chapter) honestly transcribes to nothing.
  - Some servers say "nothing" as `content: null` or as no choice at all rather than as an empty string.
  - A `null` content on a choice that finished with `stop` is the model's complete answer, and is an empty page at once.
  - An empty completion in any other shape is ambiguous, so it is retried as transient first. If every attempt comes back empty, the page is recorded as succeeded and empty rather than failed.
  - Every empty answer was an answered call, and each is billed: one `usage_log` row per attempt, not only for the last one.
  - The same holds for an answer whose choice ended early without content (`finish_reason` other than `stop`, retried as transient) and for one whose completion could not be read: the answered call's `usage` is recorded whether it is retried or fails the page.
  - A completion cut short is never read as blank (next).
  - Empty pages are dropped from the assembled markdown.
- **A page the model stopped is a failed page, at once.**
  - A choice that finished with `finish_reason` `content_filter` fails the page as `blocked`: the model or its provider blocked the answer. Gemini's recitation check, which stops a page that quotes widely copied text such as published pseudocode, is the usual one.
  - A choice that finished with `length` fails the page as `cut off`: the answer ran past the model's output cap.
  - Either fails the page whatever text came with it: a page cut short is a failed page, never a shorter one.
  - Neither is retried on the same model, because the same page asks the same thing again.
  - The call's usage is recorded like any answered call's.
  - A job with a fallback model sends a blocked page there once (§ Fallback model).
- **Every vision call caps its answer at `VISION_MAX_OUTPUT_TOKENS` (16 000) tokens**: `max_tokens`, on the transcription and the figure check alike.
  - A parse prompt that asks for endless output therefore costs at most that much per call: an answer that reaches the cap finishes with `length`, and the page is `cut off` (above).
  - Kano Proxy passes the cap on to Gemini, Claude and Grok models; a Codex model ignores it, since its upstream takes no output cap.
  - `0` sends none, for an endpoint that refuses `max_tokens`.
- **Every response's `usage` object is written to `usage_log` per call**, and every row says where its cost came from (`usage_log.cost_source`, [database.md](./database.md)):
  - **`estimated`** — Kano Proxy answered with a numeric `usage.cost`.
    - Recorded as reported, and only ever shown as an estimate.
    - Kano Proxy reports what the call would have cost at list prices, including for subscription traffic nobody is charged per call for: the endpoint priced the call, it is not a bill.
    - A JSON number only, finite, not negative, and within the column's range; anything else is `unknown`.
  - **`unknown`** — a response that carried no usable cost (missing, `null`, a string, negative, out of range), and the rows of historical provider-connection jobs.
    - The tokens are recorded and the cost is NULL, never 0: a call nobody priced must not read as a free one.
  - **`openrouter`** — OpenRouter's own billing for the call, on historical rows only.
    - No new row is written with it; the old ones stay readable and are what Usage calls *billed*.

  Usage totals keep the three apart ([admin.md](./admin.md) § Overview, jobs and usage): billed cost is never summed with estimates, and unpriced calls are counted, not priced.

  Kano Proxy's cost is always recorded (sightread knows what it reports). Never maintain a local price table and never price a call locally.
- **Upstream response bodies are read with a size cap** (`UPSTREAM_RESPONSE_MAX_BYTES`, default 32 MB). Over the cap the call fails as an upstream error. The cap exists because a broken upstream must not be able to exhaust worker/API memory with an unbounded body.
- **402** → the page is marked failed with reason `payment`, and the remaining pages still run. A second 402 in the job without a success in between (`PAYMENT_FAILURES_BEFORE_ABORT`, 2) means the key is dead, not the page, and aborts the job. A success here is any of the job's calls, transcription or figure check, that returns an answer (an empty page included) or ends `blocked` or `cut off`; a call that fails any other way leaves the count as it is.
- **401/403** (a rejected key — e.g. a Kano Proxy key revoked on its Access page) aborts the job at once.
