/**
 * The single typed client for the backend (docs/project-structure.md § Boundaries).
 *
 * Every URL is relative: production routes `/api/*` to FastAPI through Caddy,
 * development through the nitro dev proxy, so the session cookie is always same-origin.
 * Mutations carry `X-Requested-With` — the CSRF pairing the control plane enforces
 * (docs/api.md).
 */

// --- error envelope --------------------------------------------------------

export type ApiErrorType =
  | 'revision_conflict'
  | 'invalid_request'
  | 'storage_full'
  | 'auth'
  | 'rate_limit'
  | 'payment'
  | 'insufficient_credits'
  | 'checkout_unavailable'
  | 'pay_unavailable'
  | 'upstream'
  | 'internal'

/** `status` is 0 when the request never reached the server (offline, DNS, aborted). */
export class ApiRequestError extends Error {
  readonly status: number
  readonly type: ApiErrorType
  /** The envelope's other fields — a 402's `need`, `remaining`, `period_end`
   *  (docs/api.md § Errors); empty for every other error. */
  readonly details: Record<string, unknown>

  constructor(status: number, type: ApiErrorType, message: string, details: Record<string, unknown> = {}) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.type = type
    this.details = details
  }

  get isUnauthorized(): boolean {
    return this.status === 401
  }

  get isOffline(): boolean {
    return this.status === 0
  }
}

// --- response shapes (mirror apps/api/src/sightread/routes) -----------------

export interface User {
  id: number
  email: string
  name: string | null
  /** Google avatar URL, refreshed on each sign-in; null for dev sign-in. */
  picture: string | null
  created_at: string
}

/** The `chat` agent's reasoning effort — `output_config.effort` upstream (docs/chat.md). */
export type AgentEffort = 'low' | 'medium' | 'high' | 'xhigh' | 'max'

export const AGENT_EFFORTS: readonly AgentEffort[] = ['low', 'medium', 'high', 'xhigh', 'max']

/** One agent's settings (docs/agents.md); every null is that field's default. */
export interface AgentSettings {
  /** The model the agent runs (docs/api.md § GET /api/agents): the deployment's for `parse`
   *  and `check` — a fact — and for `chat` the reader's pick among the offered models, or
   *  the first offered; null when the deployment names none. Only `chat` takes a write. */
  model: string | null
  /** The user's own template; null means the agent's `default_prompt` (docs/agents.md). */
  prompt: string | null
  /** The legacy preset selection, which only an API call still makes; the page shows none. */
  prompt_preset_id: number | null
  /** Reasoning effort; meaningful for the `chat` and `check` agents, `medium` when unset. */
  effort: AgentEffort | null
  /** `chat` and `check` only: the reader's instructions, appended after the prompt; null for
   *  none (docs/chat.md § Instructions). Optional fields so a page served by an older API
   *  still renders. */
  instructions?: string | null
  /** `chat` only: language tags; null follows the reader's locale of each message. */
  answer_language?: string | null
  translate_language?: string | null
  /** `chat` only: the memory switches (docs/chat.md § Memory). */
  memory_auto?: boolean
  memory_paused?: boolean
  /** `parse` only: the deployment's fallback model, a fact (null: none), and whether a
   *  blocked page goes there on its own (docs/parsing.md § Fallback model). */
  fallback_model?: string | null
  auto_fallback?: boolean
  /** `check` only: whether a Files upload is checked before it opens (docs/parsing.md
   *  § Format check). */
  auto_check?: boolean
}

/**
 * The coordinate convention the pipeline asks the saved model for — a fact about the model's
 * family, never a setting (docs/parsing.md § Coordinate conventions).
 */
export interface BboxConvention {
  family: 'gemini' | 'claude' | 'gpt' | 'default'
  format: string
}

/** An entry of the code-defined agent registry, with the caller's settings for it. */
export interface AgentEntry {
  name: string
  capabilities: string[]
  input: string
  output: string
  /** What the prompt dialog opens on when the user saved no prompt of their own. */
  default_prompt: string
  /** The tokens the template substitutes — the prompt dialog's chips. */
  prompt_tokens?: string[]
  /** The tools this deployment really gives the agent (empty for agents without any). */
  tools: string[]
  settings: AgentSettings
  bbox: BboxConvention
}

/** What a settings write answers: the row as stored, and the convention it now implies. */
export type SavedAgentSettings = AgentSettings & { bbox: BboxConvention }

/** One model an agent may run (docs/api.md § GET /api/agents/{agent}/models). */
export interface ConnectionModel {
  id: string
  name: string | null
}

/** The server's own limits, so the file picker, the prompt dialog and the composer enforce
 *  one set of numbers. */
