"""The chat agent's runner: one turn, one tool loop (docs/chat.md).

Shape of a turn: load the conversation and its document, render the system prompt, replay
the stored messages verbatim, stream the model's answer into events, run the tools it
asked for, append the results, and go again until it stops. Everything the model produced
is stored as it came, because the next turn replays it (docs/chat.md § History).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
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
from ..sandbox.client import SandboxClient, SandboxUnavailable
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
from ..workspace.locks import clear_writing, mark_writing, shell_lock, workspace_lock
from ..workspace.reconcile import reconcile_locked, reconcile_user
from . import chat_events, chat_tools
from .queue import HISTORY_FULL, history_bytes

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
    # Encoded image bytes the turn's reads have returned so far, every round together
    # (docs/chat.md § Tools).
    image_spent: int = 0
    # The mutating tool call in flight, if any: finished before the turn is, whatever
    # ended the turn.
    writer: asyncio.Future | None = None
    # Which tool the writer runs: a shell command past its grace is killed in the VM.
    writer_tool: str | None = None
    # One image decoded at a time per turn: a round of page reads never holds a raster
    # per read (docs/chat.md § Tools).
    decode_slot: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


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
        # The history is replayed whole on every call, images included: past the cap
        # a turn would load and serialize hundreds of megabytes in the worker before
        # the upstream could refuse the context (docs/chat.md § History). The send is
        # refused past it too; this catches a turn queued before the history crossed it.
        if await history_bytes(db, conversation.id) > settings.chat_history_max_bytes:
            raise TurnFailed(HISTORY_FULL)
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
        "network": chat_agent.network_note(settings.sandbox_network, settings.sandbox_index_url),
    }
    sandbox = SandboxClient(settings) if settings.sandbox_configured else None
    tools = list(chat_agent.TOOLS if sandbox is not None else chat_agent.TOOLS_WITHOUT_BASH)
    system = [
        {
            "type": "text",
            # The tool list as the turn has it: no `bash` line without a runner.
            "text": chat_agent.render_system_prompt(
                CHAT.default_prompt_template, facts, tools=[tool["name"] for tool in tools]
            ),
        }
    ]
    if preset is not None and preset.text.strip():
        system.append({"type": "text", "text": preset.text})
    system[-1]["cache_control"] = {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL}

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
        return repaired([{"role": row.role, "content": _api_blocks(row.content)} for row in rows])


def _api_blocks(content: object) -> object:
    """The stored content as the API takes it: the `context` marker a stored block
    carries for the web app (docs/chat.md § Prompt caching) is ours, not the API's."""
    if not isinstance(content, list):
        return content
    return [
        {key: value for key, value in block.items() if key != "context"}
        if isinstance(block, dict)
        else block
        for block in content
    ]


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
        row = (
            await db.execute(
                select(ChatTurn.status, ChatTurn.finished_at).where(ChatTurn.id == ctx.turn.id)
            )
        ).one_or_none()
        # A row that is gone (the conversation was deleted under us) is a stop as well,
        # and so is a turn already finished: the worker that took the lock this one lost
        # has finished it as an orphan (docs/jobs.md § Queue).
        return row is None or row.status == "cancelled" or row.finished_at is not None


async def _still_ours(db: AsyncSession, ctx: TurnContext) -> None:
    """Lock the conversation — the order the cancel route and the orphan recovery take —
    and check, in the transaction about to write history, that the turn is still this
    worker's to write: not cancelled, and not finished by the worker that took the lock
    this one lost (docs/jobs.md § Queue). Raises `TurnCancelled` otherwise, so nothing
    is written and the loop stops."""
    await db.execute(
        select(Conversation.id).where(Conversation.id == ctx.conversation.id).with_for_update()
    )
    turn = (
        await db.execute(
            select(ChatTurn.status, ChatTurn.finished_at).where(ChatTurn.id == ctx.turn.id)
        )
    ).one_or_none()
    if turn is None or turn.status == "cancelled" or turn.finished_at is not None:
        raise TurnCancelled()


async def _store(
    ctx: TurnContext, role: str, content: list[dict], *, model: str | None = None
) -> int:
    """Append a message to the history — and, with `model`, move the conversation to the
    model that answered first — in one transaction with the check that the turn may
    still take history, so a finished or cancelled turn gets none (docs/chat.md
    § History is append-only)."""
    async with ctx.sessionmaker() as db:
        await _still_ours(db, ctx)
        if model is not None:
            await db.execute(
                update(Conversation)
                .where(Conversation.id == ctx.conversation.id)
                .values(model=model)
            )
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
    if model is not None:
        ctx.model = model
        ctx.conversation.model = model
    return message_id


