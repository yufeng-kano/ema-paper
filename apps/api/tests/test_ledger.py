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
from sightread.db.models import (
    ChatTurn,
    Conversation,
    CreditEntry,
    CreditPack,
    CreditPeriod,
    Job,
    PlanGrant,
    UsageLog,
    User,
    utcnow,
)
from sightread.jobs import chat_runner, chat_tools
from sightread.jobs.queue import fail_turn
from tests.billing_helpers import (
    credit_period,
    image_job,
    make_user,
    running_turns,
)
from tests.chat_helpers import (
    MESSAGES_URL,
    assistant_turn,
    chatting_client,
    new_conversation,
    run_queued_turn,
    seed_document,
    send,
    sse,
)
from tests.conftest import (
    CSRF_HEADERS,
    PRO_PLANS_JSON,
    TEST_MODEL_PRICES,
    base_settings,
    grant_plan,
    postgres_only,
    sign_in,
)
from tests.factories import user_row
from tests.helpers import read_library
from tests.library_helpers import ready_to_parse, upload_png


# Row locks only block on PostgreSQL; on SQLite the database-wide write lock
# serializes the two transactions with or without them.
@postgres_only
async def test_two_parses_racing_for_the_last_credits(sessionmaker) -> None:
    settings = base_settings(free_credits_per_period=3)
    user_id = await make_user(sessionmaker)
    started = asyncio.Barrier(2)

    async def charge() -> None:
        # The order `submit_parse` takes (docs/jobs.md § Credits): the user's row lock,
        # then the job, then its reservation, in one transaction. The two transactions
        # hold their lock requests at the same time; locking after the insert would
        # deadlock them on the share lock the insert's foreign key already holds.
        async with sessionmaker() as db:
            await started.wait()
            await db.execute(select(User.id).where(User.id == user_id).with_for_update())
            job = image_job(user_id)
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
    assert (await credit_period(sessionmaker, user_id)).spent == 2
    async with sessionmaker() as db:
        entries = (await db.execute(select(CreditEntry))).scalars().all()
        jobs = (await db.execute(select(Job.id))).scalars().all()
    # The refused job was never queued: its transaction rolled back whole.
    assert [entry.state for entry in entries] == ["reserved"] and len(jobs) == 1


async def test_settlement_is_idempotent_and_a_release_restores_the_balance(sessionmaker) -> None:
    settings = base_settings(free_credits_per_period=10)
    user_id = await make_user(sessionmaker)
    async with sessionmaker() as db:
        kept = image_job(user_id, status="running", page_count=3)
        lost = image_job(user_id, filename="b.png", sha256="y", status="running", page_count=4)
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
    assert (await credit_period(sessionmaker, user_id)).spent == 7

    async with sessionmaker() as db:
        await ledger.settle_parse(db, kept_id, "succeeded")
        await ledger.settle_parse(db, kept_id, "succeeded")  # a requeue that raced a finish
        await ledger.settle_parse(db, kept_id, "failed")  # a release after consumption: no-op
        await ledger.settle_parse(db, lost_id, "failed")
        await ledger.settle_parse(db, lost_id, "failed")
        await db.commit()
    assert (await credit_period(sessionmaker, user_id)).spent == 3
    async with sessionmaker() as db:
        states = {
            entry.job_id: (entry.state, entry.amount)
            for entry in (await db.execute(select(CreditEntry))).scalars()
        }
    assert states == {kept_id: ("consumed", 3), lost_id: ("released", 4)}
    async with sessionmaker() as db:
        assert await ledger.credits_by_job(db, [kept_id, lost_id]) == {kept_id: 3, lost_id: 0}


async def test_a_chat_overdraft_is_not_carried_into_the_next_period(sessionmaker) -> None:
    settings = base_settings(free_credits_per_period=1, credit_usd="0.001")
    user_id = await make_user(sessionmaker)
    _, [turn_id], _ = await running_turns(sessionmaker, settings, user_id, settle=False)
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
    # Settlement answers what a charge could still be measured against — nothing, never a
    # negative number — while the period itself is in the red.
    assert remaining == 0
    async with sessionmaker() as db:
        assert (await ledger.read_balance(db, settings, user_id)).remaining == 1 - 6
        assert await ledger.settle_chat(db, settings, turn_id) == 0  # the same rows: no move
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
    # The refusal names what is available — nothing — not the period's debt.
    assert (refused.value.need, refused.value.remaining) == (1, 0)