export interface UploadLimits {
  upload_max_bytes: number
  page_cap: number
  accepted_media_types: string[]
  /** What intake accepts by extension when the browser reports a generic media type. */
  accepted_extensions: string[]
  /** Which uploads are markdown, and their own cap: they are not parsed, they become
   *  `doc.md` (docs/workspace.md § Markdown documents). Optional only so a page served by an
   *  older API still renders. */
  markdown_media_types?: string[]
  markdown_extensions?: string[]
  markdown_max_bytes?: number
  /** What an image inserted into a document may be (docs/web.md § Editing, *Images*).
   *  Optional, as above. */
  image_media_types?: string[]
  image_max_bytes?: number
  /** The cap on an agent's prompt, a skill's body and a subagent's prompt. */
  prompt_max_chars: number
  /** The chat composer's attachments (docs/chat.md § What a user message carries). Optional
   *  only so a page served by an older API still renders. */
  attachment_max_bytes?: number
  attachments_per_message?: number
  attachment_extensions?: string[]
  /** The chat agent's instructions and one memory (docs/chat.md § Instructions, § Memory). */
  instructions_max_chars?: number
  memory_max_chars?: number
  /** Whole sections one message may carry, the cap on one section's text and on the
   *  selection, and on a section's heading (docs/chat.md § What a user message carries). */
  sections_per_message?: number
  selection_max_chars?: number
  section_heading_max_chars?: number
}

// --- memory (docs/chat.md § Memory) ----------------------------------------------------

export type MemoryKind = 'term' | 'preference' | 'about' | 'document'

export const MEMORY_KINDS: readonly MemoryKind[] = ['term', 'preference', 'about', 'document']

/** One of the reader's memories, as `GET /api/memories` lists it. */
export interface Memory {
  id: number
  kind: MemoryKind
  text: string
  /** The one document it applies to; null for every document. */
  document_id: number | null
  /** That document's name while it exists. */
  document_name: string | null
  /** Always loaded by a new conversation. */
  pinned: boolean
  /** The conversation that saved it, and its title, while it exists. */
  conversation_id: number | null
  conversation_title: string | null
  /** The `tool_use_id` of the call that saved it: what a thread's memory line finds it by. */
  source_ref: string | null
  created_at: string
  updated_at: string
}

/** A memory as the `memory` tool left it, carried by the `memory` event. */
export interface MemorySnapshot {
  id: number
  kind: MemoryKind
  text: string
  document_id: number | null
  pinned: boolean
}

export interface MemoryFields {
  kind: MemoryKind
  text: string
  document_id: number | null
  pinned: boolean
}

// --- what the chat agent receives (docs/api.md, GET /api/agents/chat/context) -------------

export type AgentContextPartKind = 'prompt' | 'preset' | 'instructions' | 'catalogue' | 'memory'

export interface AgentContextPart {
  kind: AgentContextPartKind
  text: string
  /** Its length in characters (code points). */
  chars: number
  /** `prompt`: the reader's own prompt rather than the default. */
  own?: boolean
  /** `catalogue`: how many skills and subagents it lists. */
  skills?: number
  subagents?: number
  /** `memory`: how many memories a new conversation would load, and whether memory is paused. */
  count?: number
  paused?: boolean
}

export interface AgentContextFacts {
  document_path: string
  page_count: string
  bbox_format: string
  parse_model: string
  network: string
}

export interface AgentContext {
  /** The system blocks in the order a turn sends them; a block not sent is not listed. */
  parts: AgentContextPart[]
  /** The context block a message sent now would carry. */
  turn: { text: string; chars: number }
  /** The document's facts, when one was named; null on the Agent page. */
  facts: AgentContextFacts | null
}

// --- skills and subagents (docs/chat.md § Skills, § Subagents) -----------------------

/** What a skill holds, apart from its name. */
export interface SkillFields {
  /** When to use it: what the model reads to decide. */
  description: string
  /** What its command takes, shown in the `/` menu. */
  argument_hint: string
  body: string
  /** The reader can run it as `/name`. */
  user_invocable: boolean
  /** The agent may load it on its own. */
  model_invocable: boolean
}

export interface Skill extends SkillFields {
  name: string
  /** Defined in code: editable and restorable, never deleted or renamed. */
  builtin: boolean
  /** A built-in carrying the reader's edit. */
  modified: boolean
  /** A built-in's shipped fields; null for a skill of the reader's own. */
  default: SkillFields | null
}

/** The tools a subagent may hold: the editor answers `view` only to one that cannot change
 *  files, and `bash` needs `writes` (docs/chat.md § Subagents). */
export const SUBAGENT_TOOLS = ['view_page', 'view_figure', 'str_replace_based_edit_tool', 'bash'] as const
/** The tools that need `writes`. */
export const WRITING_TOOLS: readonly string[] = ['bash']

export interface SubagentFields {
  /** When to start it: what the agent reads. */
  description: string
  /** A model id of the chat agent's catalog; null runs it on the conversation's model. */
  model: string | null
  /** May change files: the editor writes, and `bash` may be among its tools. */
  writes: boolean
  tools: string[]
  prompt: string
}

export interface Subagent extends SubagentFields {
  name: string
  builtin: boolean
  modified: boolean
  default: SubagentFields | null
}

// --- attachments (docs/chat.md § What a user message carries) -------------------------

/** One prepared upload waiting for its message. */
export interface Attachment {
  id: string
  name: string
  /** An image goes to the model as an image; a PDF or a text file as its text. */
  kind: 'image' | 'text'
  media_type: string
  size_bytes: number
  chars: number | null
}

export interface MeResponse {
  user: User
  limits: UploadLimits
  /** `free`, or the paid plan's id, and the period's balance — the shell's figures, so
   *  it needs no second request (docs/billing.md § Routes). */
  plan: string
  credits_remaining: number
  /** The balance a chat send needs (`CHAT_MIN_CREDITS`). */
  chat_min_credits: number
}

// --- billing (docs/billing.md § Routes) --------------------------------------------

