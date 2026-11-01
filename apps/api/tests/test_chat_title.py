"""`/rename` with no name: the summary model writes the conversation's title (docs/chat.md
§ Conversations, turns, events; docs/api.md; docs/billing.md § A chat turn). Every model
call is answered by a stubbed Messages stream; no test reaches the network
(docs/testing.md § Cost safety)."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import func, select, update

from sightread.agents import summary as summary_defs
from sightread.billing import ledger
from sightread.config import Settings
from sightread.db.models import (
    AgentSetting,
    Conversation,
    CreditEntry,
    CreditPeriod,
    UsageLog,
    utcnow,
)
from tests.conftest import CSRF_HEADERS
from tests.test_chat import MESSAGES_URL, _run_queued_turn, assistant_turn
from tests.test_chat_extensions import chatting_client, new_conversation, send

SUMMARY_MODEL = "codex/gpt-5.5"


def stub_calls(turn_answers: list[str], title_answers: list) -> list[dict]:
    """Answer a turn's calls with `turn_answers` and a title's — the one request whose
    system prompt is a string — with `title_answers`, text or a whole response; the title
    requests are kept."""
    turns, titles = iter(turn_answers), iter(title_answers)
    title_requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if isinstance(body["system"], str):
            title_requests.append(body)
            reply = next(titles)
            if isinstance(reply, httpx.Response):
                return reply
        else:
            reply = next(turns)
        blocks = [{"type": "text", "text": reply}]
        return httpx.Response(200, content=assistant_turn(blocks, "end_turn", model=body["model"]))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    return title_requests


async def talk(client: AsyncClient, conversation_id: int, **body) -> None:
    sent = await send(client, conversation_id, **body)
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)


async def conversation_row(client: AsyncClient, conversation_id: int) -> Conversation:
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Conversation, conversation_id)
        db.expunge(row)
        return row


async def title_entries(client: AsyncClient) -> list[CreditEntry]:
    async with client.app.state.sessionmaker() as db:
        rows = (
            (
                await db.execute(
                    select(CreditEntry).where(
                        CreditEntry.kind == "chat", CreditEntry.turn_id.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            db.expunge(row)
        return list(rows)


async def name_it(client: AsyncClient, conversation_id: int, **body) -> httpx.Response:
    return await client.post(
        f"/api/conversations/{conversation_id}/title", json=body, headers=CSRF_HEADERS
    )


@pytest.fixture
async def chat_client(make_client, tmp_path) -> AsyncClient:
    return await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))


@respx.mock
async def test_a_title_is_written_from_the_first_and_the_latest_exchange(chat_client) -> None:
    """One call on the conversation's model with no tools and no thinking, told the
    reader's language, reading the document's name and the first and latest exchanges —
    never the ones between — and its answer, cleaned, is the title; the list's order does
    not move, and the call is billed on an entry of its own (docs/chat.md § Conversations,
    turns, events; docs/billing.md § A chat turn)."""
    client = chat_client
    requests = stub_calls(
        ["The translation is below.", "MIDDLE ANSWER", "Section 3 compares UniVLA."],
        ["「UniVLA 論文翻譯與比較」。"],
    )
    conversation_id = await new_conversation(client)
    await talk(client, conversation_id, text="繁體中文", command="translate", locale="zh-TW")
    await talk(client, conversation_id, text="MIDDLE QUESTION", locale="zh-TW")
    await talk(client, conversation_id, text="How does section 3 compare?", locale="zh-TW")
    before = await conversation_row(client, conversation_id)
    assert before.title == "/translate 繁體中文"

    named = await name_it(client, conversation_id, locale="zh-TW")
    assert named.status_code == 200, named.text
    assert named.json()["title"] == "UniVLA 論文翻譯與比較"
    after = await conversation_row(client, conversation_id)
    assert after.title == "UniVLA 論文翻譯與比較"
    assert after.updated_at == before.updated_at

    [request] = requests
    assert request["model"] == "claude-code/claude-opus-5-5"
    assert "tools" not in request and "thinking" not in request
    assert "zh-TW" in request["system"]
    [message] = request["messages"]
    assert "<document>paper.pdf</document>" in message["content"]
    assert "/translate 繁體中文" in message["content"]
    assert "The translation is below." in message["content"]
    assert "How does section 3 compare?" in message["content"]
    assert "Section 3 compares UniVLA." in message["content"]
    assert "MIDDLE" not in message["content"]

    [entry] = await title_entries(client)
    assert (entry.state, entry.model) == ("consumed", "claude-code/claude-opus-5-5")
    assert entry.amount >= 1
    async with client.app.state.sessionmaker() as db:
        rows = (
            await db.execute(
                select(UsageLog.agent, UsageLog.model).where(UsageLog.turn_id.is_(None))
            )
        ).all()
        assert rows == [("chat", "claude-code/claude-opus-5-5")]


@respx.mock
async def test_the_summary_model_and_the_chosen_answer_language_are_used(
    make_client, tmp_path
) -> None:
    """`SUMMARY_MODEL` runs the call when it names one, and the answer language the reader
    chose wins over the request's locale; a conversation of one turn is read once."""
    client = await chatting_client(
        make_client(workspaces_dir=str(tmp_path / "workspaces"), summary_model=SUMMARY_MODEL)
    )
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(AgentSetting)
            .where(AgentSetting.user_id == client.user_id, AgentSetting.agent == "chat")
            .values(answer_language="ja")
        )
        await db.commit()
    requests = stub_calls(["Hello."], ["Greeting the reader"])
    conversation_id = await new_conversation(client)
    await talk(client, conversation_id, text="Hi", locale="en")

    named = await name_it(client, conversation_id, locale="en")
    assert named.status_code == 200, named.text
    [request] = requests
    assert request["model"] == SUMMARY_MODEL
    assert "whose tag is ja" in request["system"]
    assert request["messages"][0]["content"].count("<exchange>") == 1
    [entry] = await title_entries(client)
    assert entry.model == SUMMARY_MODEL


