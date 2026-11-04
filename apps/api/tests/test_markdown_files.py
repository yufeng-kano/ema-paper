"""Markdown files and markdown documents' names (docs/workspace.md § Files, *A markdown file
is read and written in place*; § Markdown documents). A `.md` that is a file — `AGENTS.md`,
one the shell wrote — is read and saved in place through `…/text` as one revision of the
reader's; no markdown document is called `AGENTS.md`; and the operator's one-time command
gives `.md` to the markdown documents named before the rule.

No upstream traffic: nothing here runs a parse or a turn (docs/testing.md § Cost safety).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from sightread import markdown_names
from sightread.agents import chat as chat_agent
from sightread.auth.sessions import SESSION_COOKIE, create_session
from sightread.config import Settings
from sightread.db.models import ChatTurn, Conversation, Document, User, utcnow
from sightread.workspace import git
from sightread.workspace.agents_file import agents_file_text
from sightread.workspace.documents import is_markdown_file
from sightread.workspace.layout import trash_root, user_root
from sightread.workspace.reconcile import reconcile_user
from tests.conftest import CSRF_HEADERS, convert_on_upload


async def _writer(make_client, tmp_path, **overrides) -> AsyncClient:
    client = make_client(workspaces_dir=str(tmp_path / "ws"), **overrides)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


@pytest.fixture
async def writer(make_client, tmp_path) -> AsyncClient:
    """Signed in, with a workspace of its own and no upstream: nothing here needs one."""
    return await _writer(make_client, tmp_path)


async def _upload(
    client: AsyncClient, name: str, data: bytes, media_type: str, folder_id: int | None = None
) -> dict:
    response = await client.post(
        "/api/library/documents",
        files={"file": (name, data, media_type)},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _agents(client: AsyncClient, data: bytes, folder_id: int | None = None) -> dict:
    """An `AGENTS.md` uploaded on the Files page: a file, never a markdown document."""
    kept = await _upload(client, "AGENTS.md", data, "text/markdown", folder_id)
    assert kept["kind"] == "file"
    return kept


async def _by_the_shell(client: AsyncClient, relative: str, data: bytes) -> Document:
    """A file the shell wrote into a folder, committed as a turn commits it and given its
    row by reconciliation (docs/workspace.md § Files)."""
    git.ensure_repo(client.root)
    path = client.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    git.commit_all(client.root, "agent:1", "shell writes")
    async with client.app.state.sessionmaker() as db:
        await reconcile_user(db, client.app.state.settings, client.user_id)
        await db.commit()
        return (
            await db.execute(
                select(Document).where(
                    Document.user_id == client.user_id, Document.name == Path(relative).name
                )
            )
        ).scalar_one()


async def _new(client: AsyncClient, name: str, folder_id: int | None = None) -> httpx.Response:
    return await client.post(
        "/api/library/documents/new",
        json={"name": name, "folder_id": folder_id},
        headers=CSRF_HEADERS,
    )


async def _folder(client: AsyncClient, name: str, parent_id: int | None = None) -> dict:
    response = await client.post(
        "/api/library/folders", json={"name": name, "parent_id": parent_id}, headers=CSRF_HEADERS
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _text(client: AsyncClient, document_id: int) -> httpx.Response:
    return await client.get(f"/api/library/documents/{document_id}/text")


async def _save(client: AsyncClient, document_id: int, markdown: str, base: str) -> httpx.Response:
    return await client.put(
        f"/api/library/documents/{document_id}/text",
        json={"markdown": markdown, "base_revision": base},
        headers=CSRF_HEADERS,
    )


async def _rename(client: AsyncClient, document_id: int, name: str) -> httpx.Response:
    return await client.put(
        f"/api/library/documents/{document_id}", json={"name": name}, headers=CSRF_HEADERS
    )


async def _row(client: AsyncClient, document_id: int) -> Document:
    async with client.app.state.sessionmaker() as db:
        return await db.get(Document, document_id)


# --- the text of a markdown file ----------------------------------------------------------


async def test_an_agents_file_is_read_as_stored_and_saved_as_one_revision(writer) -> None:
    stored = b"\xef\xbb\xbf# Rules\r\n\r\n- Be brief\r\n"
    kept = await _agents(writer, stored)
    read = await _text(writer, kept["id"])
    assert read.status_code == 200, read.text
    body = read.json()
    # As stored: the byte-order mark and the CRLF stay, and the revision is the bytes' hash.
    assert body == {
        "markdown": "﻿# Rules\r\n\r\n- Be brief\r\n",
        "revision": hashlib.sha256(stored).hexdigest(),
        "editable": True,
    }

    edited = "# Rules\n\n- Be brief\n- Cite the page\n"
    saved = await _save(writer, kept["id"], edited, body["revision"])
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"revision": hashlib.sha256(edited.encode()).hexdigest()}
    assert (writer.root / "files" / "AGENTS.md").read_bytes() == edited.encode()
    head = git.log(writer.root)[0]
    assert (head.message, head.author, head.files) == (
        "edit files/AGENTS.md",
        git.USER_AUTHOR,
        ("files/AGENTS.md",),
    )
    # The row follows the file, and the chain a new conversation reads has the edit.
    assert (await _row(writer, kept["id"])).size_bytes == len(edited.encode())
    listed = {row["id"]: row for row in (await writer.get("/api/library")).json()["documents"]}
    assert listed[kept["id"]]["size_bytes"] == len(edited.encode())
    chain = await agents_file_text(writer.app.state.settings, writer.user_id, Path("files"))
    assert "- Cite the page" in chain

    # The text it holds is no change: nothing written, no revision.
    again = await _save(writer, kept["id"], edited, saved.json()["revision"])
    assert again.status_code == 200, again.text
    assert git.log(writer.root)[0].commit == head.commit
    assert (await _text(writer, kept["id"])).json()["revision"] == saved.json()["revision"]


async def test_a_save_over_a_change_from_elsewhere_is_a_conflict(writer) -> None:
    kept = await _by_the_shell(writer, "files/Notes/todo.md", b"- one\n")
    base = (await _text(writer, kept.id)).json()["revision"]
    (writer.root / "files" / "Notes" / "todo.md").write_text("- one\n- the agent's two\n")
    refused = await _save(writer, kept.id, "- one\n- mine\n", base)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["type"] == "revision_conflict"
    assert (writer.root / "files" / "Notes" / "todo.md").read_text() == "- one\n- the agent's two\n"


async def test_a_file_history_would_not_record_is_read_only(
    make_client, tmp_path, monkeypatch
) -> None:
    """Past `REVISION_MAX_FILE_BYTES`, or binary by git's test, a save would leave no
    revision — and a save with no revision is not a save (docs/workspace.md § Revisions)."""
    monkeypatch.setattr(git, "revision_max_file_bytes", lambda: 32)
    writer = await _writer(make_client, tmp_path, revision_max_file_bytes=32)
    assert (await writer.get("/api/me")).json()["limits"]["revision_max_bytes"] == 32

    large = await _agents(writer, b"x" * 40)
    read = (await _text(writer, large["id"])).json()
    assert (read["markdown"], read["editable"]) == ("x" * 40, False)
    refused = await _save(writer, large["id"], "short\n", read["revision"])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["type"] == "invalid_request"
    assert (writer.root / "files" / "AGENTS.md").read_bytes() == b"x" * 40

    small = await _by_the_shell(writer, "files/small.md", b"# Small\n")
    read = (await _text(writer, small.id)).json()
    assert read["editable"] is True
    head = git.log(writer.root)[0].commit
    # Nor is a text saved that history would not record.
    for text in ("y" * 33, "a\0b\n"):
        refused = await _save(writer, small.id, text, read["revision"])
        assert refused.status_code == 409, refused.text
    assert (writer.root / "files" / "small.md").read_bytes() == b"# Small\n"
    assert git.log(writer.root)[0].commit == head

    nul = await _by_the_shell(writer, "files/nul.md", b"a\0b\n")
    assert (await _text(writer, nul.id)).json()["editable"] is False


async def test_a_file_too_large_to_open_or_not_text_is_refused(make_client, tmp_path) -> None:
    writer = await _writer(make_client, tmp_path, content_max_bytes=64)
    large = await _agents(writer, b"# Big\n" + b"x" * 100)
    assert (await _text(writer, large["id"])).status_code == 413
    refused = await _save(writer, large["id"], "# Small\n", "0" * 64)
    assert refused.status_code == 413, refused.text
    assert (writer.root / "files" / "AGENTS.md").stat().st_size == 106

    folder = await _folder(writer, "Latin")
    latin = await _agents(writer, b"caf\xe9\n", folder["id"])
    assert (await _text(writer, latin["id"])).status_code == 400

    gone = await _by_the_shell(writer, "files/gone.md", b"# Gone\n")
    (writer.root / "files" / "gone.md").unlink()
    assert (await _text(writer, gone.id)).status_code == 404
    (writer.root / "files" / "gone.md").mkdir()
    assert (await _text(writer, gone.id)).status_code == 409


async def test_only_a_live_markdown_file_of_the_callers_answers(
    writer, make_client, documents
) -> None:
    kept = await _agents(writer, b"# Mine\n")
    plain = await _upload(writer, "notes.txt", b"plain\n", "text/plain")
    document = (await _new(writer, "Draft")).json()
    await convert_on_upload(writer)
    parse = await _upload(writer, "scan.png", documents["png"].read_bytes(), "image/png")
    assert parse["job_id"] is not None
    # A parse named like a markdown file is still a parse.
    assert (await _rename(writer, parse["id"], "scan.md")).status_code == 200
    for document_id in (plain["id"], document["id"], parse["id"], 999_999):
        assert (await _text(writer, document_id)).status_code == 404
        assert (await _save(writer, document_id, "x", "0" * 64)).status_code == 404

    stranger = make_client(workspaces_dir=writer.app.state.settings.workspaces_dir)
    async with writer.app.state.sessionmaker() as db:
        other = User(google_sub="stranger", email="stranger@example.com")
        db.add(other)
        await db.flush()
        token = await create_session(db, other)
        await db.commit()
    stranger.cookies.set(SESSION_COOKIE, token)
    base = (await _text(writer, kept["id"])).json()["revision"]
    assert (await _text(stranger, kept["id"])).status_code == 404
    assert (await _save(stranger, kept["id"], "# Theirs\n", base)).status_code == 404

    # The CSRF header, as for every write.
    unguarded = await writer.put(
        f"/api/library/documents/{kept['id']}/text",
        json={"markdown": "# Edited\n", "base_revision": base},
    )
    assert unguarded.status_code == 403

    trashed = await writer.post(f"/api/library/documents/{kept['id']}/trash", headers=CSRF_HEADERS)
    assert trashed.status_code == 200, trashed.text
    assert (await _text(writer, kept["id"])).status_code == 404
    assert (await _save(writer, kept["id"], "# Edited\n", base)).status_code == 404
    assert (trash_root(writer.root) / f"document-{kept['id']}").read_bytes() == b"# Mine\n"


def test_a_markdown_file_is_known_by_its_name_or_its_type() -> None:
    def file(name: str, media_type: str | None = None, kind: str = "file") -> Document:
        return Document(name=name, media_type=media_type, kind=kind)

    assert is_markdown_file(file("AGENTS.md"))
    assert is_markdown_file(file("Notes.MARKDOWN"))
    assert is_markdown_file(file("README", "text/markdown"))
    assert not is_markdown_file(file("notes.txt", "text/plain"))
    assert not is_markdown_file(file("summary.md", kind="markdown"))
    assert not is_markdown_file(file("scan.md", kind="parse"))


# --- names --------------------------------------------------------------------------------


async def test_a_markdown_files_rename_keeps_its_extension(writer) -> None:
    kept = await _by_the_shell(writer, "files/Notes/todo.md", b"- one\n")
    renamed = await _rename(writer, kept.id, "plan")
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "plan.md"
    assert (writer.root / "files" / "Notes" / "plan.md").read_bytes() == b"- one\n"
    assert (await _rename(writer, kept.id, "Plan.markdown")).json()["name"] == "Plan.markdown"
    # A file may be the folder's instructions: the name is refused only to a document.
    made = await _rename(writer, kept.id, "AGENTS")
    assert made.status_code == 200, made.text
    assert made.json()["name"] == "AGENTS.md"
    chain = await agents_file_text(writer.app.state.settings, writer.user_id, Path("files/Notes"))
    assert "- one" in chain


async def test_no_markdown_document_is_called_agents_md(writer, documents) -> None:
    for name in ("AGENTS.md", "AGENTS"):
        refused = await _new(writer, name)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["type"] == "invalid_request"
        assert "instructions file" in refused.json()["error"]["message"]
    # The chain reads the name as it is spelled.
    assert (await _new(writer, "agents")).json()["name"] == "agents.md"

    draft = (await _new(writer, "Draft")).json()
    for name in ("AGENTS", "AGENTS.md"):
        refused = await _rename(writer, draft["id"], name)
        assert refused.status_code == 409, refused.text
        assert "instructions file" in refused.json()["error"]["message"]
    assert (await _row(writer, draft["id"])).name == "Draft.md"

    # One named so before the rule: a copy, a duplicate, a Put Back and an upload that
    # would make one take the suffix instead.
    async with writer.app.state.sessionmaker() as db:
        await db.execute(
            update(Document).where(Document.id == draft["id"]).values(name="AGENTS.md")
        )
        await db.commit()
    folder = await _folder(writer, "Elsewhere")
    copied = await writer.post(
        f"/api/library/documents/{draft['id']}/copy",
        json={"folder_id": folder["id"]},
        headers=CSRF_HEADERS,
    )
    assert copied.status_code == 201, copied.text
    assert copied.json()["name"] == "AGENTS (2).md"
    duplicated = await writer.post(
        f"/api/library/documents/{draft['id']}/copy", json={"folder_id": None}, headers=CSRF_HEADERS
    )
    assert duplicated.json()["name"] == "AGENTS (2).md"
    trashed = await writer.post(f"/api/library/documents/{draft['id']}/trash", headers=CSRF_HEADERS)
    assert trashed.status_code == 200, trashed.text
    restored = await writer.post(
        f"/api/library/documents/{draft['id']}/restore", headers=CSRF_HEADERS
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["name"] == "AGENTS (3).md"
    uploaded = await _upload(writer, "AGENTS", b"# Typed\n", "text/markdown", folder["id"])
    assert (uploaded["kind"], uploaded["name"]) == ("markdown", "AGENTS (3).md")


# --- the operator's one-time command ------------------------------------------------------


async def _legacy(client: AsyncClient, name: str, folder_id: int | None = None) -> int:
    """A markdown document named before the rule: written new, then given the old name."""
    created = await _new(client, f"{name}-new", folder_id)
    assert created.status_code == 201, created.text
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(Document).where(Document.id == created.json()["id"]).values(name=name)
        )
        await db.commit()
    return created.json()["id"]


async def _write(client: AsyncClient, document_id: int, markdown: str) -> None:
    content = (await client.get(f"/api/library/documents/{document_id}/content")).json()
    saved = await client.put(
        f"/api/library/documents/{document_id}/content",
        json={"markdown": markdown, "base_revision": content["revision"]},
        headers=CSRF_HEADERS,
    )
    assert saved.status_code == 200, saved.text


async def _names(client: AsyncClient) -> dict[int, str]:
    async with client.app.state.sessionmaker() as db:
        return dict((await db.execute(select(Document.id, Document.name))).all())


async def test_the_command_gives_old_markdown_documents_their_extension(
    writer, sessionmaker, capsys
) -> None:
    settings = writer.app.state.settings
    notes = await _folder(writer, "Notes")
    summary = await _legacy(writer, "summary", notes["id"])
    await _write(writer, summary, "# Summary\n")
    never = await _legacy(writer, "Draft")
    old = await _legacy(writer, "old")
    await _write(writer, old, "# Old\n")
    assert (
        await writer.post(f"/api/library/documents/{old}/trash", headers=CSRF_HEADERS)
    ).status_code == 200
    assert (await _new(writer, "plan.md")).status_code == 201
    plan = await _legacy(writer, "plan")
    await _folder(writer, "ideas.md")
    ideas = await _legacy(writer, "ideas")
    agents = await _legacy(writer, "AGENTS")
    # Called AGENTS.md before the rule refused it: the folder's instructions file's name.
    named = await _legacy(writer, "AGENTS.md", notes["id"])
    busy = await _legacy(writer, "busy")
    shelved = await _folder(writer, "Shelved")
    inner = await _legacy(writer, "inner", shelved["id"])
    assert (
        await writer.post(f"/api/library/folders/{shelved['id']}/trash", headers=CSRF_HEADERS)
    ).status_code == 200
    async with sessionmaker() as db:
        conversation = Conversation(user_id=writer.user_id, document_id=busy, session="s")
        db.add(conversation)
        await db.flush()
        turn = ChatTurn(
            user_id=writer.user_id,
            conversation_id=conversation.id,
            message_id=1,
            status="running",
        )
        db.add(turn)
        other = User(google_sub="other", email="other@example.com")
        db.add(other)
        await db.flush()
        theirs = Document(user_id=other.id, kind="markdown", name="theirs")
        db.add(theirs)
        await db.commit()
        turn_id, other_id, theirs_id = turn.id, other.id, theirs.id

    async def run(*argv: str) -> tuple[int, list[str]]:
        async with sessionmaker() as db:
            args = markdown_names.build_parser().parse_args(argv)
            status = await markdown_names.run(args, settings, db)
        return status, capsys.readouterr().out.splitlines()

    uid = writer.user_id
    renamed = [
        f"{uid}\t{summary}\tsummary → summary.md",
        f"{uid}\t{never}\tDraft → Draft.md",
        f"{uid}\t{old}\told → old.md",
        f"{uid}\t{plan}\tplan → plan (2).md",
        f"{uid}\t{ideas}\tideas → ideas (2).md",
        f"{uid}\t{agents}\tAGENTS → AGENTS (2).md",
        f"{uid}\t{named}\tAGENTS.md → AGENTS (2).md",
    ]
    skipped = [
        f"{uid}\t{busy}\tbusy\tskipped: {markdown_names.ANSWERING}",
        f"{uid}\t{inner}\tinner\tskipped: {markdown_names.IN_TRASHED_FOLDER}",
    ]
    names = await _names(writer)
    head = git.log(writer.root)[0].commit

    # The plan first: printed, and nothing written.
    status, printed = await run("--dry-run")
    assert status == 0
    assert printed == [*renamed, *skipped, f"{other_id}\t{theirs_id}\ttheirs → theirs.md"]
    assert await _names(writer) == names
    assert git.log(writer.root)[0].commit == head
    assert (writer.root / "files" / "Notes" / "summary" / "doc.md").is_file()

    status, printed = await run()
    assert status == 0
    assert printed == [*renamed, *skipped, f"{other_id}\t{theirs_id}\ttheirs → theirs.md"]
    after = await _names(writer)
    assert (after[summary], after[never], after[old], after[plan]) == (
        "summary.md",
        "Draft.md",
        "old.md",
        "plan (2).md",
    )
    assert (after[ideas], after[agents], after[named], after[theirs_id]) == (
        "ideas (2).md",
        "AGENTS (2).md",
        "AGENTS (2).md",
        "theirs.md",
    )
    assert (after[busy], after[inner]) == ("busy", "inner")
    # A live document's directory moves with it, as one revision of the reader's.
    assert not (writer.root / "files" / "Notes" / "summary").exists()
    content = await writer.get(f"/api/library/documents/{summary}/content")
    assert content.json()["markdown"] == "# Summary\n"
    assert (writer.root / "files" / "Notes" / "summary.md" / "doc.md").read_text() == "# Summary\n"
    moves = [entry for entry in git.log(writer.root) if entry.message.startswith("move ")]
    assert [(entry.message, entry.author) for entry in moves] == [
        ("move files/Notes/summary -> files/Notes/summary.md", git.USER_AUTHOR)
    ]
    # One never written has no directory to move; one in the Trash keeps its entry, named
    # by its id, and Put Back brings it out under the new name.
    assert not (writer.root / "files" / "Draft.md").exists()
    assert (trash_root(writer.root) / f"document-{old}" / "doc.md").read_text() == "# Old\n"
    restored = await writer.post(f"/api/library/documents/{old}/restore", headers=CSRF_HEADERS)
    assert restored.status_code == 200, restored.text
    assert (writer.root / "files" / "old.md" / "doc.md").read_text() == "# Old\n"

    # A second run renames nothing; once the turn has answered, it renames what it skipped.
    status, printed = await run()
    assert (status, printed) == (0, skipped)
    async with sessionmaker() as db:
        await db.execute(
            update(ChatTurn).where(ChatTurn.id == turn_id).values(finished_at=utcnow())
        )
        await db.commit()
    status, printed = await run()
    assert (status, printed) == (0, [f"{uid}\t{busy}\tbusy → busy.md", skipped[1]])


# --- the agent's prompt -------------------------------------------------------------------


def test_the_prompt_tells_a_documents_directory_from_a_markdown_file() -> None:
    prompt = chat_agent.DEFAULT_SYSTEM_PROMPT
    assert "Notes/summary.md/doc.md" in prompt and "AGENTS.md among them" in prompt
    # Within the cap a reader's own prompt is held to.
    assert len(prompt) <= Settings.model_fields["system_prompt_max_chars"].default
