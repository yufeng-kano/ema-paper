"""The per-user workspace: materialization, revisions, the tree (docs/workspace.md).

Real git in a temporary directory; no database beyond the SQLite fallback.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from sightread.config import Settings
from sightread.db.models import Document, Folder, Job, Result, User
from sightread.errors import ApiError
from sightread.workspace import documents, git, reconcile
from sightread.workspace.layout import (
    git_dir,
    inside,
    is_canonical_segment,
    path_segment,
    segment_name,
    user_root,
)
from tests.conftest import KANO_BASE, TEST_SECRET_KEY


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="local",
        secret_key=TEST_SECRET_KEY,
        workspaces_dir=str(tmp_path / "workspaces"),
        figures_dir=str(tmp_path / "figures"),
        kano_proxy_base_url=KANO_BASE,
    )


async def _seed(db, *, markdown: str = "# Title\n\nBody\n") -> tuple[User, Document]:
    user = User(google_sub="ws", email="ws@example.com")
    db.add(user)
    await db.flush()
    folder = Folder(user_id=user.id, parent_id=None, name="Papers")
    db.add(folder)
    await db.flush()
    job = Job(
        id=uuid.uuid4(),
        user_id=user.id,
        agent="parse",
        kind="pdf",
        filename="paper.pdf",
        media_type="application/pdf",
        size_bytes=10,
        sha256="a" * 64,
        model="claude-x",
        bbox_format="yxyx_norm1000",
        status="succeeded",
        page_count=2,
    )
    db.add(job)
    await db.flush()
    db.add(Result(job_id=job.id, markdown=markdown, pages=[], figures=[], errors=[], meta={}))
    document = Document(user_id=user.id, folder_id=folder.id, job_id=job.id, name="paper.pdf")
    db.add(document)
    await db.commit()
    return user, document


def test_path_segments_cannot_escape_or_shadow_the_workspace() -> None:
    assert path_segment("..") == "_.."
    assert path_segment(".hidden") == "_.hidden"
    assert path_segment(".sightread") == "_.sightread"
    assert path_segment("a/b\\c") == "a b c"
    assert path_segment(".meta.json") == "_.meta.json"
    assert path_segment("") == "_"
    # Distinct names stay distinct directories (a leading `_` is escaped by another).
    names = ["report", ".report", "_report", "_", ".", "..", ".git"]
    assert len({path_segment(name) for name in names}) == len(names)
    root = Path("/w/1")
    assert inside(root, root / "files" / "x")
    assert not inside(root, root / ".." / "2")


async def test_materialize_writes_once_and_edits_commit_revisions(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        content = await documents.read_content(db, settings, document)
        assert content.markdown == "# Title\n\nBody\n"
        assert content.notes == ""
        paths = await documents.document_paths(db, settings, document)
        assert (
            paths.directory
            == user_root(settings.workspaces_dir, user.id) / "files" / "Papers" / "paper.pdf"
        )
        assert json.loads(paths.meta.read_text())["document_id"] == document.id
        assert (paths.root / ".sightread" / "results" / str(document.job_id) / "result.md").exists()

        # A stale base is refused; the matching one commits.
        with pytest.raises(ApiError) as refused:
            await documents.write_content(
                db, settings, document, markdown="changed", base_revision="nope"
            )
        assert refused.value.status_code == 409
        revision = await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        again = await documents.read_content(db, settings, document)
        assert again.markdown == "# Title\n\nEdited\n"
        assert again.revision == revision
        # Materializing again overwrites nothing.
        await documents.materialize_document(db, settings, document)
        assert paths.doc.read_text() == "# Title\n\nEdited\n"

        history = await documents.revisions(db, settings, document)
        assert [entry.message for entry in history][:2] == [
            "edit files/Papers/paper.pdf/doc.md",
            "materialize files/Papers/paper.pdf",
        ]

        # Revert the edit: a new revision that restores the earlier text.
        restored = await documents.revert(db, settings, document, history[0].commit)
        assert restored == content.revision
        assert paths.doc.read_text() == "# Title\n\nBody\n"
        assert (await documents.revisions(db, settings, document))[0].message.startswith("revert")


async def test_reconcile_follows_a_directory_the_shell_moved(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        target = root / "files" / "Archive" / "2024" / "renamed.pdf"
        target.parent.mkdir(parents=True)
        paths.directory.rename(target)
        git.commit_all(root, "agent:1", "mv from the shell")

        changed = await reconcile.reconcile_user(db, settings, user.id)
        assert changed == 1
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "renamed.pdf"
        folders = {
            f.id: f
            for f in (await db.execute(select(Folder).where(Folder.user_id == user.id))).scalars()
        }
        assert folders[row.folder_id].name == "2024"
        assert folders[folders[row.folder_id].parent_id].name == "Archive"


async def test_keep_source_moves_the_upload_into_the_document_directory(tmp_path) -> None:
    root = tmp_path / "ws" / "7"
    upload = tmp_path / "upload.pdf"
    upload.write_bytes(b"%PDF-1.4")
    kept = documents.keep_source(root, Path("files/paper.pdf"), upload)
    assert kept == root / "files" / "paper.pdf" / "source.pdf"
    assert kept.read_bytes() == b"%PDF-1.4"
    assert not upload.exists()
    # Ignored by git: the binary never enters history.
    assert git.commit_all(root, "user", "x") is None or "source" not in git.log(root)[0].files


async def test_revert_follows_a_rename(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        content = await documents.read_content(db, settings, document)
        await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        edit = (await documents.revisions(db, settings, document))[0]
        before = await documents.document_paths(db, settings, document)
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        await documents.move_path(settings, user.id, before.relative, after.relative)
        assert after.doc.read_text() == "# Title\n\nEdited\n"

        restored = await documents.revert(db, settings, document, edit.commit)
        assert restored == content.revision
        assert after.doc.read_text() == "# Title\n\nBody\n"


async def test_materialize_refuses_a_full_workspace(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.workspace_quota_bytes = 1
    async with sessionmaker() as db:
        _, document = await _seed(db)
        root = user_root(settings.workspaces_dir, document.user_id)
        root.mkdir(parents=True)
        (root / "big.bin").write_bytes(b"x" * 10)
        with pytest.raises(ApiError) as refused:
            await documents.materialize_document(db, settings, document)
        assert refused.value.status_code == 409


async def test_the_git_directory_is_outside_the_tree_and_a_planted_hook_never_runs(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        assert not (root / ".git").exists()
        assert (git_dir(root) / "HEAD").is_file()
        assert "source.*" in (git_dir(root) / "info" / "exclude").read_text()

        # What a shell command could do: a `.git` of its own with a hook in it.
        hook_dir = root / ".git" / "hooks"
        hook_dir.mkdir(parents=True)
        hook = hook_dir / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {tmp_path / 'hook-ran'}\n")
        hook.chmod(0o755)
        content = await documents.read_content(db, settings, document)
        await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        assert not (tmp_path / "hook-ran").exists()
        assert git.log(root)[0].message.startswith("edit ")
        assert not any(name.startswith(".git/") for name in git.log(root)[0].files)


async def test_a_symlinked_document_file_is_refused(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("not yours")
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)
        paths.doc.unlink()
        paths.doc.symlink_to(outside)
        with pytest.raises(ApiError) as refused:
            await documents.read_content(db, settings, document)
        assert refused.value.status_code == 409
        with pytest.raises(ApiError):
            await documents.write_content(
                db, settings, document, markdown="x", base_revision=content.revision
            )
        assert outside.read_text() == "not yours"
        # The directory itself as a link is no better.
        paths.doc.unlink()
        paths.doc.write_text(content.markdown)
        elsewhere = tmp_path / "elsewhere"
        shutil.copytree(paths.directory, elsewhere)
        shutil.rmtree(paths.directory)
        paths.directory.symlink_to(elsewhere)
        with pytest.raises(ApiError):
            await documents.read_content(db, settings, document)


def test_segments_decode_and_know_whether_they_are_canonical() -> None:
    assert segment_name("_.report") == ".report"
    assert segment_name("report") == "report"
    assert is_canonical_segment("report")
    assert is_canonical_segment("_.report")
    assert is_canonical_segment("__report")
    assert not is_canonical_segment(".report")
    assert not is_canonical_segment("_report")


async def test_reconcile_canonicalizes_a_directory_the_shell_named(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        shell_named = paths.directory.with_name(".report")
        paths.directory.rename(shell_named)

        changed = await reconcile.reconcile_user(db, settings, user.id)
        assert changed == 1
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == ".report"
        assert not shell_named.exists()
        canonical = await documents.document_paths(db, settings, document)
        assert canonical.directory == paths.directory.with_name("_.report")
        assert canonical.doc.is_file()
        # The next read finds the moved files, not a fresh copy beside them.
        assert (
            await documents.read_content(db, settings, document)
        ).markdown == "# Title\n\nBody\n"
        assert git.log(root)[0].message.startswith("canonicalize")


def test_long_names_become_bounded_distinct_segments() -> None:
    long_a = "é" * 300
    long_b = "é" * 299 + "x"
    seg_a, seg_b = path_segment(long_a), path_segment(long_b)
    assert len(seg_a.encode()) <= 255 and len(seg_b.encode()) <= 255
    assert seg_a != seg_b
    assert "~" in seg_a and seg_a.startswith("ééé")
    assert path_segment(long_a) == seg_a  # stable
    assert len(path_segment("_" + "a" * 254).encode()) <= 255


async def test_an_inflated_document_file_is_refused(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.content_max_bytes = 64
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)
        paths.doc.write_text("x" * 65)
        with pytest.raises(ApiError) as refused:
            await documents.read_content(db, settings, document)
        assert refused.value.status_code == 409
        with pytest.raises(ApiError):
            await documents.write_content(
                db, settings, document, markdown="y", base_revision=content.revision
            )
