"""The chat agent's runner: one turn, one tool loop (docs/chat.md).

Shape of a turn: load the conversation and its document, render the system prompt, replay
the stored messages verbatim, stream the model's answer into events, run the tools it
asked for, append the results, and go again until it stops. Everything the model produced
is stored as it came, because the next turn replays it (docs/chat.md § History).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents import CHAT
from ..agents import chat as chat_agent
from ..config import Settings
from ..db.models import (
    EFFORT_DEFAULT,
    AgentSetting,
    ChatTurn,
    Conversation,
    Document,
    Job,
    Message,
    UsageLog,
    utcnow,
)
from ..errors import ApiError
from ..sandbox.client import SandboxClient
from ..upstream.anthropic import CompletedMessage, StreamEvent, stream_messages
from ..upstream.client import (
    Connection,
    PaymentRequired,
    RateLimited,
    UpstreamError,
    load_connection,
)
from ..workspace import git
from ..workspace.documents import DocumentPaths, ensure_within_quota, materialize_document
from ..workspace.reconcile import reconcile_user
from . import chat_events, chat_tools

logger = logging.getLogger(__name__)

# Text deltas are batched into events: one row per token would drown the table.
TEXT_FLUSH_CHARS = 160
# An explicit cache marker every this many messages inside a turn (docs/chat.md).
CACHE_GUARD_EVERY = 15
# How often a streaming turn looks for a cancel.
CANCEL_POLL_SECONDS = 2.0
# Retries of one upstream call on 429 / transient failures.
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 2.0


class TurnCancelled(Exception):
    pass


class TurnFailed(Exception):
    pass


@dataclass
class TurnContext:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    turn: ChatTurn
    conversation: Conversation
    document: Document
    job: Job
    paths: DocumentPaths
    connection: Connection
    model: str
    effort: str
    system: list[dict]
    tools: list[dict]
    sandbox: SandboxClient | None
    render_slots: asyncio.Semaphore
    tool_calls: int = 0


# --- loading -------------------------------------------------------------------------------


async def _load(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    turn_id: uuid.UUID,
    render_slots: asyncio.Semaphore,
) -> TurnContext:
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        if turn is None or turn.status != "running":
            raise TurnFailed("the turn is no longer running")
        conversation = await db.get(Conversation, turn.conversation_id)
        document = await db.get(Document, conversation.document_id) if conversation else None
        if conversation is None or document is None:
            raise TurnFailed("the conversation's document no longer exists")
        job = await db.get(Job, document.job_id)
        if job is None:
            raise TurnFailed("the document's parse no longer exists")
        setting = (
            await db.execute(
                select(AgentSetting).where(
                    AgentSetting.user_id == turn.user_id, AgentSetting.agent == CHAT.name
                )
            )
        ).scalar_one_or_none()
        # The conversation's model is the model of its first turn, whatever the Agent page
        # says now: the stored thinking blocks carry its signatures (docs/chat.md).
        model = conversation.model or (setting.model if setting else None)
        if not model:
            raise TurnFailed("No model chosen for the chat agent: pick one on the Agent page")
        if setting and setting.connection_id is not None:
            raise TurnFailed("The chat agent runs on Kano Proxy only")
        if conversation.model is None:
            conversation.model = model
            await db.commit()
        connection = await load_connection(db, settings, turn.user_id, None)
        if connection is None:
            raise TurnFailed("Kano Proxy is not connected: connect it on the Agent page")
        connection = Connection(
            ciphertext=connection.ciphertext,
            secret_key=connection.secret_key,
            base_url=settings.kano_proxy_anthropic_base,
            snapshot=connection.snapshot,
            builtin=True,
            report_cost=True,
        )
        preset = setting.prompt_preset if setting else None
        effort = setting.effort if setting and setting.effort else EFFORT_DEFAULT
        try:
            paths = await materialize_document(db, settings, document)
            # A full workspace runs no shell: what the turn writes has nowhere to go
            # (docs/workspace.md § Quota).
            await ensure_within_quota(settings, paths.root)
        except ApiError as exc:
            raise TurnFailed(exc.message) from exc

    facts = {
        "document_path": str(chat_tools.VM_ROOT / paths.relative.as_posix()),
        "page_count": str(job.page_count or 1),
        "bbox_format": job.bbox_format,
        "parse_model": job.model,
        "locale": turn.locale or "en",
    }
    system = [
        {
            "type": "text",
            "text": chat_agent.render_system_prompt(CHAT.default_prompt_template, facts),
        }
    ]
    if preset is not None and preset.text.strip():
        system.append({"type": "text", "text": preset.text})
    system[-1]["cache_control"] = {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL}

    sandbox = SandboxClient(settings) if settings.sandbox_configured else None
    tools = list(chat_agent.TOOLS if sandbox is not None else chat_agent.TOOLS_WITHOUT_BASH)
    return TurnContext(
        settings=settings,
        sessionmaker=sessionmaker,
        turn=turn,
        conversation=conversation,
        document=document,
        job=job,
        paths=paths,
        connection=connection,
        model=model,
        effort=effort,
        system=system,
        tools=tools,
        sandbox=sandbox,
        render_slots=render_slots,
    )


INTERRUPTED_TOOL_RESULT = "The turn was interrupted before this tool call finished."


def unanswered_tool_uses(messages: list[dict]) -> list[tuple[int, list[str]]]:
    """Where the history has tool calls without results: the index of each assistant
    message whose tool_use ids the next message does not all answer, with those ids."""
    gaps: list[tuple[int, list[str]]] = []
    for index, message in enumerate(messages):
        if message["role"] != "assistant" or not isinstance(message["content"], list):
            continue
        ids = [
            str(block.get("id"))
            for block in message["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not ids:
            continue
        answered: set[str] = set()
        if index + 1 < len(messages) and messages[index + 1]["role"] == "user":
            following = messages[index + 1]["content"]
            if isinstance(following, list):
                answered = {
                    str(block.get("tool_use_id"))
                    for block in following
                    if isinstance(block, dict) and block.get("type") == "tool_result"
                }
        missing = [tool_id for tool_id in ids if tool_id not in answered]
        if missing:
            gaps.append((index, missing))
    return gaps


def _interrupted_results(ids: list[str]) -> list[dict]:
    return [
        {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": INTERRUPTED_TOOL_RESULT,
            "is_error": True,
        }
        for tool_id in ids
    ]


def repaired(messages: list[dict]) -> list[dict]:
    """The history with every gap an interrupted turn left — a tool call without its
    result — closed by a user message of error results right after it. Computed on every
    replay rather than stored, so it lands where the API needs it whatever was stored
    after the gap; the same input gives the same output, and the cache prefix holds."""
    repaired_messages = list(messages)
    for index, missing in reversed(unanswered_tool_uses(messages)):
        repaired_messages.insert(
            index + 1, {"role": "user", "content": _interrupted_results(missing)}
        )
    return repaired_messages


async def _history(ctx: TurnContext) -> list[dict]:
    async with ctx.sessionmaker() as db:
        rows = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == ctx.conversation.id)
                .order_by(Message.id)
            )
        ).scalars()
        return repaired([{"role": row.role, "content": row.content} for row in rows])


def build_request(ctx: TurnContext, messages: list[dict], *, since_marker: int) -> dict:
    """The request body, with the cache guard on the latest tool results when a long turn
    has accrued enough positions since the last marker (docs/chat.md § Prompt caching)."""
    replay = [dict(message) for message in messages]
    if since_marker >= CACHE_GUARD_EVERY and replay and replay[-1]["role"] == "user":
        content = replay[-1]["content"]
        if isinstance(content, list) and content:
            marked = [dict(block) for block in content]
            marked[-1] = {
                **marked[-1],
                "cache_control": {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL},
            }
            replay[-1] = {"role": "user", "content": marked}
    return {
        "model": ctx.model,
        "max_tokens": ctx.settings.chat_max_output_tokens,
        "system": ctx.system,
        "tools": ctx.tools,
        "messages": replay,
        "thinking": {"type": "adaptive", "display": "updates"},
        "output_config": {"effort": ctx.effort},
        "fallbacks": "default",
        "cache_control": {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL},
    }


# --- events and persistence ------------------------------------------------------------------


async def _emit(ctx: TurnContext, event: str, data: dict) -> None:
    async with ctx.sessionmaker() as db:
        await chat_events.emit(db, ctx.conversation.id, ctx.turn.id, event, data)


async def _cancelled(ctx: TurnContext) -> bool:
    async with ctx.sessionmaker() as db:
        status = (
            await db.execute(select(ChatTurn.status).where(ChatTurn.id == ctx.turn.id))
        ).scalar_one_or_none()
        # A row that is gone (the conversation was deleted under us) is a stop as well.
        return status is None or status == "cancelled"


async def _store(ctx: TurnContext, role: str, content: list[dict]) -> int:
    async with ctx.sessionmaker() as db:
        row = Message(
            conversation_id=ctx.conversation.id,
            role=role,
            content=content,
            turn_id=ctx.turn.id,
        )
        db.add(row)
        await db.flush()
        message_id = row.id
        await db.commit()
        return message_id


async def _record_usage(ctx: TurnContext, completed: CompletedMessage) -> None:
    usage = completed.usage
    async with ctx.sessionmaker() as db:
        db.add(
            UsageLog(
                user_id=ctx.turn.user_id,
                turn_id=ctx.turn.id,
                agent=CHAT.name,
                model=completed.model or ctx.model,
                prompt_tokens=usage.input_tokens,
                completion_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost=usage.cost,
                cost_source=usage.cost_source,
            )
        )
        await db.commit()


# --- one model call ----------------------------------------------------------------------------


async def _stream_once(ctx: TurnContext, body: dict) -> CompletedMessage:
    """One streamed request, its deltas relayed as events as they arrive."""
    text_buffer: list[str] = []
    thinking_buffer: list[str] = []

    async def flush_text() -> None:
        if text_buffer:
            await _emit(ctx, chat_events.TEXT_DELTA, {"text": "".join(text_buffer)})
            text_buffer.clear()

    async def flush_thinking() -> None:
        if thinking_buffer:
            await _emit(ctx, chat_events.THINKING, {"text": "".join(thinking_buffer)})
            thinking_buffer.clear()

    completed: CompletedMessage | None = None
    last_cancel_check = time.monotonic()
    stream = stream_messages(
        ctx.connection,
        body,
        betas=chat_agent.BETAS,
        max_bytes=ctx.settings.upstream_response_max_bytes,
    )
    # The stream is read by its own task into a queue, so a cancel is noticed on a timer
    # whether or not the upstream is sending anything: a silent generation is dropped as
    # promptly as a chatty one (docs/chat.md § Conversations, turns, events).
    queue: asyncio.Queue[StreamEvent | CompletedMessage | Exception | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in stream:
                await queue.put(item)
                if isinstance(item, CompletedMessage):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - relayed to the consumer as is
            await queue.put(exc)
            return
        await queue.put(None)

    reader = asyncio.create_task(pump())
    try:
        while True:
            if time.monotonic() - last_cancel_check >= CANCEL_POLL_SECONDS:
                last_cancel_check = time.monotonic()
                if await _cancelled(ctx):
                    # A cancel must not wait out a ten-minute generation: the stream is
                    # dropped (closing the connection ends it), nothing of it is stored.
                    raise TurnCancelled()
            try:
                item = await asyncio.wait_for(queue.get(), timeout=CANCEL_POLL_SECONDS)
            except TimeoutError:
                continue
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            if isinstance(item, CompletedMessage):
                completed = item
                break
            event: StreamEvent = item
            if event.kind == "text_delta":
                text_buffer.append(event.text)
                if sum(len(part) for part in text_buffer) >= TEXT_FLUSH_CHARS:
                    await flush_text()
            elif event.kind == "thinking_delta":
                thinking_buffer.append(event.text)
            elif event.kind == "block_stop":
                await flush_text()
                await flush_thinking()
                block = event.block or {}
                if block.get("type") == "tool_use":
                    await _emit(
                        ctx,
                        chat_events.TOOL_USE,
                        {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input", {}),
                        },
                    )
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await stream.aclose()
    await flush_text()
    await flush_thinking()
    if completed is None:
        raise UpstreamError("The upstream closed the stream early", transient=True)
    return completed


async def _call_model(ctx: TurnContext, body: dict) -> CompletedMessage:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await _stream_once(ctx, body)
        except RateLimited as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(exc.retry_after or BACKOFF_BASE_SECONDS * (2**attempt))
        except UpstreamError as exc:
            if attempt == MAX_ATTEMPTS - 1 or not exc.transient:
                raise
            await asyncio.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
    raise RateLimited()


# --- tools -------------------------------------------------------------------------------------


async def _run_tool(ctx: TurnContext, block: dict, malformed: set[str]) -> dict:
    """One tool_use block → its tool_result block (plus the events along the way). A tool
    that raises answers an error result: the model corrects itself, the turn goes on."""
    try:
        return await _run_tool_unguarded(ctx, block, malformed)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - any failure is this tool's, never the turn's
        logger.exception("tool %s failed in turn %s", block.get("name"), ctx.turn.id)
        return await _report_tool(
            ctx,
            str(block.get("id")),
            block.get("name"),
            chat_tools.ToolOutcome("The tool failed unexpectedly; try another way.", is_error=True),
        )


async def _run_tool_unguarded(ctx: TurnContext, block: dict, malformed: set[str]) -> dict:
    name = block.get("name")
    tool_use_id = str(block.get("id"))
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    max_chars = ctx.settings.chat_tool_result_max_chars
    author = f"{git.AGENT_AUTHOR_PREFIX}{ctx.conversation.id}"

    if tool_use_id in malformed:
        outcome = chat_tools.ToolOutcome(
            "The tool input was not valid JSON; send it again.", is_error=True
        )
    elif name == "bash":
        if ctx.sandbox is None:
            outcome = chat_tools.ToolOutcome("No sandbox on this deployment.", is_error=True)
        else:
            outcome = await chat_tools.run_bash(
                ctx.sandbox,
                ctx.turn.user_id,
                ctx.conversation.session,
                tool_input,
                max_chars=max_chars,
                timeout_seconds=ctx.settings.chat_bash_timeout_seconds,
            )
            # What the shell changed is a revision of its own (docs/workspace.md).
            outcome = await chat_tools.commit_shell_changes(
                ctx.paths.root, outcome, tool_input, author, max_chars=max_chars
            )
    elif name == "str_replace_based_edit_tool":
        outcome = await chat_tools.run_editor(
            ctx.paths.root,
            tool_input,
            author,
            max_chars=max_chars,
            max_bytes=ctx.settings.content_max_bytes,
        )
    elif name == "view_page":
        outcome = await chat_tools.view_page(
            ctx.paths, tool_input, page_count=ctx.job.page_count, render_slots=ctx.render_slots
        )
    elif name == "view_figure":
        outcome = await chat_tools.view_figure(ctx.paths, tool_input)
    else:
        outcome = chat_tools.ToolOutcome(f"unknown tool {name!r}", is_error=True)
    return await _report_tool(ctx, tool_use_id, name, outcome)


async def _report_tool(
    ctx: TurnContext, tool_use_id: str, name: object, outcome: chat_tools.ToolOutcome
) -> dict:

    summary = outcome.content if isinstance(outcome.content, str) else "(image)"
    await _emit(
        ctx,
        chat_events.TOOL_RESULT,
        {"id": tool_use_id, "name": name, "is_error": outcome.is_error, "output": summary},
    )
    if outcome.edit is not None:
        await _emit(ctx, chat_events.EDIT, {**outcome.edit, "tool_use_id": tool_use_id})
    result: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": outcome.content}
    if outcome.is_error:
        result["is_error"] = True
    return result


MUTATING_TOOLS = frozenset({"bash", "str_replace_based_edit_tool"})


async def _run_tools(ctx: TurnContext, tool_uses: list[dict], malformed: set[str]) -> list[dict]:
    """The results of one round, in the order the calls were made. Reads run together;
    the tools that write the tree run one after another, because each commits whatever is
    dirty and two in flight would file one's changes under the other (docs/chat.md § Tools)."""
    results: dict[int, dict] = {}

    async def reads() -> None:
        reading = [
            (index, block)
            for index, block in enumerate(tool_uses)
            if block.get("name") not in MUTATING_TOOLS
        ]
        done = await asyncio.gather(*(_run_tool(ctx, block, malformed) for _, block in reading))
        for (index, _), result in zip(reading, done, strict=True):
            results[index] = result

    async def writes() -> None:
        for index, block in enumerate(tool_uses):
            if block.get("name") in MUTATING_TOOLS:
                results[index] = await _run_tool(ctx, block, malformed)

    await asyncio.gather(reads(), writes())
    return [results[index] for index in range(len(tool_uses))]


