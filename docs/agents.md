# Agents

Sightread hosts **agents**: named pieces of work a user runs against a document, each with its own upstream, model and prompt. There are two: `parse` — the vision pipeline of [parsing.md](./parsing.md) — and `chat`, a tool-using conversation about one parsed document ([chat.md](./chat.md)). The framework exists so an agent is a registry entry, a runner and a settings row.

## The registry

Agents are defined in code (`apps/api/src/sightread/agents/`), never in the database. A definition carries:

| Field | Meaning |
|-------|---------|
| `name` | Stable id stored on `jobs.agent`, `prompt_presets.agent`, `agent_settings.agent` and `usage_log.agent` (`parse`, `chat`) |
| `capabilities` | Model capabilities the agent needs (`parse`: `vision`; `chat`: `vision`, `tools`, `anthropic`). `anthropic` is the one that filters: the `chat` agent's catalog route lists only the Claude models of Kano Proxy's `claude-code` provider (`claude-code/claude-*`), because it speaks the Messages API and nothing else, and that provider is the one the catalog shows Kano Proxy passing to Anthropic untouched ([chat.md](./chat.md)). The others are declared metadata only |
| `input` / `output` | What the agent consumes and produces (`parse`: `document` → `markdown`; `chat`: `document` → `edits`) |
| `default_prompt_template` | The prompt the agent runs when the user saved none of their own (`parse`: `DEFAULT_PROMPT_TEMPLATE`, [parsing.md](./parsing.md) § Prompts; `chat`: the system prompt, [chat.md](./chat.md) § System prompt) — and the text the Agent page's prompt dialog opens on. A saved prompt replaces it for both agents; a legacy preset replaces it for `parse` and is **appended** to it for `chat` |
| `pipeline_version` | Part of the dedup key; moves when the agent's output for the same input would change (`parse`: `PIPELINE_VERSION`) |
| `bbox_format` | The coordinate contract the agent's output declares (`parse`: `yxyx_norm1000`) |

The definition says nothing about *how* the agent calls a model. `parse` makes one vision call per page; `chat` runs a tool loop over the Messages API. The run loop belongs to the agent's runner (`jobs/runner.py` is `parse`'s, `jobs/chat_runner.py` is `chat`'s), not to the registry. The worker drains two queues: `jobs` for `parse` and `chat_turns` for `chat` ([jobs.md](./jobs.md) § Chat turns).

## Per-agent settings

`agent_settings (user_id, agent)` holds what a user picked for one agent ([database.md](./database.md)):

- **Upstream** — `connection_id`: NULL is the built-in Kano Proxy, otherwise one of the user's provider connections ([api.md](./api.md) § Upstreams). Connections are shared by every agent; the choice of which one is per agent, and only an API caller makes it — the web Agent page leaves every agent on Kano Proxy.
- **Model** — `model`: an id from that upstream's live catalog. NULL means not chosen, and a job cannot be enqueued without a model (below). Changing the upstream clears the model, since it belonged to the other catalog.
- **Prompt** — `prompt`: the user's own template for this agent, or NULL for the agent's default. The Agent page edits it in a dialog that opens on the default and offers *Restore default*; a text equal to the default is stored as NULL, so a later default reaches it. `prompt_preset_id` is the legacy selection of one of the user's presets **for this agent** (`prompt_presets.agent`) — the web app no longer offers presets, an API call can still select one — and the two exclude each other: writing one clears the other. The migration that introduced `prompt` moved every selected preset into it (for `chat`, behind the default of that release, since a `chat` preset was appended), so nobody's agent changed — except a `chat` preset holding one of the template's tokens, which stays selected, since inside the template the token would be substituted ([database.md](./database.md) § Rules). The Agent page says when a preset still runs, and saving a prompt there replaces it.
- **Effort** — `effort`: `low` / `medium` / `high` / `xhigh` / `max`, the reasoning depth the `chat` agent asks for (`output_config.effort`, [chat.md](./chat.md)); default `medium`. Ignored by `parse`.

No row means all three defaults. The rows are written by `PUT /api/agents/{agent}/settings` and read by `GET /api/agents` ([api.md](./api.md)). The `chat` agent runs on Kano Proxy only: a `connection_id` on its row is refused.

## Jobs

Every job names its agent (`jobs.agent`, NOT NULL) and, when its input was another job's output, that job (`jobs.source_job_id`, no FK — immutable history). The dedup key includes the agent, so a result one agent produced never answers another agent's request ([jobs.md](./jobs.md) § Dedup). Usage needs no column of its own: `usage_log.job_id → jobs.agent` attributes every call to an agent.

## Enqueue rules

Resolved at enqueue from the agent's settings, then snapshotted on the job like every other parameter:

1. A per-request `model` overrides the stored one.
2. No model in the request and none stored → **400** `No model chosen for the parse agent: pick one on the Agent page or pass 'model'`.
3. Kano Proxy selected but no key stored → **400** `Kano Proxy is not connected: connect it on the Agent page or choose a provider connection for the parse agent`. Kano Proxy not configured on this deployment → **503** saying so.
4. The prompt is the user's own prompt, else the selected legacy preset's text, else the agent's default template.
