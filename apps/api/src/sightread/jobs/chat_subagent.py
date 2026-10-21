"""A subagent's run: a nested loop inside one `subagent` call of a turn (docs/chat.md
§ Subagents).

It is the turn's loop in miniature — the same request shape, the same streaming, cancel
polling and billing (`chat_runner`'s own) — with two differences that are the point of
it: its messages are never stored (what the conversation keeps is the call and its
result), and its events carry `parent`, the id of the call that started it, so the panel
can draw its steps — and the diff cards of a subagent that writes — inside its card. Its
tools are the subset it was given; it writes only if it may, through the agent's own
paths, one write of the turn at a time (`chat_runner.run_writer`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from ..agents import chat as chat_agent
from ..agents import subagents as subagent_defs
from ..upstream.client import PaymentRequired, RateLimited, UpstreamError
from ..workspace import git
from ..workspace.documents import MARKDOWN_KIND
from . import chat_events, chat_runner, chat_tools

logger = logging.getLogger(__name__)

# What the stored record keeps of a run: enough for the panel to draw it, bounded however
# long the run was (docs/chat.md § Subagents).
STEPS_MAX = 200
NOTE_MAX_CHARS = 600
INPUT_MAX_CHARS = 2000
# A long text field of an input — a write's new text — as the record keeps it: the step's
# label needs the command and the path, and its diff comes from the `edit` event.
FIELD_MAX_CHARS = 200
READ_ONLY = "This subagent can only read: the editor answers `view` here."
EDITOR = "str_replace_based_edit_tool"
IMAGE_RUN_BUDGET_SPENT = "This subagent's image budget is spent; report with what you have seen."
NO_REPORT = "The subagent finished without a report."


class _ImageBudget:
    """The run's own image budgets — per round, and over the whole run — kept like the
    turn's (docs/chat.md § Tools): a result that would cross one is replaced before it is
    reported, one at a time, so neither is ever exceeded."""

    def __init__(self, round_max: int, total_max: int) -> None:
        self.round_max = round_max
        self.total_max = total_max
        self.round = 0
        self.total = 0

    def start_round(self) -> None:
        self.round = 0

    def refusal(self) -> chat_tools.ToolOutcome | None:
        if self.total >= self.total_max:
            return chat_tools.ToolOutcome(IMAGE_RUN_BUDGET_SPENT, is_error=True)
        if self.round >= self.round_max:
            return chat_tools.ToolOutcome(chat_runner.IMAGE_BUDGET_SPENT, is_error=True)
        return None

    def admit(self, outcome: chat_tools.ToolOutcome) -> chat_tools.ToolOutcome:
        size = chat_runner._image_bytes({"content": outcome.content})
        if size and self.total + size > self.total_max:
            return chat_tools.ToolOutcome(IMAGE_RUN_BUDGET_SPENT, is_error=True)
        if size and self.round + size > self.round_max:
            return chat_tools.ToolOutcome(chat_runner.IMAGE_BUDGET_SPENT, is_error=True)
        self.round += size
        self.total += size
        return outcome


def _bounded_input(value: object) -> object:
    """A tool call's input as the record keeps it: as it was, unless it is long — then with
    its long text fields cut, so the step still reads as what it did, and cut whole only
    when that is not enough."""
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {}
    if len(encoded) <= INPUT_MAX_CHARS:
        return value
    if isinstance(value, dict):
        cut = {
            key: field[:FIELD_MAX_CHARS] if isinstance(field, str) else field
            for key, field in value.items()
        }
        if len(json.dumps(cut, ensure_ascii=False)) <= INPUT_MAX_CHARS:
            return cut
    return encoded[:INPUT_MAX_CHARS]


async def _run_one(
    ctx: chat_runner.TurnContext,
    definition: subagent_defs.ResolvedSubagent,
    block: dict,
    malformed: set[str],
) -> chat_tools.ToolOutcome:
    name = block.get("name")
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    if str(block.get("id")) in malformed:
        return chat_tools.ToolOutcome(
            "The tool input was not valid JSON; send it again.", is_error=True
        )
    if name not in definition.tools:
        return chat_tools.ToolOutcome(
            f"{name!r} is not one of this subagent's tools", is_error=True
        )
    if name == "view_page":
        return await chat_tools.view_page(
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
    if name == "view_figure":
        return await chat_tools.view_figure(
            ctx.paths, tool_input, max_bytes=ctx.settings.chat_image_round_max_bytes
        )
    if _writes(definition, block):
        # The agent's own paths: a revision per write, the shell under the user's locks.
        return await chat_runner.run_mutating(ctx, str(name), tool_input)
    # The editor, for `view` alone, to a subagent that may not change files (docs/chat.md
    # § Subagents).
    if tool_input.get("command") != "view":
        return chat_tools.ToolOutcome(READ_ONLY, is_error=True)
    return await chat_tools.run_editor(
        ctx.paths.root,
        tool_input,
        f"{git.AGENT_AUTHOR_PREFIX}{ctx.conversation.id}",
        max_chars=ctx.settings.chat_tool_result_max_chars,
        max_bytes=ctx.settings.content_max_bytes,
    )


def _writes(definition: subagent_defs.ResolvedSubagent, block: dict) -> bool:
    """A call that may change files: the shell, or an editor command other than `view`, of a
    subagent that may change files."""
    if not definition.writes:
        return False
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
    return block.get("name") == "bash" or (
        block.get("name") == EDITOR and tool_input.get("command") != "view"
    )


async def _run_one_guarded(
    ctx: chat_runner.TurnContext,
    definition: subagent_defs.ResolvedSubagent,
    block: dict,
    malformed: set[str],
) -> chat_tools.ToolOutcome:
    """A tool that raises answers an error result, as the turn's own do."""
    try:
        return await _run_one(ctx, definition, block, malformed)
    except (asyncio.CancelledError, chat_runner.TurnCancelled):
        raise
    except Exception:  # noqa: BLE001 - any failure is this tool's, never the run's
        logger.exception("a subagent's %s failed in turn %s", block.get("name"), ctx.turn.id)
        return chat_tools.ToolOutcome(
            "The tool failed unexpectedly; try another way.", is_error=True
        )


