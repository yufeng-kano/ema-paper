"""The chat agent's runner: one turn, one tool loop (docs/chat.md).

Shape of a turn: load the conversation and its document, render the system prompt, replay
the stored messages verbatim, stream the model's answer into events, run the tools it
asked for, append the results, and go again until it stops. Everything the model produced
is stored as it came, because the next turn replays it (docs/chat.md § History).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..agents import CHAT
from ..agents import chat as chat_agent
from ..agents import skills as skill_defs
from ..agents import subagents as subagent_defs
from ..billing import ledger
from ..config import Settings
from ..db.models import (
    COMPACTION_TURNS,
    EFFORT_DEFAULT,
    FILE_KIND,
    IDLE_COMPACT_TURN,
    AgentSetting,
    ChatToolCall,
    ChatTurn,
    Conversation,
    Document,
    Job,
    Message,
    Skill,
    Subagent,
    UsageLog,
    User,
    utcnow,
)
from ..errors import ApiError
from ..parsing.figures import discard_job_figures
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..upstream.anthropic import (
    ChatUsage,
    CompletedMessage,
    StreamEvent,
    StreamProgress,
    stream_messages,
)
from ..upstream.client import (
    Connection,
    PaymentRequired,
    RateLimited,
    UpstreamError,
    kano_connection,
)
from ..workspace import filing, git, safe_io
from ..workspace.documents import (
    MARKDOWN_KIND,
    DocumentPaths,
    ensure_within_quota,
    folder_names,
    is_live,
    materialize_document,
    measure_storage,
    recover_trash,
    stage_upload,
    trashed_folder_ids,
    user_folders,
)
from ..workspace.layout import DOC_FILE
from ..workspace.locks import (
    backup_shared,
    clear_writing,
    mark_writing,
    shell_lock,
    workspace_lock,
)
from ..workspace.memory_file import memory_file_text
from ..workspace.reconcile import reconcile_locked, reconcile_user
from . import chat_events, chat_memory, chat_subagent, chat_tools, chat_versions, chat_web
from .chat_compact import SUMMARY_CONTEXT
from .intake import convert_kept_file
from .queue import HISTORY_FULL, claim_job, history_bytes, requeue_job
from .runner import fail_job, run_job

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
# The blocks of the model's reasoning, which a replay may leave out only from the start
# (docs/chat.md § History is append-only).
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


class TurnCancelled(Exception):
    pass


class TurnFailed(Exception):
    pass


class TurnOutOfCredits(Exception):
    """The period's balance ran out mid-turn and the turn's last call, made with no tools
    left to it, still asked for one — or a subagent's retry found the allowance used, which
    its run answers on its own (docs/billing.md § A chat turn)."""


@dataclass(frozen=True)
class Compacted:
    """What a compaction stored: its summary row, and the memory file as it read it then.
    The boundary and the snapshot the turn's finish sets, with the terminal state
    (docs/chat.md § Compaction)."""

    message_id: int
    memory_file: str


@dataclass
class TurnContext:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    turn: ChatTurn
    conversation: Conversation
    document: Document
    job: Job | None  # None: a markdown document, which has no parse (docs/workspace.md)
    paths: DocumentPaths
    connection: Connection
    model: str
    effort: str
    system: list[dict]
    tools: list[dict]
    # A short hash of `system` and `tools`, stored with every thinking block the turn
    # keeps: a block is replayed only under the prompt and tools it was made under
    # (docs/chat.md § History is append-only).
    binding: str
    # The turns from the conversation's root to this one, oldest first: what the turn
    # replays, whichever branch the conversation shows by the time a worker claims it
    # (docs/chat.md § History is append-only, *An edited message starts a version*).
    path: list[uuid.UUID]
    sandbox: SandboxClient | None
    render_slots: asyncio.Semaphore
    # The reader's skills and subagents as this turn has them (docs/chat.md § Skills,
    # § Subagents), and the document's facts a subagent's preamble is rendered with.
    skills: dict[str, skill_defs.ResolvedSkill] = field(default_factory=dict)
    subagents: dict[str, subagent_defs.ResolvedSubagent] = field(default_factory=dict)
    facts: dict[str, str] = field(default_factory=dict)
    # What the turn was given beyond the document, for its `turn_start` event: whether the
    # reader's instructions went with it, and the memories its block rendered (docs/chat.md
    # § Memory).
    instructions: bool = False
    memory_ids: list[int] = field(default_factory=list)
    tool_calls: int = 0
    # Encoded image bytes the turn's reads have returned so far, every round together
    # (docs/chat.md § Tools).
    image_spent: int = 0
    # The mutating tool calls in flight — the agent's own or a subagent's, and the `memory`
    # calls with what reports them — each with the tool it runs: every one is finished
    # before the turn is, whatever ended the turn, and a shell command past its grace is
    # killed in the VM.
    writers: dict[asyncio.Future, str] = field(default_factory=dict)
    # One write of the turn at a time, the agent's or any subagent's, each started only
    # after a look for a Stop (docs/chat.md § Tools).
    write_gate: asyncio.Lock = field(default_factory=asyncio.Lock)
    # One image decoded at a time per turn: a round of page reads never holds a raster
    # per read (docs/chat.md § Tools).
    decode_slot: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    # Set by the settlement that found the period overdrawn: from then on the turn may run
    # only `chat_overdraft_max_tool_calls` more tool calls, counted here, then answers
    # (docs/billing.md § A chat turn).
    credits_exhausted: bool = False
    overdraft_tool_calls: int = 0
    # How many calls of each web tool the turn has started, against its per-turn caps
    # (docs/chat.md § Tools).
    web_calls: dict[str, int] = field(default_factory=dict)
    # A compaction's summary once stored, for the finish that makes it the boundary.
    compacted: Compacted | None = None
    # The worker's generation, which a parse the turn drives itself is claimed with
    # (docs/chat.md § Tools, *A conversion runs inside the turn*); and that parse's task
    # while it runs, which the end of the turn cancels rather than waits out.
    generation: int | None = None
    inline_parse: asyncio.Task | None = None
    # Set when the turn ends around a conversion: the wait on the run and the poll of a
    # job not yet claimed both stop at it.
    convert_stop: asyncio.Event = field(default_factory=asyncio.Event)

    def overdraft_spent(self) -> bool:
        """The credits ran out and the tool calls allowed after them are used."""
        return (
            self.credits_exhausted
            and self.overdraft_tool_calls >= self.settings.chat_overdraft_max_tool_calls
        )

    def admit_tool_call(self) -> bool:
        """Whether a tool call about to start may run — always, until the credits run out;
        then while the allowance lasts, which it spends. No await between the check and the
        count, so calls starting together never overrun it."""
        if not self.credits_exhausted:
            return True
        if self.overdraft_spent():
            return False
        self.overdraft_tool_calls += 1
        return True

    def round_admission(self, calls: int) -> Callable[[int], bool]:
        """Whether a round's call (by its index) may run. A round that starts with the
        credits already spent hands out what is left of the allowance in the order the
        calls were made; one that starts before admits each call as it starts, since a
        subagent beside it may spend the balance meanwhile."""
        if not self.credits_exhausted:
            return lambda index: self.admit_tool_call()
        admitted = [self.admit_tool_call() for _ in range(calls)]
        return lambda index: admitted[index]


# --- loading -------------------------------------------------------------------------------


async def _load(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    turn_id: uuid.UUID,
    render_slots: asyncio.Semaphore,
    generation: int | None = None,
) -> TurnContext:
    async with sessionmaker() as db:
        turn = await db.get(ChatTurn, turn_id)
        if turn is None or turn.status != "running":
            raise TurnFailed("the turn is no longer running")
        conversation = await db.get(Conversation, turn.conversation_id)
        document = await db.get(Document, conversation.document_id) if conversation else None
        if conversation is None or document is None:
            raise TurnFailed("the conversation's document no longer exists")
        job = await db.get(Job, document.job_id) if document.job_id is not None else None
        if document.job_id is not None and job is None:
            raise TurnFailed("the document's parse no longer exists")
        setting = (
            await db.execute(
                select(AgentSetting).where(
                    AgentSetting.user_id == turn.user_id, AgentSetting.agent == CHAT.name
                )
            )
        ).scalar_one_or_none()
        # The conversation's model is the model of its first turn, fixed when that message
        # was accepted, whatever the Agent page says now: the stored thinking blocks carry
        # its signatures (docs/chat.md).
        model = conversation.model
        if not model:
            raise TurnFailed("No chat model is offered on this deployment")
        if model not in settings.model_prices:
            # Never a call nobody priced (docs/billing.md § Models).
            raise TurnFailed("model_unpriced")
        # The history is replayed whole on every call, images included: past the cap
        # a turn would load and serialize hundreds of megabytes in the worker before
        # the upstream could refuse the context (docs/chat.md § History). The send is
        # refused past it too; this catches a turn queued before the history crossed it.
        path = [
            step.id
            for step in chat_versions.branch(
                await chat_versions.conversation_turns(db, conversation.id), turn.id
            )
        ]
        if (
            await history_bytes(
                db,
                conversation.id,
                since=conversation.compacted_message_id,
                turn_id=turn.id,
                path=path,
            )
            > settings.chat_history_max_bytes
        ):
            raise TurnFailed(HISTORY_FULL)
        connection = kano_connection(settings, anthropic=True)
        if connection is None:
            raise TurnFailed("Kano Proxy is not configured on this deployment")
        # The reader's own prompt replaces the default whole; a legacy preset is appended
        # to it as before, and never stands beside a prompt (docs/chat.md § System prompt).
        own_prompt = setting.prompt if setting and setting.prompt else None
        preset = setting.prompt_preset if setting and own_prompt is None else None
        # Appended after the prompt, whichever is in force (docs/chat.md § Instructions).
        instructions = setting.instructions if setting else None
        effort = setting.effort if setting and setting.effort else EFFORT_DEFAULT
        skills = skill_defs.resolve_skills(
            (await db.execute(select(Skill).where(Skill.user_id == turn.user_id))).scalars()
        )
        subagents = subagent_defs.resolve_subagents(
            (await db.execute(select(Subagent).where(Subagent.user_id == turn.user_id))).scalars()
        )
        # The memory file as the conversation's first turn finds it, stored with this
        # commit like the memory snapshot and read from there by every later turn, so the
        # agent's own writes never cost the conversation its cache (docs/chat.md § Memory,
        # *The memory file*). Before the session writes anything: the read takes the
        # workspace lock, which is taken with no row locked, the order every workspace
        # mutation keeps (docs/workspace.md § Revisions).
        if conversation.memory_file is None:
            conversation.memory_file = await memory_file_text(settings, turn.user_id)
        # The memory block: the conversation's snapshot, taken by its first turn and stored
        # with this commit, rendered from the rows as they are now (docs/chat.md § Memory).
        memory = await chat_memory.load_turn_memory(
            db,
            settings,
            conversation,
            auto=setting.memory_auto if setting else True,
            paused=setting.memory_paused if setting else False,
        )
        # The plan's entitlements, read at the turn's start (docs/billing.md § Plans).
        plan = await ledger.plan_of(db, settings, turn.user_id)
        await db.commit()
        try:
            paths = await materialize_document(db, settings, document)
            # A full workspace runs no shell: what the turn writes has nowhere to go
            # (docs/workspace.md § Quota).
            await ensure_within_quota(db, settings, turn.user_id, paths.root)
        except ApiError as exc:
            # A full workspace is a state the reader can act on: a token the panel words,
            # as `out_of_credits` is (docs/web.md § The agent panel).
            raise TurnFailed(
                "storage_full" if exc.error_type == "storage_full" else exc.message
            ) from exc

    facts = document_facts(settings, job, paths)
    # `bash` only on a plan that grants it, and only with a runner (docs/billing.md).
    shell = ledger.sandbox_granted(settings, plan)
    sandbox = SandboxClient(settings) if shell else None
    tools = turn_tools(shell, settings)
    # The prompt, then a legacy preset, the reader's instructions, the catalogue — read
    # per turn like the prompt, so an edit on the Agent page misses the cache once — and
    # the memory block last (docs/chat.md § Prompt caching).
    parts = chat_agent.system_parts(
        # The tool list as the turn has it: no `bash` line without a runner.
        chat_agent.render_system_prompt(
            own_prompt or CHAT.default_prompt_template,
            facts,
            tools=[tool["name"] for tool in tools],
        ),
        preset=preset.text if preset is not None else None,
        instructions=instructions,
        catalogue=turn_catalogue(skills, subagents),
        memory=memory.block,
        memory_file=chat_agent.render_memory_file(
            conversation.memory_file, settings.chat_memory_file_max_chars
        ),
    )

    system = chat_agent.system_blocks(parts)
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
        binding=hashlib.sha256(
            json.dumps([system, tools], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16],
        path=path,
        sandbox=sandbox,
        render_slots=render_slots,
        generation=generation,
        skills={skill.name: skill for skill in skills},
        subagents={subagent.name: subagent for subagent in subagents},
        facts=facts,
        instructions=any(part.kind == "instructions" for part in parts),
        memory_ids=memory.ids,
    )


def document_facts(settings: Settings, job: Job | None, paths: DocumentPaths) -> dict[str, str]:
    """The document's facts the prompt is rendered with (docs/chat.md § System prompt);
    a markdown document (`job` None) has no pages and no parse to name."""
    return {
        "document_path": str(chat_tools.VM_ROOT / paths.relative.as_posix()),
        "page_count": str(job.page_count or 1) if job is not None else "none",
        "bbox_format": job.bbox_format if job is not None else "none",
        "parse_model": job.model if job is not None else "not parsed (a markdown document)",
        "network": chat_agent.network_note(settings.sandbox_network, settings.sandbox_index_url),
    }


def turn_tools(shell: bool, settings: Settings) -> list[dict]:
    """The tools a turn declares: every one, in its fixed order, but `bash` only where the
    plan grants a shell and a sandbox runner exists, and the web tools only where the
    deployment can run them — a tool the harness cannot run is not offered
    (docs/sandbox.md, docs/billing.md § Plans, docs/chat.md § Tools)."""
    return [
        *(chat_agent.TOOLS if shell else chat_agent.TOOLS_WITHOUT_BASH),
        *chat_agent.web_tools(
            enabled=settings.chat_web_access,
            brave=bool(settings.brave_search_api_key),
            openalex=bool(settings.openalex_api_key),
        ),
        *chat_agent.convert_tools(parse=bool(settings.parse_model)),
    ]


def turn_catalogue(
    skills: list[skill_defs.ResolvedSkill], subagents: list[subagent_defs.ResolvedSubagent]
) -> str | None:
    """The catalogue block of the reader's skills the agent may load and their subagents
    (docs/chat.md § Skills, § Subagents)."""
    return chat_agent.render_catalogue(
        [(skill.name, skill.description) for skill in skills if skill.model_invocable],
        [(subagent.name, subagent.description, subagent.writes) for subagent in subagents],
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
    """The messages the call replays: the rows of the turns on this turn's own branch and
    the rows of no turn, which precede every turn (docs/chat.md § History is append-only,
    *An edited message starts a version*); from the summary row of the last compaction on,
    when there was one, and without the rows of any other compaction turn — a failed or
    cancelled attempt's instruction, tool calls and whatever summary it stored before it
    ended — but the one row the boundary names (docs/chat.md § Compaction)."""
    async with ctx.sessionmaker() as db:
        # The branch's rows alone are read: the versions edited away — their images among
        # them — never leave the database to be dropped here.
        query = select(Message).where(
            Message.conversation_id == ctx.conversation.id,
            or_(Message.turn_id.is_(None), Message.turn_id.in_(ctx.path)),
        )
        boundary = ctx.conversation.compacted_message_id
        if boundary is not None:
            query = query.where(Message.id >= boundary)
        rows = (await db.execute(query.order_by(Message.id))).scalars().all()
        compactions = set(
            (
                await db.execute(
                    select(ChatTurn.id).where(
                        ChatTurn.conversation_id == ctx.conversation.id,
                        ChatTurn.kind.in_(COMPACTION_TURNS),
                        ChatTurn.id != ctx.turn.id,
                    )
                )
            ).scalars()
        )
        return repaired(
            [
                {"role": message["role"], "content": _api_blocks(message["content"])}
                for message in stale_thinking_left_out(
                    [
                        {"role": row.role, "content": row.content}
                        for row in rows
                        if row.turn_id not in compactions or row.id == boundary
                    ],
                    ctx.binding,
                )
            ]
        )


def stale_thinking_left_out(messages: list[dict], binding: str) -> list[dict]:
    """The stored history without the thinking it may no longer replay: every thinking
    block up to and including the last one made under another system prompt or tool list
    than `binding` names — or stored before blocks carried one — or before the last answer
    stored with a cutoff, and an assistant message left with no content. Only from the
    start, so the blocks made since stay valid, and read from the rows alone, so every
    replay leaves out the same blocks (docs/chat.md § History is append-only)."""
    last = -1
    for index, message in enumerate(messages):
        if message["role"] != "assistant" or not isinstance(message["content"], list):
            continue
        blocks = [block for block in message["content"] if isinstance(block, dict)]
        if any(
            block.get("type") in THINKING_TYPES and block.get("binding") != binding
            for block in blocks
        ):
            last = index
        elif any(block.get("thinking_cutoff") for block in blocks):
            last = index - 1
    kept: list[dict] = []
    for index, message in enumerate(messages):
        content = message["content"]
        if index <= last and message["role"] == "assistant" and isinstance(content, list):
            content = [
                block
                for block in content
                if not (isinstance(block, dict) and block.get("type") in THINKING_TYPES)
            ]
            if not content:
                continue
        kept.append({"role": message["role"], "content": content})
    return kept


# The keys a stored block carries for the web app and the API never sees: the context
# marker (docs/chat.md § Prompt caching), a command's skill, an attachment's name, a
# section's heading and a subagent call's record (docs/chat.md § What a user message
# carries, § Subagents), and the prompt a thinking block was made under and the cutoff an
# upstream's drop left (§ History is append-only).
PRIVATE_KEYS = frozenset(
    {"context", "skill", "attachment", "section", "subagent", "binding", "thinking_cutoff"}
)


def _api_blocks(content: object) -> object:
    """The stored content as the API takes it: the keys a stored block carries for the
    web app are ours, not the API's."""
    if not isinstance(content, list):
        return content
    return [
        {key: value for key, value in block.items() if key not in PRIVATE_KEYS}
        if isinstance(block, dict)
        else block
        for block in content
    ]


