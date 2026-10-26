"""Control plane `/api/*` — session-cookie authenticated, consumed by the web app.

Mutations additionally require an `X-Requested-With` header (CSRF pairing with
SameSite=Lax), enforced by `require_csrf_header`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from ..agents import AGENTS, CHAT, CHECK, PARSE, AgentDefinition, get_agent
from ..agents import chat as chat_agent
from ..agents import memory as memory_defs
from ..agents import skills as skill_defs
from ..agents import subagents as subagent_defs
from ..agents.bbox import convention_for_model
from ..auth.api_keys import create_api_key
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..auth.oidc import (
    DEV_USER_EMAIL,
    DEV_USER_SUB,
    POST_LOGIN_KEY,
    POST_LOGIN_LOCALE_KEY,
    upsert_user,
)
from ..auth.sessions import SESSION_COOKIE, SESSION_TTL, create_session, delete_session
from ..billing import ledger, pay
from ..billing.plans import ENDED_STATUSES
from ..db.models import (
    COST_SOURCE_ESTIMATED,
    COST_SOURCE_OPENROUTER,
    COST_SOURCE_UNKNOWN,
    DOCUMENT_VIEWS,
    EFFORT_DEFAULT,
    EFFORT_LEVELS,
    AgentSetting,
    ApiKey,
    ChatAttachment,
    ChatTurn,
    Checkout,
    Conversation,
    Document,
    Job,
    PromptPreset,
    Skill,
    Subagent,
    Subscription,
    UsageLog,
    User,
    utcnow,
)
from ..errors import ApiError
from ..jobs import chat_memory, chat_runner
from ..jobs.intake import PDF_MEDIA_TYPE
from ..jobs.queue import count_active_turns
from ..jobs.sweeper import discard_account
from ..parsing.attachments import ATTACHMENT_EXTENSIONS
from ..parsing.images import ACCEPTED_IMAGE_TYPES
from ..sandbox.client import SandboxClient, SandboxUnavailable
from ..upstream.client import agent_models, offered_models
from ..workspace import accounts
from ..workspace.documents import (
    IMAGE_MEDIA_TYPES,
    MARKDOWN_EXTENSIONS,
    MARKDOWN_MEDIA_TYPE,
    document_paths,
)
from ..workspace.layout import user_root
from ..workspace.locks import workspace_lock
from ..workspace.memory_file import memory_file_text, write_memory_file
from ..workspace.trash import lock_user
from .billing import me_fields
from .chat import TITLE_MAX_CHARS, owned_document, turn_note
from .library import parse_auto_convert

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["control"])

# Registered by main.py only when APP_ENV=local and AUTH_DEV_MODE=true (docs/auth.md).
dev_router = APIRouter(prefix="/api", tags=["control"])


def set_session_cookie(response: Response, token: str) -> None:
    """Cookie flags are fixed by docs/auth.md: HttpOnly, Secure, SameSite=Lax.

    Secure is set unconditionally; browsers treat `http://localhost` as a secure context,
    so local development still receives the cookie.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# --- authentication -------------------------------------------------------------------


@router.get("/auth/login")
async def login(request: Request, settings: AppSettings, locale: str = ""):
    if not settings.google_oidc_configured:
        raise ApiError(503, "internal", "Google sign-in is not configured on this deployment")
    # The web app's locale lives in its URL, so the Google round trip is the one leg that
    # drops it. Parked here rather than round-tripped through Google's `state`, which
    # authlib owns, and validated against an allowlist on the way out (docs/auth.md § 1).
    request.session[POST_LOGIN_LOCALE_KEY] = locale
    return await request.app.state.oauth.google.authorize_redirect(
        request, f"{settings.app_url}/api/auth/callback"
    )


@router.get("/auth/callback")
async def callback(request: Request, db: DbSession, settings: AppSettings):
    try:
        token = await request.app.state.oauth.google.authorize_access_token(request)
    except OAuthError as exc:
        raise ApiError(400, "auth", f"Google sign-in failed: {exc.error}") from exc

    claims = token.get("userinfo") or {}
    if not claims.get("sub") or not claims.get("email"):
        raise ApiError(400, "auth", "Google sign-in returned no usable identity")

    user = await upsert_user(
        db, claims["sub"], claims["email"], claims.get("name"), claims.get("picture")
    )
    session_token = await create_session(db, user)
    await db.commit()

    # A connector flow parks the authorize request that sent the user here; only a path on
    # this origin is honoured, so a parked value can never become an open redirect. An
    # ordinary web login goes back to the web app in the locale it was started from.
    parked = request.session.pop(POST_LOGIN_KEY, "")
    locale = request.session.pop(POST_LOGIN_LOCALE_KEY, "")
    destination = (
        parked
        if parked.startswith("/oauth/authorize")
        else settings.web_url_for_locale(str(locale))
    )
    response = RedirectResponse(destination, status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, session_token)
    return response


@router.post("/auth/logout", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, db: DbSession) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await delete_session(db, token)
        await db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@dev_router.post("/auth/dev-login", dependencies=[CsrfGuard])
async def dev_login(db: DbSession):
    """Local-only shortcut so the stack is demoable without Google credentials."""
    user = await upsert_user(db, DEV_USER_SUB, DEV_USER_EMAIL, "Local Developer")
    session_token = await create_session(db, user)
    await db.commit()
    response = JSONResponse({"user": {"id": user.id, "email": user.email}})
    set_session_cookie(response, session_token)
    return response


