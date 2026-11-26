# MCP server

A **thin shell over the REST service layer**: no parsing logic of its own, no separate auth store. The MCP endpoint mints short-lived **tickets** and hands back copy-paste `curl` commands:

- an upload ticket for a parse (`parse`), which files the parsed document into a folder of the reader's Files library when the agent names one (§ Filing into Files);
- a document ticket for reading a document of the library or saving its text and notes (`read_document`, `write_document`).

Two kinds of answer come inline, because they carry no file and no text: the library's index (`list_library`), and what the organizing tools did to the reader's Files library (§ The organizing tools), which act within the call.

The document bytes themselves never travel through MCP. Base64 file content in tool arguments burns ~1.37× the file size in model tokens, so uploads go out-of-band over the existing REST data plane (same pattern as S3 presigned URLs).

The commands assume the MCP client can run shell commands with network access to this host (Claude Code has it natively; claude.ai chat/cowork need "Allow network egress" enabled in sandbox settings). `list_library` and the organizing tools need no shell, so a client without one can still find and organize the reader's files; parsing, reading and saving a document stay out of its reach.

## Transport & auth

- Endpoint: `POST /mcp` (streamable HTTP, official `mcp` Python SDK, mounted inside the same FastAPI app).
- Auth: `Authorization: Bearer` with either an OAuth access token (Claude Connectors path — see [auth.md](./auth.md) § 4) or a project API key (scripts/self-hosted agents).
  - Both resolve to a user, and that user's agent settings apply as they do to a Files upload: the `parse` agent's prompt and fallback switch, the `check` agent's switch and instructions ([agents.md](./agents.md)).
  - The models are the deployment's ([billing.md](./billing.md) § Models).
- Never accept tokens in query strings.

## The `parse` tool

Input: `folder` (optional) — a folder's `path` as `list_library` shows it (`DL/DINO`), or `/` for the top level of Files.

- Given, the parse is filed there (§ Filing into Files).
- Left out, the parse stays out of the library: Claude also parses PDFs it needs once, which would clutter Files and count against storage.

Output (JSON):

- a fresh single-use upload ticket ([auth.md](./auth.md) § 5);
- the models a parse may run;
- ready-to-run commands built from `APP_URL`;
- when `folder` was given, `folder: {id, path, created}`: the destination (`id` null and `path` `/` for the top level) and the paths of the folders the call made.

```json
{
  "token": "srt_…",
  "expires_at": "2026-08-21T15:00:00Z",
  "max_upload_bytes": 134217728,
  "page_cap": 500,
  "models": ["<parse-model>", "<fallback-model>"],
  "upload": "f=doc.pdf; s=\"${f%.*}.progress.sse\"; rm -f \"$s\"; curl -sSN -H 'Authorization: Bearer srt_…' -H 'Accept: text/event-stream' -F file=@\"$f\" https://<host>/v1/parse -o \"$s\" && curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "retry": "…the upload command with -F retry=true…",
  "markdown": "f=doc.pdf; curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result.md -o \"${f%.*}.md\"",
  "metadata": "f=doc.pdf; curl -sSf -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last/result -o \"${f%.*}.meta.json\"",
  "figures": "f=doc.pdf; …every placeholder of the .md fetched from /v1/jobs/last/figures/… into \"${f%.*}.figures/figN.png\", non-zero when a fetch failed…",
  "status": "curl -sS -H 'Authorization: Bearer srt_…' https://<host>/v1/jobs/last",
  "notes": "…set f, read the .md not <name>.progress.sse, what a non-zero exit means, retry, figures, form fields, recovery…"
}
```

`models` lists the parse agent's models (`PARSE_MODEL`, then `PARSE_FALLBACK_MODEL` when set). It is the answer `GET /v1/models` gives a durable credential: a ticket is refused there ([auth.md](./auth.md) § 5), so the tool hands them over itself.

**A parse through the connector behaves as a Files upload does.** It is the same parse with the same settings, and what lands on disk is what the web reader opens:

