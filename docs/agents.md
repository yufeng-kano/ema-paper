# Agents

Ema Paper hosts **agents**: named pieces of work a user runs against a document, each with its own upstream, model and prompt. There are three:

- `parse` — the vision pipeline of [parsing.md](./parsing.md).
- `chat` — a tool-using conversation about one parsed document ([chat.md](./chat.md)) or, opened from the Files page, about the reader's whole library ([chat.md](./chat.md) § Library conversations).
- `check` — checks a parse's markdown before it opens: a Files upload's, and a parse through the REST API or the connector alike ([parsing.md](./parsing.md) § Format check).

The framework exists so an agent is a registry entry, a runner and a settings row.

## The registry

Agents are defined in code (`apps/api/src/sightread/agents/`), never in the database. A definition carries:

| Field | Meaning |
|-------|---------|
| `name` | Stable id stored on `jobs.agent`, `agent_settings.agent` and `usage_log.agent` (`parse`, `chat`, `check`) |
| `input` / `output` | What the agent consumes and produces (`parse`: `document` → `markdown`; `chat`: `document` → `edits`; `check`: `markdown` → `edits`) |
| `default_prompt_template` | The prompt the agent runs when the user saved none of their own, and the text the Agent page's prompt dialog opens on |
| `prompt_tokens` | The tokens a template of this agent substitutes, the default's and a user's alike |

- **`default_prompt_template`** — `parse`: `DEFAULT_PROMPT_TEMPLATE` ([parsing.md](./parsing.md) § Prompts); `chat`: the system prompt ([chat.md](./chat.md) § System prompt); `check`: its system prompt (`agents/check.py`). A saved prompt replaces it for every agent.
- **`prompt_tokens`** — `parse`: `{page}`, `{bbox_format}`, `{bbox_instructions}`; `chat`: `{document_path}`, `{page_count}`, `{bbox_format}`, `{parse_model}`, `{network}`; `check`: none. The prompt dialog offers them as chips ([web.md](./web.md)).

The definition says nothing about *how* the agent calls a model:

- `parse` makes one vision call per page, and one or two more on a page whose figure boxes the rules flag ([parsing.md](./parsing.md) § Figure check).
- `chat` runs a tool loop over the Messages API.
- `check` runs a short one, inside the parse's own run.

The run loop belongs to the agent's runner, not to the registry: `jobs/runner.py` is `parse`'s, `jobs/chat_runner.py` is `chat`'s, `jobs/check.py` is `check`'s, called by `parse`'s. The worker drains two queues: `jobs` for `parse` and `chat_turns` for `chat` ([jobs.md](./jobs.md) § Chat turns); `check` has none of its own.

## Per-agent settings

`agent_settings (user_id, agent)` holds what a user picked for one agent ([database.md](./database.md)):

| Setting | Column | Agent | Default |
|---------|--------|-------|---------|
| Upstream | — | every agent | the operator's Kano Proxy, none to choose |
| Model | `model` | `chat` | NULL: the list's first entry |
| Prompt | `prompt` | every agent | NULL: the agent's default |
| Effort | `effort` | `chat`, `check`; ignored by `parse` | `medium` |
| Instructions | `instructions` | `chat`, `check` | NULL: none |
| Languages | `answer_language`, `translate_language` | `chat` | NULL: the reader's locale of each message |
| Memory | `memory_auto`, `memory_paused` | `chat` | `memory_auto` on, `memory_paused` off |
| Idle compaction | `idle_compact` | `chat` | **on** |
| Fallback | `auto_fallback` | `parse` | **off** |
| Convert switch | `auto_convert` | `parse` | **off** |
| Check switch | `auto_check` | `check` | **on** |

- **Upstream** — every agent runs on the operator's Kano Proxy ([api.md](./api.md) § Upstream, [billing.md](./billing.md) § Models and the operator's upstream).
- **Model** — `model`, the `chat` agent's only: one of the offered chat models (the live catalog's priced ones, [billing.md](./billing.md) § Models).
  - A stored id the list no longer carries is shown as the current choice and refused at the next send, as a model the catalog dropped is.
  - The `parse` and `check` agents have no model setting: `parse` runs `PARSE_MODEL`, `check` runs `CHECK_MODEL` or the parse model.
  - `GET /api/agents` reports those as the agents' `model` all the same, so the Agent page can state them.
- **Prompt** — `prompt`: the user's own template for this agent.
  - The Agent page edits it in a dialog that opens on the default and offers *Restore default*.
  - A text equal to the default is stored as NULL, so a later default reaches it.