# --- account --------------------------------------------------------------------------


@router.get("/site")
async def site(settings: AppSettings):
    """What the public policy pages print (docs/web.md § Pages): the deployment's support
    address and operator name, `null` when `.env` leaves them empty. Public, no session."""
    return {
        "support_email": settings.support_email or None,
        "operator_name": settings.operator_name or None,
    }


@router.get("/me")
async def me(user: SessionUser, db: DbSession, settings: AppSettings):
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "picture": user.picture,
            "created_at": user.created_at,
        },
        # The plan and the balance, so the shell shows them without a second request
        # (docs/billing.md § Routes).
        **await me_fields(db, settings, user.id),
        # The parse agent's switch, so the Files page applies to a PDF or an image the cap
        # the server will: a parse's while it converts them, a file's while it keeps them
        # (docs/web.md § Files).
        "auto_convert": await parse_auto_convert(db, user.id),
        # Which view a converted PDF or image opens on the first time it is read
        # (docs/web.md § Result viewer, *Original and Markdown*).
        "default_view": user.default_view,
        # What the upload UI is allowed to send, from the server's own configuration —
        # a limit written twice is a limit that drifts (docs/api.md § Limits).
        "limits": {
            "upload_max_bytes": settings.upload_max_bytes,
            "page_cap": settings.page_cap,
            "accepted_media_types": [PDF_MEDIA_TYPE, *ACCEPTED_IMAGE_TYPES, MARKDOWN_MEDIA_TYPE],
            # The extensions intake falls back to when a browser reports a generic type
            # for a file it has no association for (docs/api.md § Limits).
            "accepted_extensions": [
                ".pdf",
                *sorted(set(ACCEPTED_IMAGE_TYPES.values())),
                *MARKDOWN_EXTENSIONS,
            ],
            # A markdown upload is not parsed: which files are one, and their cap — the one a
            # `doc.md` is read under (docs/workspace.md § Markdown documents).
            "markdown_media_types": [MARKDOWN_MEDIA_TYPE],
            "markdown_extensions": list(MARKDOWN_EXTENSIONS),
            "markdown_max_bytes": settings.content_max_bytes,
            "image_media_types": list(IMAGE_MEDIA_TYPES),
            "image_max_bytes": settings.document_image_max_bytes,
            # The prompt dialogs count against the same cap the writes enforce.
            "prompt_max_chars": settings.system_prompt_max_chars,
            # What the chat composer may attach (docs/chat.md § What a user message carries).
            "attachment_max_bytes": settings.chat_attachment_max_bytes,
            "attachments_per_message": settings.chat_attachments_per_message,
            "attachment_extensions": list(ATTACHMENT_EXTENSIONS),
            # The Agent page's instructions and memory editors, and the composer's sections
            # and selection (docs/chat.md § Instructions, § Memory, § What a user message
            # carries).
            "instructions_max_chars": settings.chat_instructions_max_chars,
            "memory_max_chars": settings.chat_memory_max_chars,
            "sections_per_message": settings.chat_sections_per_message,
            "section_heading_max_chars": settings.chat_section_heading_max_chars,
            "selection_max_chars": settings.chat_selection_max_chars,
            # A conversation's rename field (docs/chat.md § Conversations, turns, events).
            "conversation_title_max_chars": TITLE_MAX_CHARS,
            # The Agent page's memory file editor (docs/chat.md § Memory, *The memory file*).
            "memory_file_max_chars": settings.chat_memory_file_max_chars,
            # How long the Trash keeps an item, which its tab states; 0 = never emptied by
            # itself (docs/workspace.md § The Trash).
            "trash_retention_days": settings.trash_retention_days,
            # The most a file of any other kind may be (docs/workspace.md § Files).
            "file_max_bytes": settings.file_max_bytes,
        },
    }


class PreferencesPut(BaseModel):
    default_view: str = Field(max_length=16)


@router.put("/me/preferences", dependencies=[CsrfGuard])
async def put_preferences(body: PreferencesPut, user: SessionUser, db: DbSession):
    """The reader's own preferences (docs/api.md): which view a converted PDF or image
    opens on the first time it is read (docs/web.md § Result viewer, *Original and
    Markdown*)."""
    if body.default_view not in DOCUMENT_VIEWS:
        raise ApiError(400, "invalid_request", "default_view is original or markdown")
    await db.execute(
        sa_update(User).where(User.id == user.id).values(default_view=body.default_view)
    )
    await db.commit()
    return {"default_view": body.default_view}


