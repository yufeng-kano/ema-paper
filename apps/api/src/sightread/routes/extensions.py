"""Control plane `/api/skills` and `/api/subagents` — the chat agent's skills and subagents
(docs/chat.md § Skills, § Subagents; docs/api.md).

The built-ins live in code (`agents/skills.py`, `agents/subagents.py`); what a user stores
is a row of their own, or their edit of a built-in under its name. A built-in is never
deleted or renamed here: deleting its edit restores it, and an edit equal to the built-in
is removed rather than kept.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..agents import skills as skill_defs
from ..agents import subagents as subagent_defs
from ..agents.chat import SUBAGENT_TOOL_NAMES, is_claude_model
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..db.models import Skill, Subagent, User, utcnow
from ..errors import ApiError

router = APIRouter(prefix="/api", tags=["extensions"])


def _checked_name(raw: str) -> str:
    name = raw.strip()
    if not skill_defs.NAME_PATTERN.fullmatch(name):
        raise ApiError(
            400,
            "invalid_request",
            "A name is lower case letters, digits and hyphens, starting with a letter or "
            "a digit, at most 64 characters",
        )
    return name


def _checked_text(value: str, limit: int, label: str, *, required: bool = False) -> str:
    text = value.strip()
    if required and not text:
        raise ApiError(400, "invalid_request", f"The {label} needs text")
    if len(text) > limit:
        raise ApiError(400, "invalid_request", f"The {label} exceeds {limit} characters")
    return text


async def _commit_or_taken(db: DbSession, message: str) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        # Two requests creating or renaming to one name at once: the constraint decides.
        await db.rollback()
        raise ApiError(409, "invalid_request", message) from exc


# --- skills -----------------------------------------------------------------------------


class SkillCreate(BaseModel):
    name: str
    description: str = ""
    argument_hint: str = ""
    body: str
    user_invocable: bool = True
    model_invocable: bool = True


class SkillUpdate(BaseModel):
    """Partial: only the fields present change (docs/api.md)."""

    name: str | None = None
    description: str | None = None
    argument_hint: str | None = None
    body: str | None = None
    user_invocable: bool | None = None
    model_invocable: bool | None = None


def _skill_values(source) -> dict:
    return {field: getattr(source, field) for field in skill_defs.SKILL_FIELDS}


def skill_payload(skill: skill_defs.ResolvedSkill) -> dict:
    return {
        "name": skill.name,
        **_skill_values(skill),
        "builtin": skill.builtin,
        "modified": skill.modified,
        "default": _skill_values(skill.default) if skill.default is not None else None,
    }


def _checked_skill_values(values: dict, settings) -> dict:
    return {
        "description": _checked_text(
            values["description"], skill_defs.DESCRIPTION_MAX_CHARS, "description"
        ),
        "argument_hint": _checked_text(
            values["argument_hint"], skill_defs.ARGUMENT_HINT_MAX_CHARS, "argument hint"
        ),
        "body": _checked_text(
            values["body"], settings.system_prompt_max_chars, "skill", required=True
        ),
        "user_invocable": bool(values["user_invocable"]),
        "model_invocable": bool(values["model_invocable"]),
    }


async def _skill_rows(db: DbSession, user_id: int) -> list[Skill]:
    return list((await db.execute(select(Skill).where(Skill.user_id == user_id))).scalars().all())


async def _skill_row(db: DbSession, user_id: int, name: str) -> Skill | None:
    return (
        await db.execute(select(Skill).where(Skill.user_id == user_id, Skill.name == name))
    ).scalar_one_or_none()


async def _resolved_skill(db: DbSession, user_id: int, name: str) -> dict:
    for skill in skill_defs.resolve_skills(await _skill_rows(db, user_id)):
        if skill.name == name:
            return skill_payload(skill)
    raise ApiError(404, "invalid_request", "No such skill")


@router.get("/skills")
async def list_skills(user: SessionUser, db: DbSession):
    resolved = skill_defs.resolve_skills(await _skill_rows(db, user.id))
    return {"skills": [skill_payload(skill) for skill in resolved]}


async def _lock_user(db: DbSession, user_id: int) -> None:
    """The user's row, held until the create commits: two creations near the cap must not
    both count below it (PostgreSQL; the SQLite fallback runs one request at a time), the
    same lock the turn cap takes (docs/chat.md § Conversations)."""
    await db.execute(select(User.id).where(User.id == user_id).with_for_update())


@router.post("/skills", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillCreate, user: SessionUser, db: DbSession, settings: AppSettings):
    name = _checked_name(body.name)
    if name in skill_defs.BUILTIN_SKILL_NAMES:
        raise ApiError(409, "invalid_request", "That name belongs to a built-in skill")
    await _lock_user(db, user.id)
    if await _skill_row(db, user.id, name) is not None:
        raise ApiError(409, "invalid_request", "A skill with this name already exists")
    own = (
        await db.execute(
            select(func.count())
            .select_from(Skill)
            .where(
                Skill.user_id == user.id, Skill.name.notin_(sorted(skill_defs.BUILTIN_SKILL_NAMES))
            )
        )
    ).scalar_one()
    if own >= settings.chat_skills_max:
        raise ApiError(
            400, "invalid_request", f"You can keep at most {settings.chat_skills_max} skills"
        )
    values = _checked_skill_values(body.model_dump(), settings)
    db.add(Skill(user_id=user.id, name=name, **values))
    await _commit_or_taken(db, "A skill with this name already exists")
    return await _resolved_skill(db, user.id, name)


@router.put("/skills/{name}", dependencies=[CsrfGuard])
async def update_skill(
    name: str, body: SkillUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    provided = {field: getattr(body, field) for field in body.model_fields_set}
    provided = {field: value for field, value in provided.items() if value is not None}
    row = await _skill_row(db, user.id, name)
    default = next((skill for skill in skill_defs.BUILTIN_SKILLS if skill.name == name), None)
    if default is not None:
        if "name" in provided and provided["name"].strip() != name:
            raise ApiError(400, "invalid_request", "A built-in skill cannot be renamed")
        current = _skill_values(row if row is not None else default)
        values = _checked_skill_values({**current, **provided}, settings)
        if skill_defs.same_as_default(values, default):
            # Nothing the built-in does not say: no edit is kept (docs/api.md).
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(Skill(user_id=user.id, name=name, **values))
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = utcnow()
        await _commit_or_taken(db, "The skill was just edited elsewhere; try again")
        return await _resolved_skill(db, user.id, name)

    if row is None:
        raise ApiError(404, "invalid_request", "No such skill")
    new_name = name
    if "name" in provided:
        new_name = _checked_name(provided["name"])
        if new_name != name:
            if new_name in skill_defs.BUILTIN_SKILL_NAMES:
                raise ApiError(409, "invalid_request", "That name belongs to a built-in skill")
            if await _skill_row(db, user.id, new_name) is not None:
                raise ApiError(409, "invalid_request", "A skill with this name already exists")
    values = _checked_skill_values({**_skill_values(row), **provided}, settings)
    row.name = new_name
    for field, value in values.items():
        setattr(row, field, value)
    row.updated_at = utcnow()
    await _commit_or_taken(db, "A skill with this name already exists")
    return await _resolved_skill(db, user.id, new_name)


@router.delete("/skills/{name}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(name: str, user: SessionUser, db: DbSession) -> Response:
    """One of the user's own is deleted; a built-in loses the user's edit — *restore
    default* — and is never itself deleted."""
    row = await _skill_row(db, user.id, name)
    if row is None and name not in skill_defs.BUILTIN_SKILL_NAMES:
        raise ApiError(404, "invalid_request", "No such skill")
    if row is not None:
        await db.delete(row)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- subagents --------------------------------------------------------------------------


class SubagentCreate(BaseModel):
    name: str
    description: str = ""
    model: str | None = None
    writes: bool = False
    tools: list[str] = list(subagent_defs.DEFAULT_TOOLS)
    prompt: str


class SubagentUpdate(BaseModel):
    """Partial: only the fields present change; `model: null` is the conversation's."""

    name: str | None = None
    description: str | None = None
    model: str | None = None
    writes: bool | None = None
    tools: list[str] | None = None
    prompt: str | None = None


