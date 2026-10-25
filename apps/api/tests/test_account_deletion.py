"""Deleting the account (docs/auth.md § Deleting the account): every row and every file of
the user at once, refused while a subscription still renews or while a parse or an answer
runs, and finished by the worker's sweep when the request could not remove the files.

Kano Pay and the sandbox runner exist only inside respx; no upstream is called.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select, update

from sightread.agents import PARSE
from sightread.auth.sessions import SESSION_COOKIE
from sightread.db.models import (
    AgentSetting,
    ApiKey,
    ChatAttachment,
    ChatTurn,
    Conversation,
    CreditEntry,
    CreditPeriod,
    Document,
    Folder,
    Job,
    Message,
    PayEvent,
    Result,
    Subscription,
    UsageLog,
    User,
    UserSession,
    utcnow,
)
from sightread.jobs.sweeper import finish_deletions
from sightread.parsing.attachments import FILE_PREFIX
from sightread.workspace.layout import (
    deletion_marker,
    git_dir,
    images_dir,
    sources_dir,
    user_root,
)
from tests.conftest import CSRF_HEADERS, DATABASE_URL, PARSE_MODEL, PAY_SETTINGS, grant_plan
from tests.test_document_images import _image, _insert
from tests.test_library_trash import _write
from tests.test_markdown_documents import _upload_markdown
from tests.test_pay import SUBSCRIPTIONS_URL, _event, _notify, canonical
from tests.test_retention import _stranger

postgres_only = pytest.mark.skipif(
    not (DATABASE_URL or "").startswith("postgresql"),
    reason="the users row's cascades need PostgreSQL",
)

SANDBOX_URL = "http://sandbox.test"
# Every table keyed by the user that the footprint below writes.
USER_TABLES = (
    UserSession,
    ApiKey,
    AgentSetting,
    Job,
    Folder,
    Document,
    UsageLog,
    Conversation,
    ChatTurn,
    ChatAttachment,
    Subscription,
    CreditPeriod,
    CreditEntry,
)


async def _account(make_client, tmp_path, **overrides) -> AsyncClient:
    """Signed in, with a workspace of its own."""
    client = make_client(workspaces_dir=str(tmp_path / "ws"), **overrides)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    client.user_id = (await client.get("/api/me")).json()["user"]["id"]
    client.root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    return client


async def _delete(client: AsyncClient) -> httpx.Response:
    return await client.delete("/api/account", headers=CSRF_HEADERS)


async def _job(db, user_id: int, status: str = "succeeded") -> uuid.UUID:
    job = Job(
        id=uuid.uuid4(),
        user_id=user_id,
        agent="parse",
        kind="pdf",
        filename="paper.pdf",
        media_type="application/pdf",
        size_bytes=8,
        sha256=uuid.uuid4().hex * 2,
        model=PARSE_MODEL,
        pipeline_version=PARSE.pipeline_version,
        bbox_format="yxyx_norm1000",
        status=status,
        page_count=1,
        finished_at=utcnow() if status in ("succeeded", "failed") else None,
    )
    db.add(job)
    await db.flush()
    return job.id


async def _footprint(client: AsyncClient) -> dict:
    """A bit of everything an account owns, in the database and on disk; the paths that
    must go with it."""
    settings = client.app.state.settings
    uid = client.user_id
    assert (
        await client.post("/api/keys", json={"name": "laptop"}, headers=CSRF_HEADERS)
    ).status_code == 201
    note = (await _upload_markdown(client, b"# Note\n", name="note.md")).json()
    await _write(client, note["id"], "# Note, edited\n")
    assert (await _insert(client, note["id"], _image("PNG"))).status_code == 201
    period_start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    async with client.app.state.sessionmaker() as db:
        parsed = await _job(db, uid)
        queued = await _job(db, uid, status="queued")
        db.add(Result(job_id=parsed, markdown="# P\n", pages=[], figures=[], errors=[], meta={}))
        folder = Folder(user_id=uid, name="Papers")
        db.add(folder)
        await db.flush()
        db.add(Document(user_id=uid, folder_id=folder.id, job_id=parsed, name="paper.pdf"))
        db.add(AgentSetting(user_id=uid, agent="chat", model="vendor/chat-model"))
        conversation = Conversation(user_id=uid, document_id=note["id"], session="s")
        db.add(conversation)
        await db.flush()
        db.add(Message(conversation_id=conversation.id, role="user", content=[]))
        db.add(
            ChatTurn(
                user_id=uid,
                conversation_id=conversation.id,
                message_id=1,
                status="succeeded",
                finished_at=utcnow(),
            )
        )
        attachment = ChatAttachment(
            user_id=uid, name="shot.png", kind="image", media_type="image/png", size_bytes=1
        )
        db.add(attachment)
        db.add(
            UsageLog(
                user_id=uid,
                job_id=parsed,
                agent="parse",
                model=PARSE_MODEL,
                prompt_tokens=1,
                completion_tokens=1,
                cost_source="unknown",
            )
        )
        db.add(
            CreditPeriod(
                user_id=uid,
                period_start=period_start,
                period_end=period_start + timedelta(days=30),
                granted=150,
                spent=1,
            )
        )
        db.add(
            CreditEntry(
                user_id=uid,
                period_start=period_start,
                kind="parse",
                state="consumed",
                amount=1,
                job_id=parsed,
            )
        )
        # Ended: no obstacle to the deletion, and a row that goes with it.
        db.add(
            Subscription(
                user_id=uid,
                subscription_id=f"sub_{uid}",
                status="canceled",
                plan_id="pro",
                period_start=utcnow() - timedelta(days=40),
                period_end=utcnow() - timedelta(days=10),
            )
        )
        db.add(PayEvent(id=f"evt_{uid}", type="subscription.canceled", user_id=uid))
        await db.commit()
        attachment_id = attachment.id
    figures, uploads = Path(settings.figures_dir), Path(settings.upload_dir)
    files = [
        figures / str(parsed) / "p1_0_0_500_500.png",
        figures / f"{parsed}.attempt" / "p1_0_0_500_500.png",
        uploads / f"{parsed}.pdf",
        uploads / f"{queued}.pdf",
        uploads / f"{queued}.0a1b" / "page-1.png",
    ]
    attachments = [
        uploads / f"{FILE_PREFIX}{attachment_id}.png",
        uploads / f"{FILE_PREFIX}{attachment_id}.upload",
    ]
    for path in (*files, *attachments):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    repos = git_dir(client.root).parent
    workspace = [
        client.root,
        git_dir(client.root),
        sources_dir(client.root),
        images_dir(client.root),
        repos / f"{uid}.lock",
    ]
    assert all(path.exists() for path in workspace)
    return {
        "jobs": [parsed, queued],
        "paths": [*files, *(path.parent for path in files[:2]), *workspace],
        "attachments": attachments,
    }


async def _owned_rows(client: AsyncClient, user_id: int) -> dict[str, int]:
    async with client.app.state.sessionmaker() as db:
        counts = {
            model.__tablename__: await db.scalar(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
            for model in USER_TABLES
        }
        counts["users"] = await db.scalar(
            select(func.count()).select_from(User).where(User.id == user_id)
        )
    return counts


@postgres_only
async def test_deleting_the_account_removes_every_row_and_file_of_it(make_client, tmp_path) -> None:
    account = await _account(make_client, tmp_path)
    stranger = await _stranger(make_client, tmp_path)
    theirs = (await _upload_markdown(stranger, b"# Theirs\n", name="theirs.md")).json()
    await _write(stranger, theirs["id"], "# Theirs, edited\n")
    owned = await _footprint(account)
    token = account.cookies.get(SESSION_COOKIE)
    assert all((await _owned_rows(account, account.user_id)).values())

    deleted = await _delete(account)

    assert deleted.status_code == 204, deleted.text
    # The cookie is cleared as logout clears it, and the session is gone on every device.
    assert f'{SESSION_COOKIE}=""' in deleted.headers["set-cookie"]
    assert "Max-Age=0" in deleted.headers["set-cookie"]
    assert (await account.get("/api/me")).status_code == 401
    account.cookies.set(SESSION_COOKIE, token)
    assert (await account.get("/api/me")).status_code == 401
    assert set((await _owned_rows(account, account.user_id)).values()) == {0}
    async with account.app.state.sessionmaker() as db:
        assert (
            await db.execute(select(Result).where(Result.job_id.in_(owned["jobs"])))
        ).all() == []
        # The record of a Pay event outlives the account it was about.
        event = await db.get(PayEvent, f"evt_{account.user_id}")
        assert event is not None and event.user_id is None
    assert [path for path in (*owned["paths"], *owned["attachments"]) if path.exists()] == []
    assert not deletion_marker(account.app.state.settings.workspaces_dir, account.user_id).exists()
    # The other account is untouched.
    assert (await _owned_rows(stranger, stranger.user_id))["documents"] == 1
    content = await stranger.get(f"/api/library/documents/{theirs['id']}/content")
    assert content.status_code == 200 and content.json()["markdown"] == "# Theirs, edited\n"
    assert git_dir(stranger.root).is_dir() and sources_dir(stranger.root).is_dir()
    # Signing in again makes a new, empty account.
    account.cookies.clear()
    await account.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    again = (await account.get("/api/me")).json()["user"]["id"]
    assert again != account.user_id
    assert (await account.get("/api/library")).json()["documents"] == []


@respx.mock
async def test_a_subscription_that_still_renews_refuses_until_it_is_set_to_end(
    make_client, tmp_path
) -> None:
    account = await _account(make_client, tmp_path, **PAY_SETTINGS)
    uid = account.user_id
    fetch = respx.get(f"{SUBSCRIPTIONS_URL}{uid}").mock(
        return_value=httpx.Response(200, json=canonical(uid))
    )

    refused = await _delete(account)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == {
        "type": "subscription_active",
        "message": "Cancel your subscription first",
    }
    # Pay's answer, fetched fresh, is written back as the webhook writes it.
    async with account.app.state.sessionmaker() as db:
        row = await db.get(Subscription, uid)
        assert (row.status, row.cancel_at_period_end) == ("active", False)
    assert (await account.get("/api/me")).status_code == 200

    # Cancelled in the portal: it ends with its period, which is no obstacle.
    fetch.mock(return_value=httpx.Response(200, json=canonical(uid, version=2, cancel=True)))
    deleted = await _delete(account)
    assert deleted.status_code == 204, deleted.text
    async with account.app.state.sessionmaker() as db:
        assert await db.get(User, uid) is None
    assert fetch.call_count == 2

    # The subscription's ending events still come: acknowledged, nothing fetched.
    notified = await _notify(account, _event(uid, "evt_after"))
    assert (notified.status_code, notified.json()) == (200, {})
    assert fetch.call_count == 2


@respx.mock
async def test_pay_unreachable_refuses_only_an_account_with_a_subscription_on_record(
    make_client, tmp_path
) -> None:
    account = await _account(make_client, tmp_path, **PAY_SETTINGS)
    stranger = await _stranger(make_client, tmp_path, **PAY_SETTINGS)
    await grant_plan(account.app.state.sessionmaker, account.user_id)
    respx.get(url__startswith=SUBSCRIPTIONS_URL).mock(side_effect=httpx.ConnectError("down"))

    refused = await _delete(account)
    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["type"] == "pay_unavailable"
    assert (await account.get("/api/me")).status_code == 200
    # Nothing on record, nothing to cancel: Pay's silence does not hold the deletion up.
    assert (await _delete(stranger)).status_code == 204


async def test_without_pay_the_stored_subscription_decides(make_client, tmp_path) -> None:
    account = await _account(make_client, tmp_path)
    await grant_plan(account.app.state.sessionmaker, account.user_id)

    refused = await _delete(account)
    assert refused.status_code == 409
    assert refused.json()["error"]["type"] == "subscription_active"
    async with account.app.state.sessionmaker() as db:
        await db.execute(
            update(Subscription)
            .where(Subscription.user_id == account.user_id)
            .values(status="expired")
        )
        await db.commit()
    assert (await _delete(account)).status_code == 204


@postgres_only
async def test_running_work_refuses_and_queued_work_goes_with_the_account(
    make_client, tmp_path
) -> None:
    account = await _account(make_client, tmp_path)
    uid = account.user_id
    note = (await _upload_markdown(account, b"# Note\n", name="note.md")).json()
    async with account.app.state.sessionmaker() as db:
        running = await _job(db, uid, status="running")
        conversation = Conversation(user_id=uid, document_id=note["id"], session="s")
        db.add(conversation)
        await db.commit()
        conversation_id = conversation.id

    async def refused() -> None:
        response = await _delete(account)
        assert response.status_code == 409, response.text
        assert response.json()["error"] == {
            "type": "invalid_request",
            "message": "A parse or an answer is still running; try again when it finishes",
        }
        async with account.app.state.sessionmaker() as db:
            assert await db.get(User, uid) is not None

    await refused()
    async with account.app.state.sessionmaker() as db:
        await db.execute(
            update(Job).where(Job.id == running).values(status="succeeded", finished_at=utcnow())
        )
        turn = ChatTurn(
            user_id=uid, conversation_id=conversation_id, message_id=1, status="running"
        )
        db.add(turn)
        await db.commit()
        turn_id = turn.id
    await refused()
    # Cancelled, but the worker has not wound it down yet.
    async with account.app.state.sessionmaker() as db:
        await db.execute(update(ChatTurn).where(ChatTurn.id == turn_id).values(status="cancelled"))
        await db.commit()
    await refused()

    # Queued work never started: it goes with the account.
    async with account.app.state.sessionmaker() as db:
        await db.execute(
            update(ChatTurn).where(ChatTurn.id == turn_id).values(finished_at=utcnow())
        )
        queued = await _job(db, uid, status="queued")
        db.add(
            ChatTurn(user_id=uid, conversation_id=conversation_id, message_id=2, status="queued")
        )
        await db.commit()
    assert (await _delete(account)).status_code == 204
    async with account.app.state.sessionmaker() as db:
        assert await db.get(Job, queued) is None
        assert (await db.execute(select(ChatTurn))).all() == []


@respx.mock
async def test_a_sandbox_that_cannot_be_stopped_does_not_hold_the_deletion_up(
    make_client, tmp_path
) -> None:
    account = await _account(make_client, tmp_path, sandbox_url=SANDBOX_URL, sandbox_token="t")
    stop = respx.post(f"{SANDBOX_URL}/sandboxes/{account.user_id}/stop").mock(
        side_effect=httpx.ConnectError("down")
    )
    assert (await _delete(account)).status_code == 204
    assert stop.call_count == 1


async def test_the_sweep_finishes_a_deletion_whose_files_the_request_left(
    make_client, tmp_path, monkeypatch
) -> None:
    from sightread.routes import control

    account = await _account(make_client, tmp_path)
    owned = await _footprint(account)
    settings = account.app.state.settings
    stranger = await _stranger(make_client, tmp_path)
    (await _upload_markdown(stranger, b"# Theirs\n", name="theirs.md")).json()

    async def failing(*args, **kwargs):
        raise OSError("disk unwell")

    # The rows go; the files stay behind with the marker naming the jobs.
    monkeypatch.setattr(control, "discard_account", failing)
    assert (await _delete(account)).status_code == 204
    monkeypatch.undo()
    marker = deletion_marker(settings.workspaces_dir, account.user_id)
    assert marker.is_file() and account.root.is_dir()

    # A marker whose account still stands — its commit never landed — goes alone.
    standing = deletion_marker(settings.workspaces_dir, stranger.user_id)
    standing.write_text("[]")

    finished = await finish_deletions(account.app.state.sessionmaker, settings)

    assert finished == 1
    assert [path for path in owned["paths"] if path.exists()] == []
    # The marker names jobs only: a chat attachment's file left behind is the upload
    # directory's, which the same sweep empties of anything older than a day.
    assert all(path.exists() for path in owned["attachments"])
    assert not marker.exists() and not standing.exists()
    assert sources_dir(stranger.root).is_dir()
    async with account.app.state.sessionmaker() as db:
        assert await db.get(User, stranger.user_id) is not None
    assert await finish_deletions(account.app.state.sessionmaker, settings) == 0
