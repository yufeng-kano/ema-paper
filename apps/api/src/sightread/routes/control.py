"""Control plane `/api/*` — session-cookie authenticated, consumed by the web app.

Mutations additionally require an `X-Requested-With` header (CSRF pairing with
SameSite=Lax), enforced by `require_csrf_header`.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from ..agents import AGENTS, CHAT, AgentDefinition, get_agent
from ..agents import chat as chat_agent
from ..agents import memory as memory_defs
from ..agents import skills as skill_defs
from ..agents import subagents as subagent_defs
from ..agents.bbox import convention_for_model
from ..auth.api_keys import create_api_key
from ..auth.crypto import encrypt_connection_key, mask_secret
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..auth.oidc import (
    DEV_USER_EMAIL,
    DEV_USER_SUB,
    POST_LOGIN_KEY,
    POST_LOGIN_LOCALE_KEY,
    upsert_user,
)
from ..auth.sessions import SESSION_COOKIE, SESSION_TTL, create_session, delete_session
from ..db.models import (
    COST_SOURCE_ESTIMATED,
    COST_SOURCE_OPENROUTER,
    COST_SOURCE_UNKNOWN,
    EFFORT_DEFAULT,
    EFFORT_LEVELS,
    AgentSetting,
    ApiKey,
    ChatTurn,
    Conversation,
    Document,
    Job,
    PromptPreset,
    ProviderConnection,
    Result,
    Skill,
    Subagent,
    UsageLog,
    User,
    utcnow,
)
from ..errors import ApiError
from ..jobs import chat_memory, chat_runner
from ..jobs.intake import PDF_MEDIA_TYPE
from ..jobs.runner import result_payload
from ..parsing.attachments import ATTACHMENT_EXTENSIONS
from ..parsing.images import ACCEPTED_IMAGE_TYPES
from ..upstream.client import (
    agent_models,
    fetch_connection_models,
    normalize_base_url,
    stored_connection_models,
)
from ..workspace.documents import document_paths
from .chat import owned_document, turn_note
from .library import figure_crop_response, partial_result

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


@router.get("/me")
async def me(user: SessionUser, settings: AppSettings):
    return {
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "picture": user.picture,
            "created_at": user.created_at,
        },
        # What the upload UI is allowed to send, from the server's own configuration —
        # a limit written twice is a limit that drifts (docs/api.md § Limits).
        "limits": {
            "upload_max_bytes": settings.upload_max_bytes,
            "page_cap": settings.page_cap,
            "accepted_media_types": [PDF_MEDIA_TYPE, *ACCEPTED_IMAGE_TYPES],
            # The extensions intake falls back to when a browser reports a generic type
            # for a file it has no association for (docs/api.md § Limits).
            "accepted_extensions": [".pdf", *sorted(set(ACCEPTED_IMAGE_TYPES.values()))],
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
        },
    }


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


# --- provider connections -------------------------------------------------------------


class ConnectionCreate(BaseModel):
    """An endpoint, its key and the cost flag; the model is each agent's choice
    (docs/agents.md)."""

    name: str = Field(min_length=1, max_length=255)
    base_url: str = Field(min_length=1, max_length=1024)
    api_key: str = Field(min_length=8, max_length=512)
    # Record the endpoint's reported `usage.cost` as an estimate (docs/parsing.md).
    report_cost: bool = False


class ConnectionUpdate(BaseModel):
    """Partial update; changing the endpoint or the key re-validates against it."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    base_url: str | None = Field(default=None, min_length=1, max_length=1024)
    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    report_cost: bool | None = None


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


def _connection_payload(row: ProviderConnection) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "base_url": row.base_url,
        "masked": row.masked,
        "report_cost": row.report_cost,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def _owned_connection(
    db: DbSession, user_id: int, connection_id: int, for_update: bool = False
) -> ProviderConnection:
    # `for_update` serializes concurrent edits of one connection (PostgreSQL row lock;
    # compiled away on the SQLite test fallback): two edits validating different URL/key
    # pairs must not interleave into a stored pair nobody validated.
    query = select(ProviderConnection).where(
        ProviderConnection.id == connection_id, ProviderConnection.user_id == user_id
    )
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise ApiError(404, "invalid_request", "No such connection")
    return row