- **The text is the reading copy.** The parse takes the format check whenever a Files upload would ([parsing.md](./parsing.md) § Format check): paragraphs a page break cut are joined and broken markup is fixed, the text unchanged. A check that fails fails the parse. `result.md` is that copy, as the library's `doc.md` starts from it.
- **The figures are the server's crops.** Each figure is cropped by Ema Paper from its own render after the figure check ([parsing.md](./parsing.md) § Figure crops). The **figures** command saves every one the `.md` names as `<name>.figures/figN.png`: the picture the web reader shows, not one the agent has to crop again. The placeholders still carry their boxes, and `.meta.json` each figure's `check`: an `unverified` one is what the web reader marks *Crop not verified*.
- **A failed parse is retried, not repeated.** One failed page fails the parse ([parsing.md](./parsing.md) § Vision-only conversion), as on the web. **retry** is the upload command with `-F retry=true`, on a fresh ticket. Run with the same `f` and the same `-F pages=` as the upload, it sends the file to that failed parse again: its succeeded pages are kept and only the failed ones run, on the model `-F model=` names (the fallback for a page the first model blocked) or else the parse's own while the deployment still offers it, else `PARSE_MODEL`, as the web's retry dialog does ([jobs.md](./jobs.md) § Retrying failed pages).
- **The same bytes are never parsed twice by accident.** An upload of a file whose parse is still queued or running joins it, as the web library's does ([jobs.md](./jobs.md) § Dedup cache), so re-running a command the shell cut off costs nothing.

### Filing into Files

`parse` with `folder` makes the upload **a Files upload into that folder**: the one a PDF or an image gets on the Files page with converting on arrival on ([api.md](./api.md) § Control plane, `POST /api/library/documents`). It gets a document row named after the uploaded file (a name taken there gets the ` (2)` suffix), its original kept, its notes, its Agent conversations, its retry and re-convert on the web, its storage counted. The commands are the same; the ticket carries the destination, so nothing is added to them.

- **The folder.** The path is split on `/` and each name found among the live folders under the one before it, exactly as `list_library` lists it: the names the library stores, NFC, case as typed.
  - A name that matches no folder there is **created**, with every name after it, under the workspace lock, as the chat agent's `download_file` makes its folders ([chat.md](./chat.md) § Tools).
  - A path whose folders all exist is looked up without that lock, so a shell command running in a conversation does not hold the call up.
  - Every name is checked before any folder is made. Each of these is a tool error, with nothing made:
    - a name that cleans to nothing;
    - `.` or `..` (so a folder the reader named `.` or `..` is no destination);
    - a path deeper than 64 names (`filing.FOLDER_PATH_DEPTH_MAX`), when a folder on it must be made.
  - A name a document holds there is a tool error naming it, nothing made: a folder and a document cannot share a name ([workspace.md](./workspace.md) § Layout).
  - The tool checks the ticket rate first, so a call the rate refuses makes no folder.
  - Otherwise the call makes the folders whether or not an upload follows. The answer's `created` lists them, and the notes tell the agent to say so.
- **Already parsed.** A file the connector parsed without a folder is filed by sending it again on a ticket with one. When the dedup cache answers with that parse ([jobs.md](./jobs.md) § Dedup cache), nothing is parsed or charged again, and the fresh upload becomes the document's original: the earlier parse kept none, as a connector parse keeps no source past its end ([jobs.md](./jobs.md) § Retention). A parse still running is joined, as a Files upload joins one.
  - The cache answers only the same parse: the earlier upload sent no `-F pages=`; the filing upload sends the same `-F model=` the earlier one did (none, if it sent none); and the earlier parse succeeded with no failed page and is still kept (finished less than `PARSE_RETENTION_DAYS` − 1 days ago).
  - Otherwise the filing upload is a new parse of every page, charged as one, and the notes say so, so the agent can tell the reader before it runs.
  - A failed earlier parse is retried first, on a ticket without a folder, then filed.