@router.delete("/account", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    request: Request, user: SessionUser, db: DbSession, settings: AppSettings
) -> Response:
    """Delete the signed-in account and everything of it, at once and for good
    (docs/auth.md § Deleting the account): refused while a subscription still renews or
    while a parse or an answer runs; otherwise the rows go in one commit, the files after
    it, and the session cookie is cleared."""
    user_id = user.id
    # Kano Pay has no cancel endpoint and a later account is a new subject: a subscription
    # left renewing could never be cancelled from here again. Pay's own answer, so a
    # cancellation made a minute ago counts — asked before any lock is taken, so no row
    # waits on the network — and written back under the user's lock as the webhook writes
    # it; the stored row when Pay is not configured.
    canonical: pay.PaySubscription | None = None
    unreachable: Exception | None = None
    if settings.kano_pay_configured:
        try:
            canonical = await pay.PayClient(settings).subscription(str(user_id))
        except (pay.PayUnavailable, pay.PayRefused) as exc:
            unreachable = exc
    async with workspace_lock(user_root(settings.workspaces_dir, user_id)):
        await lock_user(db, user_id)

        if not settings.kano_pay_configured:
            subscription = await db.get(Subscription, user_id)
        elif unreachable is not None:
            held = await db.get(Subscription, user_id) or await db.get(Checkout, user_id)
            if held is not None:
                logger.warning(
                    "account %d: the subscription fetch failed: %s", user_id, unreachable
                )
                raise ApiError(
                    503, "pay_unavailable", "The payment service is unavailable; try again"
                ) from unreachable
            subscription = None
        else:
            if canonical is not None:
                await pay.apply_canonical(db, user_id, canonical)
            # The newer of Pay's answer and what a notification wrote while this request
            # waited for the locks: the write keeps whichever version is newer.
            subscription = await db.get(Subscription, user_id, populate_existing=True)
        if (
            subscription is not None
            and subscription.status not in ENDED_STATUSES
            and not subscription.cancel_at_period_end
        ):
            # What Pay answered stays written, as a refresh would have left it.
            await db.commit()
            raise ApiError(409, "subscription_active", "Cancel your subscription first")
        # A checkout still open can be paid after the account is gone, and Pay would start
        # a subscription for a subject no one can sign in as again: the account waits
        # until it can no longer be paid. Pay's answer above completes a paid one.
        checkout = await db.get(Checkout, user_id, populate_existing=True)
        if (
            settings.kano_pay_configured
            and checkout is not None
            and checkout.completed_at is None
            and checkout.created_at
            > utcnow() - timedelta(seconds=settings.kano_pay_checkout_lifetime_seconds)
        ):
            await db.commit()
            raise ApiError(409, "checkout_open", "A checkout you started can still be paid")

        # Queued work is locked, so the claims' SKIP LOCKED passes it by, and goes with
        # the account; running work would go on writing files and usage as it goes.
        await db.execute(
            select(Job.id).where(Job.user_id == user_id, Job.status == "queued").with_for_update()
        )
        await db.execute(
            select(ChatTurn.id)
            .where(
                ChatTurn.user_id == user_id,
                ChatTurn.status == "queued",
                ChatTurn.finished_at.is_(None),
            )
            .with_for_update()
        )
        running_job = await db.scalar(
            select(Job.id).where(Job.user_id == user_id, Job.status == "running").limit(1)
        )
        # Running, or cancelled and not yet wound down by the worker.
        running_turn = await db.scalar(
            select(ChatTurn.id)
            .where(
                ChatTurn.user_id == user_id,
                ChatTurn.finished_at.is_(None),
                ChatTurn.status != "queued",
            )
            .limit(1)
        )
        if running_job is not None or running_turn is not None:
            raise ApiError(
                409,
                "invalid_request",
                "A parse or an answer is still running; try again when it finishes",
            )

        # The VM's only storage is the directory removed below; a runner that cannot stop
        # it does not hold the deletion up.
        if settings.sandbox_configured:
            try:
                await SandboxClient(settings).stop(user_id)
            except SandboxUnavailable:
                logger.warning("account %d: the sandbox could not be stopped", user_id)

        job_ids = list((await db.execute(select(Job.id).where(Job.user_id == user_id))).scalars())
        attachment_ids = list(
            (
                await db.execute(select(ChatAttachment.id).where(ChatAttachment.user_id == user_id))
            ).scalars()
        )
        # What the worker's sweep needs to finish the files should this process die
        # after the commit.
        await asyncio.to_thread(accounts.write_marker, settings.workspaces_dir, user_id, job_ids)
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()

        # The rows are gone: a file that will not go is logged, and the sweep finishes it.
        try:
            await discard_account(settings, user_id, job_ids, attachment_ids)
        except Exception:  # noqa: BLE001 - the account is deleted either way
            logger.exception("account %d: its files wait for the sweep", user_id)

    request.app.state.pay_refreshed.pop(user_id, None)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --- API keys -------------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