async def test_a_plan_change_raises_the_grant_and_never_lowers_it(sessionmaker) -> None:
    user_id = await make_user(sessionmaker)
    async with sessionmaker() as db:
        row, _ = await ledger.period_row(db, base_settings(free_credits_per_period=150), user_id)
        assert row.granted == 150
        row, _ = await ledger.period_row(db, base_settings(free_credits_per_period=200), user_id)
        assert row.granted == 200
        row, _ = await ledger.period_row(db, base_settings(free_credits_per_period=100), user_id)
        assert row.granted == 200
        await db.commit()
    async with sessionmaker() as db:
        balance = await ledger.read_balance(db, base_settings(free_credits_per_period=100), user_id)
    assert balance.granted == 200


async def test_the_balances_of_many_accounts_read_as_each_account_alone(sessionmaker) -> None:
    """One read for a page of accounts — no rows, a period in use, a subscription, granted
    days, packs — gives what each account's own read gives, the used period's row found
    whatever time zone the database answers its start in."""
    settings = base_settings(kano_pay_plans_json=PRO_PLANS_JSON)
    now = utcnow()
    async with sessionmaker() as db:
        users = [user_row(f"many-{n}@example.test") for n in range(4)]
        db.add_all(users)
        await db.commit()
    fresh, used, paid, banked = ids = [user.id for user in users]
    await grant_plan(sessionmaker, paid, "pro")
    async with sessionmaker() as db:
        db.add(PlanGrant(user_id=banked, plan_id="pro", days_banked=3))
        period = (await ledger.decide_period(db, settings, used, now)).period
        db.add(
            CreditPeriod(
                user_id=used,
                period_start=period.start,
                period_end=period.end,
                granted=period.credits,
                spent=7,
            )
        )
        for n, user_id in enumerate((used, used, banked)):
            db.add(
                CreditPack(
                    id=f"pack-{n}",
                    user_id=user_id,
                    pack_id="credits",
                    credits=10 + n,
                    status=ledger.PACK_PAID,
                    purchased_at=now - timedelta(days=n),
                    expires_at=now + timedelta(days=30),
                )
            )
        await db.commit()

    async with sessionmaker() as db:
        together = await ledger.read_balances(db, settings, ids, now)
        assert await ledger.read_balances(db, settings, [], now) == {}
    assert sorted(together) == sorted(ids)
    assert (together[fresh].period.source, together[fresh].spent, together[fresh].packs) == (
        "free",
        0,
        (),
    )
    assert together[used].remaining == period.credits - 7
    assert [pack.id for pack in together[used].packs] == ["pack-0", "pack-1"]
    assert (together[paid].period.source, together[paid].period.plan.id) == ("subscription", "pro")
    assert together[banked].period.source == "grant"
    assert [pack.id for pack in together[banked].packs] == ["pack-2"]
    for user_id in ids:
        async with sessionmaker() as db:
            alone = await ledger.decide_period(db, settings, user_id, now)
            assert together[user_id].period == alone.period
            assert together[user_id].grant == alone.grant


# --- routes --------------------------------------------------------------------------------


async def test_the_billing_route_and_me_show_the_plan_and_the_balance(make_client) -> None:
    client = make_client(free_credits_per_period=150)
    await sign_in(client)
    me = (await client.get("/api/me")).json()
    assert (me["plan"], me["credits_remaining"]) == ("free", 150)
    billing = (await client.get("/api/billing")).json()
    assert billing["plan"] == {
        "id": "free",
        "name": "free",
        "credits": 150,
        "storage_bytes": 512_000_000,
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
        "source": "free",
    }
    assert billing["plan_grant"] is None
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