def build_request(
    ctx: TurnContext, messages: list[dict], *, since_marker: int, final: bool = False
) -> dict:
    """The request body, with the cache guard on the latest tool results when a long turn
    has accrued enough positions since the last marker (docs/chat.md § Prompt caching).
    `final`: the turn's last call once its credits are spent — the model may call no tool
    (docs/billing.md § A chat turn)."""
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
    body = {
        "model": ctx.model,
        "max_tokens": ctx.settings.chat_max_output_tokens,
        "system": ctx.system,
        "tools": ctx.tools,
        "messages": replay,
        # A block the replay could not tell is stale is dropped, with every thinking block
        # after it, rather than failing the request (docs/chat.md § History is append-only).
        "thinking": {
            "type": "adaptive",
            "display": "updates",
            "block_binding": {"prefix_mismatch_behavior": "drop_block"},
        },
        "output_config": {"effort": ctx.effort},
        "fallbacks": "default",
        "cache_control": {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL},
    }
    if final:
        body["tool_choice"] = {"type": "none"}
    return body


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


async def _record_usage(
    ctx: TurnContext, model: str, usage: ChatUsage, *, conversation: bool = False
) -> None:
    """The call's usage row and the turn's settlement; with `conversation`, a call of the
    conversation's own — not a subagent's — which also records what the next call will
    replay and when the cache it touched expires (docs/chat.md § Compaction)."""
    async with ctx.sessionmaker() as db:
        db.add(
            UsageLog(
                user_id=ctx.turn.user_id,
                turn_id=ctx.turn.id,
                agent=CHAT.name,
                model=model,
                prompt_tokens=usage.input_tokens,
                completion_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost=usage.cost,
                cost_source=usage.cost_source,
            )
        )
        await db.flush()
        if conversation:
            context_tokens = (
                usage.input_tokens
                + usage.cache_read_tokens
                + usage.cache_write_tokens
                + usage.output_tokens
            )
            warm_until = utcnow() + chat_agent.CACHE_TTL_DELTA if usage.cached else None
            await db.execute(
                update(Conversation)
                .where(Conversation.id == ctx.conversation.id)
                .values(
                    context_tokens=context_tokens,
                    cache_warm_until=warm_until,
                    **_keep_place(ctx.turn.kind),
                )
            )
        # The turn re-priced from the same rows, in the same commit: Usage and the ledger
        # agree by construction (docs/billing.md § A chat turn).
        remaining = await ledger.settle_chat(db, ctx.settings, ctx.turn.id)
        await db.commit()
    if remaining is not None and remaining <= 0:
        # The turn finishes within the overdraft allowance (docs/billing.md § A chat turn).
        ctx.credits_exhausted = True


