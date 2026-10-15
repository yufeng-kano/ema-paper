# Agents

Sightread hosts **agents**: named pieces of work a user runs against a document, each with its own upstream, model and prompt. There is exactly one today, `parse` — the vision pipeline of [parsing.md](./parsing.md), unchanged. The framework exists so a second agent is a registry entry, a runner and a settings row, not a schema change.

## The registry

Agents are defined in code (`apps/api/src/sightread/agents/`), never in the database. A definition carries:

| Field | Meaning |
|-------|---------|
| `name` | Stable id stored on `jobs.agent`, `prompt_presets.agent` and `agent_settings.agent` (e.g. `parse`) |
| `capabilities` | Model capabilities the agent needs (`parse`: `vision`). **Declared metadata only** — nothing filters the catalog or refuses a model by it yet; the model picker lists the whole live catalog |
| `input` / `output` | What the agent consumes and produces (`parse`: `document` → `markdown`) |
| `default_prompt_template` | The prompt the agent runs when the user selected no preset (`parse`: `DEFAULT_PROMPT_TEMPLATE`, [parsing.md](./parsing.md) § Prompts) |
| `pipeline_version` | Part of the dedup key; moves when the agent's output for the same input would change (`parse`: `PIPELINE_VERSION`) |
| `bbox_format` | The coordinate contract the agent's output declares (`parse`: `yxyx_norm1000`) |

The definition says nothing about *how* the agent calls a model. `parse` happens to make one vision call per page; a future agent may run a tool loop, several calls per step, or none at all. The run loop belongs to the agent's runner (`jobs/runner.py` is `parse`'s), not to the registry. The worker runs `parse`'s runner today; a second agent brings its own and the worker picks it by `jobs.agent`.

## Per-agent settings

`agent_settings (user_id, agent)` holds what a user picked for one agent ([database.md](./database.md)):

- **Upstream** — `connection_id`: NULL is the built-in Kano Proxy, otherwise one of the user's provider connections ([api.md](./api.md) § Upstreams). Connections are shared by every agent; the choice of which one is per agent, and only an API caller makes it — the web Agent page leaves every agent on Kano Proxy.
- **Model** — `model`: an id from that upstream's live catalog. NULL means not chosen, and a job cannot be enqueued without a model (below). Changing the upstream clears the model, since it belonged to the other catalog.
- **Prompt** — `prompt_preset_id`: one of the user's presets **for this agent** (`prompt_presets.agent`), or NULL for the agent's default template.

No row means all three defaults. The rows are written by `PUT /api/agents/{agent}/settings` and read by `GET /api/agents` ([api.md](./api.md)).

## Jobs

Every job names its agent (`jobs.agent`, NOT NULL) and, when its input was another job's output, that job (`jobs.source_job_id`, no FK — immutable history). The dedup key includes the agent, so a result one agent produced never answers another agent's request ([jobs.md](./jobs.md) § Dedup). Usage needs no column of its own: `usage_log.job_id → jobs.agent` attributes every call to an agent.

## Enqueue rules

Resolved at enqueue from the agent's settings, then snapshotted on the job like every other parameter:

1. A per-request `model` overrides the stored one.
2. No model in the request and none stored → **400** `No model chosen for the parse agent: pick one on the Agent page or pass 'model'`.
3. Kano Proxy selected but no key stored → **400** `Kano Proxy is not connected: connect it on the Agent page or choose a provider connection for the parse agent`. Kano Proxy not configured on this deployment → **503** saying so.
4. The prompt is the selected preset's text, else the agent's default template.