async def test_an_upload_the_balance_cannot_cover_is_refused_and_not_stored(
    make_client, documents, tmp_path
) -> None:
    client = make_client(free_credits_per_period=0)
    await ready_to_parse(client)
    refused = await upload_png(client, documents)
    assert refused.status_code == 402, refused.text
    error = refused.json()["error"]
    assert error["type"] == "insufficient_credits"
    assert (error["need"], error["remaining"]) == (1, 0)
    assert error["period_end"]
    assert (await read_library(client))["documents"] == []
    assert not list((tmp_path / "uploads").glob("*.png"))
    async with client.app.state.sessionmaker() as db:
        assert (await db.execute(select(Job))).scalars().all() == []
        assert (await db.execute(select(CreditEntry))).scalars().all() == []


async def test_an_upload_reserves_its_pages_and_its_entry_shows_the_settled_cost(
    make_client, documents
) -> None:
    client = make_client(free_credits_per_period=5)
    await ready_to_parse(client)
    created = await upload_png(client, documents)
    assert created.status_code == 201, created.text
    assert (await client.get("/api/me")).json()["credits_remaining"] == 4
    job_id = uuid.UUID(created.json()["job_id"])
    async with client.app.state.sessionmaker() as db:
        # Reserved, not settled.
        assert await ledger.credits_by_job(db, [job_id]) == {job_id: None}
        (entry,) = (await db.execute(select(CreditEntry))).scalars().all()
        assert (entry.kind, entry.amount, entry.state) == (ledger.PARSE, 1, ledger.RESERVED)
        await ledger.settle_parse(db, job_id, "succeeded")
        await db.commit()
    async with client.app.state.sessionmaker() as db:
        assert await ledger.credits_by_job(db, [job_id]) == {job_id: 1}


async def test_a_send_without_the_minimum_balance_is_refused(make_client) -> None:
    client = await chatting_client(make_client(free_credits_per_period=0))
    conversation_id = await new_conversation(client)
    sent = await send(client, conversation_id, text="hi")
    assert sent.status_code == 402, sent.text
    assert sent.json()["error"]["need"] == 1
    # Nothing of the send was kept.
    assert (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"] == []


async def test_a_send_into_a_full_workspace_is_refused(make_client) -> None:
    # One byte of storage: the seeded document alone fills it.
    client = await sign_in(make_client(free_storage_bytes=1))
    client.document_id = await seed_document(client, client.user_id)
    conversation_id = await new_conversation(client)
    sent = await send(client, conversation_id, text="hi")
    assert sent.status_code == 409, sent.text
    assert sent.json()["error"]["type"] == "storage_full"
    # Refused before anything was queued or charged.
    assert (await client.get(f"/api/conversations/{conversation_id}")).json()["messages"] == []
    assert (await client.get("/api/billing")).json()["period"]["spent"] == 0


@postgres_only
@respx.mock
@pytest.mark.parametrize("last_stop", ["end_turn", "tool_use"])
async def test_a_turn_that_runs_out_of_credits_finishes_within_the_allowance(
    make_client, monkeypatch, last_stop: str
) -> None:
    """docs/billing.md § A chat turn: once the balance is spent the turn runs at most
    `CHAT_OVERDRAFT_MAX_TOOL_CALLS` more tool calls, then its last call goes with no tool
    left to it; a last call that still asks for one ends the turn `out_of_credits`."""
    # Each stubbed call carries 510 tokens at US$1 per million, which at US$0.0001 a
    # credit is 6 credits: the first call spends the period's six to exactly zero, which
    # is already run out. Two calls are 11, rounded once for the turn.
    client = await chatting_client(
        make_client(
            free_credits_per_period=6,
            credit_usd="0.0001",
            chat_overdraft_max_tool_calls=2,
        )
    )
    conversation_id = await new_conversation(client)
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
    sent = await send(client, conversation_id, text="hi")
    assert sent.status_code == 202, sent.text
    await run_queued_turn(client)
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
    assert (billing["period"]["spent"], billing["period"]["remaining"]) == (11, 6 - 11)
    # The shell's figure is what a charge could still take — nothing — never the debt.
    assert billing["credits_available"] == 0
    assert (await client.get("/api/me")).json()["credits_remaining"] == 0


@respx.mock
async def test_a_call_stopped_mid_stream_is_charged_for_what_it_streamed(
    make_client, monkeypatch
) -> None:
    """docs/billing.md § A chat turn: a Stop pressed with the answer on screen, before its
    stream ends, cuts the call short; the call is still charged — the input its
    message_start reported and the output that streamed — though none of it is kept."""
    monkeypatch.setattr(chat_runner, "CANCEL_POLL_SECONDS", 0.05)
    client = await chatting_client(make_client(credit_usd="0.0001"))
    conversation_id = await new_conversation(client)

    async def answer_then_stop():
        yield sse(
            {
                "type": "message_start",
                "message": {
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 300, "cache_read_input_tokens": 200},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "A" * 3000},
            },
        )
        stopped = await client.post(
            f"/api/conversations/{conversation_id}/cancel", headers=CSRF_HEADERS
        )
        assert stopped.status_code == 204
        await asyncio.sleep(30)
        yield b""  # pragma: no cover - the runner drops the stream first

    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, content=answer_then_stop()))
    sent = await send(client, conversation_id, text="hi")
    assert sent.status_code == 202, sent.text
    async with asyncio.timeout(5):
        await run_queued_turn(client)
    # The one call of the test's one turn.
    async with client.app.state.sessionmaker() as db:
        [row] = (await db.execute(select(UsageLog))).scalars().all()
    # 3,000 bytes streamed are 1,000 output tokens; the cost no figure covered is unknown.
    assert (row.prompt_tokens, row.cache_read_tokens, row.completion_tokens) == (300, 200, 1000)
    assert (row.model, row.cost, row.cost_source) == (
        "claude-code/claude-opus-5-5",
        None,
        "unknown",
    )
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    # 1,500 tokens at US$1 per million and US$0.0001 a credit.
    assert (turn["status"], turn["credits"]) == ("cancelled", 15)
    assert [message["role"] for message in thread["messages"]] == ["user"]
    assert (await client.get("/api/billing")).json()["period"]["spent"] == 15


