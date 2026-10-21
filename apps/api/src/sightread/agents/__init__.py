"""The agent registry (docs/agents.md).

An agent is defined here, in code, and nowhere else: what it is called, what it needs from
a model, what it takes and returns, and the prompt it runs by default. A definition says
nothing about how the agent calls a model — `parse` makes one vision call per page, a later
agent may run a tool loop — so the run loop belongs to the agent's runner, not to this
registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import chat, check, parse


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    # Model capabilities the agent needs. Declared, not enforced: nothing filters a catalog
    # or refuses a model by them yet (docs/agents.md).
    capabilities: tuple[str, ...]
    input: str
    output: str
    default_prompt_template: str
    # The tokens a template of this agent substitutes, the default's and a user's alike.
    prompt_tokens: tuple[str, ...]
    # Part of the dedup key: moves when this agent's output for the same input would change.
    pipeline_version: int
    bbox_format: str


PARSE = AgentDefinition(
    name="parse",
    capabilities=("vision",),
    input="document",
    output="markdown",
    default_prompt_template=parse.DEFAULT_PROMPT_TEMPLATE,
    prompt_tokens=parse.PROMPT_TOKENS,
    pipeline_version=parse.PIPELINE_VERSION,
    bbox_format=parse.BBOX_FORMAT_YXYX,
)

CHAT = AgentDefinition(
    name="chat",
    capabilities=("vision", "tools", "anthropic"),
    input="document",
    output="edits",
    default_prompt_template=chat.DEFAULT_SYSTEM_PROMPT,
    prompt_tokens=chat.PROMPT_TOKENS,
    pipeline_version=chat.PIPELINE_VERSION,
    bbox_format=chat.BBOX_FORMAT,
)

CHECK = AgentDefinition(
    name="check",
    capabilities=("tools", "anthropic"),
    input="markdown",
    output="edits",
    default_prompt_template=check.DEFAULT_PROMPT,
    prompt_tokens=(),
    pipeline_version=check.PIPELINE_VERSION,
    bbox_format=check.BBOX_FORMAT,
)

AGENTS: dict[str, AgentDefinition] = {agent.name: agent for agent in (PARSE, CHAT, CHECK)}


def get_agent(name: str) -> AgentDefinition | None:
    return AGENTS.get(name)