export interface BillingPlan {
  id: string
  name: string
  credits: number
  storage_bytes: number
  sandbox: boolean
  max_jobs: number
}

export interface BillingSubscription {
  status: string
  plan_id: string
  period_start: string
  period_end: string
  cancel_at_period_end: boolean
}

/** Credits per 1K tokens of one offered chat model. */
export interface ChatRate {
  input: number
  cache_read: number
  cache_write: number
  output: number
}

export interface BillingRates {
  parse_per_page: number
  chat: Record<string, ChatRate>
}

export interface BillingResponse {
  plan: BillingPlan
  subscription: BillingSubscription | null
  period: { start: string; end: string; granted: number; spent: number; remaining: number }
  rates: BillingRates
  checkout_available: boolean
  pay_reachable: boolean | null
}

/** A plan on sale: Pay's price and this deployment's grants (docs/billing.md § Kano Pay). */
export interface CatalogPlan {
  plan_id: string
  name: string
  /** The amount in the currency's minor unit, as Pay gives it. */
  amount: string
  currency: string
  cycle: { interval: string; frequency: number }
  credits: number
  storage_bytes: number
  sandbox: boolean
  max_jobs: number
}

export interface CatalogResponse {
  available: boolean
  plans: CatalogPlan[]
  free: { credits: number; storage_bytes: number; sandbox: boolean; max_jobs: number }
  rates: BillingRates
}

/** What the public policy pages print (docs/api.md, `GET /api/site`): the deployment's
 *  support address and operator name, each null when `.env` leaves it empty. */
export interface SiteInfo {
  support_email: string | null
  operator_name: string | null
}

/** What a 402 says (docs/billing.md § Refusals): the credits the action needs, the balance,
 *  and when the period resets. */
export interface CreditsRefusal {
  need: number
  remaining: number
  period_end: string
}

/** The refusal a failed request carries, or null for any other failure. */
export function creditsRefusalOf(error: unknown): CreditsRefusal | null {
  if (!(error instanceof ApiRequestError) || error.type !== 'insufficient_credits') {
    return null
  }
  const { need, remaining, period_end } = error.details
  if (typeof need !== 'number' || typeof remaining !== 'number' || typeof period_end !== 'string') {
    return null
  }
  return { need, remaining, period_end }
}

export interface ApiKeySummary {
  id: number
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
}

/** `key` is the plaintext, returned exactly once by `POST /api/keys` (docs/auth.md). */
export interface CreatedApiKey {
  id: number
  name: string
  prefix: string
  created_at: string
  key: string
}

// --- workspace: content, notes, revisions (docs/workspace.md) ------------------

/** The document's editable copy. `revision` is a content hash, not a commit id. */
export interface DocumentContent {
  markdown: string
  revision: string
  notes: string
  notes_revision: string
  /** Whether the original upload is kept beside it — what `view_page` needs. */
  has_source: boolean
}

export interface Revision {
  commit: string
  author: string
  message: string
  created_at: string
  files: string[]
}

// --- chat (docs/chat.md) -------------------------------------------------------

export interface Conversation {
  id: number
  title: string | null
  created_at: string
  updated_at: string
}

export interface ConversationDetail extends Conversation {
  document_id: number
}

/** What the thread knows of an attached file: its name, never its text or bytes. */
export interface AttachmentMeta {
  name: string
  kind?: 'image' | 'text'
  chars?: number | null
}

/** One thing a subagent did, as its run's record keeps it (docs/chat.md § Subagents). */
export type SubagentStep =
  /** `id` joins the step to its `edit` event; a record from before it lacks one. */
  | { type: 'tool'; id?: string; name: string; input: Record<string, unknown>; is_error: boolean }
  | { type: 'note'; text: string }

/** What a `subagent` call's result carries beside the report, for the thread to draw. */
export interface SubagentRecord {
  name: string
  model: string | null
  steps: SubagentStep[]
  tool_calls: number
  elapsed_ms: number
}

/** Anthropic-shaped content blocks, stored verbatim (docs/chat.md § History). */
export type ContentBlock =
  /**
   * `context` marks a text block the backend appended — the selected passage, the quoted
   * one, a command's skill, an attached file — not the reader's own words (docs/chat.md
   * § What a user message carries). A skill block is served without its body and an
   * attached file without its text, so `text` can be absent on them.
   */
  | {
      type: 'text'
      text: string
      context?: string
      /** The skill a `context: skill` block ran. */
      skill?: string
      /** The heading of a `context: section` block, served without the section's text. */
      section?: string
      /** The file a `context: attachment` block holds. */
      attachment?: AttachmentMeta
    }
  | { type: 'thinking'; thinking: string }
  | { type: 'tool_use'; id: string; name: string; input: Record<string, unknown> }
  | {
      type: 'tool_result'
      tool_use_id: string
      content: string | ContentBlock[]
      is_error?: boolean
      /** A `subagent` call's record of what the subagent did. */
      subagent?: SubagentRecord
    }
  /**
   * An image: a page or figure a tool returned (kept, not drawn), or one the reader
   * attached (`attachment`), whose bytes the thread asks for by the block's index. A
   * message drawn before the record holds it carries its local preview as `preview`.
   */
  | { type: 'image'; source?: unknown; attachment?: AttachmentMeta; preview?: string }
  /** Any other block the API stores: kept, not drawn. */
  | { type: 'document'; source?: unknown }

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: ContentBlock[]
  /** The turn that stored it (a UUID); null only for rows older than the turn link. */
  turn_id: string | null
  created_at: string
}