async def _run_tools(
    ctx: chat_runner.TurnContext,
    parent: str,
    definition: subagent_defs.ResolvedSubagent,
    tool_uses: list[dict],
    malformed: set[str],
    steps_by_id: dict[str, dict],
    budget: _ImageBudget,
) -> list[dict]:
    """One round of the run's calls, the results in the order of the calls. Reads run
    together, at most as many at a time as the turn's, each looking for a Stop before it
    starts; writes run one after another, each waiting its turn with every other write of
    the turn and started only if no Stop came (docs/chat.md § Tools)."""
    results: dict[int, dict] = {}
    slots = asyncio.Semaphore(chat_runner.READ_CONCURRENCY)
    stopped = asyncio.Event()
    budget.start_round()

    async def report(block: dict, outcome: chat_tools.ToolOutcome) -> dict:
        tool_use_id = str(block.get("id"))
        summary = outcome.content if isinstance(outcome.content, str) else "(image)"
        await chat_runner._emit(
            ctx,
            chat_events.TOOL_RESULT,
            {
                "id": tool_use_id,
                "name": block.get("name"),
                "is_error": outcome.is_error,
                "output": summary,
                "parent": parent,
            },
        )
        if outcome.edit is not None:
            # The viewer reloads on it, and the panel joins it to the step's diff card.
            await chat_runner._emit(
                ctx,
                chat_events.EDIT,
                {**outcome.edit, "tool_use_id": tool_use_id, "parent": parent},
            )
        if tool_use_id in steps_by_id:
            steps_by_id[tool_use_id]["is_error"] = outcome.is_error
        result: dict = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": outcome.content,
        }
        if outcome.is_error:
            result["is_error"] = True
        return result

    async def reads() -> None:
        async def one(index: int, block: dict) -> None:
            async with slots:
                # Noted, not raised, as the turn's own reads do: a raise here would leave
                # the reads beside it running while the run ends.
                if stopped.is_set() or await chat_runner._cancelled(ctx):
                    stopped.set()
                    return
                outcome = budget.refusal() or await _run_one_guarded(
                    ctx, definition, block, malformed
                )
                results[index] = await report(block, budget.admit(outcome))

        await asyncio.gather(
            *(
                one(index, block)
                for index, block in enumerate(tool_uses)
                if not _writes(definition, block)
            )
        )

    async def writes() -> None:
        async def write(block: dict) -> dict:
            # Reported inside the shielded writer: an `edit` the deadline interrupted is
            # still announced before the terminal event.
            return await report(block, await _run_one_guarded(ctx, definition, block, malformed))

        for index, block in enumerate(tool_uses):
            if _writes(definition, block):
                results[index] = await chat_runner.run_writer(
                    ctx, str(block.get("name")), lambda block=block: write(block)
                )

    # A task group: when the writes stop on a cancel, the reads still running are cancelled
    # with them rather than left to finish a run that is over.
    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(reads())
            group.create_task(writes())
    except* chat_runner.TurnCancelled:
        raise chat_runner.TurnCancelled() from None
    if stopped.is_set():
        raise chat_runner.TurnCancelled()
    return [results[index] for index in range(len(tool_uses))]