- **Effort** — `effort`: `low` / `medium` / `high` / `xhigh` / `max`, the reasoning depth the `chat` and `check` agents ask for (`output_config.effort`, [chat.md](./chat.md)).
- **Instructions** — `instructions`: the reader's own markdown, appended after the prompt ([chat.md](./chat.md) § Instructions).
  - For `chat`, a system block of its own.
  - For `check`, a section after the prompt in its one system prompt (`# The reader's instructions`, `agents/check.py` `system_prompt`): what the reader wants of the format, such as how their field writes its formulas.
  - Unlike `prompt` it replaces nothing, so it is what the Agent page offers first; `prompt` is the advanced option.
- **Languages** — `answer_language` and `translate_language`: language tags, NULL to follow the reader's locale of each message ([chat.md](./chat.md) § Instructions).
- **Memory** — `memory_auto` (on: the agent saves memories on its own) and `memory_paused` (on: memories are neither read nor saved) ([chat.md](./chat.md) § Memory).
  - The memories themselves are the user's, not an agent setting: the `memories` table.
  - So is the memory file, `memory.md` in the user's workspace, which the Agent page edits through `GET`/`PUT /api/agents/chat/memory-file` ([chat.md](./chat.md) § Memory, *The memory file*).
- **Idle compaction** — `idle_compact`: whether the worker compacts the user's long conversations before their prompt cache expires ([chat.md](./chat.md) § Compaction). Off, only the reader's `/compact` compacts.
- **Fallback** — `auto_fallback`: whether a page the job's model blocked is sent to the deployment's `PARSE_FALLBACK_MODEL` on its own ([parsing.md](./parsing.md) § Fallback model).
  - Nothing to switch on while the deployment names no fallback.
  - The fallback is also the model the retry dialog offers first ([web.md](./web.md) § Files), whatever the switch says.
- **Convert switch** — `auto_convert`: whether a PDF or an image uploaded on the Files page is converted on arrival ([workspace.md](./workspace.md) § Files, [jobs.md](./jobs.md) § Converting a file).
  - Off, it is kept as a file and converted when the reader asks for Markdown in its viewer or from its menu.
  - Settings › Preferences carries it as *Convert on upload* ([web.md](./web.md) § Pages).
- **Check switch** — `auto_check`: whether a parse is checked before it opens — a Files upload's, and a parse through the REST API or the connector alike ([parsing.md](./parsing.md) § Format check).
  - The web Agent page shows the `check` agent's settings in the *Parse* panel, as its *Format check* section, not as a tab of its own ([web.md](./web.md)).

Each agent takes only its own settings (**400** for another's): the model, the languages, the memory switches and idle compaction are `chat`'s, the instructions `chat`'s and `check`'s, the fallback and the convert switch `parse`'s, the check switch `check`'s. No row means every default. The rows are written by `PUT /api/agents/{agent}/settings` and read by `GET /api/agents` ([api.md](./api.md)).

## Jobs

Every job names its agent (`jobs.agent`, NOT NULL). The dedup key includes the agent, so a result one agent produced never answers another agent's request ([jobs.md](./jobs.md) § Dedup). Usage carries its own agent: `usage_log.agent` is written with every row — `parse`, `check` or `chat` — whatever its job or turn became ([database.md](./database.md) § Rules).

## Enqueue rules

Resolved at enqueue from the agent's settings, then snapshotted on the job like every other parameter:

1. The model is `PARSE_MODEL`. No `PARSE_MODEL` configured → **503** saying so.
2. A per-request `model` must be one of the parse agent's models (`PARSE_MODEL`, `PARSE_FALLBACK_MODEL`) → **400** otherwise.
3. Kano Proxy not configured on this deployment (`KANO_PROXY_BASE_URL` or `KANO_PROXY_API_KEY` empty) → **503** saying so.
4. The prompt is the user's own prompt, else the agent's default template.
5. The fallback model goes on the job (`jobs.fallback_model`) only when `auto_fallback` is on and the deployment names a `PARSE_FALLBACK_MODEL`. A job queued with the switch off never changes model on its own, whatever the settings say by the time it runs.
6. A parse takes the format check with it when the `check` agent's `auto_check` is on and Kano Proxy is configured.
   - A parse here is a Files upload, a conversion, a re-convert, or an upload to `/v1/parse` from the REST API or the connector.
   - `jobs.check_model`: `CHECK_MODEL`, else the parse's model. On a retry, the parse's model is the model the retry named, else the job's own.
   - `jobs.check_effort`: the `check` agent's effort.
   - `jobs.check_prompt`: its prompt (the user's own, else the default), with the instructions appended after it.
   - A queued job keeps what it was given; a retry takes the settings of its own moment ([jobs.md](./jobs.md) § Retrying failed pages).
7. A Files upload of a PDF or an image reaches these rules only while the parse agent's `auto_convert` is on.
   - With it off, the upload is filed as a file, and the rules run when the reader converts it, with the settings of that moment ([jobs.md](./jobs.md) § Converting a file).
   - A file of any other kind is never parsed.