export type TurnStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | string

export interface ChatTurn {
  id: string
  status: TurnStatus
  error: string | null
  /** What the turn cost, once settled; null while unsettled, absent from a record served by
   *  an older API (docs/billing.md § Routes). */
  credits?: number | null
  /** Null while the turn is unfinished, whatever its status: a cancel marks the status
   *  first and the writer winds down after (docs/chat.md § Conversations). */
  finished_at: string | null
}

export interface ConversationResponse {
  conversation: ConversationDetail
  messages: ChatMessage[]
  turns: ChatTurn[]
}

/**
 * The `chat_events` stream, one row per SSE frame: `event` is the frame's event name, the
 * rest its JSON data (docs/chat.md § Conversations, turns, events).
 */
export type ChatEvent = ChatEventData & {
  /** The turn the frame belongs to: every frame carries it, so a turn that never
   *  started (its load failed) still owns its `turn_error`. */
  turn_id: string
}

/**
 * `parent` names the `subagent` call a subagent's own events belong to; the turn's own
 * events carry none (docs/chat.md § Conversations, turns, events).
 */
type ChatEventData =
  | {
      event: 'turn_start'
      model: string
      /** Whether the turn had the reader's instructions, and the memories it loaded
       *  (docs/chat.md § Memory). Absent on turns from before both existed. */
      instructions?: boolean
      memory_ids?: number[]
    }
  | { event: 'text_delta'; text: string }
  | { event: 'thinking'; text: string; parent?: string }
  | { event: 'tool_use'; id: string; name: string; input: Record<string, unknown>; parent?: string }
  | {
      event: 'tool_result'
      id: string
      name: string
      is_error: boolean
      output: string
      parent?: string
      /** On a `subagent` call's result: what the subagent did. */
      subagent?: SubagentRecord
    }
  | {
      event: 'edit'
      path: string
      command: string
      commit: string
      diff: string
      tool_use_id: string
    }
  | {
      event: 'memory'
      tool_use_id: string
      action: 'saved' | 'updated' | 'forgotten'
      /** The row as the call left it; as it was before a `forgotten`. */
      memory: MemorySnapshot
      /** The row before an `updated`, else null. */
      previous: MemorySnapshot | null
    }
  | { event: 'retry'; attempt: number; parent?: string }
  | { event: 'turn_done' }
  | { event: 'turn_error'; status: string; error: string }

export const CHAT_EVENT_NAMES: readonly ChatEvent['event'][] = [
  'turn_start',
  'text_delta',
  'thinking',
  'tool_use',
  'tool_result',
  'edit',
  'memory',
  'retry',
  'turn_done',
  'turn_error',
]

// --- sandbox (docs/sandbox.md) --------------------------------------------------

export type SandboxState = 'stopped' | 'starting' | 'running' | 'unavailable'