- **No second copy.** When the folder already holds a live document of that same parse (the command was run again, or the reader uploaded the file there before), the upload is answered with that document and files nothing: re-running a command the shell cut off never makes a ` (2)`.
- **Every page.** A filed parse covers the whole document, as a Files upload does: `-F pages=` on a ticket with a folder is **400**, the ticket unspent.
- **Retried where it is.** A filed parse that fails shows in Files as a failed document, opening on its original with its retry ([web.md](./web.md) § Files).
  - **retry**, on a fresh ticket, retries that document as the Files page's retry does: from its original, its succeeded pages kept ([api.md](./api.md) § POST /v1/parse, `retry`).
  - That ticket comes from `parse` **without** `folder`: a retry resumes a parse where it is and files nothing, so `retry` on a ticket with a folder is **400**, the ticket unspent.
  - Two retries of one document that overlap are one: the later finds it queued and is answered with it.
- **Refused filing, failed command.** An upload whose filing is refused after the intake (the storage full, `storage_full`; an original that could not be kept) gives back the ticket the intake spent. The read after it gets **409** and the command exits non-zero, even when the cache answered.
- **What lands on disk** is unchanged: the `.md`, the figures, the metadata through `last`. That `.md` is a copy. The document in Files is the one the reader edits, read and saved through `read_document` and `write_document`, its id from `list_library`.
- **A folder gone meanwhile.**
  - A folder trashed between the call and the upload refuses the upload with **404**, the ticket unspent.
  - One deleted for good meanwhile leaves the ticket filing at the top level, as a Files upload whose folder goes mid-upload lands there.

### The commands

- **upload** is the whole happy path in one command. Set `f` to the file, and the command uploads it, waits on the SSE stream until the job is terminal, then lands the document **named after the source**: `paper.pdf` → `paper.md` (`${f%.*}.md`), so parsing several documents into one directory never collides.
  - No JSON to unwrap. `<!-- page: N -->` markers map every passage to its page; a paragraph the check joined across a page break maps to the page it starts on ([parsing.md](./parsing.md) § Page markers).
  - The stream goes to `<name>.progress.sse`, removed first, so what is there is always this run's and two documents never share one.
  - The notes tell the agent to read the `.md` and **never print the progress file** after a parse: its final `done` event is the entire result JSON on one line, which floods an agent's context.
- **Every command fails loudly and writes no error into a document**, with any `curl` (no option newer than the oldest ones in use).
  - The upload's own `curl` lands whatever the server answers in the progress file. The read that follows it fails with `-f`, which writes nothing on an HTTP error, so the chain exits non-zero.
  - A refused upload (402 `insufficient_credits` with the credits needed and left, 429, a file or model the service refuses) leaves only its short JSON error in the progress file and its ticket unspent, so the read gets 409.
  - A failed parse ends the stream with an `error` event naming the failed pages, and the read gets 404.
  - Either way the notes send the agent to curl's own message, the tail of the progress file or **status**, never to a `.md` holding an error envelope.
  - A read that fails, a spent ticket's after its hour among them, leaves an earlier `.md` as it was.
- `last` resolves to the job this ticket's upload created ([api.md](./api.md) § GET /v1/jobs/last*), so no command needs a job id filled in. The explicit `/v1/jobs/<job_id>` routes exist for durable credentials; `job_id` is in every SSE `progress` event and in `meta.job_id`.
- **markdown** re-fetches the `.md` alone (a dropped chain, a second look).
- **metadata** lands `<name>.meta.json`, the structured remainder: `figures` with captions and their `check`, `pages`, `meta`.
- **figures** lands the crops. It needs the `.md` and the ticket that made it, names each figure the parse holds no crop for (404), and exits non-zero when the `.md` is missing or any other fetch failed.
- **status** answers "is it done / what failed".

`notes` must spell out:

- what a non-zero exit means and where to read why;
- **retry** with the upload's own `pages`, and when to name the fallback;
- **figures** and the `unverified` status;
- the optional form fields: `model` (one of `models`, the first by default), `pages` (e.g. `1-5,8`), `force`;
- that a PDF or an image (jpg/png/webp/heic) both go to the same endpoint;
- the page markers and the coordinate order;
- the recovery rule: if the ticket is spent or expired, call `parse` again; re-uploading the same file returns its parse, finished or still running, without parsing it twice ([jobs.md](./jobs.md) § Dedup margin).

