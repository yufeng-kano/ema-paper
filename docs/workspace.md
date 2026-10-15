# Workspace — the per-user file tree

Every user owns one directory on the server, `{WORKSPACES_DIR}/{user_id}/`. It is where a document's editable copy, its notes, its original upload and its figure crops live, it is what the user's sandbox VM sees as `/workspace` ([sandbox.md](./sandbox.md)), and it is a git repository, which is how every edit — the user's, the agent's, a shell command's — becomes a revision that can be read back and undone. The workspace is never deleted: a user who stops using the service keeps their files until the operator removes the account.

## Layout

```text
{WORKSPACES_DIR}/{user_id}/            host side; the VM mounts it as /workspace
├── .git/                              revision history, one repo per user
├── .gitignore                         source files and the uv cache stay out of history
├── .sightread/
│   └── results/{job_id}/result.md     the untouched parse output, written once, never edited
└── files/                             one-to-one with the Files page's folder tree
    └── <folder>/<subfolder>/
        └── <document name>/           one document = one directory
            ├── doc.md                 the editable transcription
            ├── notes.md               the user's notes, shown beside the document
            ├── source.pdf             the original upload (or source.<image ext>)
            ├── figures/               p<page>_<y1>_<x1>_<y2>_<x2>.png, copied from FIGURES_DIR
            └── .meta.json             document_id, job_id, model, bbox_format, page_count
```

- **A document is a directory, not a file.** Content, notes, crops, the original and the metadata travel together; a rename or a move is one `git mv`.
- **The directory name is the document's name**, the folder path is its folders' names. The library already keeps names unique per level ([database.md](./database.md)); `path_segment()` keeps them unique as directories too: separators and control characters are replaced, and a name that starts with `.` or `_`, is empty, or is one of the workspace's own names (`.sightread`, `.meta.json`, `.cache`, `.git`) is kept whole behind a `_` prefix — `.report` becomes `_.report`, never `report`, so two distinct names are two directories.
- **The parse output stays immutable.** `results` in PostgreSQL remains the record of what the model produced; `.sightread/results/{job_id}/result.md` is a read-only copy for the agent to compare against. A document filed twice from one parse gets two independent `doc.md`, both copied from that one result.
- **Materialization is lazy and idempotent.** A document directory is written the first time anything needs it — the viewer asking for content, a conversation starting — and at the moment a library upload's parse finishes. `materialize_document()` writes what is missing and touches nothing that exists, so an edited `doc.md` is never overwritten by the parse result — which is also why a document whose parse has no result yet cannot be materialized at all (**409**): an empty `doc.md` written early would never be replaced.
- **The original upload is kept** for library documents: at terminal state the worker moves the source file into the document directory (a copy into each, when one parse was filed in several places) instead of deleting it ([jobs.md](./jobs.md) § Retention), so the agent can re-render a page with Poppler. Parses submitted through the API or the connector are not in the library and keep today's rule — their source is deleted.

## Revisions

The user directory is one git repository; `sightread.workspace.git` is the only module that runs git (as a subprocess, with `GIT_DIR`/`GIT_WORK_TREE` set, never the user's global config).

- Every write through the API or a tool commits: `doc.md` saves from the viewer, `notes.md` saves, every edit the agent's editor tool makes, and whatever a shell command changed — after each tool call the runner commits a dirty tree. The commit author names who did it (`user` or `agent` with the conversation id), the message names the file and the tool.
- **Writes are serialized per user** by a lock in the workspace module: the user editing in the viewer, two conversations editing in parallel and a `git mv` from a rename must not interleave their index operations. A rename, move or delete on the Files page holds that lock across its database write *and* the directory move, so no read can materialize a fresh copy at the new path in between.
- **Revisions follow renames.** A revert reads the file where it lived at that commit (`git log --follow`), so the history of a document that was renamed or moved stays revertible.
- `revision` in the API is the SHA-256 of the file's current content, not a commit id: a save carries the `base_revision` it was made from, and a save whose base no longer matches the file is refused with **409** — the client reloads and the reader sees what changed in between. The git log is the history the UI lists; `revert` writes the file as it was before a given commit and commits that, so undoing is itself a revision and nothing is ever rewritten.
- `source.*`, `.sightread/` and the sandbox's package cache are gitignored: history records text, not binaries.

## The tree is the truth

A shell command in the sandbox can `mkdir`, `mv` and `rm` under `/workspace/files`. After every conversation turn the runner reconciles the tree with the database: each `.meta.json` names its `document_id`, so a moved or renamed directory updates the row's folder and name, and a folder that appeared becomes a `folders` row. Reconciliation never deletes a row: a document directory that disappeared is materialized again from the parse result on its next read, and deleting a document is done from the Files page, where it is deliberate. A directory without `.meta.json` is not a document — a file the agent wrote is visible to the agent and in the sandbox, not in the library; `documents.job_id` stays required.

Reconciliation runs after turns and when the API itself moved something; it is not a watcher. A change made from the shell between turns shows on the Files page after the next turn.

## Quota

`WORKSPACE_QUOTA_BYTES` (default 10 GiB) is the size the Usage page shows the workspace against and the point past which the API refuses to materialize another document (**409**) and a chat turn fails before it starts — no shell runs in a full workspace. Hard enforcement is the operator's: mount `{WORKSPACES_DIR}` on a filesystem with per-directory quotas, or back each user directory with a fixed-size image ([sandbox.md](./sandbox.md) § Operations). Usage is measured by walking the tree (`du`), cached per user for the status route.

## Backups

The workspace is primary data from this version on — the parse output can be recreated from PostgreSQL, the edits, notes and originals cannot. The production backup service archives `{WORKSPACES_DIR}` with the database dump ([deployment.md](./deployment.md)).