async def test_a_turn_queued_before_the_workspace_filled_fails_storage_full(make_client) -> None:
    client = await sign_in(make_client(free_storage_bytes=1 << 20))
    client.document_id = await seed_document(client, client.user_id)
    conversation_id = await new_conversation(client)
    sent = await send(client, conversation_id, text="hi")
    assert sent.status_code == 202, sent.text
    # The workspace fills between the send and the turn's start.
    (client.root / "big.bin").write_bytes(b"0" * (1 << 20))
    await run_queued_turn(client)
    [turn] = (await client.get(f"/api/conversations/{conversation_id}")).json()["turns"]
    # A token the panel words, never the backend's sentence.
    assert (turn["status"], turn["error"]) == ("failed", "storage_full")


async def test_a_turn_the_worker_fails_closes_its_entry(sessionmaker) -> None:
    """docs/billing.md § A chat turn: `fail_turn` — a shutdown, a restart, an escaped
    error — settles what the turn was charged, as the runner's own finish does."""
    settings = base_settings(free_credits_per_period=10, credit_usd="0.001")
    user_id = await make_user(sessionmaker)
    _, [turn_id], _ = await running_turns(sessionmaker, settings, user_id)
    async with sessionmaker() as db:
        assert await ledger.credits_by_turn(db, [turn_id]) == {turn_id: None}
        await fail_turn(db, turn_id, "worker restarted")
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        assert (turn.status, turn.error) == ("failed", "worker restarted")
        # Two credits' worth of tokens, consumed; shown as the turn's cost from now on.
        assert await ledger.credits_by_turn(db, [turn_id]) == {turn_id: 2}
    assert (await credit_period(sessionmaker, user_id)).spent == 2