# --- the loop ---------------------------------------------------------------------------------


async def _loop(ctx: TurnContext) -> None:
    messages = await _history(ctx)
    since_marker = 0
    while True:
        if await _cancelled(ctx):
            raise TurnCancelled()
        body = build_request(ctx, messages, since_marker=since_marker)
        if since_marker >= CACHE_GUARD_EVERY:
            since_marker = 0
        completed = await _call_model(ctx, body)
        await _record_usage(ctx, completed)
        if await _cancelled(ctx):
            # Billed, but not kept: a cancelled turn leaves the history as it was.
            raise TurnCancelled()
        if completed.content:
            await _store(ctx, "assistant", completed.content)
            messages.append({"role": "assistant", "content": completed.content})
            # Positions are content blocks, not messages (docs/chat.md § Prompt caching).
            since_marker += len(completed.content)

        if completed.stop_reason == "refusal":
            raise TurnFailed("The model declined this request")
        tool_uses = [block for block in completed.content if block.get("type") == "tool_use"]
        if completed.stop_reason != "tool_use" or not tool_uses:
            return
        if await _cancelled(ctx):
            raise TurnCancelled()

        ctx.tool_calls += len(tool_uses)
        if ctx.tool_calls > ctx.settings.chat_max_tool_calls_per_turn:
            raise TurnFailed(
                f"The turn made more than {ctx.settings.chat_max_tool_calls_per_turn} tool calls"
            )
        results = await _run_tools(ctx, tool_uses, completed.malformed_tool_uses)
        # Every result in one user message, in the order the calls were made
        # (docs/chat.md § Tools).
        await _store(ctx, "user", list(results))
        messages.append({"role": "user", "content": list(results)})
        since_marker += len(results)


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    turn_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """The terminal state and its event in one commit, so a stream that sees the turn
    stop always finds the event that says how. A turn the user cancelled stays
    cancelled whatever the loop concluded."""
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        if turn is None:
            return
        if turn.status == "cancelled" or status == "cancelled":
            status, error = "cancelled", "cancelled"
        turn.status = status
        turn.error = error
        turn.finished_at = utcnow()
        await db.execute(
            update(Conversation)
            .where(Conversation.id == turn.conversation_id)
            .values(updated_at=utcnow())
        )
        if status == "succeeded":
            await chat_events.append(db, turn.conversation_id, turn.id, chat_events.TURN_DONE, {})
        else:
            await chat_events.append(
                db,
                turn.conversation_id,
                turn.id,
                chat_events.TURN_ERROR,
                {"status": status, "error": error or ""},
            )
        await db.commit()


