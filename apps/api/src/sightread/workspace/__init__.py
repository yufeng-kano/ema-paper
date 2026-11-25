"""The per-user workspace: every user's file tree and its revisions (docs/workspace.md).

The only package that touches `WORKSPACES_DIR` and the only one that runs git. Routes,
the worker and the chat agent's tools go through it. The one exception is a document's
original, kept outside the mount: `jobs/intake.py` copies it for a retry and reads it for
a re-convert, at the path `original_of` answers (docs/project-structure.md).
"""