A parse through the connector **not filed** is in no one's library, so it is kept `PARSE_RETENTION_DAYS` (30) days after it finishes and then deleted ([jobs.md](./jobs.md) § Retention). An unfiled ticket's notes say so of an upload on this ticket, since a `retry` on it may resume a Files document, which is kept. They also say:

- the files the commands land on disk are the copy that lasts;
- how to keep it in Files instead: `parse` with `folder`, then the same upload again, free only when it is the same parse (§ Filing into Files, *Already parsed*).

A **filed** ticket's notes say instead:

- where the document is (`Files › <path>`, named after the uploaded file);
- that for a readable entry the agent copies the file under a readable name, never renaming the reader's own file;
- the folders the call created;
- that it is kept until the reader deletes it;
- that the landed `.md` is a copy and the document in Files is edited through the library tools;
- that `pages` is refused;
- when filing a paper parsed before is free;
- that a refused filing files nothing and fails the command;
- that a failure is retried with **retry** on a fresh ticket from `parse` without `folder`.

The server instructions and the tool description stay short and explicit about the coordinate contract: the placeholder as `sightread://pPAGE/YMIN,XMIN,YMAX,XMAX`, `bbox_format` `yxyx_norm1000`, and the crops the **figures** command saves. They also say:

- when to file: the reader asks to keep, save or file a paper in Ema Paper, or names a folder of their Files library (not a directory on disk); otherwise leave `folder` out;
- that the path comes from `list_library` exactly as listed, because a name that matches no folder creates one;
- that a file parsed earlier is filed by sending it again on a ticket with the folder, free when it is the same parse.

`list_library`'s description names the folder `path`s as what `parse`'s `folder` takes (§ The library tools, *`list_library`*).

No base64 source, no MCP-side progress notifications, no result relay — the REST plane already does all three better (SSE, dedup, `-o` to disk).

## The library tools

The connector also reads the Files library and saves a document's text and notes back. Papers are read and annotated in Ema Paper, then handed to Claude — a deck made in claude.ai, Claude Code's own work.

- **The same rule as `parse`.** The tools mint and format; the bytes go over the REST routes ([api.md](./api.md) § The library on the data plane) straight to disk, so the agent needs the shell with network access the introduction assumes.
- **No text through MCP.** The tools never take or return the text itself, so a document's size never reaches the model's context through MCP. The agent reads the landed files with its own tools, in the parts it needs.
- **Scope.** The tools are the grant's `library` scope ([auth.md](./auth.md) § 4), which every grant holds, as an API key does.

### `list_library`

- **Input:** `folder` (optional) — a folder's `path` as this tool shows it, e.g. `Papers/2024`: that folder and everything below it. `name` (optional) — part of a document's name, any case. `trash` (optional, false) — list the Trash instead.
- **Output, inline:** `{folders: [{id, path}], documents: [{id, name, path, kind, status, page_count}], total, truncated}`.
  - The live library (no Trash), ordered by path.
  - At most `MCP_LIBRARY_LIST_MAX_ITEMS` (300) documents. `truncated` says more matched, with `total` how many, and the description says to narrow by `folder` or `name`.
- **With `trash: true`:** `{trash: [{id, kind, name, path, trashed_at}], total, truncated}`, the rows of `GET /v1/library/trash` ([api.md](./api.md) § The library on the data plane).
  - Each item trashed on its own, newest first, as the Trash tab lists it. An item that went to the Trash inside a folder is the folder's, not a row of its own.
  - `kind` is `folder`, or a document's kind as above.
  - `path` is where Put Back returns the item, then its name: the Trash tab's *Original location*, and what `put_back` takes (§ The organizing tools).
  - `folder` keeps the items whose `path` is under that folder, `name` those whose name holds it; a `folder` that names no live folder is the tool error above.
  - At most `MCP_LIBRARY_LIST_MAX_ITEMS` items, with `total` and `truncated` as above.
- **Why inline:** it is the index the agent picks a document from — rows, never content.
- **Unknown folder:** a `folder` that matches no live folder is a tool error that says to call the tool without it and use a `path` under `folders`.
- **The description** names the folder `path`s as what `parse`'s `folder` and the organizing tools' `to` take, documents of every kind as what the organizing tools' `paths` take (a video kept in Files is a document of kind `file`), and `trash` as what lists `put_back`'s paths.