export interface SandboxStatus {
  state: SandboxState
  /** Whether the plan gives the chat agent a sandbox at all; `state` says whether the
   *  deployment has one to give (docs/billing.md § Plans). */
  granted?: boolean
  last_used_at: string | null
  shells: number
  uptime_seconds: number
  workspace: {
    bytes: number
    quota_bytes: number
    breakdown: { path: string; bytes: number }[]
  }
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface JobSummary {
  job_id: string
  status: JobStatus
  filename: string
  kind: string
  agent: string
  model: string
  page_count: number | null
  pages_done: number
  error: string | null
  created_at: string
  finished_at: string | null
  /** What the parse cost (docs/billing.md § Routes); null while reserved and unsettled. */
  credits?: number | null
}

export interface ResultPage {
  page: number
  /** Absent on a partial result's pages — only the final outcome knows the dimensions. */
  width_pt?: number
  height_pt?: number
  method: 'vision' | null
  /** The model that wrote the page — the fallback's or a retry's when it was not the job's.
   *  Absent on results written before pages named it. */
  model?: string | null
  error: string | null
}

export interface ResultFigure {
  id: string
  page: number
  bbox: [number, number, number, number]
  caption: string | null
  /** What the figure check made of the box (docs/api.md). Absent on results parsed before
   *  it (`pipeline_version` below 10). */
  check?: 'verified' | 'adjusted' | 'unverified'
}

export interface ResultPageError {
  page: number
  reason: string
}

export interface ResultMeta {
  /** Absent on results stored before the pipeline started writing it. */
  job_id?: string
  /** Absent on results stored before agents existed; those carry `profile` instead. */
  agent?: string
  profile?: string | null
  model: string
  bbox_format: string
  pipeline_version: number
  sha256: string
  cached: boolean
  /** The figure check's statuses, counted. Absent on results parsed before it. */
  figure_check?: { verified: number; adjusted: number; unverified: number }
  /** True on a running job's snapshot — never cache one (docs/api.md § Partial results). */
  partial?: boolean
  page_count?: number | null
  pages_done?: number
}

export interface JobResult {
  markdown: string
  pages: ResultPage[]
  figures: ResultFigure[]
  errors: ResultPageError[]
  meta: ResultMeta
}

/** A directory in the file library; `parent_id` null is the root (docs/web.md § Files). */
export interface LibraryFolder {
  id: number
  parent_id: number | null
  name: string
  created_at: string
  updated_at: string
}

/**
 * One file: the entry, plus the live state of the parse behind it. The two travel together
 * so a row renders — name, status, progress — from a single `GET /api/library`. A markdown
 * document (`kind` `markdown`) has no parse: every parse field is null (docs/api.md).
 */
export interface LibraryDocument {
  id: number
  folder_id: number | null
  name: string
  job_id: string | null
  status: JobStatus | null
  kind: string
  model: string | null
  page_count: number | null
  pages_done: number | null
  size_bytes: number | null
  error: string | null
  created_at: string
  finished_at: string | null
  /** A failed parse's pages with no result — what a retry would send; null for any other
   *  (docs/api.md). */
  failed_pages: number | null
  /** A running parse past its last page, being checked before it opens (docs/parsing.md
   *  § Format check). Optional so a page served by an older API still renders. */
  checking?: boolean
  /** The re-convert queued, running or failed — null when there is none. Optional so a page
   *  served by an older API still renders. */
  reconvert?: DocumentReconvert | null
  /** What the parse cost once settled; null while its reservation is pending, which is
   *  the whole of a queued or running parse (docs/billing.md § Routes). */
  credits?: number | null
}

/**
 * A document's re-convert: a new parse of its original, which the document takes once it
 * succeeds and until then only points at — the version it has opens as before (docs/jobs.md
 * § Re-converting a document). The same fields as the row's own parse.
 */
export interface DocumentReconvert {
  job_id: string
  status: JobStatus
  model: string | null
  page_count: number | null
  pages_done: number | null
  error: string | null
  failed_pages: number | null
  checking: boolean
  created_at: string
  finished_at: string | null
}

/** One page of a parse's selection, as the retry dialog lists it (docs/api.md). */
export interface DocumentPage {
  page: number
  /** `pending`: never reached — still queued or running, or the job ended before it. */
  status: 'succeeded' | 'failed' | 'pending'
  /** Why it failed (docs/parsing.md § Vision-only conversion); null unless it did. */
  reason: string | null
  /** The model that wrote it, or failed it; null when it was never reached. */
  model: string | null
}

/** A folder in the Trash: trashed itself, never one inside a trashed folder (docs/api.md). */
export interface TrashedFolder extends LibraryFolder {
  trashed_at: string
  /** The folder names from the top level down to where Put Back returns it; `[]` is the top. */
  origin: string[]
  /** How many documents are inside it. */
  items: number
}

export interface TrashedDocument extends LibraryDocument {
  trashed_at: string
  origin: string[]
}

/** `folders` and `documents` are the live library; `trash` is what was moved to the Trash. */
export interface LibraryResponse {
  folders: LibraryFolder[]
  documents: LibraryDocument[]
  trash: { folders: TrashedFolder[]; documents: TrashedDocument[] }
}

// --- transport -------------------------------------------------------------

const CSRF_HEADER = 'X-Requested-With'

function toApiError(status: number, statusText: string, payload: unknown): ApiRequestError {
  const envelope =
    payload && typeof payload === 'object' && 'error' in payload
      ? (payload as { error?: { type?: ApiErrorType; message?: string } & Record<string, unknown> }).error
      : undefined
  const { type, message, ...details } = envelope ?? {}
  return new ApiRequestError(
    status,
    type ?? 'internal',
    message ?? statusText ?? 'Request failed',
    envelope && typeof envelope === 'object' ? details : {},
  )
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {}
  if (method !== 'GET') {
    headers[CSRF_HEADER] = 'fetch'
  }
  // `FormData` sets its own `Content-Type` with the multipart boundary in it; naming the
  // type here would send a boundary-less header and the body would arrive unparseable.
  const multipart = body instanceof FormData
  if (body !== undefined && !multipart) {
    headers['Content-Type'] = 'application/json'
  }

  let response: Response
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: 'include',
      body: body === undefined ? undefined : multipart ? (body as FormData) : JSON.stringify(body),
    })
  } catch (cause) {
    throw new ApiRequestError(0, 'internal', cause instanceof Error ? cause.message : 'Network error')
  }

  const text = response.status === 204 ? '' : await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      // A non-JSON body means something in front of the API answered (proxy, error page).
      throw new ApiRequestError(response.status, 'internal', response.statusText || 'Invalid response')
    }
  }

  if (!response.ok) {
    throw toApiError(response.status, response.statusText, payload)
  }
  // 204 answers (logout, revoke, delete) carry nothing to hand back.
  return (text ? payload : undefined) as T
}

// --- control plane ---------------------------------------------------------

export function getMe(): Promise<MeResponse> {
  return request<MeResponse>('GET', '/api/me')
}

export function logout(): Promise<void> {
  return request<undefined>('POST', '/api/auth/logout')
}

export function devLogin(): Promise<{ user: { id: number; email: string } }> {
  return request('POST', '/api/auth/dev-login')
}

/**
 * Whether `POST /api/auth/dev-login` exists on this backend (local + AUTH_DEV_MODE only).
 *
 * The probe deliberately omits the `X-Requested-With` header: the route's CSRF guard then
 * rejects it with 403 before any session is created, so probing cannot sign anyone in.
 * Only a 404 — the route was never registered — hides the button.
 */