@router.get("/keys")
async def list_keys(user: SessionUser, db: DbSession):
    rows = (
        (
            await db.execute(
                select(ApiKey)
                .where(ApiKey.user_id == user.id, ApiKey.revoked_at.is_(None))
                .order_by(ApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {
        "keys": [
            {
                "id": row.id,
                "name": row.name,
                "prefix": row.prefix,
                "created_at": row.created_at,
                "last_used_at": row.last_used_at,
            }
            for row in rows
        ]
    }


@router.post("/keys", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_key(body: ApiKeyCreate, user: SessionUser, db: DbSession):
    row, plaintext = await create_api_key(db, user, body.name)
    await db.commit()
    # `key` is returned exactly once and never stored in plaintext (docs/auth.md).
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at,
        "key": plaintext,
    }


@router.delete("/keys/{key_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def revoke_key(key_id: int, user: SessionUser, db: DbSession) -> Response:
    row = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise ApiError(404, "invalid_request", "No such API key")
    row.revoked_at = utcnow()
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- prompt presets ---------------------------------------------------------------------


async def _commit_or_name_conflict(db: DbSession, message: str) -> None:
    """Commit, mapping a unique-name race to the same 400 the pre-check gives.

    Two concurrent creates can both pass the duplicate pre-check before either commits;
    the loser's constraint violation is an ordinary name conflict, not a 500.
    """
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ApiError(400, "invalid_request", message) from exc


class PromptCreate(BaseModel):
    # The agent this prompt is for; fixed once created (docs/agents.md).
    agent: str = Field(default="parse", max_length=32)
    name: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1)


class PromptUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    text: str | None = Field(default=None, min_length=1)


def _prompt_payload(row: PromptPreset) -> dict:
    return {
        "id": row.id,
        "agent": row.agent,
        "name": row.name,
        "text": row.text,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _checked_prompt_text(text: str, settings) -> str:
    trimmed = text.strip()
    if not trimmed:
        raise ApiError(400, "invalid_request", "The prompt needs text")
    if len(trimmed) > settings.system_prompt_max_chars:
        raise ApiError(
            400,
            "invalid_request",
            f"The prompt exceeds {settings.system_prompt_max_chars} characters",
        )
    return trimmed


async def _owned_prompt(db: DbSession, user_id: int, prompt_id: int) -> PromptPreset:
    row = (
        await db.execute(
            select(PromptPreset).where(
                PromptPreset.id == prompt_id, PromptPreset.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such prompt")
    return row


async def _refuse_duplicate_prompt_name(
    db: DbSession, user_id: int, name: str, exclude_id: int | None = None
) -> None:
    query = select(PromptPreset.id).where(
        PromptPreset.user_id == user_id, PromptPreset.name == name
    )
    if exclude_id is not None:
        query = query.where(PromptPreset.id != exclude_id)
    if (await db.execute(query)).first() is not None:
        raise ApiError(400, "invalid_request", "A prompt with this name already exists")


@router.get("/prompts")
async def list_prompts(user: SessionUser, db: DbSession):
    rows = (
        (
            await db.execute(
                select(PromptPreset)
                .where(PromptPreset.user_id == user.id)
                .order_by(PromptPreset.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"prompts": [_prompt_payload(row) for row in rows]}


@router.post("/prompts", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: PromptCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    agent = get_agent(body.agent)
    if agent is None:
        raise ApiError(400, "invalid_request", f"No such agent '{body.agent}'")
    name = body.name.strip()
    if not name:
        raise ApiError(400, "invalid_request", "The prompt needs a name")
    text = _checked_prompt_text(body.text, settings)
    await _refuse_duplicate_prompt_name(db, user.id, name)
    row = PromptPreset(user_id=user.id, agent=agent.name, name=name, text=text)
    db.add(row)
    await _commit_or_name_conflict(db, "A prompt with this name already exists")
    return _prompt_payload(row)


@router.put("/prompts/{prompt_id}", dependencies=[CsrfGuard])
async def update_prompt(
    prompt_id: int, body: PromptUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    row = await _owned_prompt(db, user.id, prompt_id)
    provided = body.model_fields_set
    if "name" in provided and body.name:
        name = body.name.strip()
        if not name:
            raise ApiError(400, "invalid_request", "The prompt needs a name")
        await _refuse_duplicate_prompt_name(db, user.id, name, exclude_id=row.id)
        row.name = name
    if "text" in provided and body.text:
        row.text = _checked_prompt_text(body.text, settings)
    row.updated_at = utcnow()
    await _commit_or_name_conflict(db, "A prompt with this name already exists")
    return _prompt_payload(row)


@router.delete(
    "/prompts/{prompt_id}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_prompt(prompt_id: int, user: SessionUser, db: DbSession) -> Response:
    row = await _owned_prompt(db, user.id, prompt_id)
    # Explicit fallback to the agent's default prompt, not just the FK's SET NULL.
    await db.execute(
        sa_update(AgentSetting)
        .where(AgentSetting.user_id == user.id, AgentSetting.prompt_preset_id == row.id)
        .values(prompt_preset_id=None)
    )
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- agents ---------------------------------------------------------------------------


def _known_agent(name: str) -> AgentDefinition:
    agent = get_agent(name)
    if agent is None:
        raise ApiError(404, "invalid_request", f"No such agent '{name}'")
    return agent


def _effective_model(settings: AppSettings, agent: str, row: AgentSetting | None) -> str | None:
    """The model the agent runs, as `GET /api/agents` answers it (docs/api.md): the
    deployment's for parse and check, the reader's pick for chat — null for the default,
    the first of a list only the live catalog can say."""
    if agent == CHAT.name:
        return row.model if row else None
    models = offered_models(settings, agent)
    return models[0] if models else None


def _agent_settings_payload(settings: AppSettings, agent: str, row: AgentSetting | None) -> dict:
    return {
        "model": _effective_model(settings, agent, row),
        # The user's own template; null for the agent's default (docs/agents.md).
        "prompt": row.prompt if row else None,
        "prompt_preset_id": row.prompt_preset_id if row else None,
        "effort": (row.effort if row and row.effort else EFFORT_DEFAULT),
        # The chat agent's own: instructions appended after the prompt, the languages it
        # answers and translates into, and the memory switches (docs/agents.md).
        "instructions": row.instructions if row else None,
        "answer_language": row.answer_language if row else None,
        "translate_language": row.translate_language if row else None,
        "memory_auto": row.memory_auto if row and row.memory_auto is not None else True,
        "memory_paused": bool(row.memory_paused) if row else False,
        # And whether the worker compacts the reader's long conversations before their
        # cache expires (docs/chat.md § Compaction).
        "idle_compact": row.idle_compact if row and row.idle_compact is not None else True,
        # The parse agent's own: the model a blocked page goes to — the deployment's, a
        # fact — and whether it goes there on its own (docs/parsing.md § Fallback model).
        "fallback_model": (settings.parse_fallback_model or None) if agent == PARSE.name else None,
        "auto_fallback": bool(row.auto_fallback) if row else False,
        # And whether a PDF or an image uploaded on the Files page is converted on arrival:
        # off unless the reader turned it on (docs/workspace.md § Files).
        "auto_convert": bool(row.auto_convert) if row else False,
        # The check agent's own: whether a Files upload is checked before it opens
        # (docs/parsing.md § Format check).
        "auto_check": row.auto_check if row and row.auto_check is not None else True,
    }


def _bbox_payload(settings: AppSettings, agent: str, row: AgentSetting | None) -> dict:
    convention = convention_for_model(_effective_model(settings, agent, row))
    return {"family": convention.family, "format": convention.format}


def _agent_tools(name: str, shell: bool, settings: AppSettings) -> list[str]:
    if name != CHAT.name:
        return []
    return [str(tool["name"]) for tool in chat_runner.turn_tools(shell, settings)]


@router.get("/agents")
async def list_agents(user: SessionUser, db: DbSession, settings: AppSettings):
    """The registry with the caller's settings for each agent (docs/agents.md)."""
    rows = {
        row.agent: row
        for row in (
            await db.execute(select(AgentSetting).where(AgentSetting.user_id == user.id))
        ).scalars()
    }
    shell = ledger.sandbox_granted(settings, await ledger.plan_of(db, settings, user.id))
    return {
        "agents": [
            {
                "name": agent.name,
                "capabilities": list(agent.capabilities),
                "input": agent.input,
                "output": agent.output,
                # What the prompt dialog opens on when the user saved no prompt, and the
                # tokens it offers as chips (docs/web.md).
                "default_prompt": agent.default_prompt_template,
                "prompt_tokens": list(agent.prompt_tokens),
                # The tools this deployment really gives this user's agent: `bash` only
                # on a plan that grants it, with a sandbox runner configured
                # (docs/chat.md § Tools, docs/billing.md § Plans).
                "tools": _agent_tools(agent.name, shell, settings),
                "settings": _agent_settings_payload(settings, agent.name, rows.get(agent.name)),
                # A fact about the model, not a setting: the coordinate convention the
                # pipeline will ask it for (docs/parsing.md § Coordinate conventions).
                "bbox": _bbox_payload(settings, agent.name, rows.get(agent.name)),
            }
            for agent in AGENTS.values()
        ]
    }


class AgentSettingsPut(BaseModel):
    """Partial update: only the fields present in the body change (docs/api.md)."""

    # The chat agent's own: one of the offered models, null their first (docs/api.md).
    model: str | None = Field(default=None, max_length=255)
    # Capped in the route, where the settings are (`SYSTEM_PROMPT_MAX_CHARS`).
    prompt: str | None = None
    prompt_preset_id: int | None = None
    effort: str | None = Field(default=None, max_length=8)
    # The chat agent's own, the instructions the check agent's too (docs/agents.md);
    # checked in the route, which answers 400 for each — capped where the settings are
    # (`CHAT_INSTRUCTIONS_MAX_CHARS`).
    instructions: str | None = None
    answer_language: str | None = None
    translate_language: str | None = None
    memory_auto: bool | None = None
    memory_paused: bool | None = None
    idle_compact: bool | None = None
    # The parse agent's own (docs/agents.md).
    auto_fallback: bool | None = None
    auto_convert: bool | None = None
    # The check agent's own (docs/agents.md).
    auto_check: bool | None = None


# The settings only some agents take, and which (docs/agents.md § Per-agent settings).
SETTING_OWNERS: dict[str, tuple[str, ...]] = {
    "model": (CHAT.name,),
    "instructions": (CHAT.name, CHECK.name),
    "answer_language": (CHAT.name,),
    "translate_language": (CHAT.name,),
    "memory_auto": (CHAT.name,),
    "memory_paused": (CHAT.name,),
    "idle_compact": (CHAT.name,),
    "auto_fallback": (PARSE.name,),
    "auto_convert": (PARSE.name,),
    "auto_check": (CHECK.name,),
}
# A language tag, as the composer's locale is one (docs/api.md).
LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")
LANGUAGE_TAG_MAX_CHARS = 16


def _checked_language(value: str | None, label: str) -> str | None:
    """A language tag, or None to follow the reader's locale; 400 for anything else."""
    tag = (value or "").strip()
    if not tag:
        return None
    if len(tag) > LANGUAGE_TAG_MAX_CHARS or not LANGUAGE_TAG.fullmatch(tag):
        raise ApiError(400, "invalid_request", f"{label} must be a language tag such as zh-TW")
    return tag


def _checked_instructions(value: str | None, settings: AppSettings) -> str | None:
    """The reader's instructions cleaned of what pasted text carries unseen, trimmed and
    capped in characters as cleaned; empty is none (docs/chat.md § Instructions)."""
    instructions = memory_defs.clean_text(value or "", single_line=False) or None
    if instructions is not None and len(instructions) > settings.chat_instructions_max_chars:
        raise ApiError(
            400,
            "invalid_request",
            f"The instructions exceed {settings.chat_instructions_max_chars} characters",
        )
    return instructions


@router.put("/agents/{agent_name}/settings", dependencies=[CsrfGuard])
async def put_agent_settings(
    agent_name: str, body: AgentSettingsPut, user: SessionUser, db: DbSession, settings: AppSettings
):
    agent = _known_agent(agent_name)
    provided = body.model_fields_set
    foreign = [
        name
        for name, owners in SETTING_OWNERS.items()
        if name in provided and agent.name not in owners
    ]
    if foreign:
        raise ApiError(
            400, "invalid_request", f"The {agent.name} agent takes no {', '.join(foreign)}"
        )
    for switch in (
        "memory_auto",
        "memory_paused",
        "idle_compact",
        "auto_fallback",
        "auto_convert",
        "auto_check",
    ):
        if switch in provided and getattr(body, switch) is None:
            raise ApiError(400, "invalid_request", f"{switch} is true or false")
    instructions = (
        _checked_instructions(body.instructions, settings) if "instructions" in provided else None
    )
    answer_language = _checked_language(body.answer_language, "answer_language")
    translate_language = _checked_language(body.translate_language, "translate_language")
    prompt: str | None = None
    if "prompt" in provided:
        prompt = (body.prompt or "").strip() or None
        # The default stored as no prompt, so a later default reaches it — before the cap,
        # which is the reader's own prompt's, not the default's.
        if prompt == agent.default_prompt_template.strip():
            prompt = None
        if prompt is not None and len(prompt) > settings.system_prompt_max_chars:
            raise ApiError(
                400,
                "invalid_request",
                f"The prompt exceeds {settings.system_prompt_max_chars} characters",
            )
    if prompt is not None and body.prompt_preset_id is not None:
        # The two exclude each other (docs/agents.md § Per-agent settings).
        raise ApiError(400, "invalid_request", "Send either prompt or prompt_preset_id")
    model: str | None = None
    if "model" in provided:
        model = (body.model or "").strip() or None
        if model is not None and model not in {
            entry["id"] for entry in await agent_models(settings, CHAT.name)
        }:
            # The offered list, read live (docs/billing.md § Models).
            raise ApiError(400, "invalid_request", "That model is not one the chat agent offers")
    if body.effort is not None and body.effort not in EFFORT_LEVELS:
        raise ApiError(400, "invalid_request", "effort must be one of " + ", ".join(EFFORT_LEVELS))
    if body.prompt_preset_id is not None:
        preset = await _owned_prompt(db, user.id, body.prompt_preset_id)
        if preset.agent != agent.name:
            raise ApiError(400, "invalid_request", f"That prompt is for the {preset.agent} agent")

    if "memory_paused" in provided:
        # The reader's row, held until the commit: the `memory` tool reads the switch under
        # it, so a Pause waits for a call already deciding and every later call sees it
        # (docs/chat.md § Memory).
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    row = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user.id, AgentSetting.agent == agent.name
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgentSetting(user_id=user.id, agent=agent.name)
        db.add(row)
    if "model" in provided:
        row.model = model
    if "auto_fallback" in provided:
        row.auto_fallback = bool(body.auto_fallback)
    if "auto_convert" in provided:
        row.auto_convert = bool(body.auto_convert)
    if "auto_check" in provided:
        row.auto_check = bool(body.auto_check)
    if "prompt" in provided or "prompt_preset_id" in provided:
        # Writing either clears the other: one of the two is ever in force.
        row.prompt = prompt
        row.prompt_preset_id = body.prompt_preset_id
    if "effort" in provided:
        row.effort = body.effort or EFFORT_DEFAULT
    if "instructions" in provided:
        row.instructions = instructions
    if "answer_language" in provided:
        row.answer_language = answer_language
    if "translate_language" in provided:
        row.translate_language = translate_language
    if "memory_auto" in provided:
        row.memory_auto = bool(body.memory_auto)
    if "memory_paused" in provided:
        row.memory_paused = bool(body.memory_paused)
    if "idle_compact" in provided:
        row.idle_compact = bool(body.idle_compact)
    try:
        await db.commit()
    except IntegrityError as exc:
        # Another tab deleted the selected prompt between the ownership pre-check and
        # this commit — an ordinary selection race, not a 500.
        await db.rollback()
        raise ApiError(409, "invalid_request", "The selected prompt was just deleted") from exc
    # The convention follows the model, so a write answers the new one along with the row.
    return {
        **_agent_settings_payload(settings, agent.name, row),
        "bbox": _bbox_payload(settings, agent.name, row),
    }


@router.get("/agents/chat/context")
async def chat_agent_context(
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
    locale: str = Query(default="en", max_length=16, pattern=LANGUAGE_TAG.pattern),
    document_id: int | None = None,
):
    """What the chat agent receives, rendered by the code the runner uses, for the Agent
    page's drawer (docs/api.md): the system blocks in the order a turn sends them, the turn
    block a message sent now in `locale` would carry, and the document's facts when one is
    named. Reads only: nothing is materialized and no snapshot is taken."""
    facts: dict[str, str] | None = None
    if document_id is not None:
        document = await owned_document(db, user.id, document_id)
        # A markdown document has no job and still has facts: it has no pages to name.
        job = await db.get(Job, document.job_id) if document.job_id is not None else None
        if document.job_id is None or job is not None:
            facts = chat_runner.document_facts(
                settings, job, await document_paths(db, settings, document)
            )
    setting = (
        await db.execute(
            select(AgentSetting).where(
                AgentSetting.user_id == user.id, AgentSetting.agent == CHAT.name
            )
        )
    ).scalar_one_or_none()
    own_prompt = setting.prompt if setting and setting.prompt else None
    preset = setting.prompt_preset if setting and own_prompt is None else None
    skills = skill_defs.resolve_skills(
        (await db.execute(select(Skill).where(Skill.user_id == user.id))).scalars()
    )
    subagents = subagent_defs.resolve_subagents(
        (await db.execute(select(Subagent).where(Subagent.user_id == user.id))).scalars()
    )
    # What a new conversation would load: the first turn's selection, over every
    # document's memories and, with a document, this one's (docs/chat.md § Memory).
    auto = setting.memory_auto if setting else True
    paused = setting.memory_paused if setting else False
    loaded = (
        []
        if paused
        else chat_memory.first_turn_selection(
            await chat_memory.applicable_memories(db, user.id, document_id if facts else None),
            settings,
        )
    )
    shell = ledger.sandbox_granted(settings, await ledger.plan_of(db, settings, user.id))
    tools = [str(tool["name"]) for tool in chat_runner.turn_tools(shell, settings)]
    parts = chat_agent.system_parts(
        # Without a document the template keeps its tokens, its tool lines filtered as a
        # turn's would be.
        chat_agent.render_system_prompt(
            own_prompt or CHAT.default_prompt_template, facts, tools=tools
        ),
        preset=preset.text if preset is not None else None,
        instructions=setting.instructions if setting else None,
        catalogue=chat_runner.turn_catalogue(skills, subagents),
        memory=memory_defs.render_memory_block(loaded, auto=auto, paused=paused),
        # The memory file as a new conversation would read it (docs/chat.md § Memory).
        memory_file=chat_agent.render_memory_file(
            await memory_file_text(settings, user.id), settings.chat_memory_file_max_chars
        ),
    )
    extras = {
        "prompt": {"own": own_prompt is not None},
        "catalogue": {
            "skills": sum(1 for skill in skills if skill.model_invocable),
            "subagents": len(subagents),
        },
        "memory": {"count": len(loaded), "paused": paused},
    }
    turn = turn_note(
        "<revision>",
        locale,
        setting.answer_language if setting else None,
        setting.translate_language if setting else None,
    )
    return {
        "parts": [
            {
                "kind": part.kind,
                "text": part.text,
                "chars": len(part.text),
                **extras.get(part.kind, {}),
            }
            for part in parts
        ],
        "turn": {"text": turn, "chars": len(turn)},
        "facts": facts,
    }


@router.get("/agents/chat/memory-file")
async def read_memory_file(user: SessionUser, settings: AppSettings):
    """The chat agent's memory file as it is now, and how much of it a turn reads
    (docs/chat.md § Memory, *The memory file*)."""
    return {
        "text": await memory_file_text(settings, user.id),
        "max_chars": settings.chat_memory_file_max_chars,
    }


class MemoryFilePut(BaseModel):
    text: str


@router.put("/agents/chat/memory-file", dependencies=[CsrfGuard])
async def put_memory_file(
    body: MemoryFilePut, user: SessionUser, db: DbSession, settings: AppSettings
):
    """The reader's own text, written as a revision of theirs under the workspace lock —
    taken first, then the user's row, the order every workspace mutation takes — and
    refused while any turn of the reader is unfinished, since a turn may be writing it
    (docs/chat.md § Memory, *The memory file*)."""
    text = memory_defs.clean_text(body.text, single_line=False)
    if len(text) > settings.chat_memory_file_max_chars:
        raise ApiError(
            400,
            "invalid_request",
            f"The memory file exceeds {settings.chat_memory_file_max_chars} characters",
        )
    stored = f"{text}\n" if text else ""
    root = user_root(settings.workspaces_dir, user.id)
    async with workspace_lock(root):
        # The send's lock: no turn is queued under the decision below.
        await db.execute(select(User.id).where(User.id == user.id).with_for_update())
        if await count_active_turns(db, user.id):
            raise ApiError(
                409,
                "invalid_request",
                "A conversation is still answering; wait for it to finish before saving",
            )
        await asyncio.to_thread(write_memory_file, root, stored, settings.content_max_bytes)
        await db.commit()
    return {"text": stored}


@router.get("/agents/{agent_name}/models")
async def list_agent_models(
    agent_name: str, user: SessionUser, db: DbSession, settings: AppSettings
):
    """The models the agent may run — the Agent page's model picker (docs/api.md)."""
    agent = _known_agent(agent_name)
    return {"data": await agent_models(settings, agent.name)}


# --- usage ----------------------------------------------------------------------------


@router.get("/usage")
async def usage(user: SessionUser, db: DbSession, days: int = Query(default=30, ge=1, le=365)):
    """Usage aggregates with billed, estimated and unpriced cost kept apart (docs/api.md
    § Usage aggregates): an estimate is never summed into billing, and a call nobody
    priced is counted rather than priced at 0."""
    since = utcnow() - timedelta(days=days)
    day = func.date(UsageLog.created_at)  # UTC day bucket on both PostgreSQL and SQLite

    def of_source(source: str, value):
        return case((UsageLog.cost_source == source, value))

    figures = (
        func.count(),
        func.sum(UsageLog.prompt_tokens),
        func.sum(UsageLog.completion_tokens),
        func.sum(of_source(COST_SOURCE_OPENROUTER, UsageLog.cost)),
        func.sum(of_source(COST_SOURCE_ESTIMATED, UsageLog.cost)),
        func.count(of_source(COST_SOURCE_ESTIMATED, 1)),
        func.count(of_source(COST_SOURCE_UNKNOWN, 1)),
        func.sum(UsageLog.cache_read_tokens),
        func.sum(UsageLog.cache_write_tokens),
    )
    in_window = (UsageLog.user_id == user.id, UsageLog.created_at >= since)
    per_day = (
        await db.execute(select(day, *figures).where(*in_window).group_by(day).order_by(day))
    ).all()
    per_model = (
        await db.execute(
            select(UsageLog.model, *figures)
            .where(*in_window)
            .group_by(UsageLog.model)
            .order_by(UsageLog.model)
        )
    ).all()
    per_agent = (
        await db.execute(
            select(UsageLog.agent, *figures)
            .where(*in_window)
            .group_by(UsageLog.agent)
            .order_by(UsageLog.agent)
        )
    ).all()
    agents_by_day = (
        await db.execute(
            select(day, UsageLog.agent).where(*in_window).group_by(day, UsageLog.agent)
        )
    ).all()
    day_agents: dict[str, list[str]] = {}
    for bucket, agent in agents_by_day:
        day_agents.setdefault(str(bucket)[:10], []).append(agent)

    return {
        "days": days,
        "per_day": [
            {
                "date": str(row[0])[:10],
                "agents": sorted(day_agents.get(str(row[0])[:10], [])),
                **usage_totals(row[1:]),
            }
            for row in per_day
        ],
        "per_model": [{"model": row[0], **usage_totals(row[1:])} for row in per_model],
        "per_agent": [{"agent": row[0], **usage_totals(row[1:])} for row in per_agent],
    }


def usage_totals(row) -> dict:
    calls, prompt, completion, cost, estimated, estimated_calls, unpriced, cached, written = row
    return {
        "calls": int(calls),
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "cost": float(cost or 0),
        "estimated_cost": float(estimated or 0),
        "estimated_calls": int(estimated_calls),
        "unpriced_calls": int(unpriced),
        "cache_read_tokens": int(cached or 0),
        "cache_write_tokens": int(written or 0),
    }


@router.get("/usage/conversations")
async def usage_per_conversation(
    user: SessionUser, db: DbSession, days: int = Query(default=30, ge=1, le=365)
):
    """What each conversation cost over the period, through its turns
    (docs/api.md § Usage aggregates)."""
    since = utcnow() - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                Conversation.id,
                Conversation.title,
                Conversation.document_id,
                Document.name,
                func.count(func.distinct(ChatTurn.id)),
                func.sum(UsageLog.prompt_tokens),
                func.sum(UsageLog.cache_read_tokens),
                func.sum(UsageLog.completion_tokens),
                func.sum(case((UsageLog.cost_source == COST_SOURCE_ESTIMATED, UsageLog.cost))),
                func.count(case((UsageLog.cost_source == COST_SOURCE_UNKNOWN, 1))),
                func.sum(UsageLog.cache_write_tokens),
                # What tells an estimate of exactly $0 from no estimate (docs/api.md).
                func.count(case((UsageLog.cost_source == COST_SOURCE_ESTIMATED, 1))),
            )
            .select_from(UsageLog)
            .join(ChatTurn, ChatTurn.id == UsageLog.turn_id)
            .join(Conversation, Conversation.id == ChatTurn.conversation_id)
            .outerjoin(Document, Document.id == Conversation.document_id)
            .where(UsageLog.user_id == user.id, UsageLog.created_at >= since)
            .group_by(Conversation.id, Conversation.title, Conversation.document_id, Document.name)
            .order_by(func.sum(UsageLog.prompt_tokens).desc())
        )
    ).all()
    return {
        "conversations": [
            {
                "id": row[0],
                "title": row[1],
                "document_id": row[2],
                "document_name": row[3],
                "turns": int(row[4]),
                "prompt_tokens": int(row[5] or 0),
                "cache_read_tokens": int(row[6] or 0),
                "completion_tokens": int(row[7] or 0),
                "estimated_cost": float(row[8] or 0),
                "unpriced_calls": int(row[9]),
                "cache_write_tokens": int(row[10] or 0),
                "estimated_calls": int(row[11]),
            }
            for row in rows
        ]
    }
