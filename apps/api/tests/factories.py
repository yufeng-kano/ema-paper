"""Rows a test writes straight to the database. Each fills the NOT NULL columns a test does
not care about with one fixed value; a test passes every field it or the code under test
reads."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from sightread.db.models import Document, Folder, Job, Result, User
from tests.conftest import PARSE_MODEL


def job_row(user_id: int, **fields) -> Job:
    """A finished one-page parse of a PDF, with a sha256 of its own."""
    return Job(
        **{
            "id": uuid.uuid4(),
            "user_id": user_id,
            "agent": "parse",
            "kind": "pdf",
            "filename": "paper.pdf",
            "media_type": "application/pdf",
            "size_bytes": 1,
            "sha256": uuid.uuid4().hex * 2,
            "model": PARSE_MODEL,
            "bbox_format": "yxyx_norm1000",
            "prompt": "Transcribe page {page}.",
            "status": "succeeded",
            "page_count": 1,
            **fields,
        }
    )


def user_row(email: str, **fields) -> User:
    """An account signed in with Google as `email`."""
    return User(**{"google_sub": email, "email": email, **fields})


async def filed_parse(
    db: AsyncSession,
    user_id: int,
    *,
    markdown: str = "# Title\n\nBody\n",
    name: str = "paper.pdf",
    folder: str | None = "Papers",
    **job_fields,
) -> Document:
    """A finished parse of `name` filed in the library, `markdown` its result to materialize:
    in a new top-level folder `folder`, or at the top level. Added and flushed; the caller
    commits."""
    folder_id = None
    if folder is not None:
        row = Folder(user_id=user_id, parent_id=None, name=folder)
        db.add(row)
        await db.flush()
        folder_id = row.id
    job = job_row(
        user_id, **{"filename": name, "size_bytes": 10, "model": "claude-x", **job_fields}
    )
    db.add(job)
    await db.flush()
    db.add(Result(job_id=job.id, markdown=markdown, pages=[], figures=[], errors=[], meta={}))
    document = Document(user_id=user_id, folder_id=folder_id, job_id=job.id, name=name)
    db.add(document)
    await db.flush()
    return document
