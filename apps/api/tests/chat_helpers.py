"""What the chat tests share: a stubbed Messages stream, a signed-in reader with a chat
model and one document, the calls a test makes on a conversation, and a round's context
for `_run_tools`. No helper reaches the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import update

from sightread.agents import chat as chat_agent
from sightread.db.models import ChatTurn, Conversation
from sightread.jobs import chat_runner, chat_versions
from sightread.jobs.queue import claim_next_turn
from tests.conftest import CSRF_HEADERS, KANO_BASE, sign_in
from tests.factories import filed_parse

MESSAGES_URL = f"{KANO_BASE}/anthropic/v1/messages"
# The seeded document's text, and where the shell sees it.
MARKDOWN = "<!-- page: 1 -->\n\n# Title\n\n| Policy | Return |\n| --- | --- |\n| Greedy | 3450 |\n"
DOC_PATH = "/workspace/files/Papers/paper.pdf"
SUBAGENT_PREAMBLE_START = "You are a subagent"
# A model call that answers and ends the turn, and one that answers with a summary.
DONE = ([{"type": "text", "text": "Done."}], "end_turn")
SUMMARY = ([{"type": "text", "text": "## Requests\n\nThe reader asked for tables."}], "end_turn")


def sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def assistant_turn(
    blocks: list[dict], stop_reason: str, *, model: str = "claude-opus-5-5"
) -> bytes:
    """A Messages stream of text and tool_use blocks with the usage the credit tests count:
    300 input tokens, 200 read from the cache, 10 output."""
    return streamed(
        blocks,
        stop_reason,
        model=model,
        usage={"input_tokens": 300, "cache_read_input_tokens": 200},
        output_tokens=10,
    )


async def seed_document(client: AsyncClient, user_id: int, **fields) -> int:
    """A finished parse of `MARKDOWN` filed in the library, `paper.pdf` in `Papers` unless
    `fields` say otherwise (`factories.filed_parse`); its id."""
    async with client.app.state.sessionmaker() as db:
        document = await filed_parse(db, user_id, **{"markdown": MARKDOWN, **fields})
        await db.commit()
        return document.id


async def run_queued_turn(client: AsyncClient) -> None:
    settings = client.app.state.settings
    async with client.app.state.sessionmaker() as db:
        turn_id = await claim_next_turn(db, settings.max_chat_turns_per_user)
    assert turn_id is not None
    await chat_runner.run_turn(
        client.app.state.sessionmaker, settings, turn_id, asyncio.Semaphore(2)
    )


def streamed(
    blocks: list[dict],
    stop_reason: str,
    *,
    model: str = "claude-opus-5-5",
    usage: dict | None = None,
    output_tokens: int = 5,
    transformations: list[dict] | None = None,
) -> bytes:
    """A Messages stream of text, thinking and tool_use blocks. Its `message_start` reports
    `usage` (100 input tokens unless given) and `transformations` as its
    `input_transformations`; its `message_delta` reports `output_tokens`."""
    message: dict = {"model": model, "usage": usage or {"input_tokens": 100}}
    if transformations is not None:
        message["input_transformations"] = transformations
    events: list[dict] = [{"type": "message_start", "message": message}]
    for index, block in enumerate(blocks):
        if block["type"] == "thinking":
            start = {"type": "thinking", "thinking": ""}
            deltas = [
                {"type": "thinking_delta", "thinking": block["thinking"]},
                {"type": "signature_delta", "signature": "sig"},
            ]
        elif block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": block["text"]}]
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": json.dumps(block["input"])}]
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events += [{"type": "content_block_delta", "index": index, "delta": d} for d in deltas]
        events.append({"type": "content_block_stop", "index": index})
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


def stub_messages(main: list, sub: list | None = None) -> tuple[list[dict], list[dict]]:
    """Answer the turn's calls from `main` and a subagent's from `sub`, each `(blocks,
    stop_reason)` in order, on the model each request asked for; the requests are kept."""
    main_answers, sub_answers = iter(main), iter(sub or [])
    main_requests: list[dict] = []
    sub_requests: list[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["system"][0]["text"].startswith(SUBAGENT_PREAMBLE_START):
            sub_requests.append(body)
            blocks, stop = next(sub_answers)
        else:
            main_requests.append(body)
            blocks, stop = next(main_answers)
        return httpx.Response(200, content=streamed(blocks, stop, model=body["model"]))

    respx.post(MESSAGES_URL).mock(side_effect=answer)
    return main_requests, sub_requests


def one_call(call_id: str, name: str, tool_input: dict) -> tuple[list[dict], str]:
    """A round of one tool call."""
    return [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}], "tool_use"


async def chatting_client(client: AsyncClient) -> AsyncClient:
    """Signed in, a chat model chosen, one document. The model is saved through the route,
    which reads the catalog."""
    user_id = (await sign_in(client)).user_id
    chosen = await client.put(
        "/api/agents/chat/settings",
        json={"model": "claude-code/claude-opus-5-5"},
        headers=CSRF_HEADERS,
    )
    assert chosen.status_code == 200, chosen.text
    client.document_id = await seed_document(client, user_id)
    return client


async def new_conversation(client: AsyncClient) -> int:
    created = await client.post(
        f"/api/library/documents/{client.document_id}/conversations", headers=CSRF_HEADERS
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


async def send(client: AsyncClient, conversation_id: int, **body) -> httpx.Response:
    return await client.post(
        f"/api/conversations/{conversation_id}/messages", json=body, headers=CSRF_HEADERS
    )


async def replayed_events(client: AsyncClient, conversation_id: int) -> list[tuple[str, dict]]:
    async with client.stream(
        "GET", f"/api/conversations/{conversation_id}/events?after=0"
    ) as stream:
        body = (await stream.aread()).decode()
    events: list[tuple[str, dict]] = []
    name = None
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: ") and name is not None:
            events.append((name, json.loads(line[len("data: ") :])))
            name = None
    return events


async def tool_results(client: AsyncClient, conversation_id: int) -> list[dict]:
    return [
        data
        for name, data in await replayed_events(client, conversation_id)
        if name == "tool_result"
    ]


async def conversation_row(client: AsyncClient, conversation_id: int) -> Conversation:
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Conversation, conversation_id)
        db.expunge(row)
        return row


async def detail(client: AsyncClient, conversation_id: int) -> dict:
    response = await client.get(f"/api/conversations/{conversation_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def turns_of(client: AsyncClient, conversation_id: int) -> list[ChatTurn]:
    async with client.app.state.sessionmaker() as db:
        rows = await chat_versions.conversation_turns(db, conversation_id)
        for row in rows:
            db.expunge(row)
        return rows


async def turn_credits(client: AsyncClient, conversation_id: int) -> tuple[str, int | None]:
    thread = (await client.get(f"/api/conversations/{conversation_id}")).json()
    [turn] = thread["turns"]
    return turn["status"], turn["credits"]


async def set_clock(client: AsyncClient, conversation_id: int, *, warm_for: float, tokens: int):
    async with client.app.state.sessionmaker() as db:
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                cache_warm_until=datetime.now(UTC) + timedelta(seconds=warm_for),
                context_tokens=tokens,
                # As a turn's own writes leave it: the test reads it back unmoved.
                updated_at=Conversation.updated_at,
            )
        )
        await db.commit()


def texts(content: list[dict]) -> list[str]:
    return [block["text"] for block in content if block.get("type") == "text"]


async def edit(
    client: AsyncClient, conversation_id: int, message_id: int, **body
) -> httpx.Response:
    return await client.post(
        f"/api/conversations/{conversation_id}/messages/{message_id}/edit",
        json=body,
        headers=CSRF_HEADERS,
    )


async def attach(
    client: AsyncClient, name: str, data: bytes, media_type: str = "application/octet-stream"
) -> httpx.Response:
    return await client.post(
        "/api/attachments", files={"file": (name, data, media_type)}, headers=CSRF_HEADERS
    )


async def keep_file(
    client: AsyncClient, name: str, data: bytes, media_type: str, folder_id: int | None
) -> dict:
    """A Files upload kept as a file: `auto_convert` is off by default (docs/agents.md)."""
    response = await client.post(
        "/api/library/documents",
        files={"file": (name, data, media_type)},
        data={} if folder_id is None else {"folder_id": str(folder_id)},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "file"
    return response.json()


async def papers_folder(client: AsyncClient) -> int:
    """The `Papers` folder the seeded document sits in."""
    library = (await client.get("/api/library")).json()
    return next(folder["id"] for folder in library["folders"] if folder["name"] == "Papers")


def write_file(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def agents_part(request: dict) -> str | None:
    """The AGENTS.md part of a Messages request's system prompt, if it has one."""
    return next(
        (
            part["text"]
            for part in request["system"]
            if part["text"].startswith(chat_agent.AGENTS_FILE_INTRO)
        ),
        None,
    )