@respx.mock
async def test_nothing_to_name_and_no_credits_are_refused_before_any_call(chat_client) -> None:
    """A conversation with no exchange has nothing to name (400 `nothing_to_rename`), and
    a balance under `CHAT_MIN_CREDITS` refuses the call (402) — neither reaches the model
    nor leaves an entry (docs/api.md)."""
    client = chat_client
    requests = stub_calls(["Hello."], [])
    empty = await new_conversation(client)
    refused = await name_it(client, empty)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["type"] == "nothing_to_rename"

    conversation_id = await new_conversation(client)
    await talk(client, conversation_id, text="Hi")
    async with client.app.state.sessionmaker() as db:
        await db.execute(update(CreditPeriod).values(spent=CreditPeriod.granted))
        await db.commit()
    poor = await name_it(client, conversation_id)
    assert poor.status_code == 402, poor.text
    assert poor.json()["error"]["type"] == "insufficient_credits"
    assert requests == []
    assert await title_entries(client) == []
    assert (await conversation_row(client, conversation_id)).title == "Hi"


@respx.mock
async def test_a_failed_call_changes_nothing_and_an_empty_answer_is_still_billed(
    chat_client,
) -> None:
    """An upstream that answers nothing is a 502, the title as it was and the entry
    released at nothing; an answer that holds no title is a 502 too, but the tokens it
    used are charged (docs/billing.md § A chat turn)."""
    client = chat_client
    requests = stub_calls(["Hello."], [httpx.Response(500), "  \n"])
    conversation_id = await new_conversation(client)
    await talk(client, conversation_id, text="Hi")

    failed = await name_it(client, conversation_id)
    assert failed.status_code == 502, failed.text
    [released] = await title_entries(client)
    assert (released.state, released.amount) == ("released", 0)

    empty = await name_it(client, conversation_id)
    assert empty.status_code == 502, empty.text
    assert len(requests) == 2
    entries = await title_entries(client)
    charged = next(entry for entry in entries if entry.id != released.id)
    assert charged.state == "consumed" and charged.amount >= 1
    async with client.app.state.sessionmaker() as db:
        rows = (
            await db.execute(
                select(func.count()).select_from(UsageLog).where(UsageLog.turn_id.is_(None))
            )
        ).scalar_one()
        assert rows == 1
    assert (await conversation_row(client, conversation_id)).title == "Hi"