async def _follow_model(ctx: TurnContext, model: str) -> None:
    """The conversation follows the model that answered, with no message to store it
    with (an answer cut to nothing): fenced like a stored message."""
    async with ctx.sessionmaker() as db:
        await _still_ours(db, ctx)
        await db.execute(
            update(Conversation).where(Conversation.id == ctx.conversation.id).values(model=model)
        )
        await db.commit()
    ctx.model = model
    ctx.conversation.model = model


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


async def _record_usage_settled(ctx: TurnContext, completed: CompletedMessage) -> None:
    """The paid call reaches `usage_log` whether or not the turn is cancelled meanwhile:
    the write runs as a task of its own, and a cancel that lands during it waits for it
    to settle before it goes on — a cancelled or timed-out turn is billed all the same
    (docs/chat.md § Conversations, turns, events)."""
    recording = asyncio.ensure_future(_record_usage(ctx, completed))
    try:
        await asyncio.shield(recording)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(recording)
        raise


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
        line_max_bytes=ctx.settings.upstream_sse_line_max_bytes,
        blocks_max=ctx.settings.upstream_content_blocks_max,
        timeout=ctx.settings.chat_stream_timeout_seconds,
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
    # Kano Proxy names a model `provider/model`; the upstream answers with the bare id. Read
    # with the requested provider, the same model is not a fallback, and a real fallback is
    # followed under a name Kano Proxy can route (docs/chat.md § Request).
    provider = str(body["model"]).rpartition("/")[0]
    if provider and completed.model and "/" not in completed.model:
        completed.model = f"{provider}/{completed.model}"
    return completed


