# Agents

Ema Paper hosts **agents**: named pieces of work a user runs against a document, each with its own upstream, model and prompt. There are three: `parse` — the vision pipeline of [parsing.md](./parsing.md) — `chat`, a tool-using conversation about one parsed document ([chat.md](./chat.md)), and `check`, which checks a Files upload's markdown before it opens ([parsing.md](./parsing.md) § Format check). The framework exists so an agent is a registry entry, a runner and a settings row.

## The registry

Agents are defined in code (`apps/api/src/sightread/agents/`), never in the database. A definition carries:

| Field | Meaning |
|-------|---------|
| `name` | Stable id stored on `jobs.agent`, `prompt_presets.agent`, `agent_settings.agent` and `usage_log.agent` (`parse`, `chat`, `check`) |
| `capabilities` | Model capabilities the agent needs (`parse`: `vision`; `chat`: `vision`, `tools`, `anthropic`; `check`: `tools`, `anthropic`). Declared metadata: nothing filters a catalog by them, and since every agent runs on the operator's Kano Proxy there is no upstream to refuse. `anthropic` says which agents speak the Messages API ([chat.md](./chat.md)) |
| `input` / `output` | What the agent consumes and produces (`parse`: `document` → `markdown`; `chat`: `document` → `edits`; `check`: `markdown` → `edits`) |
| `default_prompt_template` | The prompt the agent runs when the user saved none of their own (`parse`: `DEFAULT_PROMPT_TEMPLATE`, [parsing.md](./parsing.md) § Prompts; `chat`: the system prompt, [chat.md](./chat.md) § System prompt; `check`: its system prompt, `agents/check.py`) — and the text the Agent page's prompt dialog opens on. A saved prompt replaces it for every agent; a legacy preset replaces it for `parse` and `check` and is **appended** to it for `chat` |
| `pipeline_version` | Part of the dedup key; moves when the agent's output for the same input would change (`parse`: `PIPELINE_VERSION`; `chat` and `check` have no cache, and theirs is bookkeeping) |
| `bbox_format` | The coordinate contract the agent's output declares (`parse`: `yxyx_norm1000`) |

The definition says nothing about *how* the agent calls a model. `parse` makes one vision call per page, and one or two more on a page whose figure boxes the rules flag ([parsing.md](./parsing.md) § Figure check); `chat` runs a tool loop over the Messages API; `check` runs a short one, inside the parse's own run. The run loop belongs to the agent's runner (`jobs/runner.py` is `parse`'s, `jobs/chat_runner.py` is `chat`'s, `jobs/check.py` is `check`'s, called by `parse`'s), not to the registry. The worker drains two queues: `jobs` for `parse` and `chat_turns` for `chat` ([jobs.md](./jobs.md) § Chat turns); `check` has none of its own.

## Per-agent settings

`agent_settings (user_id, agent)` holds what a user picked for one agent ([database.md](./database.md)):

