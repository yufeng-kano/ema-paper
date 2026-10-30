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
from sightread.jobs import sweeper
from sightread.workspace import documents, git, reconcile, safe_io
from sightread.workspace.layout import (
    git_dir,
    images_dir,
    inside,
    is_canonical_segment,
    path_segment,
    segment_name,
    sources_dir,
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
    # The names history excludes wherever they appear: a document so named would be a
    # directory git ignores whole.
    assert path_segment("figures") == "_figures"
    assert path_segment("source.pdf") == "_source.pdf"
    assert path_segment("source.PDF") == "_source.PDF"
    assert path_segment("sources") == "sources" and path_segment("source") == "source"
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
        # The one 409 a client answers by reloading, apart from every other.
        assert refused.value.error_type == "revision_conflict"
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
        assert changed == 3  # two folder rows made for the shell's directories, one move
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
    kept = documents.keep_source(root, Path("files/paper.pdf"), upload, document_id=31)
    # The original outside the mount, named by the document; the shell's copy in the tree.
    assert kept == tmp_path / "ws" / ".repos" / "7.sources" / "31.pdf"
    assert kept.read_bytes() == b"%PDF-1.4"
    assert (root / "files" / "paper.pdf" / "source.pdf").read_bytes() == b"%PDF-1.4"
    assert documents.original_of(root, 31) == kept
    assert documents.original_of(root, 32) is None
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
        original = documents.original_of(paths.root, document.id)
        assert original is not None and original.read_bytes() == b"%PDF-1.4"
        assert not upload.exists()
        assert job.source_deleted_at is not None
        # The shell's copy removed: the next materialization makes it again from the
        # original, which no command can reach.
        (paths.directory / "source.pdf").unlink()
        await documents.materialize_document(db, settings, document)
        assert (paths.directory / "source.pdf").read_bytes() == b"%PDF-1.4"


async def test_a_tree_a_dead_workers_command_may_write_takes_no_host_write(
    sessionmaker, tmp_path
) -> None:
    from sightread.workspace.locks import clear_writing, mark_writing, workspace_lock

    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)
        # A shell command was writing when its worker died: the marker it set stands.
        mark_writing(paths.root, 1, "s")
        with pytest.raises(ApiError) as refused:
            await documents.write_content(
                db, settings, document, markdown="# T\n\nx\n", base_revision=content.revision
            )
        assert refused.value.status_code == 409
        assert "recovering" in refused.value.message
        with pytest.raises(ApiError):
            await documents.read_content(db, settings, document)
        # The recovery itself goes through, and once it has cleared the marker so does
        # everything else.
        async with workspace_lock(paths.root, recovering=True):
            clear_writing(paths.root)
        await documents.write_content(
            db, settings, document, markdown="# T\n\nx\n", base_revision=content.revision
        )