async def _backoff(ctx: TurnContext, seconds: float) -> None:
    """Wait before a retry, looking for a cancel every poll interval: a long
    `Retry-After` must not hold a stopped turn open."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, CANCEL_POLL_SECONDS))
        if await _cancelled(ctx):
            raise TurnCancelled()


async def _call_model(ctx: TurnContext, body: dict) -> CompletedMessage:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return await _stream_once(ctx, body)
        except RateLimited as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            await _backoff(ctx, exc.retry_after or BACKOFF_BASE_SECONDS * (2**attempt))
            # What the failed attempt streamed is already out: the thread starts over
            # from here (docs/chat.md § Conversations, turns, events).
            await _emit(ctx, chat_events.RETRY, {"attempt": attempt + 2})
        except UpstreamError as exc:
            if attempt == MAX_ATTEMPTS - 1 or not exc.transient:
                raise
            await _backoff(ctx, BACKOFF_BASE_SECONDS * (2**attempt))
            await _emit(ctx, chat_events.RETRY, {"attempt": attempt + 2})
    raise RateLimited()


# --- tools -------------------------------------------------------------------------------------


Admit = Callable[[chat_tools.ToolOutcome], chat_tools.ToolOutcome]


async def _run_tool(
    ctx: TurnContext, block: dict, malformed: set[str], admit: Admit | None = None
) -> dict:
    """One tool_use block → its tool_result block (plus the events along the way). A tool
    that raises answers an error result: the model corrects itself, the turn goes on.
    `admit` sees the outcome before it is reported and may replace it."""
    try:
        return await _run_tool_unguarded(ctx, block, malformed, admit)
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


async def _run_tool_unguarded(
    ctx: TurnContext, block: dict, malformed: set[str], admit: Admit | None = None
) -> dict:
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
            # One shell at a time per user, through the command *and* its commit: two
            # conversations' shells must never have the tree dirty together, or one's
            # revision would carry the other's half-written files. The workspace lock
            # is held across the command as well: a save or another conversation's
            # editor landing on a file the command is writing would otherwise read and
            # commit a half-written file (docs/chat.md § Tools).
            async with (
                shell_lock(ctx.paths.root),
                workspace_lock(ctx.paths.root, recovering=True),
            ):
                # Marked for as long as the command may write: a worker that dies here
                # leaves the mark, and so does a command the runner could not confirm
                # over, and every host write is refused until a shell restart has
                # confirmed the command dead and the tree is recorded — here, or by
                # the recovery the next worker runs at start (docs/workspace.md
                # § Revisions). `recovering`: this path is that recovery's peer.
                mark_writing(ctx.paths.root, ctx.conversation.id, ctx.conversation.session)
                unsettled = False
                try:
                    outcome = await _run_shell_command(ctx, tool_input, author, max_chars)
                    unsettled = not outcome.settled
                finally:
                    if not unsettled:
                        clear_writing(ctx.paths.root)
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
            ctx.paths,
            tool_input,
            document_id=ctx.document.id,
            page_count=ctx.job.page_count,
            render_slots=ctx.render_slots,
            decode_slot=ctx.decode_slot,
            max_bytes=ctx.settings.upload_max_bytes,
            max_pixels=ctx.settings.chat_page_image_max_pixels,
            kind=ctx.job.kind,
        )
    elif name == "view_figure":
        # Bounded by the round's image budget, as a page is: `CONTENT_MAX_BYTES` is the
        # cap on `doc.md` and `notes.md`, and a legitimate crop can be larger.
        outcome = await chat_tools.view_figure(
            ctx.paths, tool_input, max_bytes=ctx.settings.chat_image_round_max_bytes
        )
    else:
        outcome = chat_tools.ToolOutcome(f"unknown tool {name!r}", is_error=True)
    if admit is not None:
        outcome = admit(outcome)
    return await _report_tool(ctx, tool_use_id, name, outcome)


async def _run_shell_command(
    ctx: TurnContext, tool_input: dict, author: str, max_chars: int
) -> chat_tools.ToolOutcome:
    """One shell command under the shell and workspace locks the caller holds: the
    command, its revision, and the library following the tree (docs/chat.md § Tools)."""
    outcome = await chat_tools.run_bash(
        ctx.sandbox,
        ctx.turn.user_id,
        ctx.conversation.session,
        tool_input,
        max_chars=max_chars,
        timeout_seconds=ctx.settings.chat_bash_timeout_seconds,
    )
    if not outcome.settled:
        # The runner could not say the command is over. A restart of its shell kills
        # the command inside the VM, and answers only when that is confirmed: then the
        # tree is the command's to record. Otherwise nothing is committed — a process
        # may still write — and the writing marker stays for the recovery that
        # confirms it later (docs/chat.md § Tools).
        try:
            await ctx.sandbox.restart(ctx.turn.user_id, ctx.conversation.session)
        except SandboxUnavailable:
            return chat_tools.ToolOutcome(
                f"{outcome.content}\n[The shell could not be confirmed stopped; the workspace "
                "takes no other write until a later command confirms it.]",
                is_error=True,
                settled=False,
            )
        outcome = chat_tools.ToolOutcome(
            f"{outcome.content}\n[The shell was restarted; whatever the command changed "
            "is recorded as its revision.]",
            is_error=True,
        )
    # What the shell changed is a revision of its own (docs/workspace.md).
    outcome = await chat_tools.commit_shell_changes(
        ctx.paths.root,
        outcome,
        tool_input,
        author,
        max_chars=max_chars,
        locked=True,
    )
    if outcome.edit is not None:
        # The library follows the tree before the viewer is told to reload, and before
        # the lock is released: a read waiting on it must not materialize a fresh copy
        # at the old path in between. A reconciliation that fails does not unsay the
        # commit: the outcome stands, with its edit, rather than an error the model
        # would answer by running the command again (the next read reconciles).
        try:
            async with ctx.sessionmaker() as db:
                await reconcile_locked(db, ctx.settings, ctx.turn.user_id)
        except Exception:  # noqa: BLE001 - the revision is made; this can wait
            logger.exception(
                "could not reconcile the library after a command in turn %s", ctx.turn.id
            )
    return outcome


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
# Read-only tools of one round in flight together.
READ_CONCURRENCY = 4
IMAGE_BUDGET_SPENT = "This round's image budget is spent; ask for fewer pages or figures at a time."
IMAGE_TURN_BUDGET_SPENT = (
    "This turn's image budget is spent; finish with what you have seen, or answer and let "
    "the user ask again."
)


def _image_bytes(result: dict) -> int:
    """The encoded image bytes a tool_result carries — what it costs to hold and to store."""
    content = result.get("content")
    if not isinstance(content, list):
        return 0
    return sum(
        len(block.get("source", {}).get("data", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "image"
    )


async def _run_tools(ctx: TurnContext, tool_uses: list[dict], malformed: set[str]) -> list[dict]:
    """The results of one round, in the order the calls were made. Reads run together;
    the tools that write the tree run one after another, because each commits whatever is
    dirty and two in flight would file one's changes under the other (docs/chat.md § Tools)."""
    results: dict[int, dict] = {}
    stopped = asyncio.Event()

    async def reads() -> None:
        # A few at a time, against one image budget for the round: fifty page renders at
        # once would hold their PNGs in memory together and store them all in the history.
        slots = asyncio.Semaphore(READ_CONCURRENCY)
        budget = ctx.settings.chat_image_round_max_bytes
        # The turn's budget too: the round's alone would let fifty rounds store fifty
        # rounds' worth of pages in the history every later request replays.
        turn_budget = ctx.settings.chat_image_turn_max_bytes
        spent = 0

        def exhausted() -> chat_tools.ToolOutcome | None:
            if ctx.image_spent >= turn_budget:
                return chat_tools.ToolOutcome(IMAGE_TURN_BUDGET_SPENT, is_error=True)
            if spent >= budget:
                return chat_tools.ToolOutcome(IMAGE_BUDGET_SPENT, is_error=True)
            return None

        def admit(outcome: chat_tools.ToolOutcome) -> chat_tools.ToolOutcome:
            # One result at a time, as each completes (no await in between): an image
            # that would cross a budget is replaced before it is reported or stored,
            # so neither the round nor the turn exceeds it however many reads ran
            # together.
            nonlocal spent
            size = _image_bytes({"content": outcome.content})
            if size and ctx.image_spent + size > turn_budget:
                return chat_tools.ToolOutcome(IMAGE_TURN_BUDGET_SPENT, is_error=True)
            if size and spent + size > budget:
                return chat_tools.ToolOutcome(IMAGE_BUDGET_SPENT, is_error=True)
            spent += size
            ctx.image_spent += size
            return outcome

        async def one(index: int, block: dict) -> None:
            async with slots:
                # A round of reads has no write to notice the Stop: each read looks for
                # it before it starts, so at most the few in flight finish. Noted, not
                # raised: raising here would cancel a shell command or editor write
                # still running, which must finish and commit (docs/chat.md § Tools).
                if stopped.is_set() or await _cancelled(ctx):
                    stopped.set()
                    return
                refusal = exhausted()
                if refusal is not None:
                    results[index] = await _report_tool(
                        ctx, str(block.get("id")), block.get("name"), refusal
                    )
                    return
                results[index] = await _run_tool(ctx, block, malformed, admit)

        await asyncio.gather(
            *(
                one(index, block)
                for index, block in enumerate(tool_uses)
                if block.get("name") not in MUTATING_TOOLS
            )
        )

    async def writes() -> None:
        for index, block in enumerate(tool_uses):
            if block.get("name") in MUTATING_TOOLS:
                # Stop means after the *current* call: a write not yet started — the
                # first of the round included — is not started once the user cancelled.
                if await _cancelled(ctx):
                    raise TurnCancelled()
                # Shielded: the turn's deadline cancels the loop, not a shell command
                # or editor write in flight, which run_turn waits for before the
                # terminal event (docs/chat.md § Tools).
                writer = asyncio.ensure_future(_run_tool(ctx, block, malformed))
                ctx.writer = writer
                ctx.writer_tool = block.get("name")
                # Cleared when the writer is done, not when this await ends: a cancelled
                # await leaves the writer running, and run_turn must still find it.
                writer.add_done_callback(
                    lambda done, ctx=ctx: (
                        setattr(ctx, "writer", None) if ctx.writer is done else None
                    )
                )
                results[index] = await asyncio.shield(writer)

    # A task group: when the writes stop on a cancel, the reads still running are
    # cancelled with them rather than left to finish a turn that is over.
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(reads())
            group.create_task(writes())
    except* TurnCancelled:
        raise TurnCancelled() from None
    if stopped.is_set():
        raise TurnCancelled()
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
        await _record_usage_settled(ctx, completed)
        if await _cancelled(ctx):
            # Billed, but not kept: a cancelled turn leaves the history as it was.
            raise TurnCancelled()
        followed: str | None = None
        if completed.model and completed.model != ctx.model:
            # A safety fallback answered from another model: its thinking blocks are
            # that model's, so the conversation follows it from here — in the same
            # transaction as the answer is stored, so no request ever replays them to
            # the model that did not make them, and no finished turn moves the
            # conversation (docs/chat.md § History is append-only).
            logger.info(
                "conversation %d moves from %s to %s, which answered its turn %s",
                ctx.conversation.id,
                ctx.model,
                completed.model,
                ctx.turn.id,
            )
            followed = completed.model
        tool_uses = [block for block in completed.content if block.get("type") == "tool_use"]
        content = completed.content
        if completed.stop_reason == "max_tokens":
            # What came through is kept; a tool call cut off with the limit is not — it is
            # never run, and a tool_use with no result would make the history unsendable.
            content = [block for block in content if block.get("type") != "tool_use"]
        if content:
            await _store(ctx, "assistant", content, model=followed)
            messages.append({"role": "assistant", "content": content})
            # Positions are content blocks, not messages (docs/chat.md § Prompt caching).
            since_marker += len(content)
        elif followed is not None:
            await _follow_model(ctx, followed)

        if completed.stop_reason == "refusal":
            raise TurnFailed("The model declined this request")
        if completed.stop_reason == "max_tokens":
            raise TurnFailed("The answer reached the output limit before it was complete")
        if completed.stop_reason in ("end_turn", "stop_sequence"):
            return
        if completed.stop_reason != "tool_use" or not tool_uses:
            raise TurnFailed(f"The model stopped unexpectedly ({completed.stop_reason})")
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
        # The conversation's lock first, then the turn — the order the cancel route takes
        # — so the two cannot deadlock, and a cancel committed a moment ago is seen.
        await db.execute(
            select(Conversation.id).where(Conversation.id == turn.conversation_id).with_for_update()
        )
        await db.refresh(turn)
        if turn.finished_at is not None:
            # Finished by another worker — the one that took the lock this one lost and
            # found the turn orphaned: its terminal state and event stand (docs/jobs.md).
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


