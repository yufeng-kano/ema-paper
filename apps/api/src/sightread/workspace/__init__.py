"""The per-user workspace: every user's file tree and its revisions (docs/workspace.md).

The only package that touches `WORKSPACES_DIR` and the only one that runs git. Routes,
the worker and the chat agent's tools go through it; nothing else opens a file under a
user's directory.
"""
