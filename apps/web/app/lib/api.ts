/**
 * The single typed client for the backend (docs/project-structure.md § Boundaries).
 *
 * Every URL is relative: production routes `/api/*` and `/v1/*` to FastAPI through Caddy,
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

export interface UserSettings {
  default_model: string | null
  default_profile: string | null
  /** Active upstream; null means the built-in OpenRouter (docs/api.md § Upstreams). */
  default_connection_id: number | null
  /** Selected prompt preset; null means the shipped default prompt. */
  prompt_preset_id: number | null
}

/**
 * A user-defined OpenAI-compatible endpoint; the API key only ever returns masked.
 * A connection is a complete profile — endpoint, key and model travel together; `model`
 * is null only on rows created before the column existed (docs/api.md § Upstreams).
 */
export interface ProviderConnection {
  id: number
  name: string
  base_url: string
  masked: string
  model: string | null
  created_at: string
  updated_at: string
}

export interface ConnectionModel {
  id: string
  name: string | null
}

export interface PromptPreset {
  id: number
  name: string
  text: string
  created_at: string
  updated_at: string
}

export interface OpenRouterKeyState {
  present: boolean
  masked: string | null
  updated_at: string | null
}

/** The server's own upload limits, so the file picker enforces one set of numbers. */
export interface UploadLimits {
  upload_max_bytes: number
  page_cap: number
  accepted_media_types: string[]
  /** What intake accepts by extension when the browser reports a generic media type. */
  accepted_extensions: string[]
}

export interface MeResponse {
  user: User
  settings: UserSettings
  /** What "default" means right now — the shipped prompt the backend runs without a custom one. */
  defaults: { system_prompt: string }
  openrouter_key: OpenRouterKeyState
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

export interface UsageTotals {
  prompt_tokens: number
  completion_tokens: number
  cost: number
}

export interface UsageDay extends UsageTotals {
  date: string
}

export interface UsageModel extends UsageTotals {
  model: string
}

export interface UsageResponse {
  days: number
  per_day: UsageDay[]
  per_model: UsageModel[]
}

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed'

export interface JobSummary {
  job_id: string
  status: JobStatus
  filename: string
  kind: string
  model: string
  profile: string | null
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
  model: string
  profile: string | null
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

export interface ModelEntry {
  id: string
  name: string | null
  context_length: number | null
  pricing: Record<string, string> | null
  /** True when a preset profile currently resolves to this model. */
  recommended: boolean
}

export interface ProfileEntry {
  id: string
  name: string
  description: string
  model: string | null
  bbox_format: string
  profile_version: number
  available: boolean
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

export function getOpenRouterKey(): Promise<OpenRouterKeyState> {
  return request('GET', '/api/openrouter-key')
}

export function putOpenRouterKey(key: string): Promise<OpenRouterKeyState> {
  return request('PUT', '/api/openrouter-key', { key })
}

export function deleteOpenRouterKey(): Promise<void> {
  return request<undefined>('DELETE', '/api/openrouter-key')
}

/** Partial update: only the fields present in the body change (docs/api.md). */
export function putSettings(settings: Partial<UserSettings>): Promise<UserSettings> {
  return request('PUT', '/api/settings', settings)
}

// --- provider connections ---------------------------------------------------

export function listConnections(): Promise<{ connections: ProviderConnection[] }> {
  return request('GET', '/api/connections')
}

export function createConnection(body: {
  name: string
  base_url: string
  api_key: string
  model: string
}): Promise<ProviderConnection> {
  return request('POST', '/api/connections', body)
}

export function updateConnection(
  id: number,
  body: Partial<{ name: string; base_url: string; api_key: string; model: string }>,
): Promise<ProviderConnection> {
  return request('PUT', `/api/connections/${id}`, body)
}

export function deleteConnection(id: number): Promise<void> {
  return request<undefined>('DELETE', `/api/connections/${id}`)
}

/**
 * The live model catalog the connection dialog picks from — with a candidate key before
 * the connection exists, or with the stored key (`connection_id`) while editing one.
 */
export function previewConnectionModels(body: {
  base_url: string
  api_key?: string
  connection_id?: number
}): Promise<{ data: ConnectionModel[] }> {
  return request('POST', '/api/connections/preview-models', body)
}

// --- prompt presets ---------------------------------------------------------

export function listPrompts(): Promise<{ prompts: PromptPreset[] }> {
  return request('GET', '/api/prompts')
}

export function createPrompt(body: { name: string; text: string }): Promise<PromptPreset> {
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

// --- data plane reads the web app is allowed to make (docs/web.md) ---------

export function listModels(): Promise<{ data: ModelEntry[] }> {
  return request('GET', '/v1/models')
}

export function listProfiles(): Promise<{ data: ProfileEntry[] }> {
  return request('GET', '/v1/profiles')
}