@postgres_only
async def test_a_deleted_conversation_leaves_its_entries_in_the_ledger(sessionmaker) -> None:
    """docs/billing.md § The ledger: the record outlives the thread it was charged for;
    the period's `spent` and the entries keep agreeing."""
    settings = base_settings(free_credits_per_period=10, credit_usd="0.001")
    user_id = await make_user(sessionmaker)
    conversation_id, [turn_id], _ = await running_turns(sessionmaker, settings, user_id)
    async with sessionmaker() as db:
        await ledger.close_chat(db, turn_id)
        await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
        await db.commit()
    async with sessionmaker() as db:
        assert await db.get(ChatTurn, turn_id) is None
        [entry] = (await db.execute(select(CreditEntry))).scalars().all()
    assert (entry.kind, entry.state, entry.amount, entry.turn_id) == ("chat", "consumed", 2, None)
    assert (await credit_period(sessionmaker, user_id)).spent == 2


def close_slowly(monkeypatch, *, cancel: bool) -> list[str]:
    """Every stream of the runner closes slowly once it has ended: its `aclose` waits, and
    with `cancel` the task closing it is cancelled there, as a worker's shutdown would. The
    list says where the wait was interrupted."""
    interrupted: list[str] = []
    real = chat_runner.stream_messages

    class SlowToClose:
        def __init__(self, stream) -> None:
            self.stream = stream

        def __aiter__(self):
            return self.stream.__aiter__()

        async def aclose(self) -> None:
            await self.stream.aclose()
            if cancel:
                asyncio.current_task().cancel()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                interrupted.append("aclose")
                raise

    monkeypatch.setattr(
        chat_runner, "stream_messages", lambda *args, **kwargs: SlowToClose(real(*args, **kwargs))
    )
    return interrupted


@respx.mock
@pytest.mark.parametrize("ending", ["timeout", "shutdown"])
async def test_a_call_that_completed_is_charged_whatever_cancels_its_cleanup(
    make_client, monkeypatch, ending
) -> None:
    """docs/chat.md § Usage and credits: a call whose message_stop arrived gets its usage
    row and its settlement even when the turn's timeout, or a worker's shutdown, lands
    while its stream is still being closed — once, as every call does."""
    client = await chatting_client(make_client(credit_usd="0.0001", chat_turn_timeout_seconds=2))
    interrupted = close_slowly(monkeypatch, cancel=ending == "shutdown")
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, content=assistant_turn([{"type": "text", "text": "Done."}], "end_turn")
        )
    )
    conversation_id = await new_conversation(client)
    sent = await send(client, conversation_id, text="hi")
    assert sent.status_code == 202, sent.text
    if ending == "timeout":
        await run_queued_turn(client)
    else:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.create_task(run_queued_turn(client))
    assert interrupted == ["aclose"]
    async with client.app.state.sessionmaker() as db:
        [row] = (await db.execute(select(UsageLog))).scalars().all()
    assert (row.prompt_tokens, row.cache_read_tokens, row.completion_tokens) == (300, 200, 10)
    assert row.model == "claude-code/claude-opus-5-5"
    # 510 tokens at US$1 per million and US$0.0001 a credit, settled as the row was written.
    assert (await client.get("/api/billing")).json()["period"]["spent"] == 6
    if ending == "timeout":
        [turn] = (await client.get(f"/api/conversations/{conversation_id}")).json()["turns"]
        assert (turn["status"], turn["error"], turn["credits"]) == (
            "failed",
            "the turn timed out",
            6,
        )


@respx.mock
async def test_a_lost_usage_write_does_not_hide_the_timeout_that_cut_its_cleanup(
    make_client, monkeypatch
) -> None:
    """A completed call's usage write that fails while the turn's timeout is unwinding its
    cleanup is logged: the turn ends on its timeout, not on the database's error."""
    client = await chatting_client(make_client(chat_turn_timeout_seconds=2))
    interrupted = close_slowly(monkeypatch, cancel=False)

    async def lost(*args, **kwargs) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(chat_runner, "_record_usage", lost)
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(
            200, content=assistant_turn([{"type": "text", "text": "Done."}], "end_turn")
        )
    )
    conversation_id = await new_conversation(client)
    await send(client, conversation_id, text="hi")
    await run_queued_turn(client)
    assert interrupted == ["aclose"]
    [turn] = (await client.get(f"/api/conversations/{conversation_id}")).json()["turns"]
    assert (turn["status"], turn["error"]) == ("failed", "the turn timed out")
