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
  | 'invalid_request'
  | 'auth'
  | 'rate_limit'
  | 'payment'
  | 'upstream'
  | 'internal'

/** `status` is 0 when the request never reached the server (offline, DNS, aborted). */
export class ApiRequestError extends Error {
  readonly status: number
  readonly type: ApiErrorType

  constructor(status: number, type: ApiErrorType, message: string) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.type = type
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
  /** The agent's upstream; null means Kano Proxy, the built-in one (docs/api.md § Upstreams). */
  connection_id: number | null
  /** Model id on that upstream's catalog; null means none chosen yet. */
  model: string | null
  /** Selected prompt preset; null means the agent's shipped default prompt. */
  prompt_preset_id: number | null
  /** Reasoning effort; meaningful for the `chat` agent, `medium` when unset. */
  effort: AgentEffort | null
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
  /** Declared, not enforced yet: the model picker lists the whole catalog. */
  capabilities: string[]
  input: string
  output: string
  /** What "default" means for this agent's prompt picker. */
  default_prompt: string
  settings: AgentSettings
  bbox: BboxConvention
}

/** What a settings write answers: the row as stored, and the convention it now implies. */
export type SavedAgentSettings = AgentSettings & { bbox: BboxConvention }

/** Kano Proxy as this user sees it; the key itself only ever comes back masked. */
export interface KanoState {
  /** False when this deployment has no Kano Proxy configured. */
  available: boolean
  connected: boolean
  masked: string | null
}

/** One row of a live model catalog (docs/api.md § Upstreams). */
export interface ConnectionModel {
  id: string
  name: string | null
}

export interface PromptPreset {
  id: number
  /** The agent this prompt is for; fixed once created. */
  agent: string
  name: string
  text: string
  created_at: string
  updated_at: string
}

/** The server's own limits, so the file picker and the prompt dialog enforce one set of numbers. */
export interface UploadLimits {
  upload_max_bytes: number
  page_cap: number
  accepted_media_types: string[]
  /** What intake accepts by extension when the browser reports a generic media type. */
  accepted_extensions: string[]
  /** The cap `POST /api/prompts` enforces on a preset's text. */
  prompt_max_chars: number
}