async def _record_usage_settled(
    ctx: TurnContext, model: str, usage: ChatUsage, *, conversation: bool = False
) -> None:
    """The paid call reaches `usage_log` whether or not the turn is cancelled meanwhile:
    the write runs as a task of its own, and a cancel that lands during it waits for it
    to settle before it goes on — a cancelled or timed-out turn is billed all the same
    (docs/chat.md § Conversations, turns, events)."""
    await _settled(_record_usage(ctx, model, usage, conversation=conversation))


async def _settled(recording: Awaitable[None]) -> None:
    """Run a record to its end whatever cancels the caller meanwhile."""
    task = asyncio.ensure_future(recording)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await asyncio.shield(task)
        raise


async def _record_tool_call(ctx: TurnContext, tool: str, *, settle: bool) -> None:
    """A web tool call that was answered or kept a file, as a `chat_tool_calls` row — what
    the per-day caps count — and, for a web search, the turn re-priced in the same commit
    so the search is charged the moment it is answered (docs/chat.md § Tools,
    docs/billing.md § A chat turn). Shielded like a usage row: a Stop right after the
    search does not make it free."""

    async def record() -> None:
        async with ctx.sessionmaker() as db:
            db.add(ChatToolCall(user_id=ctx.turn.user_id, turn_id=ctx.turn.id, tool=tool))
            await db.flush()
            remaining = await ledger.settle_chat(db, ctx.settings, ctx.turn.id) if settle else None
            await db.commit()
        if remaining is not None and remaining <= 0:
            ctx.credits_exhausted = True

    await _settled(record())


async def _calls_today(ctx: TurnContext, tool: str, *, user_id: int | None = None) -> int:
    """How many calls of `tool` were recorded since the UTC day began — the deployment's
    together, or one user's (docs/chat.md § Tools)."""
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    query = (
        select(func.count())
        .select_from(ChatToolCall)
        .where(ChatToolCall.tool == tool, ChatToolCall.created_at >= start)
    )
    if user_id is not None:
        query = query.where(ChatToolCall.user_id == user_id)
    async with ctx.sessionmaker() as db:
        return int((await db.execute(query)).scalar_one())


# --- one model call ----------------------------------------------------------------------------


def _with_parent(data: dict, parent: str | None) -> dict:
    """A subagent's events name the `subagent` call that started it (docs/chat.md
    § Conversations, turns, events)."""
    return {**data, "parent": parent} if parent is not None else data


async def _stream_once(
    ctx: TurnContext, body: dict, *, parent: str | None = None
) -> CompletedMessage:
    """One streamed request, its deltas relayed as events as they arrive. A subagent's
    call (`parent`) streams no text: its report arrives as its call's result."""
    text_buffer: list[str] = []
    thinking_buffer: list[str] = []

    async def flush_text() -> None:
        if text_buffer:
            if parent is None:
                await _emit(ctx, chat_events.TEXT_DELTA, {"text": "".join(text_buffer)})
            text_buffer.clear()

    async def flush_thinking() -> None:
        if thinking_buffer:
            await _emit(
                ctx, chat_events.THINKING, _with_parent({"text": "".join(thinking_buffer)}, parent)
            )
            thinking_buffer.clear()

    completed: CompletedMessage | None = None
    last_cancel_check = time.monotonic()
    progress = StreamProgress()
    stream = stream_messages(
        ctx.connection,
        body,
        betas=chat_agent.BETAS,
        max_bytes=ctx.settings.upstream_response_max_bytes,
        line_max_bytes=ctx.settings.upstream_sse_line_max_bytes,
        blocks_max=ctx.settings.upstream_content_blocks_max,
        timeout=ctx.settings.chat_stream_timeout_seconds,
        progress=progress,
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
                        _with_parent(
                            {
                                "id": block.get("id"),
                                "name": block.get("name"),
                                "input": block.get("input", {}),
                            },
                            parent,
                        ),
                    )
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await stream.aclose()
        usage = progress.usage_so_far()
        if completed is None and usage is not None:
            # The call ended before its message_stop — a Stop, the turn's timeout, a stream
            # that broke — and is billed for what it streamed, as the model it asked for
            # (docs/chat.md § Conversations, turns, events). A write that fails is logged
            # rather than raised over what ended the call.
            try:
                await _record_usage_settled(
                    ctx, str(body["model"]), usage, conversation=parent is None
                )
            except Exception:
                logger.exception("turn %s: the usage of a call cut short was lost", ctx.turn.id)
    await flush_text()
    await flush_thinking()
    if completed is None:
        raise UpstreamError("The upstream closed the stream early", transient=True)
    # Kano Proxy names a model `provider/model`; the upstream answers with the bare id. Read
    # with the requested provider, the same model is not a fallback, and a real fallback is
    # followed under a name Kano Proxy can route (docs/chat.md § Request).
    asked = str(body["model"])
    provider = asked.rpartition("/")[0]
    if provider and completed.model and "/" not in completed.model:
        completed.model = f"{provider}/{completed.model}"
    if completed.model and completed.model not in ctx.settings.model_prices:
        # A name this deployment does not price — a group alias the upstream answers under
        # the target's own id, or a fallback nobody priced — is not one to follow or bill
        # as: the conversation would sit on a name no later turn may run, and the call
        # would be charged nothing. It is the model asked for (docs/chat.md § Request).
        logger.warning(
            "turn %s: %s answered as %s, which is unpriced; kept as %s",
            ctx.turn.id,
            asked,
            completed.model,
            asked,
        )
        completed.model = asked
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