async def _refuse_duplicate_connection_name(
    db: DbSession, user_id: int, name: str, exclude_id: int | None = None
) -> None:
    query = select(ProviderConnection.id).where(
        ProviderConnection.user_id == user_id, ProviderConnection.name == name
    )
    if exclude_id is not None:
        query = query.where(ProviderConnection.id != exclude_id)
    if (await db.execute(query)).first() is not None:
        raise ApiError(400, "invalid_request", "A connection with this name already exists")


@router.get("/connections")
async def list_connections(user: SessionUser, db: DbSession):
    rows = (
        (
            await db.execute(
                select(ProviderConnection)
                .where(ProviderConnection.user_id == user.id)
                .order_by(ProviderConnection.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"connections": [_connection_payload(row) for row in rows]}


@router.post("/connections", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: ConnectionCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    name = body.name.strip()
    if not name:
        raise ApiError(400, "invalid_request", "The connection needs a name")
    base_url = await normalize_base_url(body.base_url, settings.app_env)
    candidate = body.api_key.strip()
    await _refuse_duplicate_connection_name(db, user.id, name)
    # `GET {base_url}/models` doubles as the save-time key check (docs/auth.md § 3).
    await fetch_connection_models(base_url, candidate, settings.upstream_response_max_bytes)

    row = ProviderConnection(
        user_id=user.id,
        name=name,
        base_url=base_url,
        ciphertext=encrypt_connection_key(settings.secret_key, candidate),
        masked=mask_secret(candidate),
        report_cost=body.report_cost,
    )
    db.add(row)
    await _commit_or_name_conflict(db, "A connection with this name already exists")
    return _connection_payload(row)


@router.put("/connections/{connection_id}", dependencies=[CsrfGuard])
async def update_connection(
    connection_id: int,
    body: ConnectionUpdate,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    row = await _owned_connection(db, user.id, connection_id, for_update=True)
    provided = body.model_fields_set

    name = body.name.strip() if body.name else row.name
    if not name:
        raise ApiError(400, "invalid_request", "The connection needs a name")
    if "name" in provided:
        await _refuse_duplicate_connection_name(db, user.id, name, exclude_id=row.id)

    base_url = (
        await normalize_base_url(body.base_url, settings.app_env)
        if "base_url" in provided and body.base_url
        else row.base_url
    )
    candidate = body.api_key.strip() if "api_key" in provided and body.api_key else None

    if candidate is not None:
        await fetch_connection_models(base_url, candidate, settings.upstream_response_max_bytes)
        row.ciphertext = encrypt_connection_key(settings.secret_key, candidate)
        row.masked = mask_secret(candidate)
    elif base_url != row.base_url:
        # A moved endpoint must still accept the stored key before we point jobs at it.
        await stored_connection_models(
            base_url, settings.secret_key, row.ciphertext, settings.upstream_response_max_bytes
        )

    if body.report_cost is not None:
        row.report_cost = body.report_cost

    row.name = name
    row.base_url = base_url
    row.updated_at = utcnow()
    await _commit_or_name_conflict(db, "A connection with this name already exists")
    return _connection_payload(row)


@router.delete(
    "/connections/{connection_id}",
    dependencies=[CsrfGuard],
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_connection(connection_id: int, user: SessionUser, db: DbSession) -> Response:
    row = await _owned_connection(db, user.id, connection_id)
    # Explicit fallback (not just the FK's SET NULL): every agent on this connection goes
    # back to Kano Proxy, and its model goes too — it belonged to the deleted endpoint's
    # catalog (docs/agents.md).
    await db.execute(
        sa_update(AgentSetting)
        .where(AgentSetting.user_id == user.id, AgentSetting.connection_id == row.id)
        .values(connection_id=None, model=None)
    )
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class ConnectionModelsPreview(BaseModel):
    """The connection dialog's catalog request: a candidate key, or a stored one by id."""

    base_url: str = Field(min_length=1, max_length=1024)
    api_key: str | None = Field(default=None, min_length=8, max_length=512)
    connection_id: int | None = None


@router.post("/connections/preview-models", dependencies=[CsrfGuard])
async def preview_connection_models(
    body: ConnectionModelsPreview, user: SessionUser, db: DbSession, settings: AppSettings
):
    """The live model catalog the connection dialog picks from (docs/api.md).

    Works before the connection exists (candidate `api_key`) and while editing one
    (`connection_id` reuses the stored key, so editing never requires re-typing it). The
    URL passes the same rules as saving — this must not be a free server-side probe
    (docs/auth.md § 3).
    """
    base_url = await normalize_base_url(body.base_url, settings.app_env)
    candidate = body.api_key.strip() if body.api_key else None
    if candidate:
        models = await fetch_connection_models(
            base_url, candidate, settings.upstream_response_max_bytes
        )
    elif body.connection_id is not None:
        row = await _owned_connection(db, user.id, body.connection_id)
        models = await stored_connection_models(
            base_url, settings.secret_key, row.ciphertext, settings.upstream_response_max_bytes
        )
    else:
        raise ApiError(400, "invalid_request", "Provide an API key or an existing connection")
    return {"data": models}


# --- prompt presets ---------------------------------------------------------------------


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


def _agent_settings_payload(row: AgentSetting | None) -> dict:
    return {
        "connection_id": row.connection_id if row else None,
        "model": row.model if row else None,
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
    }


def _bbox_payload(row: AgentSetting | None) -> dict:
    convention = convention_for_model(row.model if row else None)
    return {"family": convention.family, "format": convention.format}


def _agent_tools(name: str, settings: AppSettings) -> list[str]:
    if name != CHAT.name:
        return []
    tools = chat_agent.TOOLS if settings.sandbox_configured else chat_agent.TOOLS_WITHOUT_BASH
    return [str(tool["name"]) for tool in tools]


@router.get("/agents")
async def list_agents(user: SessionUser, db: DbSession, settings: AppSettings):
    """The registry with the caller's settings for each agent (docs/agents.md)."""
    rows = {
        row.agent: row
        for row in (
            await db.execute(select(AgentSetting).where(AgentSetting.user_id == user.id))
        ).scalars()
    }
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
                # The tools this deployment really gives the agent: `bash` only with a
                # sandbox runner configured (docs/chat.md § Tools).
                "tools": _agent_tools(agent.name, settings),
                "settings": _agent_settings_payload(rows.get(agent.name)),
                # A fact about the saved model, not a setting: the coordinate convention
                # the pipeline will ask it for (docs/parsing.md § Coordinate conventions).
                "bbox": _bbox_payload(rows.get(agent.name)),
            }
            for agent in AGENTS.values()
        ]
    }


class AgentSettingsPut(BaseModel):
    """Partial update: only the fields present in the body change (docs/api.md)."""

    connection_id: int | None = None
    model: str | None = Field(default=None, max_length=255)
    # Capped in the route, where the settings are (`SYSTEM_PROMPT_MAX_CHARS`).
    prompt: str | None = None
    prompt_preset_id: int | None = None
    effort: str | None = Field(default=None, max_length=8)
    # The chat agent's own (docs/agents.md); checked in the route, which answers 400 for
    # each — capped where the settings are (`CHAT_INSTRUCTIONS_MAX_CHARS`).
    instructions: str | None = None
    answer_language: str | None = None
    translate_language: str | None = None
    memory_auto: bool | None = None
    memory_paused: bool | None = None


# The chat agent's settings no other agent takes (docs/agents.md § Per-agent settings).
CHAT_ONLY_SETTINGS = (
    "instructions",
    "answer_language",
    "translate_language",
    "memory_auto",
    "memory_paused",
)
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
    chat_only = [name for name in CHAT_ONLY_SETTINGS if name in provided]
    if chat_only and agent.name != CHAT.name:
        raise ApiError(
            400, "invalid_request", f"The {agent.name} agent takes no {', '.join(chat_only)}"
        )
    for switch in ("memory_auto", "memory_paused"):
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
        if prompt is not None and len(prompt) > settings.system_prompt_max_chars:
            raise ApiError(
                400,
                "invalid_request",
                f"The prompt exceeds {settings.system_prompt_max_chars} characters",
            )
        # The default stored as no prompt, so a later default reaches it.
        if prompt == agent.default_prompt_template.strip():
            prompt = None
    if prompt is not None and body.prompt_preset_id is not None:
        # The two exclude each other (docs/agents.md § Per-agent settings).
        raise ApiError(400, "invalid_request", "Send either prompt or prompt_preset_id")
    if body.connection_id is not None:
        if "anthropic" in agent.capabilities:
            # The chat agent speaks the Messages API through Kano Proxy only (docs/chat.md).
            raise ApiError(
                400, "invalid_request", f"The {agent.name} agent runs on Kano Proxy only"
            )
        await _owned_connection(db, user.id, body.connection_id)
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
    if "connection_id" in provided and body.connection_id != row.connection_id:
        row.connection_id = body.connection_id
        # The stored model belonged to the other upstream's catalog (docs/agents.md).
        if "model" not in provided:
            row.model = None
    if "model" in provided:
        row.model = (body.model or "").strip() or None
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
    try:
        await db.commit()
    except IntegrityError as exc:
        # Another tab deleted the selected connection/prompt between the ownership
        # pre-check and this commit — an ordinary selection race, not a 500.
        await db.rollback()
        raise ApiError(
            409, "invalid_request", "The selected connection or prompt was just deleted"
        ) from exc
    # The convention follows the model, so a write answers the new one along with the row.
    return {**_agent_settings_payload(row), "bbox": _bbox_payload(row)}


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
        job = await db.get(Job, document.job_id)
        if job is not None:
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
    tools = [str(tool["name"]) for tool in chat_runner.turn_tools(settings)]
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


@router.get("/agents/{agent_name}/models")
async def list_agent_models(
    agent_name: str, user: SessionUser, db: DbSession, settings: AppSettings
):
    """The live catalog of the agent's saved upstream — the Agent page's model picker."""
    agent = _known_agent(agent_name)
    return {"data": await agent_models(db, settings, user.id, agent.name)}


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


# --- job history ----------------------------------------------------------------------


@router.get("/jobs")
async def list_jobs(user: SessionUser, db: DbSession, limit: int = Query(default=50, ge=1, le=200)):
    rows = (
        (
            await db.execute(
                select(Job)
                .where(Job.user_id == user.id)
                .order_by(Job.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {
        "jobs": [
            {
                "job_id": str(row.id),
                "agent": row.agent,
                "status": row.status,
                "filename": row.filename,
                "kind": row.kind,
                "model": row.model,
                "page_count": row.page_count,
                "pages_done": row.pages_done,
                "error": row.error,
                "created_at": row.created_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ]
    }


async def owned_job(db: DbSession, user_id: int, job_id: uuid.UUID) -> Job:
    job = (
        await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    ).scalar_one_or_none()
    if job is None:
        raise ApiError(404, "invalid_request", "No such job")
    return job


@router.get("/jobs/{job_id}/result")
async def job_result(job_id: uuid.UUID, user: SessionUser, db: DbSession):
    job = await owned_job(db, user.id, job_id)
    result = (await db.execute(select(Result).where(Result.job_id == job_id))).scalar_one_or_none()
    if result is not None:
        return result_payload(result)
    # A running job answers with its finished pages (docs/api.md § Partial results).
    return await partial_result(db, job)


@router.get("/jobs/{job_id}/figures/{page}/{bbox}")
async def job_figure(
    job_id: uuid.UUID,
    page: int,
    bbox: str,
    user: SessionUser,
    db: DbSession,
    settings: AppSettings,
):
    return await figure_crop_response(settings, await owned_job(db, user.id, job_id), page, bbox)