def _subagent_values(source) -> dict:
    return {
        "description": source.description,
        "model": source.model or None,
        "writes": bool(source.writes),
        "tools": list(subagent_defs.canonical_tools(source.tools or ())),
        "prompt": source.prompt,
    }


def subagent_payload(subagent: subagent_defs.ResolvedSubagent) -> dict:
    return {
        "name": subagent.name,
        **_subagent_values(subagent),
        "builtin": subagent.builtin,
        "modified": subagent.modified,
        "default": _subagent_values(subagent.default) if subagent.default is not None else None,
    }


def _checked_subagent_values(values: dict, settings) -> dict:
    model = (values["model"] or "").strip() or None
    if model is not None and (len(model) > 255 or not is_claude_model(model)):
        # The chat agent's catalog filter: a subagent runs on the Messages route too.
        raise ApiError(400, "invalid_request", "A subagent runs claude-code/claude-* models only")
    tools = values["tools"] or []
    unknown = [tool for tool in tools if tool not in SUBAGENT_TOOL_NAMES]
    if unknown:
        raise ApiError(
            400,
            "invalid_request",
            "A subagent's tools are a subset of " + ", ".join(SUBAGENT_TOOL_NAMES),
        )
    canonical = list(subagent_defs.canonical_tools(tools))
    if not canonical:
        raise ApiError(400, "invalid_request", "A subagent needs at least one tool")
    writes = bool(values["writes"])
    if "bash" in canonical and not writes:
        # A shell can change anything: it is given only to a subagent that may.
        raise ApiError(400, "invalid_request", "Only a subagent that can change files has bash")
    return {
        "description": _checked_text(
            values["description"], skill_defs.DESCRIPTION_MAX_CHARS, "description"
        ),
        "model": model,
        "writes": writes,
        "tools": canonical,
        "prompt": _checked_text(
            values["prompt"], settings.system_prompt_max_chars, "prompt", required=True
        ),
    }


