"""The chat agent's runner: one turn, one tool loop (docs/chat.md).

Shape of a turn: load the conversation and its document, render the system prompt, replay
the stored messages verbatim, stream the model's answer into events, run the tools it
asked for, append the results, and go again until it stops. Everything the model produced
is stored as it came, because the next turn replays it (docs/chat.md § History).
"""

from __future__ import annotations

import asyncio
import logging
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
from ..workspace.documents import DocumentPaths, materialize_document
from ..workspace.reconcile import reconcile_user
from . import chat_events, chat_tools

logger = logging.getLogger(__name__)

# Text deltas are batched into events: one row per token would drown the table.
TEXT_FLUSH_CHARS = 160
# An explicit cache marker every this many messages inside a turn (docs/chat.md).
CACHE_GUARD_EVERY = 15
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
    locale: str,
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
        model = setting.model if setting else None
        if not model:
            raise TurnFailed("No model chosen for the chat agent: pick one on the Agent page")
        if setting and setting.connection_id is not None:
            raise TurnFailed("The chat agent runs on Kano Proxy only")
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
        except ApiError as exc:
            raise TurnFailed(exc.message) from exc

    facts = {
        "document_path": str(chat_tools.VM_ROOT / paths.relative.as_posix()),
        "page_count": str(job.page_count or 1),
        "bbox_format": job.bbox_format,
        "parse_model": job.model,
        "locale": locale,
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


async def _history(ctx: TurnContext) -> list[dict]:
    async with ctx.sessionmaker() as db:
        rows = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == ctx.conversation.id)
                .order_by(Message.id)
            )
        ).scalars()
        return [{"role": row.role, "content": row.content} for row in rows]


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
        return status == "cancelled"


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
    async for item in stream_messages(
        ctx.connection,
        body,
        betas=chat_agent.BETAS,
        max_bytes=ctx.settings.upstream_response_max_bytes,
    ):
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
    """One tool_use block → its tool_result block (plus the events along the way)."""
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
            )
    elif name == "str_replace_based_edit_tool":
        outcome = await chat_tools.run_editor(
            ctx.paths.root, tool_input, author, max_chars=max_chars
        )
    elif name == "view_page":
        outcome = await chat_tools.view_page(
            ctx.paths, tool_input, page_count=ctx.job.page_count, render_slots=ctx.render_slots
        )
    elif name == "view_figure":
        outcome = await chat_tools.view_figure(ctx.paths, tool_input)
    else:
        outcome = chat_tools.ToolOutcome(f"unknown tool {name!r}", is_error=True)

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
        if completed.content:
            await _store(ctx, "assistant", completed.content)
            messages.append({"role": "assistant", "content": completed.content})
            since_marker += 1

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
        results = await asyncio.gather(
            *(_run_tool(ctx, block, completed.malformed_tool_uses) for block in tool_uses)
        )
        # Every result in one user message, in the order the calls were made
        # (docs/chat.md § Tools).
        await _store(ctx, "user", list(results))
        messages.append({"role": "user", "content": list(results)})
        since_marker += 1


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    turn_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
) -> None:
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        if turn is None:
            return
        turn.status = status
        turn.error = error
        turn.finished_at = utcnow()
        await db.execute(
            update(Conversation)
            .where(Conversation.id == turn.conversation_id)
            .values(updated_at=utcnow())
        )
        await db.commit()
        if status == "succeeded":
            await chat_events.emit(db, turn.conversation_id, turn.id, chat_events.TURN_DONE, {})
        else:
            await chat_events.emit(
                db,
                turn.conversation_id,
                turn.id,
                chat_events.TURN_ERROR,
                {"status": status, "error": error or ""},
            )


async def run_turn(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    turn_id: uuid.UUID,
    render_slots: asyncio.Semaphore,
    *,
    locale: str = "en",
) -> None:
    """Drive one claimed turn to a terminal state. Never raises for the turn's own faults."""
    try:
        ctx = await _load(sessionmaker, settings, turn_id, render_slots, locale)
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