async def _settle_writer(ctx: TurnContext) -> None:
    """Wait for the mutating tool call the deadline interrupted: cancelling the loop does
    not stop a shell command in the VM or an editor thread, and the terminal event must
    not precede their commit. Past the grace a shell command is killed in the VM (its
    shell restarted) and the writer is then waited for to its end — every step of it is
    bounded on its own (the runner client's deadline, git's timeouts), so the wait is
    too — rather than the turn being finished over a write still in flight."""
    writer = ctx.writer
    if writer is None or writer.done():
        return
    # The command's own timeout, the time the runner client allows a reply past it, and
    # the margin for the commit and reconciliation that follow (docs/chat.md § Tools).
    grace = (
        ctx.settings.chat_bash_timeout_seconds
        + ctx.settings.sandbox_start_timeout_seconds
        + ctx.settings.sandbox_exec_grace_seconds
        + ctx.settings.chat_writer_grace_seconds
    )
    try:
        await asyncio.wait_for(asyncio.shield(writer), timeout=grace)
        return
    except TimeoutError:
        logger.warning("turn %s: the writer outlived its grace; waiting it out", ctx.turn.id)
    except Exception:  # noqa: BLE001 - the writer's own failure is its result
        return
    if ctx.writer_tool == "bash" and ctx.sandbox is not None:
        # The command, if it is what runs on: ended where it runs, and the exec returns.
        with contextlib.suppress(Exception):
            await ctx.sandbox.restart(ctx.turn.user_id, ctx.conversation.session)
    with contextlib.suppress(Exception):
        await asyncio.shield(writer)


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
        await _settle_writer(ctx)
        await _finish(sessionmaker, turn_id, status="cancelled", error="cancelled")
    except TurnFailed as exc:
        await _settle_writer(ctx)
        await _finish(sessionmaker, turn_id, status="failed", error=str(exc))
    except TimeoutError:
        await _settle_writer(ctx)
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
        # A worker shutting down: the write in flight finishes and commits first.
        await _settle_writer(ctx)
        raise
    except Exception:
        # A read's own failure (its cancel check lost the database) cancels the round
        # around a shielded writer: that write finishes and commits before the terminal
        # event, like a timeout's (docs/chat.md § Tools).
        logger.exception("turn %s failed unexpectedly", turn_id)
        await _settle_writer(ctx)
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