async def _call_model(
    ctx: TurnContext, body: dict, *, parent: str | None = None
) -> CompletedMessage:
    for attempt in range(MAX_ATTEMPTS):
        if attempt and parent is not None and ctx.overdraft_spent():
            # Another call of the turn used the allowance while this subagent's call waited
            # to retry: no further attempt, the run reports what it has (docs/billing.md
            # § A chat turn).
            raise TurnOutOfCredits()
        try:
            return await _stream_once(ctx, body, parent=parent)
        except RateLimited as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise
            await _backoff(ctx, exc.retry_after or BACKOFF_BASE_SECONDS * (2**attempt))
            # What the failed attempt streamed is already out: the thread starts over
            # from here — a subagent's steps only, for a call of one (docs/chat.md
            # § Conversations, turns, events).
            await _emit(ctx, chat_events.RETRY, _with_parent({"attempt": attempt + 2}, parent))
        except UpstreamError as exc:
            if attempt == MAX_ATTEMPTS - 1 or not exc.transient:
                raise
            await _backoff(ctx, BACKOFF_BASE_SECONDS * (2**attempt))
            await _emit(ctx, chat_events.RETRY, _with_parent({"attempt": attempt + 2}, parent))
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
    except (asyncio.CancelledError, TurnCancelled):
        # A Stop a subagent saw inside its run is the turn's, not a failed tool.
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

    if tool_use_id in malformed:
        outcome = chat_tools.ToolOutcome(
            "The tool input was not valid JSON; send it again.", is_error=True
        )
    elif name in UNDECLARABLE_TOOLS and all(tool.get("name") != name for tool in ctx.tools):
        # Switched off (`CHAT_WEB_ACCESS`), without its key, or without a parse model,
        # the tool is not this turn's, whatever the conversation's history still names:
        # never run (docs/chat.md § Tools).
        outcome = chat_tools.ToolOutcome(f"{name!r} is not one of this turn's tools", is_error=True)
    elif name in MUTATING_TOOLS:
        outcome = await run_mutating(ctx, name, tool_input)
    elif name == "view_page":
        outcome = await chat_tools.view_page(
            ctx.paths,
            tool_input,
            document_id=ctx.document.id,
            page_count=ctx.job.page_count if ctx.job is not None else None,
            render_slots=ctx.render_slots,
            decode_slot=ctx.decode_slot,
            max_bytes=ctx.settings.upload_max_bytes,
            max_pixels=ctx.settings.chat_page_image_max_pixels,
            kind=ctx.job.kind if ctx.job is not None else MARKDOWN_KIND,
        )
    elif name == "view_figure":
        # Bounded by the round's image budget, as a page is: `CONTENT_MAX_BYTES` is the
        # cap on `doc.md` and `notes.md`, and a legitimate crop can be larger.
        outcome = await chat_tools.view_figure(
            ctx.paths, tool_input, max_bytes=ctx.settings.chat_image_round_max_bytes
        )
    elif name == "skill":
        outcome = _load_skill(ctx, tool_input)
    elif name == "subagent":
        outcome = await chat_subagent.run_subagent(ctx, tool_use_id, tool_input)
    elif name == "memory":
        return await _run_memory(ctx, tool_use_id, tool_input, admit)
    elif name in WEB_READ_TOOLS:
        outcome = await _run_web_read(ctx, name, tool_input)
    else:
        outcome = chat_tools.ToolOutcome(f"unknown tool {name!r}", is_error=True)
    if admit is not None:
        outcome = admit(outcome)
    return await _report_tool(ctx, tool_use_id, name, outcome)


async def _run_memory(
    ctx: TurnContext, tool_use_id: str, tool_input: dict, admit: Admit | None
) -> dict:
    """One `memory` call. It writes rows, no file, so it runs with the reads (docs/chat.md
    § Tools); but its commit and what reports it — the tool_result and the `memory` event —
    are one task, shielded and kept with the turn's writers, which the turn's end waits
    for: a cancel between the two would leave a memory the thread draws no line for, with
    no Undo (docs/chat.md § Memory)."""

    # The task outlives a Stop or a timeout, and then nobody awaits it — so its failures are
    # logged and reported here, not left to `_run_tool`, which by then has moved on.
    async def run() -> dict:
        try:
            outcome = await chat_memory.run_memory_tool(
                ctx.sessionmaker,
                ctx.settings,
                user_id=ctx.turn.user_id,
                conversation_id=ctx.conversation.id,
                document_id=ctx.document.id,
                tool_use_id=tool_use_id,
                tool_input=tool_input,
            )
        except Exception:  # noqa: BLE001 - any failure is this call's, never the turn's
            logger.exception("tool memory failed in turn %s", ctx.turn.id)
            outcome = chat_tools.ToolOutcome(
                "The tool failed unexpectedly; try another way.", is_error=True
            )
        if admit is not None:
            outcome = admit(outcome)
        try:
            return await _report_tool(ctx, tool_use_id, "memory", outcome)
        except Exception:  # noqa: BLE001 - the events are lost, the model's answer is not
            logger.exception("tool memory's events were not recorded in turn %s", ctx.turn.id)
            result = {"type": "tool_result", "tool_use_id": tool_use_id, "content": outcome.content}
            if outcome.is_error:
                result["is_error"] = True
            return result

    task = asyncio.ensure_future(run())
    ctx.writers[task] = "memory"
    # Dropped when the call is done, not when this await ends, as a write's is.
    task.add_done_callback(lambda done, ctx=ctx: ctx.writers.pop(done, None))
    return await asyncio.shield(task)


async def run_mutating(ctx: TurnContext, name: str, tool_input: dict) -> chat_tools.ToolOutcome:
    """A `bash` command or an editor call, the agent's own or a subagent's that may change
    files: the shell under the user's shell and workspace locks with its writing marker, the
    editor on the host, each write a revision (docs/chat.md § Tools)."""
    max_chars = ctx.settings.chat_tool_result_max_chars
    author = f"{git.AGENT_AUTHOR_PREFIX}{ctx.conversation.id}"
    if name == "download_file":
        return await _run_download(ctx, tool_input)
    if name == "convert_file":
        return await _run_convert(ctx, tool_input)
    if name != "bash":
        return await chat_tools.run_editor(
            ctx.paths.root,
            tool_input,
            author,
            max_chars=max_chars,
            max_bytes=ctx.settings.content_max_bytes,
        )
    if ctx.sandbox is None:
        return chat_tools.ToolOutcome("No sandbox on this deployment.", is_error=True)
    # One shell at a time per user, through the command *and* its commit: two
    # conversations' shells must never have the tree dirty together, or one's revision
    # would carry the other's half-written files. The workspace lock is held across the
    # command as well: a save or another conversation's editor landing on a file the
    # command is writing would otherwise read and commit a half-written file (docs/chat.md
    # § Tools).
    async with (
        shell_lock(ctx.paths.root),
        workspace_lock(ctx.paths.root, recovering=True),
    ):
        # Marked for as long as the command may write: a worker that dies here leaves the
        # mark, and so does a command the runner could not confirm over, and every host
        # write is refused until a shell restart has confirmed the command dead and the tree
        # is recorded — here, or by the recovery the next worker runs at start
        # (docs/workspace.md § Revisions). `recovering`: this path is that recovery's peer.
        mark_writing(ctx.paths.root, ctx.conversation.id, ctx.conversation.session)
        unsettled = False
        try:
            outcome = await _run_shell_command(ctx, tool_input, author, max_chars)
            unsettled = not outcome.settled
        finally:
            if not unsettled:
                clear_writing(ctx.paths.root)
    return outcome


async def run_writer[Written](
    ctx: TurnContext, name: str, start: Callable[[], Awaitable[Written]]
) -> Written:
    """One mutating call of the turn — the agent's own or a subagent's — started once every
    write before it has finished, and only if no Stop came: Stop means after the *current*
    write, whoever makes it. Shielded: the turn's deadline cancels the loop, not a shell
    command or editor write in flight, which `run_turn` waits for before the terminal event;
    so `start` reports its own result (docs/chat.md § Tools)."""
    async with ctx.write_gate:
        # A write whose caller was cancelled may still be running: it finishes first.
        if ctx.writers:
            await asyncio.wait(list(ctx.writers))
        if await _cancelled(ctx):
            raise TurnCancelled()
        writer = asyncio.ensure_future(start())
        ctx.writers[writer] = name
        # Dropped when the writer is done, not when this await ends: a cancelled await
        # leaves the writer running, and run_turn must still find it.
        writer.add_done_callback(lambda done, ctx=ctx: ctx.writers.pop(done, None))
        return await asyncio.shield(writer)