### `read_document`

Input: `document_id`. Output: a `read` document ticket ([auth.md](./auth.md) § 6), `expires_at`, `document` (the row `GET /v1/library/documents/{id}` answers: `{id, folder_id, name, path, kind, status, media_type, page_count, size_bytes, created_at, finished_at, original}`) and the commands:

- **download**, for a parse or a markdown document:
  - `d=` names a local directory. By default it is the directory name the workspace gives the document — no separator, never `.`, `..` or a hidden name — behind `./` when it starts with `-`, quoted as one shell word. Every name the command writes is quoted the same way.
  - In that directory the command lands `doc.md` and `notes.md` as the web reader holds them, and `document.json` (the row).
  - Under `figures/` it lands the crop of every `sightread://` placeholder in either file as `p<page>_<ymin>_<xmin>_<ymax>_<xmax>.png` (the name the workspace gives it), and every inserted image as `img-<hash>.png|jpg` — the name its `![…](figures/img-…)` link already points at, so the markdown renders where it landed.
  - It saves each file's ETag beside it (`.doc.md.etag`, `.notes.md.etag`) for the save.
  - A picture with none stored (404) — a placeholder's crop or an inserted image — is named and skipped. Any other failed fetch makes the command exit non-zero; the row, `doc.md` and `notes.md` are fetched with `curl -f`, so any error on them stops the command.
  - Downloading again replaces `doc.md` and `notes.md` in `d`.
- **download**, for a file of the library: lands the file itself under its name. There is nothing to save.
- **original** — a parse's original upload as `source.<ext>` in `d`. Present only when `…/original` answers (`original` in the row): the original is kept or, until it is filed, the upload it is filed from answers in its place. Separate, as it can be the size of the upload.
- `notes` spell out:
  - the layout above;
  - that a parse still queued or running has no `doc.md` yet (409);
  - that the placeholder's coordinates are `yxyx_norm1000`;
  - that saving goes through `write_document`.

### `write_document`

Input: `document_id`. Output: a `write` document ticket, `expires_at`, `document`, and **save**.

- **save** — with `d` as **download** used it and `f` set to `doc.md` or `notes.md`, the command `PUT`s the local file with `If-Match` from its `.etag`.
  - On 200 it stores the new ETag, so the next save follows on.
  - Anything else prints the server's short JSON error and exits non-zero, the local file untouched.
- `notes` spell out:
  - edit the downloaded files in place;
  - keep the `<!-- page: N -->` markers and the figure placeholders as they are, since the web reader draws them;
  - a new `figures/img-…` link has no image behind it;
  - on **409** `revision_conflict` the file changed in Ema Paper since the download (the reader or its Agent edited it): download it again into another `d`, carry the edits over and save from there;
  - every save is one revision of the document's history.
- Each save's revision is authored `api`.
- A file of the library is a tool error: it has no text to save.

## The organizing tools

The connector also puts the reader's Files library in order, as the Files page does: `new_folder`, `move`, `rename`, `copy`, `trash`, `put_back`, and `undo`. A reader who asked Claude to tidy their papers into folders otherwise watched it drive a browser through the Files page.

- **They act within the call.** A move carries no file and no text, so there is no ticket and no command: the tool does it and answers what it did. They need no shell.
- **They are the Files Agent's tools.** The first six take the inputs, run the operations and answer the sentences of a library conversation's library tools ([chat.md](./chat.md) § Library conversations, *The library tools*), from the same code (`workspace/organize.py`). What holds there holds here, and this section says only what differs.
  - An item is named by its place as `list_library` lists it — `DL/DINO/iBOT.pdf`, a folder the same way, `/` for the top. A path under `/workspace/files/` names it too, as there, though only a library conversation's agent sees one.
  - The website's guards hold: live rows only; ` (2)` where a move, a copy or a Put Back meets a taken name; a rename that collides refused; a markdown document's `.md` kept; the quota before a copy; a folder never into its own subtree; the refusal while a conversation about a document an item holds is answering.
  - Every path is looked up before the first item changes, and each item is its own transaction: a refused item is reported and the next goes on.
  - A call names at most `CHAT_LIBRARY_MAX_PATHS` (100) paths.
  - `move` and `copy` take a folder that exists: a `to` that names none refuses the whole call and makes nothing, and the description says to make it with `new_folder` first. `parse`'s `folder` makes what is missing because a paper filed somewhere new is the point of the call; a move names items the reader already has, and a typo should not grow a folder.
  - Every tree commit they make is authored `api` ([workspace.md](./workspace.md) § Revisions).
