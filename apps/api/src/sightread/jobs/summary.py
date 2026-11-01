"""The summary model's one call, and the title `/rename` asks it for (docs/chat.md
§ Conversations, turns, events, *The summary model*; docs/billing.md § A chat turn).

The call is made by the API, between turns: one request on `SUMMARY_MODEL` — or the
conversation's own model — with no tools, bounded in total by `SUMMARY_TIMEOUT_SECONDS`
and not retried. A title is billed by its tokens on a `chat` entry of its own, reserved
before the call and settled after it.
"""

from __future__ import annotations

import asyncio
import contextlib

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents import CHAT
from ..agents import summary as summary_defs
from ..billing import ledger
from ..billing.credits import credits_for_usd
from ..config import Settings
from ..db.models import (
    MESSAGE_TURN,
    AgentSetting,
    Conversation,
    Document,
    Message,
    UsageLog,
    User,
)
from ..errors import ApiError
from ..upstream.anthropic import CompletedMessage, StreamProgress, stream_messages
from ..upstream.client import Connection, UpstreamError, kano_connection
from . import chat_versions
from .intake import insufficient_credits


async def call(
    connection: Connection, settings: Settings, body: dict, progress: StreamProgress
) -> CompletedMessage:
    """One request to the summary model, in at most `SUMMARY_TIMEOUT_SECONDS` from the
    first byte sent to the last read. `progress` follows the stream, so a call that fails
    or runs out of time still says what it used."""
    try:
        async with asyncio.timeout(settings.summary_timeout_seconds):
            async with contextlib.aclosing(
                stream_messages(
                    connection,
                    body,
                    max_bytes=settings.upstream_response_max_bytes,
                    line_max_bytes=settings.upstream_sse_line_max_bytes,
                    blocks_max=settings.upstream_content_blocks_max,
                    timeout=settings.summary_timeout_seconds,
                    progress=progress,
                )
            ) as stream:
                async for item in stream:
                    if isinstance(item, CompletedMessage):
                        return item
    except TimeoutError as exc:
        raise UpstreamError("The upstream took too long") from exc
    raise UpstreamError("The upstream closed the stream early")


async def write_title(
    db: AsyncSession, settings: Settings, conversation: Conversation, *, locale: str
) -> str:
    """Ask the summary model for the conversation's title, bill the call and write the
    title, `updated_at` kept as it was. Returns the title; raises `ApiError` for every
    refusal and failure of docs/api.md, the title then unchanged."""
    connection = kano_connection(settings, anthropic=True)
    if connection is None:
        raise ApiError(503, "upstream", "Kano Proxy is not configured on this deployment")

    # The branch the conversation shows, root first, and its `message` turns' rows: what
    # the reader sees is what the title is about (docs/chat.md § History is append-only).
    shown = chat_versions.branch(
        await chat_versions.conversation_turns(db, conversation.id), conversation.head_id
    )
    message_turns = [turn.id for turn in shown if turn.kind == MESSAGE_TURN]
    messages = (
        (
            await db.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    # The first and the latest: the two a title is written from.
                    Message.turn_id.in_({message_turns[0], message_turns[-1]}),
                )
                .order_by(Message.id)
            )
        )
        .scalars()
        .all()
        if message_turns
        else []
    )
    exchanges = summary_defs.title_exchanges(
        message_turns, messages, settings.title_message_max_chars
    )
    if not exchanges:
        raise ApiError(
            400, "nothing_to_rename", "There is nothing to name in this conversation yet"
        )

    model = settings.summary_model.strip() or conversation.model
    price = settings.model_prices.get(model) if model else None
    if model is None or price is None:
        raise ApiError(
            503, "upstream", "The model that writes titles is not priced on this deployment"
        )
    document = await db.get(Document, conversation.document_id)
    setting = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == conversation.user_id, AgentSetting.agent == CHAT.name
            )
        )
    ).scalar_one_or_none()
    language = (setting.answer_language if setting else None) or locale
    body = summary_defs.title_request(
        model,
        document.name if document else "",
        exchanges,
        language,
        settings.summary_max_output_tokens,
    )

    # The entry first, under the user's row lock, as a send takes it: the call needs the
    # balance a send needs (docs/billing.md § A chat turn). Committed before the call, so
    # no lock is held while the model writes.
    await db.execute(select(User.id).where(User.id == conversation.user_id).with_for_update())
    try:
        entry = await ledger.reserve(
            db,
            settings,
            user_id=conversation.user_id,
            kind=ledger.CHAT,
            amount=0,
            model=model,
            minimum=settings.chat_min_credits,
        )
    except ledger.InsufficientCredits as exc:
        raise insufficient_credits(exc) from exc
    entry_id = entry.id
    await db.commit()

    progress = StreamProgress()
    completed: CompletedMessage | None = None
    try:
        completed = await call(connection, settings, body, progress)
    except UpstreamError:
        completed = None
    usage = completed.usage if completed is not None else progress.usage_so_far()

    # What the call used is billed whatever came of it, as a cut-short turn's call is.
    amount = 0
    if usage is not None:
        db.add(
            UsageLog(
                user_id=conversation.user_id,
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
        amount = credits_for_usd(
            price.usd(
                prompt_tokens=usage.input_tokens,
                completion_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            ),
            settings.credit_usd,
        )
    await ledger.settle_call(db, entry_id, amount)

    title = ""
    if completed is not None and completed.stop_reason != "refusal":
        title = summary_defs.clean_title(
            "".join(
                str(block.get("text") or "")
                for block in completed.content
                if block.get("type") == "text"
            )
        )
    if title:
        # The title alone: the list orders by `updated_at`, and naming a thread is not
        # continuing it (docs/chat.md § Conversations, turns, events).
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(title=title, updated_at=Conversation.updated_at)
        )
    await db.commit()
    if completed is None:
        raise ApiError(502, "upstream", "The model could not be reached; try again")
    if not title:
        raise ApiError(502, "upstream", "The model wrote no title; try again")
    return title