def _load_skill(ctx: TurnContext, tool_input: dict) -> chat_tools.ToolOutcome:
    """A skill the agent may load, rendered with the call's arguments (docs/chat.md
    § Skills); one the catalogue does not list is an error the model can correct."""
    name = tool_input.get("name")
    skill = ctx.skills.get(name.strip()) if isinstance(name, str) else None
    if skill is None or not skill.model_invocable:
        return chat_tools.ToolOutcome(
            f"No skill named {name!r} is available; the system prompt lists the ones there are.",
            is_error=True,
        )
    arguments = tool_input.get("arguments")
    rendered = skill_defs.render_skill(skill.body, arguments if isinstance(arguments, str) else "")
    return chat_tools.ToolOutcome(f"Skill {skill.name} — follow these instructions:\n\n{rendered}")


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
    data = {"id": tool_use_id, "name": name, "is_error": outcome.is_error, "output": summary}
    if outcome.subagent is not None:
        data["subagent"] = outcome.subagent
    await _emit(ctx, chat_events.TOOL_RESULT, data)
    if outcome.edit is not None:
        await _emit(ctx, chat_events.EDIT, {**outcome.edit, "tool_use_id": tool_use_id})
    if outcome.memory is not None:
        # The thread's quiet line with Undo, which finds the row by this id (docs/chat.md
        # § Memory).
        await _emit(ctx, chat_events.MEMORY, {"tool_use_id": tool_use_id, **outcome.memory})
    result: dict = {"type": "tool_result", "tool_use_id": tool_use_id, "content": outcome.content}
    if outcome.is_error:
        result["is_error"] = True
    if outcome.subagent is not None:
        # Kept for the panel, stripped before the history goes upstream (PRIVATE_KEYS).
        result["subagent"] = outcome.subagent
    return result


