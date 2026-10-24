"""The credit ledger (docs/billing.md § Acceptance): reservations under contention,
idempotent settlement, the chat overdraft, the 402s, and what the payloads show."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import delete, select

from sightread.billing import ledger
from sightread.config import Settings
from sightread.db.models import (
    ChatTurn,
    Conversation,
    CreditEntry,
    CreditPeriod,
    Document,
    Job,
    UsageLog,
    User,
)
from sightread.jobs import chat_runner, chat_tools
from sightread.jobs.queue import claim_next_turn, fail_turn
from sightread.workspace.layout import user_root
from tests.conftest import CSRF_HEADERS, TEST_MODEL_PRICES
from tests.test_chat import MESSAGES_URL, _seed_document, assistant_turn
from tests.test_library import _read, _ready_to_parse, _upload

pytestmark = pytest.mark.usefixtures("documents")


def settings_with(**overrides) -> Settings:
    return Settings(
        app_env="local",
        auth_dev_mode=True,
        secret_key="test-secret-key-not-a-real-one",
        database_url="sqlite+aiosqlite://",
        model_prices_json=TEST_MODEL_PRICES,
        **overrides,
    )


async def _user(sessionmaker) -> int:
    async with sessionmaker() as db:
        user = User(google_sub="ledger-user", email="ledger@example.test")
        db.add(user)
        await db.commit()
        return user.id


async def _period(sessionmaker, user_id: int) -> CreditPeriod:
    async with sessionmaker() as db:
        return (
            await db.execute(select(CreditPeriod).where(CreditPeriod.user_id == user_id))
        ).scalar_one()


async def test_two_parses_racing_for_the_last_credits(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=3)
    user_id = await _user(sessionmaker)
    started = asyncio.Barrier(2)

    async def charge() -> None:
        # The order `submit_parse` takes (docs/jobs.md § Credits): the user's row lock,
        # then the job, then its reservation, in one transaction. The two transactions
        # hold their lock requests at the same time; locking after the insert would
        # deadlock them on the share lock the insert's foreign key already holds.
        async with sessionmaker() as db:
            await started.wait()
            await db.execute(select(User.id).where(User.id == user_id).with_for_update())
            job = Job(
                user_id=user_id,
                agent="parse",
                kind="image",
                filename="a.png",
                media_type="image/png",
                size_bytes=1,
                sha256="x",
                status="queued",
                model="m",
                bbox_format="yxyx_norm1000",
                page_count=1,
                pages_done=0,
            )
            db.add(job)
            await db.flush()
            await ledger.reserve(
                db, settings, user_id=user_id, kind=ledger.PARSE, amount=2, job_id=job.id
            )
            await db.commit()

    outcomes = await asyncio.gather(charge(), charge(), return_exceptions=True)
    refused = [outcome for outcome in outcomes if isinstance(outcome, ledger.InsufficientCredits)]
    assert len(refused) == 1, outcomes
    assert (refused[0].need, refused[0].remaining) == (2, 1)
    assert (await _period(sessionmaker, user_id)).spent == 2
    async with sessionmaker() as db:
        entries = (await db.execute(select(CreditEntry))).scalars().all()
        jobs = (await db.execute(select(Job.id))).scalars().all()
    # The refused job was never queued: its transaction rolled back whole.
    assert [entry.state for entry in entries] == ["reserved"] and len(jobs) == 1


async def test_settlement_is_idempotent_and_a_release_restores_the_balance(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=10)
    user_id = await _user(sessionmaker)
    async with sessionmaker() as db:
        kept = Job(
            user_id=user_id,
            agent="parse",
            kind="image",
            filename="a.png",
            media_type="image/png",
            size_bytes=1,
            sha256="x",
            status="running",
            model="m",
            bbox_format="yxyx_norm1000",
            page_count=3,
            pages_done=0,
        )
        lost = Job(
            user_id=user_id,
            agent="parse",
            kind="image",
            filename="b.png",
            media_type="image/png",
            size_bytes=1,
            sha256="y",
            status="running",
            model="m",
            bbox_format="yxyx_norm1000",
            page_count=4,
            pages_done=0,
        )
        db.add_all([kept, lost])
        await db.flush()
        await ledger.reserve(
            db, settings, user_id=user_id, kind=ledger.PARSE, amount=3, job_id=kept.id
        )
        await ledger.reserve(
            db, settings, user_id=user_id, kind=ledger.PARSE, amount=4, job_id=lost.id
        )
        await db.commit()
        kept_id, lost_id = kept.id, lost.id
    assert (await _period(sessionmaker, user_id)).spent == 7

    async with sessionmaker() as db:
        await ledger.settle_parse(db, kept_id, "succeeded")
        await ledger.settle_parse(db, kept_id, "succeeded")  # a requeue that raced a finish
        await ledger.settle_parse(db, kept_id, "failed")  # a release after consumption: no-op
        await ledger.settle_parse(db, lost_id, "failed")
        await ledger.settle_parse(db, lost_id, "failed")
        await db.commit()
    assert (await _period(sessionmaker, user_id)).spent == 3
    async with sessionmaker() as db:
        states = {
            entry.job_id: (entry.state, entry.amount)
            for entry in (await db.execute(select(CreditEntry))).scalars()
        }
    assert states == {kept_id: ("consumed", 3), lost_id: ("released", 4)}
    async with sessionmaker() as db:
        assert await ledger.credits_by_job(db, [kept_id, lost_id]) == {kept_id: 3, lost_id: 0}


async def test_a_chat_overdraft_is_not_carried_into_the_next_period(sessionmaker) -> None:
    settings = settings_with(free_credits_per_period=1, credit_usd="0.001")
    user_id = await _user(sessionmaker)
    async with sessionmaker() as db:
        # The turn's foreign keys, which PostgreSQL enforces: a document and its thread.
        document = Document(user_id=user_id, name="d.md")
        db.add(document)
        await db.flush()
        conversation = Conversation(user_id=user_id, document_id=document.id, session="s")
        db.add(conversation)
        await db.flush()
        turn = ChatTurn(
            user_id=user_id, conversation_id=conversation.id, message_id=1, status="running"
        )
        db.add(turn)
        await db.flush()
        await ledger.reserve(
            db,
            settings,
            user_id=user_id,
            kind=ledger.CHAT,
            amount=0,
            turn_id=turn.id,
            minimum=settings.chat_min_credits,
        )
        await db.commit()
        turn_id = turn.id
    # A second send needs the minimum balance: one credit is left, so it may go.
    async with sessionmaker() as db:
        balance = await ledger.read_balance(db, settings, user_id)
    assert (balance.granted, balance.spent, balance.remaining) == (1, 0, 1)

    async with sessionmaker() as db:
        for _ in range(2):
            # 3,000 tokens at US$1 per million = US$0.003 per call: 3 credits each.
            db.add(
                UsageLog(
                    user_id=user_id,
                    turn_id=turn_id,
                    agent="chat",
                    model="m",
                    prompt_tokens=1000,
                    completion_tokens=1000,
                    cache_read_tokens=500,
                    cache_write_tokens=500,
                    cost_source="unknown",
                )
            )
            await db.flush()
            remaining = await ledger.settle_chat(db, settings, turn_id)
        await db.commit()
    assert remaining == 1 - 6
    async with sessionmaker() as db:
        assert await ledger.settle_chat(db, settings, turn_id) == -5  # the same rows: no move
        await ledger.close_chat(db, turn_id)
        await db.commit()
        assert await ledger.credits_by_turn(db, [turn_id]) == {turn_id: 6}
        # The next period starts clean: a fresh grant, nothing owed.
        later = datetime.now(UTC).replace(day=1) + timedelta(days=32)
        fresh = await ledger.read_balance(db, settings, user_id, later)
    assert (fresh.granted, fresh.spent) == (1, 0)
    # And a send in the overdrawn period is refused.
    async with sessionmaker() as db:
        with pytest.raises(ledger.InsufficientCredits) as refused:
            await ledger.reserve(
                db, settings, user_id=user_id, kind=ledger.CHAT, amount=0, turn_id=None, minimum=1
            )
    assert (refused.value.need, refused.value.remaining) == (1, -5)


async def test_a_plan_change_raises_the_grant_and_never_lowers_it(sessionmaker) -> None:
    user_id = await _user(sessionmaker)
    async with sessionmaker() as db:
        row, _ = await ledger.period_row(db, settings_with(free_credits_per_period=150), user_id)
        assert (row.granted, row.plan_id) == (150, None)
        row, _ = await ledger.period_row(db, settings_with(free_credits_per_period=200), user_id)
        assert row.granted == 200
        row, _ = await ledger.period_row(db, settings_with(free_credits_per_period=100), user_id)
        assert row.granted == 200
        await db.commit()
    async with sessionmaker() as db:
        balance = await ledger.read_balance(db, settings_with(free_credits_per_period=100), user_id)
    assert balance.granted == 200


# --- routes --------------------------------------------------------------------------------


async def test_the_billing_route_and_me_show_the_plan_and_the_balance(make_client) -> None:
    client = make_client(free_credits_per_period=150)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    me = (await client.get("/api/me")).json()
    assert (me["plan"], me["credits_remaining"]) == ("free", 150)
    billing = (await client.get("/api/billing")).json()
    assert billing["plan"] == {
        "id": "free",
        "name": "free",
        "credits": 150,
        "storage_bytes": 52_428_800,
        "sandbox": False,
        "max_jobs": 1,
    }
    assert billing["subscription"] is None
    assert billing["period"] == {
        "start": billing["period"]["start"],
        "end": billing["period"]["end"],
        "granted": 150,
        "spent": 0,
        "remaining": 150,
    }
    assert billing["rates"]["parse_per_page"] == 1
    # Every priced model, in the price table's order. US$1 per million tokens at US$0.002
    # a credit: half a credit per 1K tokens.
    assert list(billing["rates"]["chat"]) == list(json.loads(TEST_MODEL_PRICES))
    assert billing["rates"]["chat"]["claude-opus-5-5"] == {
        "input": 0.5,
        "cache_read": 0.5,
        "cache_write": 0.5,
        "output": 0.5,
    }
    assert billing["checkout_available"] is False
    empty = (await client.get("/api/billing/entries")).json()
    assert empty == {"days": 30, "per_day": [], "per_agent": []}


async def test_an_upload_the_balance_cannot_cover_is_refused_and_not_stored(
    make_client, documents, tmp_path
) -> None:
    client = make_client(free_credits_per_period=0)
    await _ready_to_parse(client)
    refused = await _upload(client, documents)
    assert refused.status_code == 402, refused.text
    error = refused.json()["error"]
    assert error["type"] == "insufficient_credits"
    assert (error["need"], error["remaining"]) == (1, 0)
    assert error["period_end"]
    assert (await _read(client))["documents"] == []
    assert not list((tmp_path / "uploads").glob("*.png"))
    async with client.app.state.sessionmaker() as db:
        assert (await db.execute(select(Job))).scalars().all() == []
        assert (await db.execute(select(CreditEntry))).scalars().all() == []


async def test_an_upload_reserves_its_pages_and_the_payloads_show_the_settled_cost(
    make_client, documents
) -> None:
    client = make_client(free_credits_per_period=5)
    await _ready_to_parse(client)
    created = await _upload(client, documents)
    assert created.status_code == 201, created.text
    assert created.json()["credits"] is None  # reserved, not settled
    assert (await client.get("/api/me")).json()["credits_remaining"] == 4
    job_id = created.json()["job_id"]
    listed = (await client.get("/api/jobs")).json()["jobs"]
    assert listed[0]["credits"] is None
    entries = (await client.get("/api/billing/entries")).json()
    assert entries["per_agent"] == [{"agent": "parse", "credits": 1}]
    assert entries["per_day"][0]["parse"] == 1

    async with client.app.state.sessionmaker() as db:
        await ledger.settle_parse(db, __import__("uuid").UUID(job_id), "succeeded")
        await db.commit()
    assert (await _read(client))["documents"][0]["credits"] == 1
    assert (await client.get("/api/jobs")).json()["jobs"][0]["credits"] == 1


async def test_a_send_without_the_minimum_balance_is_refused(make_client, tmp_path) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "workspaces"), free_credits_per_period=0)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    chosen = await client.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    assert chosen.status_code == 200, chosen.text
    document_id = await _seed_document(client, user_id)
    created = await client.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    conversation_id = created.json()["id"]
    sent = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 402, sent.text
    assert sent.json()["error"]["need"] == 1
    # Nothing of the send was kept.
    assert (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"] == []


async def test_a_send_into_a_full_workspace_is_refused(make_client, tmp_path) -> None:
    # One byte of storage: the seeded document alone fills it.
    client = make_client(workspaces_dir=str(tmp_path / "workspaces"), free_storage_bytes=1)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    document_id = await _seed_document(client, user_id)
    created = await client.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    conversation_id = created.json()["id"]
    sent = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 409, sent.text
    assert sent.json()["error"]["type"] == "storage_full"
    # Refused before anything was queued or charged.
    assert (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"] == []
    assert (await client.get("/api/billing")).json()["period"]["spent"] == 0


@respx.mock
@pytest.mark.parametrize("granted", [1, 6])
@pytest.mark.parametrize("last_stop", ["end_turn", "tool_use"])
async def test_a_turn_that_runs_out_of_credits_finishes_within_the_allowance(
    make_client, tmp_path, monkeypatch, granted: int, last_stop: str
) -> None:
    """docs/billing.md § A chat turn: once the balance is spent the turn runs at most
    `CHAT_OVERDRAFT_MAX_TOOL_CALLS` more tool calls, then its last call goes with no tool
    left to it; a last call that still asks for one ends the turn `out_of_credits`."""
    # Each stubbed call carries 510 tokens at US$1 per million, which at US$0.0001 a
    # credit is 6 credits: the first call overdraws a period of one credit, and spends a
    # period of six to exactly zero. Two calls are 11, rounded once for the turn.
    client = make_client(
        workspaces_dir=str(tmp_path / "workspaces"),
        free_credits_per_period=granted,
        credit_usd="0.0001",
        chat_overdraft_max_tool_calls=2,
    )
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    await client.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    document_id = await _seed_document(client, user_id)
    created = await client.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    conversation_id = created.json()["id"]
    seen: list[int] = []

    async def view_page(paths, tool_input, **kwargs):
        seen.append(tool_input["page"])
        return chat_tools.ToolOutcome(f"Page {tool_input['page']}.")

    monkeypatch.setattr(chat_tools, "view_page", view_page)
    # Three calls in the round the balance ran out on: two run, the third is past the
    # allowance.
    answers = iter(
        [
            assistant_turn(
                [{"type": "text", "text": "Looking."}]
                + [
                    {
                        "type": "tool_use",
                        "id": f"t{page}",
                        "name": "view_page",
                        "input": {"page": page},
                    }
                    for page in (1, 2, 3)
                ],
                "tool_use",
            ),
            assistant_turn(
                [{"type": "text", "text": "Page 1 says so."}]
                + (
                    [{"type": "tool_use", "id": "t4", "name": "view_page", "input": {"page": 4}}]
                    if last_stop == "tool_use"
                    else []
                ),
                last_stop,
            ),
        ]
    )
    requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=next(answers))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    sent = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 202, sent.text
    settings = client.app.state.settings
    async with client.app.state.sessionmaker() as db:
        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
    await chat_runner.run_turn(
        client.app.state.sessionmaker, settings, turn_id, asyncio.Semaphore(2)
    )
    assert seen == [1, 2]
    assert "tool_choice" not in requests[0]
    assert requests[1]["tool_choice"] == {"type": "none"}
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    expected = ("succeeded", None) if last_stop == "end_turn" else ("failed", "out_of_credits")
    assert (turn["status"], turn["error"], turn["credits"]) == (*expected, 11)
    results = thread["messages"][2]["content"]
    assert [block.get("is_error", False) for block in results] == [False, False, True]
    assert results[2]["content"] == chat_runner.NOT_RUN_OUT_OF_CREDITS
    # The last answer's text stays in the thread; a tool call it still made is dropped.
    last = thread["messages"][3]["content"]
    assert [block["type"] for block in last] == ["text"]
    billing = (await client.get("/api/billing")).json()
    assert (billing["period"]["spent"], billing["period"]["remaining"]) == (11, granted - 11)
    assert (await client.get("/api/me")).json()["credits_remaining"] == granted - 11


async def test_a_turn_queued_before_the_workspace_filled_fails_storage_full(
    make_client, tmp_path
) -> None:
    client = make_client(workspaces_dir=str(tmp_path / "workspaces"), free_storage_bytes=1 << 20)
    await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)
    user_id = (await client.get("/api/me")).json()["user"]["id"]
    document_id = await _seed_document(client, user_id)
    created = await client.post(
        f"/api/library/documents/{document_id}/conversations", headers=CSRF_HEADERS
    )
    conversation_id = created.json()["id"]
    sent = await client.post(
        f"/api/conversations/{conversation_id}/messages", json={"text": "hi"}, headers=CSRF_HEADERS
    )
    assert sent.status_code == 202, sent.text
    # The workspace fills between the send and the turn's start.
    settings = client.app.state.settings
    (user_root(settings.workspaces_dir, user_id) / "big.bin").write_bytes(b"0" * (1 << 20))
    async with client.app.state.sessionmaker() as db:
        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
    await chat_runner.run_turn(
        client.app.state.sessionmaker, settings, turn_id, asyncio.Semaphore(2)
    )
    [turn] = (await client.get(f"/api/conversations/{conversation_id}")).json()["turns"]
    # A token the panel words, never the backend's sentence.
    assert (turn["status"], turn["error"]) == ("failed", "storage_full")


async def _running_turn(sessionmaker, settings: Settings, user_id: int) -> tuple[int, uuid.UUID]:
    """A conversation with one running turn and its reserved chat entry."""
    async with sessionmaker() as db:
        document = Document(user_id=user_id, name="d.md")
        db.add(document)
        await db.flush()
        conversation = Conversation(user_id=user_id, document_id=document.id, session="s")
        db.add(conversation)
        await db.flush()
        turn = ChatTurn(
            user_id=user_id, conversation_id=conversation.id, message_id=1, status="running"
        )
        db.add(turn)
        await db.flush()
        await ledger.reserve(
            db, settings, user_id=user_id, kind=ledger.CHAT, amount=0, turn_id=turn.id, minimum=1
        )
        db.add(
            UsageLog(
                user_id=user_id,
                turn_id=turn.id,
                agent="chat",
                model="m",
                prompt_tokens=1000,
                completion_tokens=1000,
                cost_source="unknown",
            )
        )
        await db.flush()
        await ledger.settle_chat(db, settings, turn.id)
        await db.commit()
        return conversation.id, turn.id


async def test_a_turn_the_worker_fails_closes_its_entry(sessionmaker) -> None:
    """docs/billing.md § A chat turn: `fail_turn` — a shutdown, a restart, an escaped
    error — settles what the turn was charged, as the runner's own finish does."""
    settings = settings_with(free_credits_per_period=10, credit_usd="0.001")
    user_id = await _user(sessionmaker)
    _, turn_id = await _running_turn(sessionmaker, settings, user_id)
    async with sessionmaker() as db:
        assert await ledger.credits_by_turn(db, [turn_id]) == {turn_id: None}
        await fail_turn(db, turn_id, "worker restarted")
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert (turn.status, turn.error) == ("failed", "worker restarted")
        # Two credits' worth of tokens, consumed; shown as the turn's cost from now on.
        assert await ledger.credits_by_turn(db, [turn_id]) == {turn_id: 2}
    assert (await _period(sessionmaker, user_id)).spent == 2


async def test_a_deleted_conversation_leaves_its_entries_in_the_ledger(sessionmaker) -> None:
    """docs/billing.md § The ledger: the record outlives the thread it was charged for;
    the period's `spent` and the entries keep agreeing."""
    settings = settings_with(free_credits_per_period=10, credit_usd="0.001")
    user_id = await _user(sessionmaker)
    conversation_id, turn_id = await _running_turn(sessionmaker, settings, user_id)
    async with sessionmaker() as db:
        await ledger.close_chat(db, turn_id)
        await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await db.commit()
    async with sessionmaker() as db:
        assert await db.get(ChatTurn, turn_id) is None
        [entry] = (await db.execute(select(CreditEntry))).scalars().all()
    assert (entry.kind, entry.state, entry.amount, entry.turn_id) == ("chat", "consumed", 2, None)
    assert (await _period(sessionmaker, user_id)).spent == 2
