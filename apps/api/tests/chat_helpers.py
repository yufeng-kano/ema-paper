"""What the chat tests share: a stubbed Messages stream, a signed-in reader with a chat
model and one document, and the calls a test makes on a conversation. No helper reaches
the network (docs/testing.md § Cost safety)."""

from __future__ import annotations

import asyncio
import json

import httpx
import respx
from httpx import AsyncClient

from sightread.db.models import AgentSetting, Conversation, Document, Folder, Result
from sightread.jobs import chat_runner
from sightread.jobs.queue import claim_next_turn
from tests.conftest import CSRF_HEADERS, KANO_BASE, sign_in
from tests.factories import job_row

MESSAGES_URL = f"{KANO_BASE}/anthropic/v1/messages"
# The seeded document's text, and where the shell sees it.
MARKDOWN = "<!-- page: 1 -->\n\n# Title\n\n| Policy | Return |\n| --- | --- |\n| Greedy | 3450 |\n"
DOC_PATH = "/workspace/files/Papers/paper.pdf"
SUBAGENT_PREAMBLE_START = "You are a subagent"


def sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def assistant_turn(
    blocks: list[dict], stop_reason: str, *, output_tokens: int = 10, model: str = "claude-opus-5-5"
) -> bytes:
    events: list[dict] = [
        {
            "type": "message_start",
            "message": {
                "model": model,
                "usage": {"input_tokens": 300, "cache_read_input_tokens": 200},
            },
        }
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            events += [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": block["text"]},
                },
                {"type": "content_block_stop", "index": index},
            ]
        elif block["type"] == "tool_use":
            events += [
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": block["id"],
                        "name": block["name"],
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block["input"]),
                    },
                },
                {"type": "content_block_stop", "index": index},
            ]
    events += [
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
        {"type": "message_stop"},
    ]
    return sse(*events)


async def seed_document(client: AsyncClient, user_id: int) -> int:
    """A finished parse filed in the library, with a result to materialize."""
    async with client.app.state.sessionmaker() as db:
        folder = Folder(user_id=user_id, parent_id=None, name="Papers")
        db.add(folder)
        await db.flush()
        job = job_row(user_id, size_bytes=10, sha256="b" * 64, model="claude-x")
        db.add(job)
        await db.flush()
        db.add(Result(job_id=job.id, markdown=MARKDOWN, pages=[], figures=[], errors=[], meta={}))
        document = Document(user_id=user_id, folder_id=folder.id, job_id=job.id, name="paper.pdf")
        db.add(document)
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
    transformations: list[dict] | None = None,
) -> bytes:
    """A Messages stream of text, thinking and tool_use blocks; `transformations` is the
    `input_transformations` its `message_start` reports."""
    message: dict = {"model": model, "usage": {"input_tokens": 100}}
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
            "usage": {"output_tokens": 5},
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
    """A round of one tool call. The tests make one call a round: SQLite takes the events'
    sequence numbers without the row lock PostgreSQL holds (chat_events.append), so calls
    finishing together would collide on the fallback database only."""
    return [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}], "tool_use"


async def chatting_client(client: AsyncClient, *, store_directly: bool = False) -> AsyncClient:
    """Signed in, a chat model chosen, one document. The model is saved through the route,
    which reads the catalog; `store_directly` writes the row as a save leaves it instead,
    so a test with its own catalog stubs it only once the turn runs."""
    user_id = (await sign_in(client)).user_id
    if store_directly:
        async with client.app.state.sessionmaker() as db:
            db.add(AgentSetting(user_id=user_id, agent="chat", model="claude-code/claude-opus-5-5"))
            await db.commit()
    else:
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


async def conversation_row(client: AsyncClient, conversation_id: int) -> Conversation:
    async with client.app.state.sessionmaker() as db:
        row = await db.get(Conversation, conversation_id)
        db.expunge(row)
        return row