async def _subagent_rows(db: DbSession, user_id: int) -> list[Subagent]:
    return list(
        (await db.execute(select(Subagent).where(Subagent.user_id == user_id))).scalars().all()
    )


async def _subagent_row(db: DbSession, user_id: int, name: str) -> Subagent | None:
    return (
        await db.execute(select(Subagent).where(Subagent.user_id == user_id, Subagent.name == name))
    ).scalar_one_or_none()


async def _resolved_subagent(db: DbSession, user_id: int, name: str) -> dict:
    for subagent in subagent_defs.resolve_subagents(await _subagent_rows(db, user_id)):
        if subagent.name == name:
            return subagent_payload(subagent)
    raise ApiError(404, "invalid_request", "No such subagent")


@router.get("/subagents")
async def list_subagents(user: SessionUser, db: DbSession):
    resolved = subagent_defs.resolve_subagents(await _subagent_rows(db, user.id))
    return {"subagents": [subagent_payload(subagent) for subagent in resolved]}


@router.post("/subagents", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_subagent(
    body: SubagentCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    name = _checked_name(body.name)
    if name in subagent_defs.BUILTIN_SUBAGENT_NAMES:
        raise ApiError(409, "invalid_request", "That name belongs to a built-in subagent")
    await _lock_user(db, user.id)
    if await _subagent_row(db, user.id, name) is not None:
        raise ApiError(409, "invalid_request", "A subagent with this name already exists")
    own = (
        await db.execute(
            select(func.count())
            .select_from(Subagent)
            .where(
                Subagent.user_id == user.id,
                Subagent.name.notin_(sorted(subagent_defs.BUILTIN_SUBAGENT_NAMES)),
            )
        )
    ).scalar_one()
    if own >= settings.chat_subagents_max:
        raise ApiError(
            400,
            "invalid_request",
            f"You can keep at most {settings.chat_subagents_max} subagents",
        )
    values = _checked_subagent_values(body.model_dump(), settings)
    db.add(Subagent(user_id=user.id, name=name, **values))
    await _commit_or_taken(db, "A subagent with this name already exists")
    return await _resolved_subagent(db, user.id, name)


@router.put("/subagents/{name}", dependencies=[CsrfGuard])
async def update_subagent(
    name: str, body: SubagentUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    provided = {field: getattr(body, field) for field in body.model_fields_set}
    # `model: null` is a value — the conversation's model — not an absent field.
    provided = {
        field: value for field, value in provided.items() if value is not None or field == "model"
    }
    row = await _subagent_row(db, user.id, name)
    default = next(
        (subagent for subagent in subagent_defs.BUILTIN_SUBAGENTS if subagent.name == name), None
    )
    if default is not None:
        if "name" in provided and provided["name"].strip() != name:
            raise ApiError(400, "invalid_request", "A built-in subagent cannot be renamed")
        provided.pop("name", None)
        current = _subagent_values(row if row is not None else default)
        values = _checked_subagent_values({**current, **provided}, settings)
        if subagent_defs.same_as_default(values, default):
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(Subagent(user_id=user.id, name=name, **values))
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = utcnow()
        await _commit_or_taken(db, "The subagent was just edited elsewhere; try again")
        return await _resolved_subagent(db, user.id, name)

    if row is None:
        raise ApiError(404, "invalid_request", "No such subagent")
    new_name = name
    if "name" in provided:
        new_name = _checked_name(provided.pop("name"))
        if new_name != name:
            if new_name in subagent_defs.BUILTIN_SUBAGENT_NAMES:
                raise ApiError(409, "invalid_request", "That name belongs to a built-in subagent")
            if await _subagent_row(db, user.id, new_name) is not None:
                raise ApiError(409, "invalid_request", "A subagent with this name already exists")
    values = _checked_subagent_values({**_subagent_values(row), **provided}, settings)
    row.name = new_name
    for field, value in values.items():
        setattr(row, field, value)
    row.updated_at = utcnow()
    await _commit_or_taken(db, "A subagent with this name already exists")
    return await _resolved_subagent(db, user.id, new_name)


@router.delete(
    "/subagents/{name}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_subagent(name: str, user: SessionUser, db: DbSession) -> Response:
    """One of the user's own is deleted; a built-in loses the user's edit."""
    row = await _subagent_row(db, user.id, name)
    if row is None and name not in subagent_defs.BUILTIN_SUBAGENT_NAMES:
        raise ApiError(404, "invalid_request", "No such subagent")
    if row is not None:
        await db.delete(row)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