export async function isDevLoginAvailable(): Promise<boolean> {
  try {
    const response = await fetch('/api/auth/dev-login', { method: 'POST', credentials: 'include' })
    return response.status !== 404
  } catch {
    return false
  }
}

export function listKeys(): Promise<{ keys: ApiKeySummary[] }> {
  return request('GET', '/api/keys')
}

export function createKey(name: string): Promise<CreatedApiKey> {
  return request('POST', '/api/keys', { name })
}

export function revokeKey(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/keys/${id}`)
}

// --- agents (docs/agents.md) ---------------------------------------------------

export function listAgents(): Promise<{ agents: AgentEntry[] }> {
  return request('GET', '/api/agents')
}

/** Partial update: only the fields present change; `model` is the chat agent's alone. */
export function putAgentSettings(
  agent: string,
  settings: Partial<AgentSettings>,
): Promise<SavedAgentSettings> {
  return request('PUT', `/api/agents/${encodeURIComponent(agent)}/settings`, settings)
}

/** The models the agent may run — what a model picker lists (docs/billing.md § Models). */
export function listAgentModels(agent: string): Promise<{ data: ConnectionModel[] }> {
  return request('GET', `/api/agents/${encodeURIComponent(agent)}/models`)
}

/** What the chat agent receives, rendered by the runner's own code, for a message sent in
 *  `locale`; with a document, its facts filled in. */
export function getAgentContext(locale: string, documentId?: number): Promise<AgentContext> {
  const query = new URLSearchParams({ locale })
  if (documentId !== undefined) query.set('document_id', String(documentId))
  return request('GET', `/api/agents/chat/context?${query}`)
}

// --- memory (docs/chat.md § Memory) ----------------------------------------------------

export function listMemories(): Promise<{ memories: Memory[]; max: number }> {
  return request('GET', '/api/memories')
}

/** Saves one again: how a thread's Undo puts back what the agent forgot, and Save again
 *  restores what Undo removed (with the call's `tool_use_id` as `source_ref`). */
export function createMemory(
  body: Pick<MemoryFields, 'kind' | 'text'> &
    Partial<Pick<MemoryFields, 'document_id' | 'pinned'>> & {
      conversation_id?: number | null
      source_ref?: string | null
    },
): Promise<Memory> {
  return request('POST', '/api/memories', body)
}

/** Writes the fields present; `document_id: null` widens it to every document. */
export function updateMemory(id: number, body: Partial<MemoryFields>): Promise<Memory> {
  return request('PUT', `/api/memories/${id}`, body)
}

export function deleteMemory(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/memories/${id}`)
}

/** Clear all memories; the switches stay as they are. */
export function clearMemories(): Promise<void> {
  return request<undefined>('DELETE', '/api/memories')
}

// --- skills and subagents (docs/chat.md § Skills, § Subagents) -----------------------

export function listSkills(): Promise<{ skills: Skill[] }> {
  return request('GET', '/api/skills')
}

/** One of the reader's own; a built-in's name is never free for it (409). */
export function createSkill(body: SkillFields & { name: string }): Promise<Skill> {
  return request('POST', '/api/skills', body)
}

/** Writes the fields present. On a built-in it stores the reader's edit — one equal to the
 *  default is removed instead; `name` renames one of the reader's own. */
export function updateSkill(name: string, body: Partial<SkillFields & { name: string }>): Promise<Skill> {
  return request('PUT', `/api/skills/${encodeURIComponent(name)}`, body)
}

/** Deletes one of the reader's own; on a built-in, removes the edit — restore default. */
export function deleteSkill(name: string): Promise<void> {
  return request<undefined>('DELETE', `/api/skills/${encodeURIComponent(name)}`)
}

export function listSubagents(): Promise<{ subagents: Subagent[] }> {
  return request('GET', '/api/subagents')
}

export function createSubagent(body: SubagentFields & { name: string }): Promise<Subagent> {
  return request('POST', '/api/subagents', body)
}

export function updateSubagent(
  name: string,
  body: Partial<SubagentFields & { name: string }>,
): Promise<Subagent> {
  return request('PUT', `/api/subagents/${encodeURIComponent(name)}`, body)
}

export function deleteSubagent(name: string): Promise<void> {
  return request<undefined>('DELETE', `/api/subagents/${encodeURIComponent(name)}`)
}

// --- sandbox (docs/sandbox.md) --------------------------------------------------

export function getBilling(): Promise<BillingResponse> {
  return request('GET', '/api/billing')
}

/** Public: no session needed (docs/billing.md § Kano Pay). */
export function getBillingCatalog(): Promise<CatalogResponse> {
  return request('GET', '/api/billing/catalog')
}

export function getSite(): Promise<SiteInfo> {
  return request('GET', '/api/site')
}

/** Pay's checkout page for the plan — the browser goes there (docs/billing.md § Kano Pay). */
export function createCheckout(planId: string): Promise<{ url: string }> {
  return request('POST', '/api/billing/checkout', { plan_id: planId })
}

/** Pay's customer portal: payment method, invoices, cancelling. */
export function createPortal(): Promise<{ url: string }> {
  return request('POST', '/api/billing/portal', {})
}

export function getSandbox(): Promise<SandboxStatus> {
  return request('GET', '/api/sandbox')
}

/** Stops the VM; everything on disk stays. */
export function stopSandbox(): Promise<void> {
  return request<undefined>('POST', '/api/sandbox/stop')
}