@respx.mock
async def test_one_title_at_a_time_and_a_crash_left_entry_stops_counting(chat_client) -> None:
    """A title reserves nothing, so a reader has one in flight at a time: another asked
    for meanwhile is a 429 before any call, or calls made side by side would all pass the
    balance check. An entry a crash left reserved stops counting after twice the call's
    bound (docs/billing.md § A chat turn)."""
    client = chat_client
    requests = stub_calls(["Hello."], ["Greeting"])
    conversation_id = await new_conversation(client)
    await talk(client, conversation_id, text="Hi")
    settings = client.app.state.settings
    async with client.app.state.sessionmaker() as db:
        in_flight = await ledger.reserve(
            db,
            settings,
            user_id=client.user_id,
            kind=ledger.CHAT,
            amount=0,
            model="claude-code/claude-opus-5-5",
        )
        await db.commit()
        entry_id = in_flight.id

    busy = await name_it(client, conversation_id)
    assert busy.status_code == 429, busy.text
    assert busy.json()["error"]["type"] == "rate_limit"
    assert busy.headers["Retry-After"] == str(settings.chat_retry_after_seconds)
    assert requests == []
    assert len(await title_entries(client)) == 1

    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(CreditEntry)
            .where(CreditEntry.id == entry_id)
            .values(
                created_at=utcnow() - timedelta(seconds=2 * settings.summary_timeout_seconds + 1)
            )
        )
        await db.commit()
    named = await name_it(client, conversation_id)
    assert named.status_code == 200, named.text
    assert named.json()["title"] == "Greeting"


@respx.mock
async def test_a_conversation_model_nobody_prices_is_never_run_for_a_title(chat_client) -> None:
    """With no summary model, the conversation's own runs the call; one the price table
    no longer carries is a 503 before any call, as a turn refuses it (docs/billing.md
    § Models)."""
    client = chat_client
    requests = stub_calls(["Hello."], [])
    conversation_id = await new_conversation(client)
    await talk(client, conversation_id, text="Hi")
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(model="vendor/unpriced")
        )
        await db.commit()
    refused = await name_it(client, conversation_id)
    assert refused.status_code == 503, refused.text
    assert requests == []
    assert await title_entries(client) == []


def test_a_summary_model_must_be_priced() -> None:
    """A model the deployment runs on its own is priced, or it does not start."""
    with pytest.raises(ValueError, match="MODEL_PRICES_JSON prices none of: vendor/small"):
        Settings(summary_model="vendor/small", model_prices_json="{}")


@pytest.mark.parametrize(
    ("answer", "title"),
    [
        ("UniVLA translation", "UniVLA translation"),
        ('"Comparing VLA policies."', "Comparing VLA policies"),
        ("\n\n『潛在動作的翻譯』。\nBecause the reader asked…", "潛在動作的翻譯"),
        ("**Bold title**", "Bold title"),
        ("x" * 200, "x" * summary_defs.TITLE_MAX_CHARS),
        ('""', '""'),
        ("   ", ""),
    ],
)
def test_an_answer_is_cleaned_into_a_title(answer: str, title: str) -> None:
    """The first non-empty line, its quotes or brackets and its full stop taken off, cut
    at the cap; a wrapper with nothing inside is left as it is."""
    assert summary_defs.clean_title(answer) == title
