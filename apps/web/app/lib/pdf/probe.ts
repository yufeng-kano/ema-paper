/**
 * The WebGPU device check's framework-free half (docs/pdf-viewer.md § The device check): what
 * a run records, which adapter limits it keeps, and the plain-text form the page copies.
 *
 * The checks themselves need a browser and a GPU and live in `probe-gpu.ts`; everything here
 * runs in node, so vitest covers it (docs/testing.md § Frontend).
 */

/** The checks, in the order they run and are listed. */
export const PROBE_CHECKS = ['gpu', 'adapter', 'device', 'float16', 'canvases', 'worker', 'deviceLoss'] as const

export type ProbeCheckId = (typeof PROBE_CHECKS)[number]

/** `skipped` is a check that could not run because an earlier one failed. */
export type ProbeStatus = 'pass' | 'fail' | 'skipped'

/**
 * What a check has to say beside its status. A code, not a sentence: the page says it in the
 * reader's language (`pdfProbe.notes.<code>`) and the copied report prints the code itself.
 */
export const PROBE_NOTES = [
  'drawn',
  'afterPresent',
  'timedOut',
  'wrongColor',
  'noFrame',
  'noGpu',
  'insecureContext',
  'noAdapter',
  'noContext',
  'noOffscreenCanvas',
  'lost',
  'lostUnreported',
] as const

export type ProbeNoteCode = (typeof PROBE_NOTES)[number]

export interface ProbeNote {
  code: ProbeNoteCode
  /** The note's numbers and names: `drawn` has `count` and `total`, `lost` the browser's `reason`. */
  params?: Record<string, string | number>
}

export interface ProbeCheck {
  id: ProbeCheckId
  status: ProbeStatus
  /** How long the check took, where a duration says something (adapter, device, worker, recovery). */
  ms?: number
  notes?: ProbeNote[]
  /** The browser's own words, an error's message. Data, never translated. */
  detail?: string
}

export interface ProbeAdapter {
  vendor: string
  architecture: string
  device: string
  description: string
}

export interface ProbeReport {
  userAgent: string
  /** `navigator.gpu` exists in a secure context only, so a plain-HTTP page says nothing of the device. */
  secureContext: boolean
  devicePixelRatio: number
  hardwareConcurrency: number
  /** `navigator.gpu.getPreferredCanvasFormat()`, absent without WebGPU. */
  canvasFormat?: string
  adapter?: ProbeAdapter
  /** The adapter's features, sorted. */
  features: string[]
  /** The adapter limits in `PROBE_LIMITS` that the browser reported. */
  limits: Record<string, number>
  checks: ProbeCheck[]
}

/**
 * The adapter limits a page renderer is sized by: the largest texture (a page or a tile), how
 * many layer textures one pass may bind and write, and the buffer sizes its geometry must fit.
 */
export const PROBE_LIMITS = [
  'maxTextureDimension2D',
  'maxTextureArrayLayers',
  'maxBindGroups',
  'maxSampledTexturesPerShaderStage',
  'maxColorAttachments',
  'maxBufferSize',
  'maxStorageBufferBindingSize',
  'maxUniformBufferBindingSize',
  'maxVertexBuffers',
] as const

/** The limits of `PROBE_LIMITS` that `limits` holds as numbers; a missing one is left out. */
export function pickLimits(limits: object): Record<string, number> {
  const source = limits as Record<string, unknown>
  const picked: Record<string, number> = {}
  for (const name of PROBE_LIMITS) {
    const value = source[name]
    if (typeof value === 'number' && Number.isFinite(value)) {
      picked[name] = value
    }
  }
  return picked
}

/** The five clear colors the canvases check draws, as RGBA bytes: all different, none black. */
export const PROBE_COLORS: readonly (readonly [number, number, number, number])[] = [
  [255, 59, 48, 255],
  [52, 199, 89, 255],
  [0, 122, 255, 255],
  [255, 204, 0, 255],
  [175, 82, 222, 255],
]

/** Whether a pixel read back is the color drawn, within `tolerance` per channel. */
export function pixelMatches(actual: ArrayLike<number>, expected: ArrayLike<number>, tolerance = 2): boolean {
  if (actual.length < 4 || expected.length < 4) {
    return false
  }
  for (let channel = 0; channel < 4; channel += 1) {
    if (Math.abs(actual[channel]! - expected[channel]!) > tolerance) {
      return false
    }
  }
  return true
}

/** A thrown value's message; the browser's own words go into the report as they are. */
export function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** A check that every later step of a failed run is recorded as. */
export function skipped(id: ProbeCheckId): ProbeCheck {
  return { id, status: 'skipped' }
}

/**
 * The report as plain text, for pasting into a message: one `key: value` line per fact, the
 * checks first, a note as its code and what it carries (`drawn(count=3, total=5)`). English and
 * stable on purpose — it is read by whoever debugs the renderer, and the page's own labels are
 * the translated ones.
 */
export function reportText(report: ProbeReport): string {
  const lines: string[] = []
  for (const check of report.checks) {
    const parts: string[] = [check.status]
    if (check.ms !== undefined) {
      parts.push(`${Math.round(check.ms)} ms`)
    }
    for (const { code, params } of check.notes ?? []) {
      const carried = Object.entries(params ?? {}).map(([name, value]) => `${name}=${value}`)
      parts.push(carried.length > 0 ? `${code}(${carried.join(', ')})` : code)
    }
    if (check.detail) {
      parts.push(check.detail)
    }
    lines.push(`${check.id}: ${parts.join(', ')}`)
  }
  lines.push(`userAgent: ${report.userAgent}`)
  lines.push(`secureContext: ${report.secureContext}`)
  lines.push(`devicePixelRatio: ${report.devicePixelRatio}`)
  lines.push(`hardwareConcurrency: ${report.hardwareConcurrency}`)
  if (report.canvasFormat) {
    lines.push(`canvasFormat: ${report.canvasFormat}`)
  }
  if (report.adapter) {
    const { vendor, architecture, device, description } = report.adapter
    lines.push(`adapterInfo: ${[vendor, architecture, device, description].filter(Boolean).join(' / ') || 'unnamed'}`)
  }
  for (const [name, value] of Object.entries(report.limits)) {
    lines.push(`${name}: ${value}`)
  }
  if (report.features.length > 0) {
    lines.push(`features: ${report.features.join(' ')}`)
  }
  return lines.join('\n')
}