- **The answer** is `{result, done, failed, undo}`:
  - `result` — the sentences, one per item: *Moved DL/iBOT.pdf (document 34) to DL/DINO/iBOT.pdf.*, *Could not move DL/x.pdf: nothing is there.*
  - `done`, `failed` — how many items changed and how many were refused.
  - `undo` — the undo record of what the call changed, in the shapes the Files page's undo keeps its steps in ([web.md](./web.md) § Files, *Undo*); null when it changed nothing.
  - A call that changed nothing because it was refused — the whole call, or every item it named — is a tool error with those sentences.

| Tool | Input | What it does |
|------|-------|--------------|
| `new_folder` | `{path}` | Makes every folder of `path` that is missing. |
| `move` | `{paths, to}` | Each item into the folder `to` names (`/` the top). |
| `rename` | `{path, name}` | The item renamed; `name` is the new name alone. |
| `copy` | `{paths, to}` | Each item copied into `to`. |
| `trash` | `{paths}` | Each item to the Trash. |
| `put_back` | `{paths}` | Each item of the Trash put back, named as `list_library` with `trash: true` lists it. |
| `undo` | `{step, redo?}` | An earlier answer's `undo` taken back, or done again. |

- **`undo`** takes `step`, an earlier answer's `undo` unchanged, and replays its inverse as the Files page's ⌘Z replays a step, by the ids the record names:
  - a move goes back where it came from, under its old name when the destination had suffixed it;
  - a rename goes back to its old name;
  - a trash is put back;
  - an `appear` — a new folder, a copy, a Put Back — goes to the Trash, never deleted for good.
  - Its items go in reverse order, each its own transaction; an item gone or changed since is that item's error, as a step the page cannot replay is dropped with its message.
  - An item moved or renamed since the record is refused, never put back: *Could not move document 34: it was moved or renamed since.* A move replays only an item still in the folder, and under the name, the record left it; a rename only one still named what it left it. An agent may keep a record a while, and the reader may have changed the library since.
  - `redo: true` does the step again, as ⇧⌘Z does.
  - Its answer carries no record of its own: the same `step`, with `redo` flipped, reverses it.
  - `step` is checked against the four record shapes before anything changes, at most `CHAT_LIBRARY_MAX_PATHS` items; anything else is a tool error.
  - It is a tool of the connector's alone. A library conversation has the card's Undo, which runs the page's own code ([web.md](./web.md) § The Files page's Agent card).
  - Why by id: a path-based inverse can reach the wrong item, since the old place may hold a new item of the old name and `put_back` by path takes the newest of a name.
- **Nothing is deleted for good.** No tool reaches *Delete Immediately* or *Empty Trash* ([workspace.md](./workspace.md) § The page's operations are the agent's). The Trash keeps an item until the reader empties it or `TRASH_RETENTION_DAYS` pass ([workspace.md](./workspace.md) § The Trash), and `trash`'s description says so.
- **A call runs to its end.** It waits for the workspace lock, which a conversation's shell command can hold for up to `CHAT_BASH_TIMEOUT_SECONDS`, so a call may outlast the client's wait. A client that stops waiting never cuts a batch off between items: the call is shielded from the request's cancellation. The descriptions say to check `list_library` before repeating a call that timed out, since a `copy` repeated copies again.
- **Scope.** The tools are the grant's `organize` scope ([auth.md](./auth.md) § 4), which every live grant holds, as an API key does.
- **The data plane has the same operations**: `POST /v1/library/<tool>` with the tool's input as the JSON body, answered from the same function ([api.md](./api.md) § The library on the data plane).

