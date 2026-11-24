"""Rows a test writes straight to the database. Each fills the NOT NULL columns a test does
not care about with one fixed value; a test passes every field it or the code under test
reads."""

from __future__ import annotations

import uuid

from sightread.db.models import Job, User
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