async def run_turn(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    turn_id: uuid.UUID,
    render_slots: asyncio.Semaphore,
) -> None:
    """Drive one claimed turn to a terminal state. Never raises for the turn's own faults."""
    try:
        ctx = await _load(sessionmaker, settings, turn_id, render_slots)
    except TurnFailed as exc:
        await _finish(sessionmaker, turn_id, status="failed", error=str(exc))
        return

    await _emit(ctx, chat_events.TURN_START, {"model": ctx.model})
    try:
        async with asyncio.timeout(settings.chat_turn_timeout_seconds):
            await _loop(ctx)
    except TurnCancelled:
        await _finish(sessionmaker, turn_id, status="cancelled", error="cancelled")
    except TurnFailed as exc:
        await _finish(sessionmaker, turn_id, status="failed", error=str(exc))
    except TimeoutError:
        await _finish(sessionmaker, turn_id, status="failed", error="the turn timed out")
    except PaymentRequired:
        await _finish(
            sessionmaker, turn_id, status="failed", error="the upstream reported exhausted credits"
        )
    except RateLimited:
        await _finish(sessionmaker, turn_id, status="failed", error="the upstream rate-limited")
    except UpstreamError as exc:
        await _finish(sessionmaker, turn_id, status="failed", error=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("turn %s failed unexpectedly", turn_id)
        await _finish(sessionmaker, turn_id, status="failed", error="internal error")
    else:
        await _finish(sessionmaker, turn_id, status="succeeded")
    finally:
        # The shell may have moved things: bring the library in line (docs/workspace.md).
        try:
            async with sessionmaker() as db:
                await reconcile_user(db, settings, ctx.turn.user_id)
        except Exception:  # noqa: BLE001 - reconciliation is best effort after a turn
            logger.exception("reconcile after turn %s failed", turn_id)


def workspace_root(settings: Settings, user_id: int) -> Path:
    from ..workspace.layout import user_root

    return user_root(settings.workspaces_dir, user_id)