MUTATING_TOOLS = frozenset({"bash", "str_replace_based_edit_tool", "download_file", "convert_file"})
# The tools a deployment may not declare — the web tools and `convert_file` — which a
# history may still name (docs/chat.md § Tools).
UNDECLARABLE_TOOLS = frozenset({*chat_agent.WEB_TOOL_NAMES, "convert_file"})
# The web tools that run with the reads (docs/chat.md § Tools).
WEB_READ_TOOLS = frozenset({"web_search", "search_papers", "read_url"})
# Tools that return no image: a spent image budget is no reason to refuse them.
NO_IMAGE_TOOLS = frozenset({"memory", *WEB_READ_TOOLS})
# Read-only tools of one round in flight together.
READ_CONCURRENCY = 4
IMAGE_BUDGET_SPENT = "This round's image budget is spent; ask for fewer pages or figures at a time."
IMAGE_TURN_BUDGET_SPENT = (
    "This turn's image budget is spent; finish with what you have seen, or answer and let "
    "the user ask again."
)
NOT_RUN_OUT_OF_CREDITS = (
    "This tool was not run: the account's credits are spent and the tool calls left to this "
    "turn are used. Answer with what you have."
)
NOT_RUN_COMPACT_ALLOWANCE = (
    "This tool was not run: the compaction's allowance of tool calls is used. Write the "
    "summary now."
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


async def _run_tools(
    ctx: TurnContext, tool_uses: list[dict], malformed: set[str], *, room: int | None = None
) -> list[dict]:
    """The results of one round, in the order the calls were made. Reads run together;
    the tools that write the tree run one after another, because each commits whatever is
    dirty and two in flight would file one's changes under the other (docs/chat.md § Tools).
    `room` is how many of the round a compaction turn's allowance leaves: the rest are
    answered, not run (docs/chat.md § Compaction)."""
    results: dict[int, dict] = {}
    stopped = asyncio.Event()
    # The credits admit only the calls the allowance leaves: the rest are refused before
    # the credits are asked, so a refused call spends no overdraft allowance.
    may_run = ctx.round_admission(len(tool_uses) if room is None else min(len(tool_uses), room))

    def refused(index: int) -> str | None:
        """Why the call is not run, or None when it runs: past the compaction's allowance,
        or past the credits and the calls allowed after them."""
        if room is not None and index >= room:
            return NOT_RUN_COMPACT_ALLOWANCE
        return None if may_run(index) else NOT_RUN_OUT_OF_CREDITS

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
                # `memory` and the web reads return no image, so a spent budget is no
                # reason to refuse them: a reader's "remember this" late in a page-by-page
                # check still lands.
                refusal = exhausted() if block.get("name") not in NO_IMAGE_TOOLS else None
                if refusal is None and (why := refused(index)) is not None:
                    # The credits ran out and the calls allowed after them are used, or
                    # the compaction's allowance is: a read not yet started is not
                    # started (docs/billing.md § A chat turn).
                    results[index] = await _report_tool(
                        ctx,
                        str(block.get("id")),
                        block.get("name"),
                        chat_tools.ToolOutcome(why, is_error=True),
                    )
                    return
                if refusal is not None:
                    results[index] = await _report_tool(
                        ctx, str(block.get("id")), block.get("name"), refusal
                    )
                    return
                try:
                    results[index] = await _run_tool(ctx, block, malformed, admit)
                except TurnCancelled:
                    # A subagent that saw the Stop inside its run ends quietly like any
                    # other read, so a write still running finishes (docs/chat.md
                    # § Subagents) and the reads beside it are not left running.
                    stopped.set()

        await asyncio.gather(
            *(
                one(index, block)
                for index, block in enumerate(tool_uses)
                if block.get("name") not in MUTATING_TOOLS
            )
        )

    async def write(index: int, block: dict) -> dict:
        # Admitted when its turn at the gate has come, not while it waited: a write still
        # waiting once the calls allowed after the credits ran out are used does not start;
        # one already running finishes and commits as it would (docs/billing.md § A chat
        # turn).
        if (why := refused(index)) is not None:
            return await _report_tool(
                ctx,
                str(block.get("id")),
                block.get("name"),
                chat_tools.ToolOutcome(why, is_error=True),
            )
        return await _run_tool(ctx, block, malformed)

    async def writes() -> None:
        for index, block in enumerate(tool_uses):
            if block.get("name") in MUTATING_TOOLS:
                # Stop means after the *current* call: a write not yet started — the
                # first of the round included — is not started once the user cancelled.
                results[index] = await run_writer(
                    ctx,
                    str(block.get("name")),
                    lambda index=index, block=block: write(index, block),
                )

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


# --- the web tools ------------------------------------------------------------------------------

NO_CREDITS_FOR_SEARCH = (
    "The web search was not run: the account's credits are spent, and a search would cost "
    "more. Answer with what you have."
)
WEB_TOOL_TITLES = {
    "web_search": "web searches",
    "search_papers": "paper searches",
    "read_url": "URL reads",
    "download_file": "downloads",
    "convert_file": "conversions",
}


def _turn_cap(ctx: TurnContext, name: str, limit: int) -> bool:
    """Count the call against the turn's cap for its tool; False once the cap is reached.
    No await between the check and the count, so the calls of one round never overrun it
    (docs/chat.md § Tools)."""
    made = ctx.web_calls.get(name, 0)
    if made >= limit:
        return False
    ctx.web_calls[name] = made + 1
    return True


def _turn_cap_reached(name: str, limit: int) -> chat_tools.ToolOutcome:
    return chat_tools.ToolOutcome(
        f"This turn has made its {limit} {WEB_TOOL_TITLES[name]}; answer with what you have.",
        is_error=True,
    )


def _day_cap_reached(name: str, *, per_user: bool) -> chat_tools.ToolOutcome:
    whose = "the user's" if per_user else "this deployment's"
    return chat_tools.ToolOutcome(
        f"Today's limit on {WEB_TOOL_TITLES[name]} ({whose}) is reached; try again tomorrow.",
        is_error=True,
    )


def _string_input(tool_input: dict, key: str) -> str:
    value = tool_input.get(key)
    return value if isinstance(value, str) else ""


async def _run_web_read(ctx: TurnContext, name: str, tool_input: dict) -> chat_tools.ToolOutcome:
    """One `web_search`, `search_papers` or `read_url` call: its caps, the call, and — for
    a search that was answered — its record, a web search charged with it (docs/chat.md
    § Tools)."""
    settings = ctx.settings
    if name == "web_search":
        if not settings.brave_search_api_key:
            return chat_tools.ToolOutcome("No web search on this deployment.", is_error=True)
        if ctx.credits_exhausted:
            return chat_tools.ToolOutcome(NO_CREDITS_FOR_SEARCH, is_error=True)
        if await _calls_today(ctx, name) >= settings.web_search_max_per_day:
            return _day_cap_reached(name, per_user=False)
        if not _turn_cap(ctx, name, settings.web_search_max_per_turn):
            return _turn_cap_reached(name, settings.web_search_max_per_turn)
        outcome = await chat_web.web_search(
            _string_input(tool_input, "query"),
            url=settings.brave_search_url,
            api_key=settings.brave_search_api_key,
            timeout=settings.web_tool_timeout_seconds,
            user_agent=settings.web_tool_user_agent,
        )
        if not outcome.is_error:
            await _record_tool_call(ctx, name, settle=True)
        return outcome
    if name == "search_papers":
        if not settings.openalex_api_key:
            return chat_tools.ToolOutcome("No paper search on this deployment.", is_error=True)
        if await _calls_today(ctx, name) >= settings.paper_search_max_per_day:
            return _day_cap_reached(name, per_user=False)
        if not _turn_cap(ctx, name, settings.paper_search_max_per_turn):
            return _turn_cap_reached(name, settings.paper_search_max_per_turn)
        outcome = await chat_web.search_papers(
            _string_input(tool_input, "query"),
            url=settings.openalex_works_url,
            api_key=settings.openalex_api_key,
            timeout=settings.web_tool_timeout_seconds,
            user_agent=settings.web_tool_user_agent,
        )
        if not outcome.is_error:
            await _record_tool_call(ctx, name, settle=False)
        return outcome
    if not _turn_cap(ctx, name, settings.read_url_max_per_turn):
        return _turn_cap_reached(name, settings.read_url_max_per_turn)
    return await chat_web.read_url(
        _string_input(tool_input, "url"),
        max_bytes=settings.read_url_max_bytes,
        max_chars=settings.chat_tool_result_max_chars,
        timeout=settings.web_tool_timeout_seconds,
        pdf_timeout=settings.chat_attachment_pdf_timeout_seconds,
        user_agent=settings.web_tool_user_agent,
    )


async def _run_download(ctx: TurnContext, tool_input: dict) -> chat_tools.ToolOutcome:
    """One `download_file` call, a write of the turn: the bytes streamed to a staging file
    at the top of `files/` within the storage the quota leaves, then filed under the
    workspace lock exactly as an upload is — the folders the call names made where missing
    (docs/chat.md § Tools, docs/workspace.md § Files)."""
    settings = ctx.settings
    user_id = ctx.turn.user_id
    root = ctx.paths.root
    url = _string_input(tool_input, "url").strip()
    if not url:
        return chat_tools.ToolOutcome("The URL is empty.", is_error=True)
    if await _calls_today(ctx, "download_file", user_id=user_id) >= settings.download_max_per_day:
        return _day_cap_reached("download_file", per_user=True)
    # The room the quota leaves, read before a byte lands; the filing checks the bytes again.
    async with ctx.sessionmaker() as db:
        limit = ledger.storage_limit(settings, await ledger.plan_of(db, settings, user_id))
        used = (await measure_storage(db, settings, user_id, root)).total
    if used >= limit:
        return chat_tools.ToolOutcome(
            "The user's storage is full; nothing was downloaded.", is_error=True
        )
    fd, temp = await asyncio.to_thread(stage_upload, root)
    try:
        try:
            got = await chat_web.download(
                url,
                fd,
                max_bytes=min(settings.download_max_bytes, settings.file_max_bytes),
                room=limit - used,
                timeout=settings.download_timeout_seconds,
                user_agent=settings.web_tool_user_agent,
            )
        except chat_web.FetchRefused as exc:
            return chat_tools.ToolOutcome(str(exc), is_error=True)
        except (httpx.HTTPError, TimeoutError) as exc:
            logger.info("download_file could not fetch: %s", exc.__class__.__name__)
            reason = chat_web.failure(exc, settings.download_timeout_seconds)
            return chat_tools.ToolOutcome(
                f"The file could not be downloaded: {reason}; {chat_web.ANOTHER_SOURCE}.",
                is_error=True,
            )
        if got.size == 0:
            return chat_tools.ToolOutcome(
                "The URL answered an empty body; nothing was kept.", is_error=True
            )
        wanted = _string_input(tool_input, "name").strip() or got.filename
        filename = chat_web.with_extension(wanted, got.media_type)
        segments = [part for part in _string_input(tool_input, "folder").split("/") if part.strip()]
        try:
            async with backup_shared(root), workspace_lock(root), ctx.sessionmaker() as db:
                folder_id = await filing.ensure_folders(db, settings, user_id, segments)
                row = await filing.keep_file(
                    db,
                    settings,
                    user_id=user_id,
                    folder_id=folder_id,
                    temp=temp,
                    filename=filename,
                    declared_type=got.media_type,
                    size=got.size,
                    what="The download",
                    revision="download",
                )
                place = await folder_names(db, user_id, folder_id)
        except ApiError as exc:
            if exc.error_type == "storage_full":
                return chat_tools.ToolOutcome(
                    "The user's storage is full; the file was not kept.", is_error=True
                )
            return chat_tools.ToolOutcome(f"The file was not kept: {exc.message}", is_error=True)
    finally:
        # Whatever was not moved into place goes; the descriptor last, since the staging
        # file's lock lives with it (docs/workspace.md § Files).
        with contextlib.suppress(OSError, safe_io.UnsafeEntry):
            await asyncio.to_thread(safe_io.remove_file, root, temp)
        with contextlib.suppress(OSError):
            os.close(fd)
    await _record_tool_call(ctx, "download_file", settle=False)
    where = "/".join(place) if place else "the top level of the user's files"
    return chat_tools.ToolOutcome(
        f"Kept {row.name} ({row.size_bytes:,} bytes, {row.media_type}) in {where}. "
        f"Tell the user it is their file {row.name}."
    )


# --- converting a kept file ----------------------------------------------------------------------

NO_CREDITS_FOR_CONVERSION = (
    "The conversion was not run: the account's credits are spent, and a conversion would "
    "cost more. Answer with what you have."
)
# How often a turn driving a parse looks for a Stop (docs/chat.md § Tools).
CONVERT_POLL_SECONDS = 2.0
VM_FILES = chat_tools.VM_ROOT / "files"


def _library_segments(raw: str) -> list[str] | None:
    """The folders and the name a `convert_file` path names, as the Files page shows them
    (`Papers/2017/attention.pdf`) or under `/workspace/files/`; None for a path that names
    nothing, or one with a `.` or `..` component."""
    text = raw.strip().replace("\\", "/")
    prefix = f"{VM_FILES.as_posix()}/"
    if text.startswith(prefix):
        text = text[len(prefix) :]
    segments = [part.strip() for part in text.split("/") if part.strip()]
    if not segments or any(part in (".", "..") for part in segments):
        return None
    return segments


async def _folder_by_path(
    db: AsyncSession, user_id: int, names: list[str]
) -> tuple[bool, int | None]:
    """The live folder a path of names leads to, as `filing.ensure_folders` finds one but
    making none: `(found, folder id)`, the id None for the top level."""
    folders = await user_folders(db, user_id)
    trashed = trashed_folder_ids(folders.values())
    parent_id: int | None = None
    for name in names:
        found = next(
            (
                folder
                for folder in folders.values()
                if folder.parent_id == parent_id
                and folder.name == name
                and folder.id not in trashed
            ),
            None,
        )
        if found is None:
            return False, None
        parent_id = found.id
    return True, parent_id


async def _run_convert(ctx: TurnContext, tool_input: dict) -> chat_tools.ToolOutcome:
    """One `convert_file` call, a write of the turn: the file becomes a document exactly as
    the Files page converts one, and the parse is then driven inside the turn (docs/chat.md
    § Tools, *A conversion runs inside the turn*; docs/jobs.md § Converting a file)."""
    settings = ctx.settings
    user_id = ctx.turn.user_id
    root = ctx.paths.root
    segments = _library_segments(_string_input(tool_input, "path"))
    if segments is None:
        return chat_tools.ToolOutcome(
            "The path names no file: give the file's folders and name under the user's "
            "files, as in 'Papers/2017/attention.pdf'.",
            is_error=True,
        )
    if ctx.credits_exhausted:
        return chat_tools.ToolOutcome(NO_CREDITS_FOR_CONVERSION, is_error=True)
    if not _turn_cap(ctx, "convert_file", settings.convert_file_max_per_turn):
        return _turn_cap_reached("convert_file", settings.convert_file_max_per_turn)
    *folders, name = segments
    shown_path = "/".join(segments)
    shown_doc = (VM_FILES / shown_path / DOC_FILE).as_posix()
    try:
        async with backup_shared(root), workspace_lock(root), ctx.sessionmaker() as db:
            await recover_trash(db, settings, user_id)
            found, folder_id = await _folder_by_path(db, user_id, folders)
            row = None
            if found:
                # What is in the Trash takes no name (docs/workspace.md § Files): a trashed
                # row of the same name and place is not the file named.
                row = (
                    await db.execute(
                        select(Document).where(
                            Document.user_id == user_id,
                            Document.name == name,
                            Document.trashed_at.is_(None),
                            Document.folder_id == folder_id
                            if folder_id is not None
                            else Document.folder_id.is_(None),
                        )
                    )
                ).scalar_one_or_none()
            if row is None or not is_live(row, await user_folders(db, user_id)):
                return chat_tools.ToolOutcome(
                    f"No file at {shown_path} in the user's files.", is_error=True
                )
            row_id = row.id
            converted_here = row.kind == FILE_KIND
            if converted_here:
                user = await db.get(User, user_id)
                if user is None:
                    return chat_tools.ToolOutcome("The user no longer exists.", is_error=True)
                row = await convert_kept_file(db, settings, user=user, row=row)
            job = await db.get(Job, row.job_id) if row.job_id is not None else None
            if job is None or (job.status == "succeeded" and not converted_here):
                # A document already — a markdown one has no parse at all.
                return chat_tools.ToolOutcome(
                    f"{shown_path} is a document already: read {shown_doc}.", is_error=True
                )
            if job.status == "failed":
                return chat_tools.ToolOutcome(
                    f"The conversion of {shown_path} failed earlier: "
                    f"{job.error or 'the parse failed'}. The user can retry it from the Files "
                    "page.",
                    is_error=True,
                )
            # A document whose parse still waits — an upload converting on arrival, queued
            # behind this turn — is driven below as this call's own conversion would be; a
            # job already at its end answered from the cache: this call charged nothing.
            job_id = job.id
            converted_earlier = converted_here and job.status == "succeeded"
    except ApiError as exc:
        return chat_tools.ToolOutcome(
            f"{shown_path} was not converted: {exc.message}", is_error=True
        )
    if converted_here:
        await _record_tool_call(ctx, "convert_file", settle=False)
    job = await _drive_parse(ctx, job_id)
    if job is None:
        if ctx.convert_stop.is_set() or await _cancelled(ctx):
            return chat_tools.ToolOutcome(
                f"The conversion of {shown_path} was interrupted with this turn and goes on "
                "after it: the document opens in Files when it is done, charged once.",
                is_error=True,
            )
        return chat_tools.ToolOutcome(
            f"The conversion of {shown_path} is not finished after "
            f"{int(settings.convert_file_timeout_seconds)} seconds and goes on after this "
            "turn: the document opens in Files when it is done, charged once. Read it in a "
            "later turn.",
            is_error=True,
        )
    if job.status != "succeeded":
        return chat_tools.ToolOutcome(
            f"The conversion of {shown_path} failed: {job.error or 'the parse failed'}. Its "
            "credits were released; the user can retry it from the Files page.",
            is_error=True,
        )
    async with ctx.sessionmaker() as db:
        row = await db.get(Document, row_id)
        if row is None:
            return chat_tools.ToolOutcome(f"{shown_path} is gone.", is_error=True)
        try:
            # Done by the finish of a parse the turn drove; a cache hit's document is
            # written here, as a reader's first open would write it.
            paths = await materialize_document(db, settings, row)
        except ApiError as exc:
            return chat_tools.ToolOutcome(
                f"The document of {shown_path} could not be written: {exc.message}",
                is_error=True,
            )
        credits = (await ledger.credits_by_job(db, [job.id])).get(job.id)
    doc_path = (chat_tools.VM_ROOT / paths.relative.as_posix() / DOC_FILE).as_posix()
    cost = (
        "no credits: the same file was converted before"
        if converted_earlier
        else f"{credits:,} credits"
        if credits is not None
        else "no credits"
    )
    pages = job.page_count or 0
    return chat_tools.ToolOutcome(
        f"Converted {row.name} ({pages} page{'s' if pages != 1 else ''}, {cost}) into a "
        f"document at {doc_path}; read it with str_replace_based_edit_tool. Tell the user "
        f"it is their document {row.name}."
    )


async def _drive_parse(ctx: TurnContext, job_id: uuid.UUID) -> Job | None:
    """The job a conversion queued, driven to its end inside the turn: claimed as the
    worker would claim it and run with the parse runner, or — already running, a stale
    worker's — waited for; a job the dedup reuse found already queued is claimed the
    same way. None when the wait ended before the job did — the tool's timeout, a Stop,
    the end of the turn — with the job put back on the queue keeping its finished pages,
    for the worker to take after the turn (docs/chat.md § Tools, *A conversion runs
    inside the turn*)."""
    settings = ctx.settings
    deadline = time.monotonic() + settings.convert_file_timeout_seconds
    while True:
        async with ctx.sessionmaker() as db:
            job = await db.get(Job, job_id)
            if job is None:
                return None
            if job.status in ("succeeded", "failed"):
                return job
            claimed = job.status == "queued" and await claim_job(db, job_id, ctx.generation)
        if not claimed:
            # Another worker's — a stale one still holding it — or claimed and finished
            # between the two reads: looked at again, within the same bounds, and no longer
            # once the turn is ending around this call.
            if time.monotonic() >= deadline or ctx.convert_stop.is_set() or await _cancelled(ctx):
                return None
            await asyncio.sleep(CONVERT_POLL_SECONDS)
            continue
        run = asyncio.create_task(
            run_job(ctx.sessionmaker, settings, job_id, ctx.render_slots, ctx.generation)
        )
        ctx.inline_parse = run
        try:
            finished = await _await_inline_parse(ctx, run, deadline)
        finally:
            ctx.inline_parse = None
        if finished and run.exception() is not None:
            # As the worker loop answers for a run that raised past the runner: failed,
            # never requeued, since the parse ran and billed (docs/jobs.md § Queue).
            logger.error("job %s raised past its runner", job_id, exc_info=run.exception())
            try:
                await fail_job(ctx.sessionmaker, settings, job_id, "internal error", ctx.generation)
            except Exception:  # noqa: BLE001 - the next worker start requeues what stays running
                logger.exception("could not fail job %s", job_id)
            continue
        if finished:
            continue
        async with ctx.sessionmaker() as db:
            requeued = await requeue_job(db, job_id, ctx.generation)
        if requeued:
            # This attempt's crops go with it, as on a worker's shutdown (docs/jobs.md).
            discard_job_figures(Path(settings.figures_dir), job_id)
            logger.info("job %s put back on the queue by the turn that drove it", job_id)
            return None
        # Finished before the cancel landed: its state is read on the next pass.


async def _await_inline_parse(ctx: TurnContext, run: asyncio.Task, deadline: float) -> bool:
    """Wait for the parse the turn drives: True once it has run to its end, False when
    the deadline passed, the user pressed Stop, or the end of the turn asked for it or
    cancelled it — the run cancelled and gathered either way, so the caller can put the
    job back."""
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = await asyncio.wait({run}, timeout=min(CONVERT_POLL_SECONDS, remaining))
            if done:
                return not run.cancelled()
            if ctx.convert_stop.is_set() or await _cancelled(ctx):
                break
    except asyncio.CancelledError:
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        raise
    run.cancel()
    await asyncio.gather(run, return_exceptions=True)
    return False


# --- the loop ---------------------------------------------------------------------------------


async def _loop(ctx: TurnContext) -> None:
    messages = await _history(ctx)
    since_marker = 0
    compaction = ctx.turn.kind in COMPACTION_TURNS
    while True:
        if await _cancelled(ctx):
            raise TurnCancelled()
        # The credits ran out and the tool calls allowed after them are used: this call is
        # the turn's last, and answers with what the turn has (docs/billing.md § A chat turn).
        # A compaction turn's last call comes once its own allowance of tool calls is used:
        # it answers with the summary (docs/chat.md § Compaction).
        final = ctx.overdraft_spent() or (
            compaction and ctx.tool_calls >= ctx.settings.chat_compact_max_tool_calls
        )
        body = build_request(ctx, messages, since_marker=since_marker, final=final)
        if since_marker >= CACHE_GUARD_EVERY:
            since_marker = 0
        completed = await _call_model(ctx, body)
        await _record_usage_settled(
            ctx, completed.model or ctx.model, completed.usage, conversation=True
        )
        if completed.thinking_dropped:
            # The replay sent a block the API no longer takes: a change the binding cannot
            # see. The API would drop it again on every later call, with every block made
            # since, so the rest of the turn sends none of the request's thinking — the
            # messages as sent carry no binding, so every thinking block in them is left
            # out — and the answer is stored with the cutoff a later replay starts its
            # thinking from (docs/chat.md § History is append-only).
            logger.warning(
                "turn %s: the upstream dropped %d thinking blocks bound to another prompt",
                ctx.turn.id,
                completed.thinking_dropped,
            )
            messages = stale_thinking_left_out(messages, ctx.binding)
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
        if completed.stop_reason == "max_tokens" or final:
            # What came through is kept; a tool call cut off with the limit is not — it is
            # never run, and a tool_use with no result would make the history unsendable.
            # Nor is one the last call made with no tool left to it.
            content = [block for block in content if block.get("type") != "tool_use"]
        if content:
            # Each thinking block is stored with the prompt and tools it was made under, so a
            # later replay can tell whether it may still be sent (docs/chat.md § History is
            # append-only); the replay of this turn sends it as the API returned it.
            stored = [
                {**block, "binding": ctx.binding} if block.get("type") in THINKING_TYPES else block
                for block in content
            ]
            if completed.thinking_dropped:
                stored[0] = {**stored[0], "thinking_cutoff": True}
            await _store(ctx, "assistant", stored, model=followed)
            messages.append({"role": "assistant", "content": content})
            # Positions are content blocks, not messages (docs/chat.md § Prompt caching).
            since_marker += len(content)
        elif followed is not None:
            await _follow_model(ctx, followed)

        if final and completed.stop_reason == "tool_use" and ctx.overdraft_spent():
            # Kept and billed: the answer so far stays in the thread, and no tool runs on
            # a balance that is gone (docs/billing.md § A chat turn).
            raise TurnOutOfCredits()
        if completed.stop_reason == "refusal":
            raise TurnFailed("The model declined this request")
        if completed.stop_reason == "max_tokens":
            raise TurnFailed("The answer reached the output limit before it was complete")
        if completed.stop_reason in ("end_turn", "stop_sequence") or (
            final and completed.stop_reason == "tool_use"
        ):
            if compaction:
                # The answer is the summary every later turn replays in place of the
                # history (docs/chat.md § Compaction).
                await _store_summary(ctx, content)
            return
        if completed.stop_reason != "tool_use" or not tool_uses:
            raise TurnFailed(f"The model stopped unexpectedly ({completed.stop_reason})")
        if await _cancelled(ctx):
            raise TurnCancelled()

        room = len(tool_uses)
        if compaction:
            # What the compaction's allowance leaves this round: the first calls run, the
            # rest are answered, so no round overruns it (docs/chat.md § Compaction).
            room = max(0, ctx.settings.chat_compact_max_tool_calls - ctx.tool_calls)
        ctx.tool_calls += len(tool_uses)
        if ctx.tool_calls > ctx.settings.chat_max_tool_calls_per_turn:
            raise TurnFailed(
                f"The turn made more than {ctx.settings.chat_max_tool_calls_per_turn} tool calls"
            )
        results = await _run_tools(ctx, tool_uses, completed.malformed_tool_uses, room=room)
        # Every result in one user message, in the order the calls were made
        # (docs/chat.md § Tools).
        await _store(ctx, "user", list(results))
        # Replayed as the API takes it: a subagent's record is the panel's, not the model's.
        messages.append({"role": "user", "content": _api_blocks(list(results))})
        since_marker += len(results)


async def _store_summary(ctx: TurnContext, content: list[dict]) -> None:
    """The compaction's summary as the user message the replay starts from, stored as any
    message is: the turn's finish makes it the boundary, in the commit that ends the turn
    as succeeded (`_finish`), so a cancel or a worker's death that lands between leaves a
    turn that set nothing and a row no replay reads. The memory file is read here, before
    that commit, for the snapshot the finish stores (docs/chat.md § Compaction)."""
    summary = "\n\n".join(
        block["text"]
        for block in content
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    ).strip()
    if not summary:
        raise TurnFailed("compact_empty")
    memory_file = await memory_file_text(ctx.settings, ctx.turn.user_id)
    async with ctx.sessionmaker() as db:
        await _still_ours(db, ctx)
        row = Message(
            conversation_id=ctx.conversation.id,
            role="user",
            content=[
                {
                    "type": "text",
                    "text": chat_agent.render_summary(summary),
                    "context": SUMMARY_CONTEXT,
                }
            ],
            turn_id=ctx.turn.id,
        )
        db.add(row)
        await db.flush()
        message_id = row.id
        await db.commit()
    ctx.compacted = Compacted(message_id, memory_file)


def _keep_place(kind: str) -> dict:
    """The `updated_at` an idle compaction's writes leave as it was: the list orders by it,
    and the worker compacting on its own is no thread continued (docs/chat.md
    § Compaction, *Idle compaction*). Every other turn's writes move it, as its finish does."""
    if kind == IDLE_COMPACT_TURN:
        return {"updated_at": Conversation.updated_at}
    return {}


async def _finish(
    sessionmaker: async_sessionmaker[AsyncSession],
    turn_id: uuid.UUID,
    *,
    status: str,
    error: str | None = None,
    compacted: Compacted | None = None,
) -> None:
    """The terminal state and its event in one commit, so a stream that sees the turn
    stop always finds the event that says how. A turn the user cancelled stays
    cancelled whatever the loop concluded. A compaction's boundary (`compacted`) is set
    in the same commit, and only by a turn that ends succeeded: one that ends any other
    way set nothing, whatever it stored (docs/chat.md § Compaction)."""
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
        await ledger.close_chat(db, turn.id)
        # The list orders by `updated_at`: a thread continued moves, one the worker
        # compacted on its own does not (docs/chat.md § Compaction).
        values: dict = (
            {"updated_at": utcnow()} if turn.kind != IDLE_COMPACT_TURN else _keep_place(turn.kind)
        )
        if status == "succeeded" and compacted is not None:
            # The boundary; the memory snapshots taken again — the history that held the
            # agent's writes is no longer sent — and the context size and the cache clock
            # cleared: what the next call replays is the summary, whose size only that
            # call reports, cached by none before it (docs/chat.md § Compaction).
            values.update(
                compacted_message_id=compacted.message_id,
                memory_ids=None,
                memory_file=compacted.memory_file,
                context_tokens=None,
                cache_warm_until=None,
            )
        elif turn.kind in COMPACTION_TURNS:
            # A compaction that ended any other way: the clock its own calls set is
            # cleared too, or the idle pass would queue another paid attempt at what just
            # failed within the hour; the next message's call sets it again (docs/chat.md
            # § Compaction, *Idle compaction*).
            values["cache_warm_until"] = None
        await db.execute(
            update(Conversation).where(Conversation.id == turn.conversation_id).values(**values)
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


async def _settle_writers(ctx: TurnContext) -> None:
    """Wait for every mutating tool call the end of the turn interrupted — the agent's own
    or a subagent's, and a `memory` call (docs/chat.md § Tools): cancelling the loop does
    not stop a shell command in the VM or an editor thread, and the terminal event must not
    precede their commit, nor a memory's commit precede no event at all."""
    while pending := [writer for writer in ctx.writers if not writer.done()]:
        writer = pending[0]
        await _settle_writer(ctx, writer, ctx.writers.get(writer))
        ctx.writers.pop(writer, None)


async def _settle_writer(ctx: TurnContext, writer: asyncio.Future, tool: str | None) -> None:
    """One writer, waited for. Past the grace a shell command is killed in the VM (its
    shell restarted) and the writer is then waited for to its end — every step of it is
    bounded on its own (the runner client's deadline, git's timeouts), so the wait is
    too — rather than the turn being finished over a write still in flight."""
    if writer.done():
        return
    if tool == "convert_file":
        # A parse the turn drives is not waited out: stopped, it goes back on the queue
        # keeping its pages, and the worker takes it after the turn (docs/chat.md § Tools,
        # *A conversion runs inside the turn*) — the wait on it ends at the event, and so
        # does the poll of a job not yet claimed.
        ctx.convert_stop.set()
        if ctx.inline_parse is not None and not ctx.inline_parse.done():
            ctx.inline_parse.cancel()
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
    if tool == "bash" and ctx.sandbox is not None:
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
    generation: int | None = None,
) -> None:
    """Drive one claimed turn to a terminal state. Never raises for the turn's own faults.
    `generation` is the worker's, for a parse the turn drives itself (docs/chat.md
    § Tools, *A conversion runs inside the turn*)."""
    try:
        ctx = await _load(sessionmaker, settings, turn_id, render_slots, generation)
    except TurnFailed as exc:
        await _finish(sessionmaker, turn_id, status="failed", error=str(exc))
        return

    # What the turn was given beyond the document: the answer's *Used:* line (docs/chat.md
    # § Memory).
    await _emit(
        ctx,
        chat_events.TURN_START,
        {
            "model": ctx.model,
            "kind": ctx.turn.kind,
            "instructions": ctx.instructions,
            "memory_ids": ctx.memory_ids,
        },
    )
    try:
        async with asyncio.timeout(settings.chat_turn_timeout_seconds):
            await _loop(ctx)
    except TurnCancelled:
        await _settle_writers(ctx)
        await _finish(sessionmaker, turn_id, status="cancelled", error="cancelled")
    except TurnFailed as exc:
        await _settle_writers(ctx)
        await _finish(sessionmaker, turn_id, status="failed", error=str(exc))
    except TurnOutOfCredits:
        await _settle_writers(ctx)
        await _finish(sessionmaker, turn_id, status="failed", error="out_of_credits")
    except TimeoutError:
        await _settle_writers(ctx)
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
        await _settle_writers(ctx)
        raise
    except Exception:
        # A read's own failure (its cancel check lost the database) cancels the round
        # around a shielded writer: that write finishes and commits before the terminal
        # event, like a timeout's (docs/chat.md § Tools).
        logger.exception("turn %s failed unexpectedly", turn_id)
        await _settle_writers(ctx)
        await _finish(sessionmaker, turn_id, status="failed", error="internal error")
    else:
        await _finish(sessionmaker, turn_id, status="succeeded", compacted=ctx.compacted)
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