def round_ctx(
    *,
    round_budget: int = 32 << 20,
    turn_budget: int = 128 << 20,
    settings: dict | None = None,
    **fields,
) -> SimpleNamespace:
    """What `_run_tools` reads from its context, made fresh per call: the settings it reads,
    with `settings` over them; the turn's image budgets for the round and the whole turn;
    the document a page renders from; the conversation's shell; and the turn's writes in
    flight with the gate that starts them one at a time. `fields` replace any of these or
    add more."""
    return SimpleNamespace(
        **{
            "settings": SimpleNamespace(
                **{
                    "chat_image_round_max_bytes": round_budget,
                    "chat_image_turn_max_bytes": turn_budget,
                    "upload_max_bytes": 1,
                    "chat_page_image_max_pixels": 1,
                    "chat_tool_result_max_chars": 1000,
                    "chat_bash_timeout_seconds": 1,
                    "sandbox_start_timeout_seconds": 1,
                    "sandbox_exec_grace_seconds": 1,
                    "chat_writer_grace_seconds": 1,
                    "chat_attachment_image_long_edge": 1568,
                    "content_max_bytes": 1000,
                    **(settings or {}),
                }
            ),
            "sessionmaker": None,
            "turn": SimpleNamespace(id=uuid.uuid4(), user_id=1),
            "images": chat_runner.ImageBudget(
                round_budget, turn_budget, chat_runner.IMAGE_TURN_BUDGET_SPENT
            ),
            "credits_exhausted": False,
            "round_admission": lambda calls: lambda index: True,
            "tools": [{"name": "view_page"}, {"name": "view_figure"}],
            "paths": None,
            "document": SimpleNamespace(id=1),
            "job": SimpleNamespace(page_count=12, kind="pdf"),
            "render_slots": None,
            "decode_slot": asyncio.Semaphore(1),
            "conversation": SimpleNamespace(id=1, session="s"),
            "sandbox": None,
            "writers": {},
            "write_gate": asyncio.Lock(),
            **fields,
        }
    )


def record_round(monkeypatch) -> list[tuple[str, dict]]:
    """The events a round emits, recorded instead of written; the turn is never cancelled."""
    emitted: list[tuple[str, dict]] = []

    async def record(ctx, event, data):
        emitted.append((event, data))

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_runner, "_emit", record)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)
    return emitted