The server instructions say the connector organizes the Files library with these tools; that they need no shell or browser and do what the Files page does; that items are named by path exactly as `list_library` lists it, `/` for the top, and a folder is made with `new_folder` before anything moves into it; that when the reader asks, the agent acts without asking first, since each step comes back with `undo`; and that nothing is deleted for good — the reader empties the Trash in the Trash tab. The `parse` description adds that a document already in Files is moved or renamed with these tools, never parsed again.

## Annotations

| Tool (`ToolAnnotations`) | `read_only_hint` | `destructive_hint` | `open_world_hint` |
|---|---|---|---|
| `list_library` | true | — | false |
| `read_document` | true | — | false |
| `write_document` | false | false | false |
| `parse` | false | false | false |
| `new_folder`, `move`, `rename`, `copy`, `trash`, `put_back`, `undo` | false | false | false |

- `list_library` and `read_document` are read-only, so a client may run them without asking: neither changes the library. `read_document` still records the ticket it mints and counts against its mint rate.
- `write_document` is not read-only and not destructive: the save its command makes is a revision, revertible.
- `parse` is marked the same way, since it may make folders and file a document: nothing it does removes or overwrites anything.
- The organizing tools are not destructive either: each step is taken back with `undo`, and `trash` keeps what it takes in the Trash, as the Files page's *Move to Trash* does without asking ([web.md](./web.md) § Files).
- All are closed-world: they reach this deployment only.

## Claude Connectors flow (the reason the OAuth AS exists)

1. User adds `https://<host>/mcp` as a custom connector.
2. Claude discovers `/.well-known/oauth-protected-resource` → AS metadata → performs Dynamic Client Registration → browser consent (Google session) → token.
3. `parse` calls arrive with the OAuth bearer; the tickets it mints are scoped to that user, and usage lands in the same `usage_log` as REST calls.

## Implementation notes

- **Stateless streamable HTTP.** Every request carries its own bearer and gets its own transport. So a connector never depends on session affinity, and a token can never be inherited from an earlier session. The SDK's session manager runs for the app's lifetime (FastAPI lifespan).
- **Auth is ours, not the SDK's.** A small ASGI guard in front of the transport resolves the bearer through the same code path as `/v1`.
  - It answers 401 with `WWW-Authenticate: Bearer resource_metadata="<APP_URL>/.well-known/oauth-protected-resource"` — the pointer that starts a connector's OAuth discovery (RFC 9728).
  - When a bearer was presented and resolves to no one, the challenge also carries `error="invalid_token"`: `Bearer error="invalid_token", resource_metadata="…"`.
- **The library tools** resolve the caller in the same guard.
  - They read the library through the control plane's own reads (the one snapshot, the live rows, the document's own liveness).
  - They mint through the document ticket module ([auth.md](./auth.md) § 6).
  - The commands use options of any `curl` the `parse` commands may meet (`--etag-save` since 7.68), are written for bash, dash and zsh, and quote every name the reader gave as one shell word.
- **The organizing tools** resolve the caller in the same guard, then call `workspace.organize.run_locked`: the one function a library conversation's tools call, which takes the backup lock shared, the workspace lock and a session of its own, in that order, and runs the operation by its name.
  - `mcp/server.py` takes no lock and writes nothing itself; `/v1/library/<tool>` runs the same operation under the same locks.
  - The call is awaited through `asyncio.shield`, so a client that goes away leaves the batch to finish.
  - Its author is `api`.
- **`parse`** does these things and nothing else:
  - resolves the caller to a user;
  - with `folder`, finds or makes the destination through the library's own filing function (`workspace.filing.find_or_make_folder_path`, the one `POST /v1/parse`'s `folder` field takes);
  - mints the ticket via the shared ticket module, with the destination ([auth.md](./auth.md) § 5 — TTL, mint rate limit, opportunistic cleanup live there, not in MCP);
  - reads the parse agent's models from the settings;
  - formats the command strings from `APP_URL`.
- The check, the crops, the retry, the in-flight join and the filing are the data plane's ([api.md](./api.md)), so MCP still owns no parsing or filing decision.