async def run_subagent(
    ctx: chat_runner.TurnContext, tool_use_id: str, tool_input: dict
) -> chat_tools.ToolOutcome:
    """One `subagent` call: the subagent named, on the task given, to its report."""
    name = tool_input.get("name")
    task = tool_input.get("task")
    definition = ctx.subagents.get(name.strip()) if isinstance(name, str) else None
    if definition is None:
        return chat_tools.ToolOutcome(
            f"No subagent named {name!r}; the system prompt lists the ones there are.",
            is_error=True,
        )
    if not isinstance(task, str) or not task.strip():
        return chat_tools.ToolOutcome("subagent needs a `task`", is_error=True)

    started = time.monotonic()
    model = definition.model or ctx.model
    # The shell only to one that may change files, and only where there is a sandbox: a
    # tool the harness cannot run is never offered (docs/chat.md § Subagents).
    shell = definition.writes and "bash" in definition.tools and ctx.sandbox is not None
    system = [
        {
            "type": "text",
            "text": subagent_defs.render_system(
                definition.prompt, ctx.facts, writes=definition.writes, shell=shell
            ),
            "cache_control": {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL},
        }
    ]
    tools = [
        tool
        for tool in chat_agent.SUBAGENT_TOOLS
        if tool["name"] in definition.tools and (tool["name"] != "bash" or shell)
    ]
    messages: list[dict] = [{"role": "user", "content": [{"type": "text", "text": task.strip()}]}]
    steps: list[dict] = []
    calls = 0
    budget = _ImageBudget(
        ctx.settings.chat_image_round_max_bytes, ctx.settings.chat_image_turn_max_bytes
    )
    report = ""
    note: str | None = None

    def record() -> dict:
        return {
            "name": definition.name,
            "model": model,
            "steps": steps[:STEPS_MAX],
            "tool_calls": calls,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }

    while True:
        if await chat_runner._cancelled(ctx):
            raise chat_runner.TurnCancelled()
        body = {
            "model": model,
            "max_tokens": ctx.settings.chat_max_output_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "thinking": {"type": "adaptive", "display": "updates"},
            "output_config": {"effort": ctx.effort},
            "fallbacks": "default",
            "cache_control": {"type": "ephemeral", "ttl": chat_agent.CACHE_TTL},
        }
        try:
            completed = await chat_runner._call_model(ctx, body, parent=tool_use_id)
        except PaymentRequired:
            return chat_tools.ToolOutcome(
                "The subagent could not run: the upstream reported exhausted credits.",
                is_error=True,
                subagent=record(),
            )
        except (RateLimited, UpstreamError) as exc:
            reason = str(exc) or "the upstream rate-limited"
            return chat_tools.ToolOutcome(
                f"The subagent's model call failed: {reason}", is_error=True, subagent=record()
            )
        # Billed whether or not anything of it is used, under the model it ran on, not the
        # conversation's (docs/chat.md § Subagents).
        if not completed.model:
            completed.model = model
        await chat_runner._record_usage_settled(ctx, completed)
        if await chat_runner._cancelled(ctx):
            raise chat_runner.TurnCancelled()
        if completed.model and completed.model != model:
            # A fallback answered: the blocks the run replays are that model's now.
            model = completed.model

        tool_uses = [block for block in completed.content if block.get("type") == "tool_use"]
        content = completed.content
        if completed.stop_reason == "max_tokens":
            # A call cut with the limit is never run, as in the turn's own loop.
            content = [block for block in content if block.get("type") != "tool_use"]
        steps_by_id: dict[str, dict] = {}
        for block in content:
            if block.get("type") == "thinking":
                text = str(block.get("thinking") or "").strip()
                if text:
                    steps.append({"type": "note", "text": text[:NOTE_MAX_CHARS]})
            elif block.get("type") == "tool_use":
                step = {
                    "type": "tool",
                    # What joins the step to its `edit` event, and so to its diff card.
                    "id": str(block.get("id")),
                    "name": block.get("name"),
                    "input": _bounded_input(block.get("input")),
                    "is_error": None,
                }
                steps.append(step)
                steps_by_id[str(block.get("id"))] = step
        # The report is the last answer that said anything (docs/chat.md § Subagents).
        said = [
            str(block.get("text") or "").strip()
            for block in content
            if block.get("type") == "text" and str(block.get("text") or "").strip()
        ]
        if said:
            report = "\n\n".join(said)
        if content:
            messages.append({"role": "assistant", "content": content})

        stop = completed.stop_reason
        if stop in ("end_turn", "stop_sequence"):
            break
        if stop == "max_tokens":
            note = "The subagent's answer reached the output limit; this is what it had."
            break
        if stop == "refusal":
            return chat_tools.ToolOutcome(
                "The subagent declined the task.", is_error=True, subagent=record()
            )
        if stop != "tool_use" or not tool_uses:
            return chat_tools.ToolOutcome(
                f"The subagent stopped unexpectedly ({stop}).", is_error=True, subagent=record()
            )
        calls += len(tool_uses)
        cap = ctx.settings.chat_subagent_max_tool_calls
        if calls > cap:
            # The calls past the cap are never run; the record says so rather than
            # leaving them pending.
            for step in steps_by_id.values():
                step["is_error"] = True
            note = f"The subagent made more than {cap} tool calls and was stopped here."
            break
        results = await _run_tools(
            ctx,
            tool_use_id,
            definition,
            tool_uses,
            completed.malformed_tool_uses,
            steps_by_id,
            budget,
        )
        messages.append({"role": "user", "content": results})

    answer = report.strip() or NO_REPORT
    if note is not None:
        answer = f"{answer}\n\n[{note}]"
    return chat_tools.ToolOutcome(answer, subagent=record())