/** Stops the VM and clears the package cache and shell state; documents are untouched. */
export function resetSandbox(): Promise<void> {
  return request<undefined>('POST', '/api/sandbox/reset')
}

export function listJobs(limit: number): Promise<{ jobs: JobSummary[] }> {
  return request('GET', `/api/jobs?limit=${limit}`)
}

export function getJobResult(jobId: string): Promise<JobResult> {
  return request('GET', `/api/jobs/${jobId}/result`)
}

// --- file library (docs/web.md § Files) ------------------------------------

export function getLibrary(): Promise<LibraryResponse> {
  return request('GET', '/api/library')
}

export function createFolder(body: {
  name: string
  parent_id: number | null
}): Promise<LibraryFolder> {
  return request('POST', '/api/library/folders', body)
}

/** Partial: rename, move, or both. A rename onto a taken name answers 409. */
export function updateFolder(
  id: number,
  body: Partial<{ name: string; parent_id: number | null }>,
): Promise<LibraryFolder> {
  return request('PUT', `/api/library/folders/${id}`, body)
}

/** Delete Immediately: a live folder or one in the Trash, for good, with what is inside it. */
export function deleteFolder(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/library/folders/${id}`)
}

/** Move to Trash, with everything inside it (docs/web.md § Files, *The Trash*). */
export function trashFolder(id: number): Promise<LibraryFolder> {
  return request('POST', `/api/library/folders/${id}/trash`)
}

/** Put Back: where it came from, or the nearest live folder above it; a taken name is suffixed. */
export function restoreFolder(id: number): Promise<LibraryFolder> {
  return request('POST', `/api/library/folders/${id}/restore`)
}

/** A copy of the folder and everything live under it, into `parentId` (null: the top level). */
export function copyFolder(id: number, parentId: number | null): Promise<LibraryFolder> {
  return request('POST', `/api/library/folders/${id}/copy`, { parent_id: parentId })
}

/**
 * The upload. The body is `FormData`, so `Content-Type` is left to the browser — it has to
 * carry the multipart boundary, and setting the header by hand drops it.
 *
 * Same origin, like every other call here: production routes `/api/*` to FastAPI at the
 * edge, so the bytes never enter the Nuxt server (docs/web.md § Rules).
 */
export function uploadDocument(file: File, folderId: number | null): Promise<LibraryDocument> {
  const body = new FormData()
  body.append('file', file)
  if (folderId !== null) {
    body.append('folder_id', String(folderId))
  }
  return request('POST', '/api/library/documents', body)
}

/** An image for a document's text, kept as the document's: the path its markdown line names
 *  (`figures/img-<hash>.png`). The line is the caller's to write (docs/api.md). */
export function uploadDocumentImage(documentId: number, file: File): Promise<{ path: string }> {
  const body = new FormData()
  body.append('document_id', String(documentId))
  body.append('file', file)
  return request('POST', '/api/library/images', body)
}

/** A new, empty markdown document; a taken name is suffixed (docs/web.md § Files). */
export function createDocument(body: { name: string; folder_id: number | null }): Promise<LibraryDocument> {
  return request('POST', '/api/library/documents/new', body)
}

/** File a parse that is already in the history — no upload, no second parse
 *  (docs/web.md § History). */
export function fileJob(jobId: string, folderId: number | null): Promise<LibraryDocument> {
  return request('POST', '/api/library/documents/from-job', {
    job_id: jobId,
    folder_id: folderId,
  })
}

export function updateDocument(
  id: number,
  body: Partial<{ name: string; folder_id: number | null }>,
): Promise<LibraryDocument> {
  return request('PUT', `/api/library/documents/${id}`, body)
}

/** Delete Immediately: a live file or one in the Trash, for good. The parse stays in History. */
export function deleteDocument(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/library/documents/${id}`)
}

export function trashDocument(id: number): Promise<LibraryDocument> {
  return request('POST', `/api/library/documents/${id}/trash`)
}

export function restoreDocument(id: number): Promise<LibraryDocument> {
  return request('POST', `/api/library/documents/${id}/restore`)
}

/** A copy with its own text, notes, original and images; the parse is shared, not repeated. */
export function copyDocument(id: number, folderId: number | null): Promise<LibraryDocument> {
  return request('POST', `/api/library/documents/${id}/copy`, { folder_id: folderId })
}

/** Empty Trash: every item in it, for good. */
export function emptyTrash(): Promise<void> {
  return request<undefined>('DELETE', '/api/library/trash')
}

export function getDocumentResult(id: number): Promise<JobResult> {
  return request('GET', `/api/library/documents/${id}/result`)
}

export function getDocumentPages(id: number): Promise<{ pages: DocumentPage[] }> {
  return request('GET', `/api/library/documents/${id}/pages`)
}

/** A failed parse back on the queue for the pages it is missing, on `model` — null runs
 *  them on the job's own (docs/jobs.md § Retrying failed pages). */
export function retryDocument(id: number, model: string | null): Promise<{ job_id: string; status: JobStatus }> {
  return request('POST', `/api/library/documents/${id}/retry`, { model })
}

/** A new parse of the document's original on `model` — null runs it on the parse agent's
 *  own. The document keeps its version until that parse succeeds (docs/jobs.md
 *  § Re-converting a document). */
export function reconvertDocument(id: number, model: string | null): Promise<{ job_id: string; status: JobStatus }> {
  return request('POST', `/api/library/documents/${id}/reconvert`, { model })
}

/** A failed re-convert let go: the document stops pointing at it; the parse stays in History. */
export function dismissReconvert(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/library/documents/${id}/reconvert`)
}

