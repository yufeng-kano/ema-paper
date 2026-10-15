"""The per-user workspace: materialization, revisions, the tree (docs/workspace.md).

Real git in a temporary directory; no database beyond the SQLite fallback.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from sightread.config import Settings
from sightread.db.models import Document, Folder, Job, Result, User
from sightread.errors import ApiError
from sightread.workspace import documents, git, reconcile, safe_io
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
        restored = (await documents.revert(db, settings, document, history[0].commit)).revision
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

        restored = (await documents.revert(db, settings, document, edit.commit)).revision
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


async def test_a_fifo_or_oversized_save_is_refused(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.content_max_bytes = 64
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)
        with pytest.raises(ApiError) as too_big:
            await documents.write_content(
                db, settings, document, markdown="x" * 65, base_revision=content.revision
            )
        assert too_big.value.status_code == 409
        assert paths.doc.read_text() == content.markdown
        paths.notes.unlink()
        os.mkfifo(paths.notes)
        with pytest.raises(ApiError) as fifo:
            await documents.read_content(db, settings, document)
        assert fifo.value.status_code == 409


async def test_a_move_puts_the_document_where_the_row_says(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)
        await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        # The shell left an unrelated directory under the name the user now picks.
        squatter = before.directory.with_name("renamed.pdf")
        squatter.mkdir()
        (squatter / "stray.txt").write_text("from bash")
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        await documents.move_path(settings, user.id, before.relative, after.relative)
        assert after.doc.read_text() == "# Title\n\nEdited\n"
        assert (await documents.read_content(db, settings, document)).markdown.endswith("Edited\n")
        moved_aside = [
            p for p in after.directory.parent.iterdir() if p.name.startswith("renamed.pdf (")
        ]
        assert len(moved_aside) == 1 and (moved_aside[0] / "stray.txt").read_text() == "from bash"


def test_shell_diffs_and_revisions_are_read_within_a_bound(tmp_path) -> None:
    root = tmp_path / "ws" / "9"
    (root / "files").mkdir(parents=True)
    git.ensure_repo(root)
    (root / "files" / "big.txt").write_text("line\n" * 20_000)
    commit = git.commit_all(root, "agent:1", "big")
    assert commit is not None
    diff = git.show_commit(root, commit, 2_000)
    assert len(diff) < 2_100 and diff.rstrip().endswith("[... diff cut by the workspace ...]")
    with pytest.raises(git.GitError):
        git.show(root, commit, Path("files/big.txt"), 1_000)
    assert git.show(root, commit, Path("files/big.txt"), 1_000_000) == "line\n" * 20_000


async def test_reconcile_ignores_oversized_metadata(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        moved = paths.directory.with_name("moved.pdf")
        paths.directory.rename(moved)
        (moved / ".meta.json").write_text(json.dumps({"document_id": document.id}) + " " * 70_000)
        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "paper.pdf"


async def test_a_directory_where_a_file_belongs_is_refused(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        paths.notes.unlink()
        paths.notes.mkdir()
        with pytest.raises(ApiError) as refused:
            await documents.read_content(db, settings, document)
        assert refused.value.status_code == 409


async def test_the_lock_lives_outside_the_mount_and_saves_commit_only_their_file(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        assert not (root / ".sightread" / "lock").exists()
        assert (git_dir(root).parent / f"{root.name}.lock").exists()
        # What a shell would leave half-written while the viewer saves.
        stray = root / "files" / "stray.txt"
        stray.write_text("from a running command")
        content = await documents.read_content(db, settings, document)
        await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        latest = git.log(root)[0]
        assert latest.files == (paths.doc_relative.as_posix(),)
        assert stray.read_text() == "from a running command"
        assert "files/stray.txt" not in {f for rev in git.log(root) for f in rev.files}


async def test_revert_undoes_every_path_of_a_shell_commit(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        # A shell command edited the document and left a helper file next to it.
        paths.doc.write_text("# Title\n\nShelled\n")
        (paths.directory / "helper.py").write_text("print(1)\n")
        commit = git.commit_all(root, "agent:1", "bash: python fix.py")
        assert commit is not None
        restored = (await documents.revert(db, settings, document, commit)).revision
        assert paths.doc.read_text() == "# Title\n\nBody\n"
        assert not (paths.directory / "helper.py").exists()
        assert restored == documents.revision_of("# Title\n\nBody\n")


async def test_reconcile_leaves_a_row_alone_when_its_id_is_in_two_places(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        shutil.copytree(paths.directory, paths.directory.with_name("copy.pdf"))
        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "paper.pdf"


async def test_a_late_library_row_adopts_the_finished_upload(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        job = (await db.execute(select(Job).where(Job.id == document.job_id))).scalar_one()
        upload = tmp_path / "upload.pdf"
        upload.write_bytes(b"%PDF-1.4")
        job.source_path = str(upload)
        job.retain_source = True
        await db.commit()
        await documents.adopt_source(db, settings, document, job)
        paths = await documents.document_paths(db, settings, document)
        assert (paths.directory / "source.pdf").read_bytes() == b"%PDF-1.4"
        assert not upload.exists()
        assert job.source_deleted_at is not None


async def test_reverting_a_rename_moves_the_directory_back_and_the_row_follows(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)
        await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        await documents.move_path(settings, user.id, before.relative, after.relative)
        rename = git.log(after.root)[0]
        assert rename.message.startswith("move ")

        reverted = await documents.revert(db, settings, document, rename.commit)
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "paper.pdf"
        assert before.doc.read_text() == "# Title\n\nEdited\n"
        assert not after.directory.exists()
        assert reverted.revision == documents.revision_of("# Title\n\nEdited\n")
        assert git.log(after.root)[0].commit == reverted.commit


async def test_non_utf8_content_is_refused_not_a_crash(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        paths.doc.write_bytes(b"\xff\xfe not text")
        with pytest.raises(ApiError) as refused:
            await documents.read_content(db, settings, document)
        assert refused.value.status_code == 409


async def test_a_cached_library_upload_keeps_its_fresh_file(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        job = (await db.execute(select(Job).where(Job.id == document.job_id))).scalar_one()
        fresh = tmp_path / "fresh.pdf"
        fresh.write_bytes(b"%PDF-1.4 fresh")
        await documents.adopt_source(db, settings, document, job, upload=fresh)
        paths = await documents.document_paths(db, settings, document)
        assert (paths.directory / "source.pdf").read_bytes() == b"%PDF-1.4 fresh"
        assert not fresh.exists()


async def test_a_revert_that_cannot_complete_touches_nothing(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        big = paths.directory / "big.txt"
        big.write_text("x" * 5000)
        git.commit_all(root, "agent:1", "bash: seed")
        paths.doc.write_text("# Title\n\nShelled\n")
        big.write_text("y" * 5000)
        commit = git.commit_all(root, "agent:1", "bash: change both")
        settings.content_max_bytes = 1000
        with pytest.raises(ApiError) as refused:
            await documents.revert(db, settings, document, commit)
        assert refused.value.status_code == 409
        # Neither path moved: the document still reads as the shell left it.
        assert paths.doc.read_text() == "# Title\n\nShelled\n"
        assert big.read_text() == "y" * 5000
        assert git.log(root)[0].commit == commit


async def test_deleting_through_a_linked_ancestor_is_refused(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        elsewhere = tmp_path / "elsewhere"
        shutil.copytree(paths.directory.parent, elsewhere)
        shutil.rmtree(paths.directory.parent)
        paths.directory.parent.symlink_to(elsewhere)
        with pytest.raises(ApiError) as refused:
            await documents.remove_path(settings, user.id, paths.relative)
        assert refused.value.status_code == 409
        assert (elsewhere / "paper.pdf" / "doc.md").exists()


def test_the_kept_source_is_named_by_the_parse_kind(tmp_path) -> None:
    root = tmp_path / "ws" / "8"
    upload = tmp_path / "upload.bin"
    upload.write_bytes(b"%PDF-1.4")
    kept = documents.keep_source(root, Path("files/paper"), upload, kind="pdf")
    assert kept.name == "source.pdf"
    image = tmp_path / "photo.jpeg"
    image.write_bytes(b"\xff\xd8")
    kept = documents.keep_source(root, Path("files/photo"), image, kind="image")
    assert kept.name == "source.jpeg"


def test_a_failed_write_leaves_the_file_as_it_was(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    root.mkdir(parents=True)  # the service's own directory; everything below is the user's
    target = root / "files" / "doc.md"
    safe_io.write_text(root, target, "before")

    class Breaks:
        calls = 0

        def read(self, size: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                return b"half"
            raise OSError("disk full")

    with pytest.raises(OSError):
        safe_io.write_from(root, target, Breaks())
    # Neither truncated nor half-written: the replacement never reached the name.
    assert target.read_text() == "before"
    assert not list(target.parent.glob(f"{safe_io.TEMP_PREFIX}*"))
    # Creating refuses what exists, and leaves nothing behind either.
    with pytest.raises(FileExistsError):
        safe_io.write_text(root, target, "again", exist_ok=False)
    assert target.read_text() == "before"
    assert not list(target.parent.glob(f"{safe_io.TEMP_PREFIX}*"))
    safe_io.write_text(root, target, "after")
    assert target.read_text() == "after"
    # The in-progress name never enters history.
    git.ensure_repo(root)
    (target.parent / f"{safe_io.TEMP_PREFIX}abc").write_text("x")
    commit = git.commit_all(root, "user", "x")
    assert commit is not None
    assert all(safe_io.TEMP_PREFIX not in path for path in git.log(root)[0].files)


async def test_figure_crops_are_copied_file_by_file_and_never_through_a_link(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        crops = tmp_path / "figures" / str(document.job_id)
        crops.mkdir(parents=True)
        (crops / "p1_0_0_10_10.png").write_bytes(b"\x89PNG crop")
        paths = await documents.materialize_document(db, settings, document)
        assert (paths.figures / "p1_0_0_10_10.png").read_bytes() == b"\x89PNG crop"

        # A second document of the same parse whose `figures` the shell made a link: the
        # directory is refused as a whole, and nothing is written through the link.
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        other = Document(
            user_id=document.user_id, folder_id=None, job_id=document.job_id, name="twin.pdf"
        )
        db.add(other)
        await db.commit()
        twin_dir = user_root(settings.workspaces_dir, document.user_id) / "files" / "twin.pdf"
        twin_dir.mkdir(parents=True)
        (twin_dir / "figures").symlink_to(elsewhere)
        with pytest.raises(ApiError) as refused:
            await documents.materialize_document(db, settings, other)
        assert refused.value.status_code == 409
        assert not list(elsewhere.iterdir())


async def test_the_history_follows_a_rename(sessionmaker, tmp_path) -> None:
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
        history = await documents.revisions(db, settings, document)
        # The move is the newest; the edit made under the old name is still listed.
        assert history[0].commit != edit.commit
        assert edit.commit in {revision.commit for revision in history}
        assert "doc.md" in "".join(
            next(revision for revision in history if revision.commit == edit.commit).files
        )


def test_missing_parents_are_made_through_descriptors(tmp_path) -> None:
    root = tmp_path / "ws" / "2"
    root.mkdir(parents=True)
    safe_io.write_text(root, root / "files" / "a" / "b" / "doc.md", "deep")
    assert (root / "files" / "a" / "b" / "doc.md").read_text() == "deep"
    # A link where a parent should be: nothing is made or written beyond it.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "files" / "link").symlink_to(elsewhere)
    with pytest.raises(safe_io.UnsafeEntry):
        safe_io.write_text(root, root / "files" / "link" / "sub" / "doc.md", "x")
    with pytest.raises(safe_io.UnsafeEntry):
        safe_io.ensure_dir(root, root / "files" / "link" / "sub")
    with pytest.raises(safe_io.UnsafeEntry):
        safe_io.rename(root, root / "files" / "a", root / "files" / "link" / "moved")
    assert not list(elsewhere.iterdir())
    assert (root / "files" / "a" / "b" / "doc.md").exists()


async def test_a_revert_never_writes_over_what_sits_at_the_old_place(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        await documents.move_path(settings, user.id, before.relative, after.relative)
        rename = (await documents.revisions(db, settings, document))[0]
        # The shell wrote a new file where the document's used to be.
        before.directory.mkdir()
        before.doc.write_text("newer")
        with pytest.raises(ApiError) as refused:
            await documents.revert(db, settings, document, rename.commit)
        assert refused.value.status_code == 409
        assert before.doc.read_text() == "newer"
        assert after.doc.exists()


async def test_missing_crops_are_copied_on_a_later_materialization(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        crops = tmp_path / "figures" / str(document.job_id)
        crops.mkdir(parents=True)
        (crops / "p1_0_0_10_10.png").write_bytes(b"one")
        (crops / "p2_0_0_10_10.png").write_bytes(b"two")
        paths = await documents.materialize_document(db, settings, document)
        # A copy that failed halfway left the directory with one crop missing.
        (paths.figures / "p2_0_0_10_10.png").unlink()
        await documents.materialize_document(db, settings, document)
        assert (paths.figures / "p2_0_0_10_10.png").read_bytes() == b"two"


async def test_reconcile_records_no_folder_under_a_documents_name(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        other = Document(
            user_id=user.id, folder_id=document.folder_id, job_id=document.job_id, name="twin.pdf"
        )
        db.add(other)
        await db.commit()
        twin = await documents.materialize_document(db, settings, other)
        # The shell nested the twin inside the first document's directory.
        shutil.move(str(twin.directory), str(paths.directory / "twin.pdf"))
        git.commit_all(paths.root, "agent:1", "mv from the shell")
        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        names = set(
            (await db.execute(select(Folder.name).where(Folder.user_id == user.id))).scalars()
        )
        assert "paper.pdf" not in names
        row = (await db.execute(select(Document).where(Document.id == other.id))).scalar_one()
        assert row.folder_id == document.folder_id


async def test_a_revert_of_a_rename_that_also_edited_restores_the_bytes(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    # Long enough that git still sees the edited file as the renamed one (R, not D + A).
    original = "# Title\n\n" + "".join(f"Paragraph {index}.\n\n" for index in range(30))
    async with sessionmaker() as db:
        user, document = await _seed(db, markdown=original)
        before = await documents.materialize_document(db, settings, document)
        # One shell revision: the directory moved and the document edited.
        after_dir = before.directory.with_name("renamed.pdf")
        before.directory.rename(after_dir)
        (after_dir / "doc.md").write_text(original + "And a line the shell added\n")
        git.commit_all(before.root, "agent:1", "mv and edit from the shell")
        await reconcile.reconcile_user(db, settings, user.id)
        shell = (await documents.revisions(db, settings, document))[0]
        assert any(status == "R" for status, _, _ in git.changed_paths(before.root, shell.commit))
        await documents.revert(db, settings, document, shell.commit)
        # Back at the old path, with the bytes from before the commit.
        assert before.doc.read_text() == original
        assert not after_dir.exists()


def test_a_staging_failure_is_an_error_not_a_silent_save(tmp_path) -> None:
    root = tmp_path / "ws" / "3"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    safe_io.write_text(root, root / "files" / "doc.md", "one")
    assert git.commit_paths(root, "user", "first", [Path("files/doc.md")]) is not None
    # A pathspec that matches nothing is skipped...
    assert git.commit_paths(root, "user", "none", [Path("files/gone")]) is None
    # ...a stale index lock is not.
    (git_dir(root) / "index.lock").write_text("")
    safe_io.write_text(root, root / "files" / "doc.md", "two")
    with pytest.raises(git.GitError):
        git.commit_paths(root, "user", "second", [Path("files/doc.md")])


async def test_reconcile_never_walks_through_a_linked_files_directory(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        # The shell swapped files/ for a link to another tree holding a document of its
        # own: the walk sees an empty tree, reads nothing there, records nothing.
        elsewhere = tmp_path / "elsewhere"
        shutil.copytree(paths.root / "files", elsewhere)
        shutil.rmtree(paths.root / "files")
        (paths.root / "files").symlink_to(elsewhere)
        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "paper.pdf"


def test_tree_sizes_never_follow_a_link(tmp_path) -> None:
    root = tmp_path / "ws" / "4"
    (root / "files").mkdir(parents=True)
    (root / "files" / "doc.md").write_bytes(b"x" * 100)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "big.bin").write_bytes(b"y" * 100_000)
    (root / "files" / "link").symlink_to(elsewhere)
    (root / "link-dir").symlink_to(elsewhere)
    assert documents.directory_size(root, root / "files") < 1_000
    assert documents.directory_size(root) < 1_000
    # The linked directory itself is nothing to a walk, not a tree.
    assert documents.directory_size(root, root / "link-dir") == 0


def test_the_revision_log_is_bounded(tmp_path) -> None:
    root = tmp_path / "ws" / "5"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    for index in range(git.LOG_FILES_MAX + 50):
        safe_io.write_text(root, root / "files" / f"f{index:04d}.txt", "x")
    git.commit_all(root, "agent:1", "a flood of files")
    (revision,) = git.log(root)
    # The first LOG_FILES_MAX paths and a count of the rest, never the whole list.
    assert len(revision.files) == git.LOG_FILES_MAX + 1
    assert revision.files[-1] == "... and 50 more"


def test_a_revert_past_the_total_bound_is_refused_before_anything_moves(tmp_path) -> None:
    root = tmp_path / "ws" / "6"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    for index in range(4):
        safe_io.write_text(root, root / "files" / "d" / f"f{index}.txt", "x" * 1000)
    safe_io.write_text(root, root / "files" / "d" / ".meta.json", "{}")
    git.commit_all(root, "user", "four files")
    for index in range(4):
        safe_io.write_text(root, root / "files" / "d" / f"f{index}.txt", "y" * 1000)
    (shell,) = [git.commit_all(root, "agent:1", "edit all four")]
    paths = documents.DocumentPaths(root=root, relative=Path("files/d"))
    with pytest.raises(ApiError) as refused:
        documents._revert(paths, shell, max_bytes=10_000, total_max_bytes=2_500)
    assert refused.value.status_code == 409
    assert (root / "files" / "d" / "f0.txt").read_text() == "y" * 1000
    # Within the bound, the same revert goes through.
    documents._revert(paths, shell, max_bytes=10_000, total_max_bytes=10_000)
    assert (root / "files" / "d" / "f0.txt").read_text() == "x" * 1000


def test_a_revision_whose_list_reaches_the_bound_is_still_listed(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws" / "7"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    for index in range(60):
        safe_io.write_text(root, root / "files" / f"a-rather-long-file-name-{index:04d}.txt", "x")
    git.commit_all(root, "agent:1", "many names")
    monkeypatch.setattr(git, "LOG_MAX_BYTES", 600)
    (revision,) = git.log(root)
    assert revision.message == "many names"
    assert revision.files[-1] == "... (list cut)"
    assert 0 < len(revision.files) < 60


def test_tree_sizes_survive_a_very_deep_tree(tmp_path) -> None:
    # Deeper than the interpreter's recursion limit and longer than PATH_MAX: made the
    # way a shell can make it, one descriptor-relative step at a time.
    root = tmp_path / "ws" / "8"
    (root / "files").mkdir(parents=True)
    fd = os.open(root / "files", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(1500):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open("leaf.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
        os.write(leaf, b"z" * 10)
        os.close(leaf)
    finally:
        os.close(fd)
    assert documents.directory_size(root) == 10
    assert safe_io.remove_tree(root, root / "files")
    assert not (root / "files").exists()


def test_workspace_writes_wait_for_the_backup_lock(tmp_path) -> None:
    import fcntl
    import threading

    from sightread.workspace import locks

    root = tmp_path / "ws" / "9"
    git.ensure_repo(root)
    backup = locks._backup_lock_path(root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    holder = backup.open("a+")
    fcntl.flock(holder, fcntl.LOCK_EX)  # the backup pass is running
    entered = threading.Event()

    def writer() -> None:
        handle, shared = locks._acquire(locks._lock_path(root, locks.WORKSPACE_SUFFIX), backup)
        entered.set()
        fcntl.flock(handle, fcntl.LOCK_UN)
        fcntl.flock(shared, fcntl.LOCK_UN)

    thread = threading.Thread(target=writer)
    thread.start()
    assert not entered.wait(0.3)  # held out while the backup captures rows and tree
    fcntl.flock(holder, fcntl.LOCK_UN)
    assert entered.wait(5)
    thread.join()


def test_tree_sizes_survive_a_wide_tree_under_a_descriptor_limit(tmp_path) -> None:
    import resource

    root = tmp_path / "ws" / "10"
    (root / "files").mkdir(parents=True)
    for index in range(200):
        (root / "files" / f"d{index:03d}").mkdir()
        (root / "files" / f"d{index:03d}" / "f").write_bytes(b"x")
    # A limit a walk that opens every sibling at once cannot fit in, but one that holds
    # a descriptor per level can: just above what the process has open now.
    highest = max(int(name) for name in os.listdir("/proc/self/fd"))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(highest + 12, hard), hard))
    try:
        assert documents.directory_size(root) == 200
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_the_backup_hold_waits_for_the_backup_lock(tmp_path) -> None:
    import fcntl
    import threading

    from sightread.workspace import locks

    root = tmp_path / "ws" / "11"
    git.ensure_repo(root)
    backup = locks._backup_lock_path(root)
    backup.parent.mkdir(parents=True, exist_ok=True)
    holder = backup.open("a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    entered = threading.Event()

    def writer() -> None:
        shared = locks._acquire_shared(backup)
        # The workspace lock inside the hold takes the backup lock shared again.
        handle, inner = locks._acquire(locks._lock_path(root, locks.WORKSPACE_SUFFIX), backup)
        entered.set()
        for held in (handle, inner, shared):
            fcntl.flock(held, fcntl.LOCK_UN)

    thread = threading.Thread(target=writer)
    thread.start()
    assert not entered.wait(0.3)
    fcntl.flock(holder, fcntl.LOCK_UN)
    assert entered.wait(5)
    thread.join()
