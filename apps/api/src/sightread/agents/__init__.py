"""The agent registry (docs/agents.md).

An agent is defined here, in code, and nowhere else: what it is called, what it takes and
returns, and the prompt it runs by default. A definition says nothing about how the agent
calls a model — `parse` makes one vision call per page, `chat` and `check` run a tool
loop — so the run loop belongs to the agent's runner, not to this registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import chat, check, parse


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    input: str
    output: str
    default_prompt_template: str
    # The tokens a template of this agent substitutes, the default's and a user's alike.
    prompt_tokens: tuple[str, ...]


PARSE = AgentDefinition(
    name="parse",
    input="document",
    output="markdown",
    default_prompt_template=parse.DEFAULT_PROMPT_TEMPLATE,
    prompt_tokens=parse.PROMPT_TOKENS,
)

CHAT = AgentDefinition(
    name="chat",
    input="document",
    output="edits",
    default_prompt_template=chat.DEFAULT_SYSTEM_PROMPT,
    prompt_tokens=chat.PROMPT_TOKENS,
)

CHECK = AgentDefinition(
    name="check",
    input="markdown",
    output="edits",
    default_prompt_template=check.DEFAULT_PROMPT,
    prompt_tokens=(),
)

AGENTS: dict[str, AgentDefinition] = {agent.name: agent for agent in (PARSE, CHAT, CHECK)}


def get_agent(name: str) -> AgentDefinition | None:
    return AGENTS.get(name)