- **Upstream** — none to choose: every agent runs on the operator's Kano Proxy ([api.md](./api.md) § Upstream, [billing.md](./billing.md) § Models and the operator's upstream).
- **Model** — `model`, the `chat` agent's only: one of `CHAT_MODELS`, NULL for the default, the list's first entry. A stored id the list no longer carries is shown as the current choice and refused at the next send, as a model the catalog dropped is. The `parse` and `check` agents have no model setting: `parse` runs `PARSE_MODEL`, `check` runs `CHECK_MODEL` or the parse model; `GET /api/agents` reports those as the agents' `model` all the same, so the Agent page can state them.
- **Prompt** — `prompt`: the user's own template for this agent, or NULL for the agent's default. The Agent page edits it in a dialog that opens on the default and offers *Restore default*; a text equal to the default is stored as NULL, so a later default reaches it. `prompt_preset_id` is the legacy selection of one of the user's presets **for this agent** (`prompt_presets.agent`) — the web app no longer offers presets, an API call can still select one — and the two exclude each other: writing one clears the other. The migration that introduced `prompt` moved every selected preset into it (for `chat`, behind the default of that release, since a `chat` preset was appended), so nobody's agent changed — except a `chat` preset holding one of the template's tokens, which stays selected, since inside the template the token would be substituted ([database.md](./database.md) § Rules). The Agent page says when a preset still runs, and saving a prompt there replaces it.
- **Effort** — `effort`: `low` / `medium` / `high` / `xhigh` / `max`, the reasoning depth the `chat` and `check` agents ask for (`output_config.effort`, [chat.md](./chat.md)); default `medium`. Ignored by `parse`.
- **Instructions** (`chat` and `check`) — `instructions`: the reader's own markdown, appended after the prompt as a system block of its own; NULL for none ([chat.md](./chat.md) § Instructions; for `check`, what the reader wants of the format, such as how their field writes its formulas). Unlike `prompt` it replaces nothing, so it is what the Agent page offers first; `prompt` is the advanced option.
- **Languages** (`chat` only) — `answer_language` and `translate_language`: language tags, NULL to follow the reader's locale of each message ([chat.md](./chat.md) § Instructions).
- **Memory** (`chat` only) — `memory_auto` (default on: the agent saves memories on its own) and `memory_paused` (default off: memories are neither read nor saved while on) ([chat.md](./chat.md) § Memory). The memories themselves are the user's, not an agent setting: the `memories` table.
- **Fallback** (`parse` only) — `auto_fallback` (default **off**): whether a page the job's model blocked is sent to the deployment's `PARSE_FALLBACK_MODEL` on its own ([parsing.md](./parsing.md) § Fallback model); nothing to switch on while the deployment names no fallback. The fallback is also the model the retry dialog offers first ([web.md](./web.md) § Files), whatever the switch says.

- **Check switch** (`check` only) — `auto_check` (default **on**): whether a Files upload is checked before it opens ([parsing.md](./parsing.md) § Format check). The web Agent page shows the `check` agent's settings in the *Parse* panel, as its *Format check* section, not as a tab of its own ([web.md](./web.md)).

Each agent takes only its own settings (**400** for another's): the languages and the memory switches are `chat`'s, the instructions `chat`'s and `check`'s, the fallback `parse`'s, the switch `check`'s. No row means every default. The rows are written by `PUT /api/agents/{agent}/settings` and read by `GET /api/agents` ([api.md](./api.md)).

## Jobs

Every job names its agent (`jobs.agent`, NOT NULL) and, when its input was another job's output, that job (`jobs.source_job_id`, no FK — immutable history). The dedup key includes the agent, so a result one agent produced never answers another agent's request ([jobs.md](./jobs.md) § Dedup). Usage needs no column of its own: `usage_log.job_id → jobs.agent` attributes every call to an agent.

## Enqueue rules

Resolved at enqueue from the agent's settings, then snapshotted on the job like every other parameter:

1. The model is `PARSE_MODEL`; a per-request `model` must be one of the parse agent's models (`PARSE_MODEL`, `PARSE_FALLBACK_MODEL`) → **400** otherwise.
2. No `PARSE_MODEL` configured → **503** saying so.
3. Kano Proxy not configured on this deployment (`KANO_PROXY_BASE_URL` or `KANO_PROXY_API_KEY` empty) → **503** saying so.
4. The prompt is the user's own prompt, else the selected legacy preset's text, else the agent's default template.
5. The fallback model goes on the job (`jobs.fallback_model`) when `auto_fallback` is on and the deployment names a `PARSE_FALLBACK_MODEL`, and only then: a job queued with the switch off never changes model on its own, whatever the settings say by the time it runs.
6. A Files upload takes the format check with it when the `check` agent's `auto_check` is on and Kano Proxy is configured: `jobs.check_model` (`CHECK_MODEL`, else the parse's — on a retry, the model the retry named, else the job's own), `check_effort`, and `check_prompt` — its prompt (the user's own, else a selected legacy preset's text, else the default) with the instructions appended after it. A queued job keeps what it was given; a retry takes the settings of its own moment ([jobs.md](./jobs.md) § Retrying failed pages).