export interface MeResponse {
  user: User
  limits: UploadLimits
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

/**
 * One bucket of `GET /api/usage`. Billed, estimated and unpriced cost never share a field
 * (docs/api.md § Usage aggregates): `cost` is billing only, which today means the
 * historical OpenRouter rows — nothing new is ever billed.
 */
export interface UsageTotals {
  calls: number
  prompt_tokens: number
  completion_tokens: number
  cost: number
  /** What Kano Proxy and connections with `report_cost` reported — an estimate, not a bill. */
  estimated_cost: number
  estimated_calls: number
  /** Calls whose cost nobody reported; counted, never priced as 0. */
  unpriced_calls: number
  /** Prompt-cache reads and writes — chat rows only (docs/chat.md § Prompt caching). */
  cache_read_tokens: number
  cache_write_tokens: number
}

export interface UsageDay extends UsageTotals {
  date: string
  /** The agents that ran that day (`parse`, `chat`). */
  agents: string[]
}

export interface UsageModel extends UsageTotals {
  model: string
}

export interface UsageAgent extends UsageTotals {
  agent: string
}

export interface UsageResponse {
  days: number
  per_day: UsageDay[]
  per_model: UsageModel[]
  per_agent: UsageAgent[]
}

/** One conversation's totals over the period (`GET /api/usage/conversations`). */
export interface UsageConversation {
  id: number
  title: string | null
  document_id: number | null
  document_name: string | null
  turns: number
  prompt_tokens: number
  cache_read_tokens: number
  completion_tokens: number
  estimated_cost: number
  unpriced_calls: number
}

export interface UsageConversationsResponse {
  conversations: UsageConversation[]
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

/** Anthropic-shaped content blocks, stored verbatim (docs/chat.md § History). */
export type ContentBlock =
  | { type: 'text'; text: string }
  | { type: 'thinking'; thinking: string }
  | { type: 'tool_use'; id: string; name: string; input: Record<string, unknown> }
  | {
      type: 'tool_result'
      tool_use_id: string
      content: string | ContentBlock[]
      is_error?: boolean
    }
  /** Any other block the API stores (an image a tool returned, say): kept, not drawn. */
  | { type: 'image' | 'document'; source?: unknown }

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  content: ContentBlock[]
  turn_id: number
  created_at: string
}

export type TurnStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | string

export interface ChatTurn {
  id: number
  status: TurnStatus
  error: string | null
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
export type ChatEvent =
  | { event: 'turn_start'; model: string }
  | { event: 'text_delta'; text: string }
  | { event: 'thinking'; text: string }
  | { event: 'tool_use'; id: string; name: string; input: Record<string, unknown> }
  | { event: 'tool_result'; id: string; name: string; is_error: boolean; output: string }
  | {
      event: 'edit'
      path: string
      command: string
      commit: string
      diff: string
      tool_use_id: string
    }
  | { event: 'turn_done' }
  | { event: 'turn_error'; status: string; error: string }

export const CHAT_EVENT_NAMES: readonly ChatEvent['event'][] = [
  'turn_start',
  'text_delta',
  'thinking',
  'tool_use',
  'tool_result',
  'edit',
  'turn_done',
  'turn_error',
]

// --- sandbox (docs/sandbox.md) --------------------------------------------------

export type SandboxState = 'stopped' | 'starting' | 'running' | 'unavailable'

export interface SandboxStatus {
  state: SandboxState
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
}

export interface ResultPage {
  page: number
  /** Absent on a partial result's pages — only the final outcome knows the dimensions. */
  width_pt?: number
  height_pt?: number
  method: 'vision' | null
  error: string | null
}

export interface ResultFigure {
  id: string
  page: number
  bbox: [number, number, number, number]
  caption: string | null
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
 * so a row renders — name, status, progress — from a single `GET /api/library`.
 */
export interface LibraryDocument {
  id: number
  folder_id: number | null
  name: string
  job_id: string
  status: JobStatus
  kind: string
  model: string
  page_count: number | null
  pages_done: number
  size_bytes: number
  error: string | null
  created_at: string
  finished_at: string | null
}

export interface LibraryResponse {
  folders: LibraryFolder[]
  documents: LibraryDocument[]
}

// --- transport -------------------------------------------------------------

const CSRF_HEADER = 'X-Requested-With'

function toApiError(status: number, statusText: string, payload: unknown): ApiRequestError {
  const envelope =
    payload && typeof payload === 'object' && 'error' in payload
      ? (payload as { error?: { type?: ApiErrorType; message?: string } }).error
      : undefined
  return new ApiRequestError(
    status,
    envelope?.type ?? 'internal',
    envelope?.message ?? statusText ?? 'Request failed',
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

// --- Kano Proxy (docs/auth.md § 6) --------------------------------------------

export function getKano(): Promise<KanoState> {
  return request('GET', '/api/kano')
}

/** Forgets the key here only; it stays listed on Kano Proxy's Access page. */
export function deleteKano(): Promise<void> {
  return request<undefined>('DELETE', '/api/kano')
}

/**
 * Where Connect / Reconnect send the browser: a full-page navigation into the server's
 * redirect flow, like sign-in — never a fetch. `locale` brings the user back to the same
 * language.
 */
export function kanoConnectUrl(locale: string): string {
  return locale ? `/api/kano/connect?locale=${encodeURIComponent(locale)}` : '/api/kano/connect'
}

// --- agents (docs/agents.md) ---------------------------------------------------

export function listAgents(): Promise<{ agents: AgentEntry[] }> {
  return request('GET', '/api/agents')
}

/**
 * Partial update: only the fields present change. Changing `connection_id` without
 * `model` clears the model on the server — it belonged to the other upstream's catalog.
 */
export function putAgentSettings(
  agent: string,
  settings: Partial<AgentSettings>,
): Promise<SavedAgentSettings> {
  return request('PUT', `/api/agents/${encodeURIComponent(agent)}/settings`, settings)
}

/** The live catalog of the agent's saved upstream — what its model picker lists. */
export function listAgentModels(agent: string): Promise<{ data: ConnectionModel[] }> {
  return request('GET', `/api/agents/${encodeURIComponent(agent)}/models`)
}

// --- prompt presets ---------------------------------------------------------

export function listPrompts(): Promise<{ prompts: PromptPreset[] }> {
  return request('GET', '/api/prompts')
}

export function createPrompt(body: {
  agent: string
  name: string
  text: string
}): Promise<PromptPreset> {
  return request('POST', '/api/prompts', body)
}

export function updatePrompt(
  id: number,
  body: Partial<{ name: string; text: string }>,
): Promise<PromptPreset> {
  return request('PUT', `/api/prompts/${id}`, body)
}

export function deletePrompt(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/prompts/${id}`)
}

export function getUsage(days: number): Promise<UsageResponse> {
  return request('GET', `/api/usage?days=${days}`)
}

export function getUsageConversations(days: number): Promise<UsageConversationsResponse> {
  return request('GET', `/api/usage/conversations?days=${days}`)
}

// --- sandbox (docs/sandbox.md) --------------------------------------------------

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

export function deleteFolder(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/library/folders/${id}`)
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

export function deleteDocument(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/library/documents/${id}`)
}

export function getDocumentResult(id: number): Promise<JobResult> {
  return request('GET', `/api/library/documents/${id}/result`)
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
export function revertRevision(id: number, commit: string): Promise<{ revision: string }> {
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

/** Stores the user message and queues a turn: 202 with the turn's id. 429 when too many
 *  turns are already running, 400 when the chat agent has no model. */
export function sendMessage(
  id: number,
  body: { text: string; selection?: string },
): Promise<{ turn_id: number }> {
  return request('POST', `/api/conversations/${id}/messages`, body)
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