async def test_a_document_named_like_an_excluded_artifact_keeps_its_revisions(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        document.name = "source.pdf"
        await db.commit()
        paths = await documents.materialize_document(db, settings, document)
        assert paths.directory.name == "_source.pdf"
        content = await documents.read_content(db, settings, document)
        revision = await documents.write_content(
            db, settings, document, markdown="# Title\n\nEdited\n", base_revision=content.revision
        )
        assert revision == documents.revision_of("# Title\n\nEdited\n")
        # Both the materialization and the save are revisions: nothing git ignored.
        head, materialized = git.log(paths.root)[:2]
        assert head.files == ("files/Papers/_source.pdf/doc.md",)
        assert "files/Papers/_source.pdf/doc.md" in materialized.files


def test_path_scoped_commits_name_their_paths_literally(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    (root / "files" / "a*").mkdir(parents=True)
    (root / "files" / "abc").mkdir(parents=True)
    git.ensure_repo(root)
    (root / "files" / "a*" / "doc.md").write_text("star\n")
    (root / "files" / "abc" / "doc.md").write_text("other\n")
    # A name with a glob character names exactly its own directory, never a sibling's.
    commit = git.commit_paths(root, "user", "save a*", [Path("files/a*")])
    assert commit is not None
    assert git.log(root)[0].files == ("files/a*/doc.md",)
    assert git.commit_paths(root, "user", "the rest", [Path("files/abc")]) is not None
    assert [entry.message for entry in git.log(root, Path("files/a*"))] == ["save a*"]
    # A pathspec that matches nothing is still skipped, not an error.
    assert git.commit_paths(root, "user", "nothing", [Path("files/zzz")]) is None


async def test_a_folder_set_aside_by_a_crashed_delete_comes_back_whole(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        # What the shell left in the folder beside the document: no row names it.
        (root / "files" / "Papers" / "scratch.txt").write_text("notes\n")
        (root / "files" / "Papers" / "drafts").mkdir()
        git.commit_all(root, "agent:1", "bash: scratch")
        # The folder's delete set the directory aside and recorded the removal, then the
        # process died before the rows went.
        removal = await documents.remove_path(settings, user.id, Path("files/Papers"))
        assert removal is not None and not (root / "files" / "Papers").exists()
        assert await reconcile.reconcile_user(db, settings, user.id) >= 1
        # Back whole: the document, and the shell's files with it.
        assert paths.doc.read_text() == "# Title\n\nBody\n"
        assert (root / "files" / "Papers" / "scratch.txt").read_text() == "notes\n"
        assert (root / "files" / "Papers" / "drafts").is_dir()
        assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]
        assert git.log(root)[0].message == "restore files/Papers"


async def test_an_aside_holding_a_deleted_document_does_not_come_back_whole(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        other = Document(
            user_id=user.id, folder_id=document.folder_id, job_id=document.job_id, name="other.pdf"
        )
        db.add(other)
        await db.commit()
        paths = await documents.materialize_document(db, settings, document)
        other_paths = await documents.materialize_document(db, settings, other)
        root = paths.root
        (root / "files" / "Papers" / "scratch.txt").write_text("notes\n")
        git.commit_all(root, "agent:1", "bash: scratch")
        removal = await documents.remove_path(settings, user.id, Path("files/Papers"))
        assert removal is not None
        # One of the folder's documents was deleted for good before the recovery ran:
        # the folder does not come back whole around it.
        await db.delete(other)
        await db.commit()
        assert await reconcile.reconcile_user(db, settings, user.id) >= 1
        assert paths.doc.read_text() == "# Title\n\nBody\n"
        assert not other_paths.directory.exists()
        assert not (root / "files" / "Papers" / "scratch.txt").exists()
        assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]


async def test_a_failed_library_parse_keeps_its_original(sessionmaker, tmp_path) -> None:
    from sightread.jobs import runner

    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        job = (await db.execute(select(Job).where(Job.id == document.job_id))).scalar_one()
        upload = tmp_path / "upload.pdf"
        upload.write_bytes(b"%PDF-1.4")
        job.source_path = str(upload)
        job.status = "running"
        await db.commit()
        document_id, job_id = document.id, job.id
    root = user_root(settings.workspaces_dir, user.id)
    # The parse fails: the row keeps the upload as its original all the same, so the
    # document is not lost with the error (docs/jobs.md § Retention).
    await runner._finish(
        sessionmaker, job_id, status="failed", error="the document is unreadable", settings=settings
    )
    original = documents.original_of(root, document_id)
    assert original is not None and original.read_bytes() == b"%PDF-1.4"
    assert not upload.exists()
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        assert job.status == "failed" and job.source_deleted_at is not None


async def test_retention_copies_for_its_recipients_when_a_late_row_took_the_upload(
    sessionmaker, tmp_path
) -> None:
    from sightread.jobs import runner

    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, first = await _seed(db)
        job = (await db.execute(select(Job).where(Job.id == first.job_id))).scalar_one()
        upload = tmp_path / "upload.pdf"
        upload.write_bytes(b"%PDF-1.4")
        job.source_path = str(upload)
        job.status = "running"
        late = Document(user_id=user.id, folder_id=first.folder_id, job_id=job.id, name="again.pdf")
        db.add(late)
        await db.commit()
        # Filed again in the moment between the worker's terminal commit and its
        # retention: no original to copy yet, so the row takes the upload outright.
        await documents.adopt_source(db, settings, late, job, upload=upload)
        root = user_root(settings.workspaces_dir, user.id)
        assert not upload.exists()
        assert documents.original_of(root, late.id) is not None
        first_id, late_id, job_id = first.id, late.id, job.id

    # The worker's retention finds the upload gone, and still keeps an original for the
    # recipient it found at the terminal commit: a copy of the late row's.
    await runner._finish(sessionmaker, job_id, status="succeeded", settings=settings)
    original = documents.original_of(root, first_id)
    assert original is not None and original.read_bytes() == b"%PDF-1.4"
    assert documents.original_of(root, late_id).read_bytes() == b"%PDF-1.4"
    assert (root / "files" / "Papers" / "paper.pdf" / "source.pdf").read_bytes() == b"%PDF-1.4"
    async with sessionmaker() as db:
        job = await db.get(Job, job_id)
        assert job.status == "succeeded" and job.source_deleted_at is not None


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
    kept = documents.keep_source(root, Path("files/paper"), upload, document_id=1, kind="pdf")
    assert kept.name == "1.pdf"
    assert (root / "files" / "paper" / "source.pdf").exists()
    image = tmp_path / "photo.jpeg"
    image.write_bytes(b"\xff\xd8")
    kept = documents.keep_source(root, Path("files/photo"), image, document_id=2, kind="image")
    assert kept.name == "2.jpeg"
    assert (root / "files" / "photo" / "source.jpeg").exists()


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
    assert safe_io.tree_size(root, root / "files") < 1_000
    assert documents.storage_usage(root, tmp_path / "figures", {}).total < 1_000
    # The linked directory itself is nothing to a walk, not a tree.
    assert safe_io.tree_size(root, root / "link-dir") == 0


def test_the_revision_log_is_bounded(tmp_path) -> None:
    root = tmp_path / "ws" / "5"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    for index in range(git._log_files_max() + 50):
        safe_io.write_text(root, root / "files" / f"f{index:04d}.txt", "x")
    git.commit_all(root, "agent:1", "a flood of files")
    (revision,) = git.log(root)
    # The first LOG_FILES_MAX paths and a count of the rest, never the whole list.
    assert len(revision.files) == git._log_files_max() + 1
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
        documents._revert(paths, shell, max_bytes=10_000, total_max_bytes=2_500, max_paths=100)
    assert refused.value.status_code == 409
    assert (root / "files" / "d" / "f0.txt").read_text() == "y" * 1000
    # Within the bound, the same revert goes through.
    documents._revert(paths, shell, max_bytes=10_000, total_max_bytes=10_000, max_paths=100)
    assert (root / "files" / "d" / "f0.txt").read_text() == "x" * 1000


def test_a_revision_whose_list_reaches_the_bound_is_still_listed(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws" / "7"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    for index in range(60):
        safe_io.write_text(root, root / "files" / f"a-rather-long-file-name-{index:04d}.txt", "x")
    git.commit_all(root, "agent:1", "many names")
    monkeypatch.setattr(git, "_log_max_bytes", lambda: 600)
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
    # Past the walk's depth bound, a size is refused rather than undercounted
    # (docs/workspace.md § Quota)...
    with pytest.raises(safe_io.TreeTooDeep):
        documents.storage_usage(root, tmp_path / "figures", {})
    # ...and a removal goes through with a bounded set of descriptors however deep.
    import resource

    highest = max(int(name) for name in os.listdir("/proc/self/fd"))
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(highest + 100, hard), hard))
    try:
        assert safe_io.remove_tree(root, root / "files")
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    assert not (root / "files").exists()


def test_a_tree_within_the_depth_bound_is_measured(tmp_path) -> None:
    root = tmp_path / "ws" / "8b"
    (root / "files").mkdir(parents=True)
    fd = os.open(root / "files", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(safe_io.DEPTH_MAX - 10):
            os.mkdir("d", dir_fd=fd)
            child = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open("leaf.txt", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd)
        os.write(leaf, b"z" * 10)
        os.close(leaf)
    finally:
        os.close(fd)
    assert documents.storage_usage(root, tmp_path / "figures", {}).files == 10


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
        assert documents.storage_usage(root, tmp_path / "figures", {}).files == 200
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


def test_a_revert_over_too_many_paths_or_too_much_metadata_is_refused(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "ws" / "12"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    safe_io.write_text(root, root / "files" / "d" / ".meta.json", "{}")
    git.commit_all(root, "user", "the document")
    for index in range(20):
        safe_io.write_text(root, root / "files" / "d" / f"f{index:02d}.txt", "x")
    shell = git.commit_all(root, "agent:1", "twenty files")
    paths = documents.DocumentPaths(root=root, relative=Path("files/d"))
    with pytest.raises(ApiError) as refused:
        documents._revert(paths, shell, max_bytes=1_000, total_max_bytes=10_000, max_paths=10)
    assert refused.value.status_code == 409
    assert (root / "files" / "d" / "f00.txt").exists()
    # The path list alone past the log bound is refused too, before anything is read.
    monkeypatch.setattr(git, "_log_max_bytes", lambda: 200)
    with pytest.raises(ApiError) as cut:
        documents._revert(paths, shell, max_bytes=1_000, total_max_bytes=10_000, max_paths=100)
    assert cut.value.status_code == 409
    monkeypatch.setattr(git, "_log_max_bytes", lambda: 4 * 1024 * 1024)
    documents._revert(paths, shell, max_bytes=1_000, total_max_bytes=10_000, max_paths=100)
    assert not (root / "files" / "d" / "f00.txt").exists()


def test_reaches_beyond_tells_a_documents_own_revision_from_a_shells(tmp_path) -> None:
    root = tmp_path / "ws" / "13"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    safe_io.write_text(root, root / "files" / "a" / "doc.md", "a")
    safe_io.write_text(root, root / "files" / "b" / "doc.md", "b")
    git.commit_all(root, "user", "two documents")
    safe_io.write_text(root, root / "files" / "a" / "doc.md", "a2")
    own = git.commit_all(root, "user", "edit a")
    safe_io.write_text(root, root / "files" / "a" / "doc.md", "a3")
    safe_io.write_text(root, root / "files" / "b" / "doc.md", "b3")
    both = git.commit_all(root, "agent:1", "bash: edit both")
    paths = documents.DocumentPaths(root=root, relative=Path("files/a"))
    assert documents.reaches_beyond(paths, own) is False
    assert documents.reaches_beyond(paths, both) is True
    # The document moved since: its own older revision is still its own.
    git.move(root, root / "files" / "a", root / "files" / "moved")
    git.commit_paths(root, "user", "rename a", [Path("files/a"), Path("files/moved")])
    moved = documents.DocumentPaths(root=root, relative=Path("files/moved"))
    assert documents.reaches_beyond(moved, own) is False
    assert documents.reaches_beyond(moved, "0" * 40) is False


async def test_a_removal_whose_rows_stay_is_put_back(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
    root = paths.root
    paths.doc.write_text("# Title\n\nEdited\n")
    git.commit_paths(root, git.USER_AUTHOR, "save", [paths.doc_relative])
    removal = await documents.remove_path(settings, user.id, paths.relative)
    assert removal is not None and not paths.directory.exists()
    await documents.restore_removed(settings, user.id, paths.relative, removal)
    assert paths.doc.read_text() == "# Title\n\nEdited\n"
    (newest,) = git.log(root, paths.relative, 1)
    assert newest.message == f"restore {paths.relative}"


def test_the_name_migration_brings_legacy_names_into_one_namespace() -> None:
    import importlib.util

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, insert, text

    from sightread.db.models import Base

    spec = importlib.util.spec_from_file_location(
        "normalize_library_names",
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "c8f28f7cd45e_normalize_library_names.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    engine = create_engine("sqlite://")
    # The two tables the migration reads; SQLite enforces no foreign key here, and the
    # core inserts fill the timestamps the models default.
    Base.metadata.create_all(engine, tables=[Folder.__table__, Document.__table__])
    with engine.begin() as connection:
        connection.execute(
            insert(Folder.__table__).values(id=1, user_id=1, parent_id=None, name="Report")
        )
        for row_id, name in ((1, "Report"), (2, "e\u0301.pdf"), (3, "\u00e9.pdf"), (4, "a/b")):
            connection.execute(
                insert(Document.__table__).values(
                    id=row_id, user_id=1, folder_id=None, job_id=uuid.uuid4(), name=name
                )
            )
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        names = connection.execute(text("SELECT id, name FROM documents ORDER BY id")).all()
    # The folder keeps `Report`; the document that shared it is suffixed; the decomposed
    # spelling becomes the composed one, and the second of the two is suffixed; a
    # separator is cleaned as a fresh name would be.
    assert names == [(1, "Report (2)"), (2, "\u00e9.pdf"), (3, "\u00e9 (2).pdf"), (4, "a b")]


async def test_a_reverted_rename_takes_the_untracked_assets_along(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        # The original and a crop: in the directory, outside history.
        (before.directory / "source.pdf").write_bytes(b"%PDF-1.4 original")
        (before.directory / "figures").mkdir()
        (before.directory / "figures" / "p1_0_0_10_10.png").write_bytes(b"png")
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        await documents.move_path(settings, user.id, before.relative, after.relative)
        assert (after.directory / "source.pdf").exists()
        rename = git.log(after.root)[0]

        await documents.revert(db, settings, document, rename.commit)
        assert (before.directory / "source.pdf").read_bytes() == b"%PDF-1.4 original"
        assert (before.directory / "figures" / "p1_0_0_10_10.png").exists()
        assert not after.directory.exists()


def test_a_git_that_stalls_is_ended_at_the_deadline(tmp_path, monkeypatch) -> None:
    import stat as stat_module

    root = tmp_path / "ws" / "14"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    # A git that answers a few bytes and then hangs, instead of the real one.
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nprintf 'abc'\nsleep 30\n")
    fake.chmod(fake.stat().st_mode | stat_module.S_IEXEC)
    monkeypatch.setattr(git, "GIT", str(fake))
    monkeypatch.setattr(git, "_timeout", lambda: 0.3)
    with pytest.raises(git.GitError) as timed_out:
        git._git_bounded(root, "log", max_bytes=1000)
    assert "timed out" in str(timed_out.value)


async def test_a_revert_whose_revision_cannot_be_recorded_is_rolled_back(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        _, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        paths.doc.write_text("# Title\n\nShelled\n")
        (paths.directory / "helper.py").write_text("print(1)\n")
        shell = git.commit_all(root, "agent:1", "bash: python fix.py")
        assert shell is not None
        real = git.commit_paths

        def refuse(root_, author, message, paths_):
            if message.startswith("revert "):
                raise git.GitError("disk full")
            return real(root_, author, message, paths_)

        # Edits a failed shell commit left unrecorded — elsewhere in the tree, and in the
        # very file the revert rewrites: not this revert's to discard when it rolls back.
        paths.notes.write_text("unrecorded notes\n")
        (paths.directory / "scratch.txt").write_text("untracked\n")
        paths.doc.write_text("# Title\n\nShelled, then edited\n")

        monkeypatch.setattr(git, "commit_paths", refuse)
        with pytest.raises(ApiError) as refused:
            await documents.revert(db, settings, document, shell)
        assert refused.value.status_code == 409
        # The tree as it was before the revert began, the head unchanged: the paths the
        # revert touched hold what they held, the unrecorded edits still there.
        assert paths.doc.read_text() == "# Title\n\nShelled, then edited\n"
        assert (paths.directory / "helper.py").read_text() == "print(1)\n"
        assert paths.notes.read_text() == "unrecorded notes\n"
        assert (paths.directory / "scratch.txt").read_text() == "untracked\n"
        dirty = git._git(root, "status", "--porcelain", check=False).stdout.strip().splitlines()
        assert sorted(line.split()[-1] for line in dirty) == [
            "files/Papers/paper.pdf/doc.md",
            "files/Papers/paper.pdf/notes.md",
            "files/Papers/paper.pdf/scratch.txt",
        ]
        assert git.log(root)[0].commit == shell
        paths.notes.write_text("")
        paths.doc.write_text("# Title\n\nShelled\n")
        (paths.directory / "scratch.txt").unlink()
        monkeypatch.setattr(git, "commit_paths", real)
        await documents.revert(db, settings, document, shell)
        assert paths.doc.read_text() == "# Title\n\nBody\n"


def test_reconciliation_sweeps_what_a_delete_left_aside(tmp_path) -> None:
    root = tmp_path / "ws" / "15"
    (root / "files").mkdir(parents=True)
    git.ensure_repo(root)
    aside = root / f"{safe_io.TEMP_PREFIX}abc"
    (aside / "deep").mkdir(parents=True)
    (aside / "deep" / "source.pdf").write_bytes(b"x")
    assert documents.sweep_aside(root) == 1
    assert not aside.exists()
    assert documents.sweep_aside(root) == 0


def test_a_failed_path_commit_leaves_the_index_clean(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws" / "16"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    safe_io.write_text(root, root / "files" / "a.txt", "a")

    def refuse(*_args, **_kwargs):
        raise git.GitError("disk full")

    monkeypatch.setattr(git, "_commit_staged", refuse)
    with pytest.raises(git.GitError):
        git.commit_paths(root, "user", "save", [Path("files/a.txt")])
    with pytest.raises(git.GitError):
        git.commit_all(root, "agent:1", "bash: x")
    assert git._git(root, "diff", "--cached", "--quiet", check=False).returncode == 0


def test_a_removal_whose_revision_cannot_be_recorded_moves_back(tmp_path, monkeypatch) -> None:
    root = tmp_path / "ws" / "17"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    safe_io.write_text(root, root / "files" / "d" / "doc.md", "x")
    (root / "files" / "d" / "source.pdf").write_bytes(b"%PDF")
    git.commit_all(root, "user", "seed")
    real = git.commit_paths

    def refuse(root_, author, message, paths_):
        if message.startswith("delete "):
            raise git.GitError("disk full")
        return real(root_, author, message, paths_)

    monkeypatch.setattr(git, "commit_paths", refuse)
    with pytest.raises(ApiError) as refused:
        documents._remove_tree(root, Path("files/d"), "delete files/d")
    assert refused.value.status_code == 409
    assert (root / "files" / "d" / "source.pdf").read_bytes() == b"%PDF"
    assert not [
        name for name, _ in safe_io.list_dir(root, root) if name.startswith(safe_io.TEMP_PREFIX)
    ]


async def test_a_move_whose_commit_fails_is_undone(sessionmaker, tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        root = before.root
        (before.doc).write_text("# Edited by hand\n")
        target = before.relative.with_name("renamed.pdf")
        # A squatter at the destination: it moves aside, and must come back too.
        (root / target).mkdir()
        (root / target / "scratch.txt").write_text("the shell's")

        def failing(*args, **kwargs):
            raise git.GitError("git is unwell")

        monkeypatch.setattr(git, "commit_paths", failing)
        with pytest.raises(ApiError) as refused:
            await documents.move_path(settings, user.id, before.relative, target)
        assert refused.value.status_code == 409
        assert "could not be recorded" in refused.value.message
    # The document where it was, with its edit; the squatter where it was; nothing aside.
    assert before.doc.read_text() == "# Edited by hand\n"
    assert (root / target / "scratch.txt").read_text() == "the shell's"
    assert sorted(name for name, _ in safe_io.list_dir(root, root / "files" / "Papers")) == [
        "paper.pdf",
        "renamed.pdf",
    ]


async def test_reconcile_leaves_what_a_command_made_inside_a_document(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        # Names the library would encode (`_.venv`, `__assets`), inside the document: the
        # command's own, left under the names it chose, never recorded as folders.
        (paths.directory / ".venv" / "bin").mkdir(parents=True)
        (paths.directory / "_assets").mkdir()
        (paths.directory / "_assets" / "a.txt").write_text("a")
        git.commit_all(paths.root, "agent:1", "from the shell")
        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        assert (paths.directory / ".venv" / "bin").is_dir()
        assert (paths.directory / "_assets" / "a.txt").read_text() == "a"
        assert not (paths.directory / "_.venv").exists()
        names = set(
            (await db.execute(select(Folder.name).where(Folder.user_id == user.id))).scalars()
        )
        assert names == {"Papers"}
        # A folder the shell named outside any document is still canonicalized.
        (paths.root / "files" / ".drafts").mkdir()
        git.commit_all(paths.root, "agent:1", "mkdir from the shell")
        await reconcile.reconcile_user(db, settings, user.id)
        assert (paths.root / "files" / "_.drafts").is_dir()


async def test_reconcile_follows_two_documents_that_traded_places(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, first = await _seed(db, markdown="# First\n")
        second = Document(
            user_id=user.id, folder_id=first.folder_id, job_id=first.job_id, name="second.pdf"
        )
        db.add(second)
        await db.commit()
        a = await documents.materialize_document(db, settings, first)
        b = await documents.materialize_document(db, settings, second)
        (b.doc).write_text("# Second\n")
        # One command: the two directories swapped.
        scratch = a.directory.with_name("swap-tmp")
        a.directory.rename(scratch)
        b.directory.rename(a.directory)
        scratch.rename(b.directory)
        git.commit_all(a.root, "agent:1", "swap from the shell")

        assert await reconcile.reconcile_user(db, settings, user.id) == 2
        rows = {
            row.id: row
            for row in (await db.execute(select(Document).where(Document.user_id == user.id)))
            .scalars()
            .all()
        }
        assert rows[first.id].name == "second.pdf"
        assert rows[second.id].name == "paper.pdf"
        # Each row opens its own content where it now lives.
        assert (await documents.read_content(db, settings, rows[first.id])).markdown == "# First\n"
        assert (
            await documents.read_content(db, settings, rows[second.id])
        ).markdown == "# Second\n"


async def test_reconcile_refuses_a_document_that_took_a_folders_name(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        reports = Folder(user_id=user.id, parent_id=None, name="Reports")
        db.add(reports)
        await db.commit()
        # The shell took the folder's place with the document.
        paths.directory.rename(paths.root / "files" / "Reports")
        git.commit_all(paths.root, "agent:1", "mv from the shell")

        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "paper.pdf" and row.folder_id == document.folder_id
        folder = (await db.execute(select(Folder).where(Folder.id == reports.id))).scalar_one()
        assert folder.name == "Reports"


async def test_a_save_whose_commit_fails_leaves_the_file_as_it_was(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        content = await documents.read_content(db, settings, document)

        def failing(*args, **kwargs):
            raise git.GitError("git is unwell")

        monkeypatch.setattr(git, "commit_paths", failing)
        with pytest.raises(ApiError) as refused:
            await documents.write_content(
                db, settings, document, markdown="# Changed\n", base_revision=content.revision
            )
        assert refused.value.status_code == 409
        assert paths.doc.read_text() == "# Title\n\nBody\n"
        with pytest.raises(ApiError):
            await documents.write_notes(
                db, settings, document, text="a note", base_revision=content.notes_revision
            )
        assert paths.notes.read_text() == ""


async def test_a_materialization_whose_commit_fails_is_written_again(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        real = git.commit_paths

        def failing(*args, **kwargs):
            raise git.GitError("git is unwell")

        monkeypatch.setattr(git, "commit_paths", failing)
        with pytest.raises(ApiError) as refused:
            await documents.materialize_document(db, settings, document)
        assert refused.value.status_code == 409
        paths = await documents.document_paths(db, settings, document)
        # Nothing tracked was left behind for a later commit to absorb.
        assert not paths.doc.exists() and not paths.meta.exists()
        monkeypatch.setattr(git, "commit_paths", real)
        await documents.materialize_document(db, settings, document)
        assert paths.doc.read_text() == "# Title\n\nBody\n"
        assert git.log(paths.root)[0].message.startswith("materialize")


async def test_reconcile_records_an_empty_folder_the_shell_made(sessionmaker, tmp_path) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        (paths.root / "files" / "Research" / "2026").mkdir(parents=True)
        git.commit_all(paths.root, "agent:1", "mkdir from the shell")
        assert await reconcile.reconcile_user(db, settings, user.id) == 2
        rows = {
            row.name: row
            for row in (await db.execute(select(Folder).where(Folder.user_id == user.id)))
            .scalars()
            .all()
        }
        assert set(rows) == {"Papers", "Research", "2026"}
        assert rows["2026"].parent_id == rows["Research"].id
        assert rows["Research"].parent_id is None
        # Idempotent: the rows exist, nothing changes on the next run.
        assert await reconcile.reconcile_user(db, settings, user.id) == 0


async def test_a_canonicalization_whose_commit_fails_is_undone(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        shell_named = paths.directory.with_name(".report")
        paths.directory.rename(shell_named)
        real = git.commit_paths

        def failing(*args, **kwargs):
            raise git.GitError("git is unwell")

        monkeypatch.setattr(git, "commit_paths", failing)
        assert await reconcile.reconcile_user(db, settings, user.id) == 0
        # Where the shell left it, under the name it gave; the row untouched.
        assert shell_named.is_dir() and not shell_named.with_name("_.report").exists()
        row = (await db.execute(select(Document).where(Document.id == document.id))).scalar_one()
        assert row.name == "paper.pdf"
        monkeypatch.setattr(git, "commit_paths", real)
        assert await reconcile.reconcile_user(db, settings, user.id) == 1
        assert shell_named.with_name("_.report").is_dir()


async def test_the_documents_own_rename_revision_does_not_reach_beyond_it(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        moved = await documents.move_path(settings, user.id, before.relative, after.relative)
        assert moved is not None and moved.commit is not None
        # The rename's old side is this document's: reverting it waits for this
        # document's conversations only, not for every turn of the user.
        assert documents.reaches_beyond(after, moved.commit) is False
        # A shell revision that moved another document's file in still reaches beyond.
        other = Document(
            user_id=user.id, folder_id=document.folder_id, job_id=document.job_id, name="other.pdf"
        )
        db.add(other)
        await db.commit()
        other_paths = await documents.materialize_document(db, settings, other)
        (other_paths.directory / "extra.md").write_text("x")
        git.commit_all(before.root, "agent:1", "an extra file")
        (other_paths.directory / "extra.md").rename(after.directory / "extra.md")
        git.commit_all(before.root, "agent:1", "mv from the shell")
        shell = git.log(before.root)[0].commit
        # Its new place is here, but its old side is the other document's, which still
        # stands: not this document's own rename, so the revision reaches beyond.
        assert documents.reaches_beyond(after, shell) is True


async def test_a_delete_the_process_died_in_is_restored_by_reconciliation(
    sessionmaker, tmp_path
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        paths.doc.write_text("# Edited\n")
        git.commit_all(paths.root, "user", "edit")
        # The removal's revision was made and the directory set aside; the process died
        # before the row's delete committed, so the row survives.
        removal = await documents.remove_path(settings, user.id, paths.relative)
        assert removal is not None and not paths.directory.exists()
        assert await reconcile.reconcile_user(db, settings, user.id) == 1
        assert paths.doc.read_text() == "# Edited\n"
        assert git.log(paths.root)[0].message.startswith("restore ")
        assert not [name for name in os.listdir(paths.root) if name.startswith(".sr-tmp-")]
        # An aside whose rows are gone for good is swept, as before.
        other = Document(
            user_id=user.id, folder_id=document.folder_id, job_id=document.job_id, name="gone.pdf"
        )
        db.add(other)
        await db.commit()
        other_paths = await documents.materialize_document(db, settings, other)
        gone = await documents.remove_path(settings, user.id, other_paths.relative)
        assert gone is not None
        await db.delete(other)
        await db.commit()
        await reconcile.reconcile_user(db, settings, user.id)
        assert not [name for name in os.listdir(paths.root) if name.startswith(".sr-tmp-")]
        assert not other_paths.directory.exists()


async def test_a_move_that_fails_after_displacing_a_squatter_puts_it_back(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        root = before.root
        target = before.relative.with_name("renamed.pdf")
        (root / target).mkdir()
        (root / target / "scratch.txt").write_text("the shell's")
        real = git.move
        calls = {"n": 0}

        def flaky(root_, source, dest):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(5, "Input/output error")
            return real(root_, source, dest)

        monkeypatch.setattr(git, "move", flaky)
        with pytest.raises(ApiError) as refused:
            await documents.move_path(settings, user.id, before.relative, target)
        assert refused.value.status_code == 409
    # The squatter back under its name, the document where it was, nothing aside.
    assert (root / target / "scratch.txt").read_text() == "the shell's"
    assert before.doc.is_file()
    assert sorted(name for name, _ in safe_io.list_dir(root, root / "files" / "Papers")) == [
        "paper.pdf",
        "renamed.pdf",
    ]


def test_a_gitignore_from_the_shell_hides_nothing_from_history(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    git.ensure_repo(root)
    directory = root / "files" / "paper.pdf"
    directory.mkdir(parents=True)
    (directory / "doc.md").write_text("# A\n")
    assert git.commit_all(root, "user", "seed") is not None
    # A command writes an ignore file that would hide its own changes, and the files
    # the workspace keeps out of history on its own account.
    (root / ".gitignore").write_text("*.md\nfiles/\n")
    (directory / "notes.md").write_text("not hidden\n")
    (directory / "source.pdf").write_bytes(b"%PDF-1.4")
    (directory / "figures").mkdir()
    (directory / "figures" / "p1.png").write_bytes(b"png")
    (root / f"{safe_io.TEMP_PREFIX}x").mkdir()
    (root / f"{safe_io.TEMP_PREFIX}x" / "half.md").write_text("half\n")
    (root / ".cache").mkdir()
    (root / ".cache" / "pip").write_text("x")
    (root / ".sightread").mkdir()
    (root / ".sightread" / "r.json").write_text("{}")
    assert git.commit_all(root, "agent:1", "bash: hide") is not None
    assert set(git.log(root)[0].files) == {".gitignore", "files/paper.pdf/notes.md"}
    # A host write's path-scoped commit is as blind to the ignore file.
    (directory / "doc.md").write_text("# B\n")
    assert git.commit_paths(root, "user", "save", [Path("files/paper.pdf/doc.md")]) is not None
    assert git.log(root)[0].files == ("files/paper.pdf/doc.md",)
    (directory / "doc.md").write_text("# C\n")
    (directory / "source.pdf").write_bytes(b"%PDF-1.5")
    assert git.commit_paths(root, "user", "save", [Path("files/paper.pdf")]) is not None
    assert git.log(root)[0].files == ("files/paper.pdf/doc.md",)


async def test_an_aside_without_document_metadata_comes_back_to_its_origin(
    sessionmaker, tmp_path
) -> None:
    """A delete the process died in, of a folder holding only what the shell made, or of
    a document whose `.meta.json` a command removed: nothing inside names a row, so the
    origin written beside the aside is what brings it back while a row still names that
    path (docs/workspace.md § Revisions)."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        drafts = Folder(user_id=user.id, parent_id=None, name="Drafts")
        db.add(drafts)
        await db.commit()
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        (root / "files" / "Drafts").mkdir()
        (root / "files" / "Drafts" / "notes.txt").write_text("from bash\n")
        git.commit_all(root, "agent:1", "bash: drafts")
        removal = await documents.remove_path(settings, user.id, Path("files/Drafts"))
        assert removal is not None
        assert documents.aside_origin(root, removal.aside) == Path("files/Drafts")
        assert await reconcile.reconcile_user(db, settings, user.id) >= 1
        assert (root / "files" / "Drafts" / "notes.txt").read_text() == "from bash\n"
        assert git.log(root)[0].message == "restore files/Drafts"
        assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]
        # A document stripped of its metadata by a command comes back the same way.
        paths.meta.unlink()
        paths.doc.write_text("# Edited\n")
        git.commit_all(root, "agent:1", "bash: strip")
        removal = await documents.remove_path(settings, user.id, paths.relative)
        assert removal is not None
        assert await reconcile.reconcile_user(db, settings, user.id) >= 1
        assert paths.doc.read_text() == "# Edited\n"
        assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]
        # Once the rows are gone for good, an aside and its origin are swept together.
        await db.delete(drafts)
        await db.commit()
        removal = await documents.remove_path(settings, user.id, Path("files/Drafts"))
        assert removal is not None
        await reconcile.reconcile_user(db, settings, user.id)
        assert not (root / "files" / "Drafts").exists()
        assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]


def test_a_git_that_times_out_is_a_git_error_and_leaves_no_lock(tmp_path, monkeypatch) -> None:
    """A regular git call past `WORKSPACE_GIT_TIMEOUT_SECONDS` raises the module's own
    error, so every caller's rollback runs, and the lock the killed git left is removed
    rather than refusing every command after it."""
    import stat as stat_module

    root = tmp_path / "ws" / "15"
    root.mkdir(parents=True)
    git.ensure_repo(root)
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(fake.stat().st_mode | stat_module.S_IEXEC)
    (git_dir(root) / "index.lock").write_text("")
    monkeypatch.setattr(git, "GIT", str(fake))
    monkeypatch.setattr(git, "_timeout", lambda: 0.3)
    with pytest.raises(git.GitError) as timed_out:
        git._git(root, "status")
    assert "timed out" in str(timed_out.value)
    assert not (git_dir(root) / "index.lock").exists()


async def test_a_removal_whose_origin_cannot_be_written_is_refused_whole(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    """A full disk at the origin marker must not leave the directory aside with nothing
    to bring it back: it goes back where it was, and the delete is refused."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
    root = paths.root
    (root / "files" / "Drafts").mkdir()
    (root / "files" / "Drafts" / "notes.txt").write_text("from bash\n")
    real = safe_io.write_text

    def failing(root_, path, text, **kwargs):
        if path.name.endswith(documents.ORIGIN_SUFFIX):
            raise OSError(28, "No space left on device")
        return real(root_, path, text, **kwargs)

    monkeypatch.setattr(documents.safe_io, "write_text", failing)
    with pytest.raises(ApiError) as refused:
        await documents.remove_path(settings, user.id, Path("files/Drafts"))
    assert refused.value.status_code == 409
    assert (root / "files" / "Drafts" / "notes.txt").read_text() == "from bash\n"
    assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]


async def test_undoing_a_move_puts_the_displaced_squatter_back_too(sessionmaker, tmp_path) -> None:
    """A move whose row did not commit is undone whole: the document back where it was,
    and the directory the move displaced back under the name it had (docs/workspace.md
    § Layout)."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
    root = before.root
    target = before.relative.with_name("renamed.pdf")
    (root / target).mkdir()
    (root / target / "scratch.txt").write_text("the shell's")
    git.commit_all(root, "agent:1", "bash: scratch")
    moved = await documents.move_path(settings, user.id, before.relative, target)
    assert moved is not None and moved.displaced is not None
    assert (root / moved.displaced / "scratch.txt").is_file()
    await documents.undo_move(settings, user.id, before.relative, target, moved.displaced)
    assert before.doc.is_file()
    assert (root / target / "scratch.txt").read_text() == "the shell's"
    assert not (root / moved.displaced).exists()
    assert git.log(root)[0].message == f"restore {before.relative}"


async def test_a_file_moved_in_from_another_document_reaches_beyond(sessionmaker, tmp_path) -> None:
    """The exemption for the old side of a rename is the document's own rename only: a
    shell revision that moved a file in from another document is that document's too,
    so its revert waits for every turn of the user (docs/workspace.md § Revisions)."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        other = Document(
            user_id=user.id, folder_id=document.folder_id, job_id=document.job_id, name="other.pdf"
        )
        db.add(other)
        await db.commit()
        paths = await documents.materialize_document(db, settings, document)
        other_paths = await documents.materialize_document(db, settings, other)
    root = paths.root
    git.move(root, other_paths.notes, paths.directory / "extra.md")
    moved_in = git.commit_all(root, "agent:1", "bash: mv")
    assert moved_in is not None
    assert documents.reaches_beyond(paths, moved_in) is True
    # The document's own rename, by contrast, stays its own.
    renamed = paths.relative.with_name("renamed.pdf")
    own = await documents.move_path(settings, user.id, paths.relative, renamed)
    assert own is not None and own.commit is not None
    renamed_paths = documents.DocumentPaths(root=root, relative=renamed)
    assert documents.reaches_beyond(renamed_paths, own.commit) is False


async def test_reconciliation_removes_the_originals_of_rows_that_are_gone(
    sessionmaker, tmp_path
) -> None:
    """A delete the process died in after its rows' commit leaves the originals beside the
    git directory; the next reconciliation takes them, and only them."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        originals = documents.sources_dir(root)
        originals.mkdir(parents=True, exist_ok=True)
        (originals / f"{document.id}.pdf").write_bytes(b"%PDF live")
        (originals / "999.pdf").write_bytes(b"%PDF orphan")
        (originals / ".tmp-1000.pdf").write_bytes(b"staging")
        await reconcile.reconcile_user(db, settings, user.id)
    assert (originals / f"{document.id}.pdf").is_file()
    assert not (originals / "999.pdf").exists()
    assert (originals / ".tmp-1000.pdf").is_file()


async def test_materialized_files_left_uncommitted_are_committed_next_time(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    """The process died between the write and the commit: the files are in place and
    untracked, and the next materialization — which writes nothing — commits them, so
    the first edit is a change against the parse baseline (docs/workspace.md § Layout)."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        real = git.commit_paths
        # The write landed and the commit never ran, as a kill in between leaves it.
        monkeypatch.setattr(git, "commit_paths", lambda *args, **kwargs: None)
        paths = await documents.materialize_document(db, settings, document)
        monkeypatch.setattr(git, "commit_paths", real)
        assert paths.doc.is_file()
        assert not git.tracked_at_head(paths.root, paths.relative / "doc.md")
        await documents.materialize_document(db, settings, document)
        assert git.tracked_at_head(paths.root, paths.relative / "doc.md")
        assert git.tracked_at_head(paths.root, paths.relative / ".meta.json")
        head = git.log(paths.root)[0]
        assert head.message == f"materialize {paths.relative}"
        # Committed, nothing is asked of the next one.
        await documents.materialize_document(db, settings, document)
        assert git.log(paths.root)[0].commit == head.commit


async def test_the_origin_is_written_before_the_directory_moves_aside(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    """The marker stands before the rename is tried, so a process that dies between the
    two leaves a marker beside no aside — swept, the directory where it was — and never
    an aside no marker names; a rename that is refused takes its marker with it
    (docs/workspace.md § Revisions)."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        root = paths.root
        real = safe_io.rename
        markers_at_rename: list[list[str]] = []

        def refusing(root_, source, target):
            markers_at_rename.append(
                [name for name in os.listdir(root) if name.endswith(documents.ORIGIN_SUFFIX)]
            )
            raise safe_io.UnsafeEntry("planted")

        monkeypatch.setattr(safe_io, "rename", refusing)
        with pytest.raises(ApiError) as refused:
            await documents.remove_path(settings, user.id, paths.relative)
        monkeypatch.setattr(safe_io, "rename", real)
        assert refused.value.status_code == 409
        assert len(markers_at_rename[0]) == 1
        assert not [name for name in os.listdir(root) if name.startswith(safe_io.TEMP_PREFIX)]
        assert paths.doc.is_file()
        # A marker whose rename never came: the next reconciliation sweeps the marker and
        # leaves the directory, which still stands where the marker says.
        stale = root / f"{safe_io.TEMP_PREFIX}{'0' * 32}{documents.ORIGIN_SUFFIX}"
        stale.write_text(paths.relative.as_posix() + "\n")
        await reconcile.reconcile_user(db, settings, user.id)
        assert not stale.exists()
        assert paths.doc.is_file()


async def test_a_revert_whose_reconciliation_fails_is_reported_as_reverted_and_followed_later(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    """The revert is a revision before the rows follow it: a reconciliation that fails
    after its commit is reported as such, and the next read of the document brings the
    row in line rather than writing a fresh copy at the path it still names
    (docs/workspace.md § Revisions)."""
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        before = await documents.materialize_document(db, settings, document)
        document.name = "renamed.pdf"
        await db.commit()
        after = await documents.document_paths(db, settings, document)
        await documents.move_path(settings, user.id, before.relative, after.relative)
        rename = git.log(after.root)[0]

        real = reconcile.reconcile_locked

        async def failing(*args, **kwargs):
            raise RuntimeError("the database went away")

        monkeypatch.setattr(reconcile, "reconcile_locked", failing)
        with pytest.raises(ApiError) as refused:
            await documents.revert(db, settings, document, rename.commit)
        monkeypatch.setattr(reconcile, "reconcile_locked", real)
        assert refused.value.status_code == 409
        assert "reverted" in refused.value.message
        # The tree is reverted; the row still names the vacated path.
        assert before.doc.is_file() and not after.directory.exists()
        assert git.log(after.root)[0].message == f"revert {rename.commit[:12]}"
        await db.refresh(document)
        assert document.name == "renamed.pdf"
        # The next read follows the tree instead of materializing at the row's path.
        paths = await documents.materialize_document(db, settings, document)
        assert paths.relative == before.relative
        assert document.name == "paper.pdf"
        assert not after.directory.exists()


# --- what the history stores, and what the quota counts -----------------------------------


def _stored(root: Path, data: bytes) -> bool:
    """Whether the repository holds a blob of these bytes, loose or packed."""
    blob = git._git_raw(root, "hash-object", "--stdin", stdin=data).strip().decode()
    return git._git(root, "cat-file", "-e", blob, check=False).returncode == 0


def test_what_history_excludes_is_never_hashed_into_it(tmp_path) -> None:
    root = tmp_path / "ws" / "20"
    git.ensure_repo(root)
    directory = root / "files" / "paper.pdf"
    excluded = {
        directory / "source.pdf": b"%PDF-1.4 the original",
        directory / "figures" / "p1_0_0_1000_1000.png": b"a crop",
        root / ".sightread" / "results" / "job" / "result.md": b"the result",
        root / ".cache" / "pip" / "wheel": b"a package",
    }
    for path, data in excluded.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (directory / "doc.md").write_text("# A\n")
    assert git.commit_all(root, "agent:1", "bash: everything") is not None
    (directory / "doc.md").write_text("# B\n")
    (directory / "source.pdf").write_bytes(b"%PDF-1.5 a changed copy")
    assert git.commit_paths(root, "user", "save", [Path("files/paper.pdf")]) is not None
    # The texts are revisions. The excluded files were never staged, so not even an
    # unreachable blob of theirs is left in the store (docs/workspace.md § Revisions).
    assert _stored(root, b"# A\n") and _stored(root, b"# B\n")
    assert not any(_stored(root, data) for data in excluded.values())
    assert not _stored(root, b"%PDF-1.5 a changed copy")


def test_staging_follows_deletions_and_swaps_between_files_and_directories(tmp_path) -> None:
    root = tmp_path / "ws" / "21"
    git.ensure_repo(root)
    files = root / "files"
    (files / "was-dir").mkdir(parents=True)
    (files / "was-dir" / "inner.txt").write_text("inner")
    (files / "was-file").write_text("file")
    (files / "gone.txt").write_text("x")
    (files / "doc").mkdir()
    (files / "doc" / "doc.md").write_text("a")
    assert git.commit_all(root, "agent:1", "seed") is not None
    (files / "gone.txt").unlink()
    (files / "was-file").unlink()
    (files / "was-file").mkdir()
    (files / "was-file" / "now.txt").write_text("now a directory")
    shutil.rmtree(files / "was-dir")
    (files / "was-dir").write_text("now a file")
    assert git.commit_all(root, "agent:1", "swap") is not None
    # A host write's path-scoped commit takes a swap as well.
    shutil.rmtree(files / "doc")
    (files / "doc").write_text("now a file")
    assert git.commit_paths(root, "user", "swap", [Path("files/doc")]) is not None
    assert git._git(root, "ls-files").stdout.split() == [
        "files/doc",
        "files/was-dir",
        "files/was-file/now.txt",
    ]
    assert git._git(root, "status", "--porcelain").stdout == ""


def test_packing_keeps_every_revision_and_drops_what_none_names(tmp_path) -> None:
    root = tmp_path / "ws" / "22"
    assert git.loose_objects(root) == 0  # no repository yet
    git.ensure_repo(root)
    for text in ("one", "two", "three"):
        safe_io.write_text(root, root / "files" / "doc.md", text)
        git.commit_all(root, "user", text)
    # What staging used to leave behind: a blob no revision names.
    garbage = git._git_raw(root, "hash-object", "-w", "--stdin", stdin=b"an original").strip()
    assert git.loose_objects(root) > 0
    git.pack(root)
    assert git.loose_objects(root) == 0
    assert git._git(root, "cat-file", "-e", garbage.decode(), check=False).returncode != 0
    assert [revision.message for revision in git.log(root)][:3] == ["three", "two", "one"]
    assert all(_stored(root, text.encode()) for text in ("one", "two", "three"))


async def test_the_sweep_packs_only_the_histories_past_the_threshold(tmp_path) -> None:
    workspaces = tmp_path / "ws"
    busy, quiet = workspaces / "1", workspaces / "2"
    for root, count in ((busy, 30), (quiet, 1)):
        git.ensure_repo(root)
        for index in range(count):
            safe_io.write_text(root, root / "files" / f"f{index}.txt", str(index))
        git.commit_all(root, "user", "seed")
    (workspaces / "3").mkdir()  # a user with no history yet
    (workspaces / "not-a-user").mkdir()
    threshold = git.loose_objects(quiet) + 1
    assert git.loose_objects(busy) >= threshold
    assert await sweeper.pack_histories(workspaces, loose_objects=threshold) == 1
    assert git.loose_objects(busy) == 0
    assert git.loose_objects(quiet) == threshold - 1


def test_storage_counts_each_original_once_and_never_the_service_copies(tmp_path) -> None:
    root = tmp_path / "ws" / "23"
    figures_dir = tmp_path / "figures"
    job = uuid.uuid4()
    git.ensure_repo(root)
    original, image, crop = b"o" * 1000, b"i" * 300, b"c" * 200
    sources_dir(root).mkdir(parents=True)
    (sources_dir(root) / "1.pdf").write_bytes(original)
    (sources_dir(root) / "2.md").write_bytes(b"m" * 40)
    (images_dir(root) / "1").mkdir(parents=True)
    (images_dir(root) / "1" / "img-0123456789abcdef.png").write_bytes(image)
    (figures_dir / str(job)).mkdir(parents=True)
    (figures_dir / str(job) / "p1_0_0_1000_1000.png").write_bytes(crop)

    def directory(path: Path, document_id: int) -> int:
        """A document directory as materialization writes it; returns its own bytes."""
        (path / "figures").mkdir(parents=True)
        (path / ".meta.json").write_text(json.dumps({"document_id": document_id}))
        (path / "doc.md").write_bytes(b"d" * 50)
        (path / "source.pdf").write_bytes(original)
        (path / "figures" / "p1_0_0_1000_1000.png").write_bytes(crop)
        (path / "figures" / "img-0123456789abcdef.png").write_bytes(image)
        return (path / ".meta.json").stat().st_size + 50

    own = directory(root / "files" / "Papers" / "paper.pdf", 1)
    # A command copied the directory: the copies are the service's in one of the two only.
    directory(root / "files" / "copy", 1)
    # One naming no document of the user's excuses nothing.
    stranger = directory(root / "files" / "stranger", 99)
    (root / "files" / "loose.txt").write_bytes(b"l" * 5)
    # Metadata a command made that no id comes out of is no document's.
    (root / "files" / "deep").mkdir()
    (root / "files" / "deep" / ".meta.json").write_bytes(b"[" * 60_000)
    (root / "files" / "huge").mkdir()
    (root / "files" / "huge" / ".meta.json").write_bytes(b'{"document_id": 1e400}')
    (root / ".cache").mkdir()
    (root / ".cache" / "pip").write_bytes(b"p" * 25)
    (root / ".sightread" / "results" / str(job)).mkdir(parents=True)
    (root / ".sightread" / "results" / str(job) / "result.md").write_bytes(b"r" * 999)
    trashed = root / ".sightread" / "trash" / "document-2"
    trashed.mkdir(parents=True)
    (trashed / ".meta.json").write_text(json.dumps({"document_id": 2}))
    (trashed / "doc.md").write_bytes(b"t" * 30)

    usage = documents.storage_usage(root, figures_dir, {1: job, 2: None}, {2})
    copies = len(original) + len(crop) + len(image)
    held = len(original) + len(image)  # the original and the image, once
    assert usage.documents == {1: own + held, 2: usage.trash}
    assert usage.trash == (trashed / ".meta.json").stat().st_size + 30 + 40
    # The directory the document claimed without its copies, the command's copy of it
    # and the stranger whole, the rest of the tree (the results are the database's).
    assert usage.files == own + (own + copies) + (stranger + copies) + (5 + 60_000 + 22 + 25) + held
    assert usage.history == safe_io.tree_size(git_dir(root), git_dir(root)) > 0
    assert usage.total == usage.files + usage.trash + usage.history
    # The documents' shares alone: the same, without the rest of the tree or the history.
    shares = documents.storage_usage(root, figures_dir, {1: job, 2: None}, {2}, whole=False)
    assert (shares.documents, shares.history) == (usage.documents, 0)


def test_a_file_with_several_names_counts_once(tmp_path) -> None:
    root = tmp_path / "ws" / "24"
    git.ensure_repo(root)
    # uv's way: a package's file in the cache, hard-linked into the default environment.
    cached = root / ".cache" / "uv" / "archive-v0" / "abc" / "numpy" / "core.so"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"n" * 4000)
    installed = root / ".cache" / "venv" / "lib" / "python3.11" / "site-packages" / "core.so"
    installed.parent.mkdir(parents=True)
    os.link(cached, installed)
    # A name in a document's directory, met first, carries the file; and in the Trash.
    document = root / "files" / "paper"
    document.mkdir(parents=True)
    (document / ".meta.json").write_text(json.dumps({"document_id": 1}))
    (document / "data.bin").write_bytes(b"d" * 700)
    os.link(document / "data.bin", root / ".cache" / "data-again.bin")
    trashed = root / ".sightread" / "trash" / "document-2"
    trashed.mkdir(parents=True)
    (trashed / ".meta.json").write_text(json.dumps({"document_id": 2}))
    os.link(document / "data.bin", trashed / "data.bin")
    # Two separate files of one size are two files.
    (root / ".cache" / "twin-a").write_bytes(b"t" * 300)
    (root / ".cache" / "twin-b").write_bytes(b"t" * 300)

    usage = documents.storage_usage(root, tmp_path / "figures", {1: None, 2: None}, {2})
    metas = (document / ".meta.json").stat().st_size
    assert usage.documents[1] == metas + 700
    assert usage.trash == (trashed / ".meta.json").stat().st_size
    assert usage.files == metas + 700 + 4000 + 600


async def test_a_workspace_too_deep_to_measure_is_refused_not_undercounted(
    sessionmaker, tmp_path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    async with sessionmaker() as db:
        user, document = await _seed(db)
        paths = await documents.materialize_document(db, settings, document)
        (paths.root / "files" / "a" / "b" / "c" / "d").mkdir(parents=True)
        monkeypatch.setattr(safe_io, "DEPTH_MAX", 3)
        with pytest.raises(ApiError) as refused:
            await documents.measure_storage(db, settings, user.id, paths.root)
        assert refused.value.status_code == 409
