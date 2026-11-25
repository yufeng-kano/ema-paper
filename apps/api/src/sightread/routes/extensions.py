"""Control plane `/api/skills` and `/api/subagents` — the chat agent's skills and subagents
(docs/chat.md § Skills, § Subagents; docs/api.md).

The built-ins live in code (`agents/skills.py`, `agents/subagents.py`); what a user stores
is a row of their own, or their edit of a built-in under its name. A built-in is never
deleted or renamed here: deleting its edit restores it, and an edit equal to the built-in
is removed rather than kept. Both kinds follow these rules through one set of helpers,
each kind described once by a `_Kind`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from operator import attrgetter
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select

from ..agents import skills as skill_defs
from ..agents import subagents as subagent_defs
from ..agents.chat import SUBAGENT_TOOL_NAMES
from ..auth.deps import AppSettings, CsrfGuard, DbSession, SessionUser
from ..config import Settings
from ..db.models import Skill, Subagent, utcnow
from ..db.session import commit_or_conflict
from ..errors import ApiError
from ..workspace.trash import lock_user

router = APIRouter(prefix="/api", tags=["extensions"])


COMPOSER_COMMAND_TAKEN = (
    "That name is one of the composer's own commands ("
    + ", ".join(f"/{command}" for command in sorted(skill_defs.COMPOSER_COMMANDS))
    + "); choose another"
)


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
        # Left out of a library conversation's composer (docs/chat.md § Skills).
        "document_only": skill.document_only,
    }


def _checked_skill_values(values: dict, settings: Settings) -> dict:
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
        "document_only": subagent.document_only,
    }


def _checked_subagent_values(values: dict, settings: Settings) -> dict:
    model = (values["model"] or "").strip() or None
    if model is not None and len(model) > 255:
        raise ApiError(400, "invalid_request", "A subagent's model id is at most 255 characters")
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


# --- both kinds -------------------------------------------------------------------------


@dataclass(frozen=True)
class _Kind:
    """What tells a skill from a subagent in the routes; every rule below is shared."""

    # Its plural, the list's key and the cap's word, is the noun with an s.
    noun: str
    model: type[Skill] | type[Subagent]
    builtins: tuple[Any, ...]
    builtin_names: frozenset[str]
    resolve: Callable[[list], list]
    same_as_default: Callable[[dict, Any], bool]
    # The stored fields of a row, a built-in or a resolved entry.
    values: Callable[[Any], dict]
    # The fields as sent, checked and normalized for storing.
    checked: Callable[[dict, Settings], dict]
    payload: Callable[[Any], dict]
    cap: Callable[[Settings], int]
    # Names refused besides the built-ins', with the 400 message that says why.
    reserved: frozenset[str] = frozenset()
    reserved_message: str = ""
    # Fields whose `null` in an update is a value, not an absent field.
    nullable: frozenset[str] = frozenset()


SKILLS = _Kind(
    noun="skill",
    model=Skill,
    builtins=skill_defs.BUILTIN_SKILLS,
    builtin_names=skill_defs.BUILTIN_SKILL_NAMES,
    resolve=skill_defs.resolve_skills,
    same_as_default=skill_defs.same_as_default,
    values=_skill_values,
    checked=_checked_skill_values,
    payload=skill_payload,
    cap=attrgetter("chat_skills_max"),
    # `/compact` must never run a skill in the runner's place (docs/chat.md § Skills).
    reserved=skill_defs.COMPOSER_COMMANDS,
    reserved_message=COMPOSER_COMMAND_TAKEN,
)

SUBAGENTS = _Kind(
    noun="subagent",
    model=Subagent,
    builtins=subagent_defs.BUILTIN_SUBAGENTS,
    builtin_names=subagent_defs.BUILTIN_SUBAGENT_NAMES,
    resolve=subagent_defs.resolve_subagents,
    same_as_default=subagent_defs.same_as_default,
    values=_subagent_values,
    checked=_checked_subagent_values,
    payload=subagent_payload,
    cap=attrgetter("chat_subagents_max"),
    # `model: null` is the conversation's model.
    nullable=frozenset({"model"}),
)


async def _rows(kind: _Kind, db: DbSession, user_id: int) -> list:
    return list(
        (await db.execute(select(kind.model).where(kind.model.user_id == user_id))).scalars().all()
    )


async def _row(kind: _Kind, db: DbSession, user_id: int, name: str):
    return (
        await db.execute(
            select(kind.model).where(kind.model.user_id == user_id, kind.model.name == name)
        )
    ).scalar_one_or_none()


async def _resolved(kind: _Kind, db: DbSession, user_id: int, name: str) -> dict:
    for entry in kind.resolve(await _rows(kind, db, user_id)):
        if entry.name == name:
            return kind.payload(entry)
    raise ApiError(404, "invalid_request", f"No such {kind.noun}")


def _refuse_reserved(kind: _Kind, name: str) -> None:
    """A name the user's own may not take: a built-in's, or one the kind reserves."""
    if name in kind.builtin_names:
        raise ApiError(409, "invalid_request", f"That name belongs to a built-in {kind.noun}")
    if name in kind.reserved:
        raise ApiError(400, "invalid_request", kind.reserved_message)


async def _list(kind: _Kind, user: SessionUser, db: DbSession) -> dict:
    resolved = kind.resolve(await _rows(kind, db, user.id))
    return {f"{kind.noun}s": [kind.payload(entry) for entry in resolved]}


async def _create(
    kind: _Kind, sent: dict, user: SessionUser, db: DbSession, settings: Settings
) -> dict:
    name = _checked_name(sent["name"])
    _refuse_reserved(kind, name)
    # The user's row, held until the create commits: two creations near the cap must
    # not both count below it (PostgreSQL; the SQLite fallback runs one request at a
    # time), the same lock the turn cap takes (docs/chat.md § Conversations).
    await lock_user(db, user.id)
    taken = f"A {kind.noun} with this name already exists"
    if await _row(kind, db, user.id, name) is not None:
        raise ApiError(409, "invalid_request", taken)
    own = (
        await db.execute(
            select(func.count())
            .select_from(kind.model)
            .where(
                kind.model.user_id == user.id,
                kind.model.name.notin_(sorted(kind.builtin_names)),
            )
        )
    ).scalar_one()
    cap = kind.cap(settings)
    if own >= cap:
        raise ApiError(400, "invalid_request", f"You can keep at most {cap} {kind.noun}s")
    values = kind.checked(sent, settings)
    db.add(kind.model(user_id=user.id, name=name, **values))
    # Two requests creating one name at once: the constraint decides.
    await commit_or_conflict(db, taken)
    return await _resolved(kind, db, user.id, name)


async def _update(
    kind: _Kind,
    name: str,
    body: BaseModel,
    user: SessionUser,
    db: DbSession,
    settings: Settings,
) -> dict:
    provided = {
        field: value
        for field in body.model_fields_set
        if (value := getattr(body, field)) is not None or field in kind.nullable
    }
    row = await _row(kind, db, user.id, name)
    default = next((builtin for builtin in kind.builtins if builtin.name == name), None)
    if default is not None:
        if "name" in provided and provided.pop("name").strip() != name:
            raise ApiError(400, "invalid_request", f"A built-in {kind.noun} cannot be renamed")
        current = kind.values(row if row is not None else default)
        values = kind.checked({**current, **provided}, settings)
        if kind.same_as_default(values, default):
            # Nothing the built-in does not say: no edit is kept (docs/api.md).
            if row is not None:
                await db.delete(row)
        elif row is None:
            db.add(kind.model(user_id=user.id, name=name, **values))
        else:
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_at = utcnow()
        # Two first edits of one built-in at once: the constraint decides.
        await commit_or_conflict(db, f"The {kind.noun} was just edited elsewhere; try again")
        return await _resolved(kind, db, user.id, name)

    if row is None:
        raise ApiError(404, "invalid_request", f"No such {kind.noun}")
    taken = f"A {kind.noun} with this name already exists"
    new_name = name
    if "name" in provided:
        new_name = _checked_name(provided.pop("name"))
        if new_name != name:
            _refuse_reserved(kind, new_name)
            if await _row(kind, db, user.id, new_name) is not None:
                raise ApiError(409, "invalid_request", taken)
    values = kind.checked({**kind.values(row), **provided}, settings)
    row.name = new_name
    for field, value in values.items():
        setattr(row, field, value)
    row.updated_at = utcnow()
    # Two requests renaming to one name at once: the constraint decides.
    await commit_or_conflict(db, taken)
    return await _resolved(kind, db, user.id, new_name)


async def _delete(kind: _Kind, name: str, user: SessionUser, db: DbSession) -> Response:
    row = await _row(kind, db, user.id, name)
    if row is None and name not in kind.builtin_names:
        raise ApiError(404, "invalid_request", f"No such {kind.noun}")
    if row is not None:
        await db.delete(row)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- routes -----------------------------------------------------------------------------


@router.get("/skills")
async def list_skills(user: SessionUser, db: DbSession):
    return await _list(SKILLS, user, db)


@router.post("/skills", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillCreate, user: SessionUser, db: DbSession, settings: AppSettings):
    return await _create(SKILLS, body.model_dump(), user, db, settings)


@router.put("/skills/{name}", dependencies=[CsrfGuard])
async def update_skill(
    name: str, body: SkillUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    return await _update(SKILLS, name, body, user, db, settings)


@router.delete("/skills/{name}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(name: str, user: SessionUser, db: DbSession) -> Response:
    """One of the user's own is deleted; a built-in loses the user's edit — *restore
    default* — and is never itself deleted."""
    return await _delete(SKILLS, name, user, db)


@router.get("/subagents")
async def list_subagents(user: SessionUser, db: DbSession):
    return await _list(SUBAGENTS, user, db)


@router.post("/subagents", dependencies=[CsrfGuard], status_code=status.HTTP_201_CREATED)
async def create_subagent(
    body: SubagentCreate, user: SessionUser, db: DbSession, settings: AppSettings
):
    return await _create(SUBAGENTS, body.model_dump(), user, db, settings)


@router.put("/subagents/{name}", dependencies=[CsrfGuard])
async def update_subagent(
    name: str, body: SubagentUpdate, user: SessionUser, db: DbSession, settings: AppSettings
):
    return await _update(SUBAGENTS, name, body, user, db, settings)


@router.delete(
    "/subagents/{name}", dependencies=[CsrfGuard], status_code=status.HTTP_204_NO_CONTENT
)
async def delete_subagent(name: str, user: SessionUser, db: DbSession) -> Response:
    """One of the user's own is deleted; a built-in loses the user's edit."""
    return await _delete(SUBAGENTS, name, user, db)