// --- workspace: content, notes, revisions (docs/web.md § Editing) -------------------

/** The editable copy, materialized on first read (docs/workspace.md). */
export function getDocumentContent(id: number): Promise<DocumentContent> {
  return request('GET', `/api/library/documents/${id}/content`)
}

/** A save carries the revision it was made from; a stale one answers 409. */
export function putDocumentContent(
  id: number,
  body: { markdown: string; base_revision: string },
): Promise<{ revision: string }> {
  return request('PUT', `/api/library/documents/${id}/content`, body)
}

export function putDocumentNotes(
  id: number,
  body: { text: string; base_revision: string },
): Promise<{ revision: string }> {
  return request('PUT', `/api/library/documents/${id}/notes`, body)
}

export function listRevisions(id: number): Promise<{ revisions: Revision[] }> {
  return request('GET', `/api/library/documents/${id}/revisions`)
}

/** Writes the files as they were before `commit`, as a new revision — undo is itself a
 *  revision, nothing is rewritten (docs/workspace.md § Revisions). */
export function revertRevision(id: number, commit: string): Promise<{ revision: string; commit: string }> {
  return request('POST', `/api/library/documents/${id}/revisions/${encodeURIComponent(commit)}/revert`)
}

// --- chat (docs/chat.md) ----------------------------------------------------------

export function listConversations(documentId: number): Promise<{ conversations: Conversation[] }> {
  return request('GET', `/api/library/documents/${documentId}/conversations`)
}

export function createConversation(documentId: number): Promise<Conversation> {
  return request('POST', `/api/library/documents/${documentId}/conversations`, {})
}

export function getConversation(id: number): Promise<ConversationResponse> {
  return request('GET', `/api/conversations/${id}`)
}

/** What one message carries besides its text (docs/chat.md § What a user message carries). */
export interface MessageBody {
  /** The reader's words; a command's arguments when `command` is set. */
  text: string
  selection?: string
  /** Whole sections the reader added by heading; `text` null for one past the selection
   *  cap, which the agent then reads from the document. */
  sections?: { heading: string; text: string | null }[]
  /** The passage of an earlier answer the reader replies to. */
  quote?: string
  /** A skill run as `/name`. */
  command?: string
  /** Ids from `POST /api/attachments`. */
  attachments?: string[]
  locale: string
}

/** Stores the user message and queues a turn: 202 with the turn's id. 429 when too many
 *  turns are already running, 400 when the chat agent has no model, 409 for an attachment
 *  that is gone. */
export function sendMessage(id: number, body: MessageBody): Promise<{ turn_id: string; message_id: number }> {
  return request('POST', `/api/conversations/${id}/messages`, body)
}

/** The bytes of the image attached to a stored user message, by its block's index. */
export function attachmentUrl(conversationId: number, messageId: number, index: number): string {
  return `/api/conversations/${conversationId}/messages/${messageId}/attachments/${index}`
}

/** An upload in flight: its result, and a way to stop it. */
export interface AttachmentUpload {
  promise: Promise<Attachment>
  abort: () => void
}

/**
 * One attachment, uploaded on its own so the composer can draw its progress — which
 * `fetch` cannot report for a request body, so this is the one call made with
 * `XMLHttpRequest`. Same origin, the session cookie and the CSRF header like every other
 * mutation; the bytes never enter the Nuxt server (docs/web.md § Rules).
 */
export function uploadAttachment(
  file: File,
  onProgress?: (loaded: number, total: number) => void,
): AttachmentUpload {
  const xhr = new XMLHttpRequest()
  const promise = new Promise<Attachment>((resolve, reject) => {
    xhr.open('POST', '/api/attachments')
    xhr.withCredentials = true
    xhr.setRequestHeader(CSRF_HEADER, 'fetch')
    xhr.responseType = 'text'
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(event.loaded, event.total)
      }
    }
    xhr.onload = () => {
      let payload: unknown
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null
      } catch {
        reject(new ApiRequestError(xhr.status, 'internal', xhr.statusText || 'Invalid response'))
        return
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as Attachment)
      } else {
        reject(toApiError(xhr.status, xhr.statusText, payload))
      }
    }
    xhr.onerror = () => reject(new ApiRequestError(0, 'internal', 'Network error'))
    xhr.onabort = () => reject(new ApiRequestError(0, 'internal', 'Aborted'))
    const body = new FormData()
    body.append('file', file)
    xhr.send(body)
  })
  return { promise, abort: () => xhr.abort() }
}

/** Drops an attachment not yet sent. */
export function deleteAttachment(id: string): Promise<void> {
  return request<undefined>('DELETE', `/api/attachments/${encodeURIComponent(id)}`)
}

/** The stream's URL — `EventSource` opens it, not `fetch` (docs/chat.md). */
export function conversationEventsUrl(id: number, after: number): string {
  return `/api/conversations/${id}/events?after=${after}`
}

export function cancelTurn(id: number): Promise<void> {
  return request<undefined>('POST', `/api/conversations/${id}/cancel`)
}

export function deleteConversation(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/conversations/${id}`)
}
