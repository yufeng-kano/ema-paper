/// <reference types="@webgpu/types" />

/**
 * The WebGPU device check's browser half (docs/pdf-viewer.md § The device check). It answers,
 * on the device it runs on, the questions the page renderer's design rests on:
 *
 * - is there WebGPU at all, and how long do an adapter and a device take;
 * - can several canvases draw from one device at once (one canvas per visible page);
 * - can a worker draw into an `OffscreenCanvas`;
 * - after the device is lost, does a new one draw again without a reload.
 *
 * Nothing here can run in node, so nothing here is unit tested; what can be is in `probe.ts`.
 */
import {
  PROBE_CHECKS,
  PROBE_COLORS,
  errorText,
  pickLimits,
  pixelMatches,
  skipped,
  type ProbeCheck,
  type ProbeCheckId,
  type ProbeNote,
  type ProbeReport,
} from './probe'
import { PROBE_CANVAS_SIZE, clearTo, type ProbeWorkerReply, type ProbeWorkerRequest } from './probe-draw'

/** How long the worker and the lost-device promise may take before the check gives up. */
const STEP_TIMEOUT_MS = 8000

/** How long to wait for a frame to be shown: a hidden tab runs no frames at all. */
const PRESENT_TIMEOUT_MS = 1000

export interface ProbeRun {
  report: ProbeReport
  /** Releases the device the run left drawing the canvases. */
  dispose: () => void
}

/** The pixel at the middle of a canvas or a bitmap, read through a 2D canvas. */
function centerPixel(source: HTMLCanvasElement | ImageBitmap): Uint8ClampedArray {
  const scratch = document.createElement('canvas')
  scratch.width = PROBE_CANVAS_SIZE
  scratch.height = PROBE_CANVAS_SIZE
  const context = scratch.getContext('2d', { willReadFrequently: true })
  if (!context) {
    throw new Error('no 2d context')
  }
  context.drawImage(source, 0, 0)
  return context.getImageData(PROBE_CANVAS_SIZE / 2, PROBE_CANVAS_SIZE / 2, 1, 1).data
}

/**
 * Resolves once the frame drawn in this task has been shown: a frame callback runs before its
 * frame is presented, so it takes the callback of the frame after. A hidden tab resolves by
 * the timer instead.
 */
function presented(): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, PRESENT_TIMEOUT_MS)
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        clearTimeout(timer)
        resolve()
      }),
    )
  })
}

interface Drawing {
  drawn: number
  /** At least one canvas read as drawn only once its frame had been shown. */
  afterPresent: boolean
  /** At least one canvas gave no WebGPU context. */
  noContext: boolean
  error?: string
}

/**
 * Draws one color per canvas from `device`, starting at `offset` in the palette, and counts
 * the canvases whose middle pixel reads back as drawn.
 *
 * The read is made twice when it has to be: in the task that drew, where the canvas still
 * holds its current texture, and after the frame is presented. A browser may answer either.
 */
async function drawCanvases(
  device: GPUDevice,
  format: GPUTextureFormat,
  canvases: readonly HTMLCanvasElement[],
  offset: number,
): Promise<Drawing> {
  const colors = canvases.map((_, index) => PROBE_COLORS[(index + offset) % PROBE_COLORS.length]!)
  const matched = canvases.map(() => false)
  let noContext = false
  let error: string | undefined
  for (const [index, canvas] of canvases.entries()) {
    try {
      canvas.width = PROBE_CANVAS_SIZE
      canvas.height = PROBE_CANVAS_SIZE
      const context = canvas.getContext('webgpu')
      if (!context) {
        noContext = true
        continue
      }
      context.configure({ device, format, alphaMode: 'opaque' })
      clearTo(device, context, colors[index]!)
      matched[index] = pixelMatches(centerPixel(canvas), colors[index]!)
    } catch (caught) {
      error ??= errorText(caught)
    }
  }
  let afterPresent = false
  if (matched.includes(false)) {
    await presented()
    for (const [index, canvas] of canvases.entries()) {
      if (matched[index]) {
        continue
      }
      try {
        if (pixelMatches(centerPixel(canvas), colors[index]!)) {
          matched[index] = true
          afterPresent = true
        }
      } catch (caught) {
        error ??= errorText(caught)
      }
    }
  }
  return { drawn: matched.filter(Boolean).length, afterPresent, noContext, error }
}

/** A drawing as a check: passed when every canvas drew, and there was a canvas to draw on. */
function drawingCheck(id: ProbeCheckId, drawing: Drawing, total: number, lead: ProbeNote[] = [], ms?: number): ProbeCheck {
  const notes: ProbeNote[] = [...lead, { code: 'drawn', params: { count: drawing.drawn, total } }]
  if (drawing.afterPresent) {
    notes.push({ code: 'afterPresent' })
  }
  if (drawing.noContext) {
    notes.push({ code: 'noContext' })
  }
  return { id, status: total > 0 && drawing.drawn === total ? 'pass' : 'fail', ms, notes, detail: drawing.error }
}

/**
 * Whether a worker can draw with WebGPU into an `OffscreenCanvas` and hand the frame back.
 * The time is the worker's own, from asking for an adapter to the frame drawn.
 */
function workerCheck(): Promise<ProbeCheck> {
  return new Promise((resolve) => {
    const fail = (rest: Pick<ProbeCheck, 'notes' | 'detail'>): ProbeCheck => ({ id: 'worker', status: 'fail', ...rest })
    let worker: Worker
    try {
      worker = new Worker(new URL('./probe.worker.ts', import.meta.url), { type: 'module' })
    } catch (error) {
      resolve(fail({ detail: errorText(error) }))
      return
    }
    const finish = (check: ProbeCheck) => {
      clearTimeout(timer)
      worker.terminate()
      resolve(check)
    }
    const timer = setTimeout(() => finish(fail({ notes: [{ code: 'timedOut' }] })), STEP_TIMEOUT_MS)
    worker.onerror = (event) => finish(fail({ notes: [{ code: 'noFrame' }], detail: event.message || undefined }))
    worker.onmessage = (event: MessageEvent<ProbeWorkerReply>) => {
      const { bitmap, ms, note, error } = event.data
      if (!bitmap) {
        finish(fail({ notes: [{ code: note ?? 'noFrame' }], detail: error }))
        return
      }
      try {
        finish(
          pixelMatches(centerPixel(bitmap), PROBE_COLORS[0]!)
            ? { id: 'worker', status: 'pass', ms }
            : fail({ notes: [{ code: 'wrongColor' }] }),
        )
      } catch (caught) {
        finish(fail({ detail: errorText(caught) }))
      } finally {
        bitmap.close()
      }
    }
    const request: ProbeWorkerRequest = { color: PROBE_COLORS[0]! }
    worker.postMessage(request)
  })
}

/**
 * Destroys `device`, waits for the browser to report it lost, then asks for a new adapter and
 * device and redraws every canvas in the next color of the palette. The time is the recovery
 * alone: from the loss to the last canvas drawn.
 *
 * A destroyed device is the one loss a page can cause itself. A loss the GPU process causes
 * takes the same path from `device.lost` on, which is the part being checked.
 */
async function deviceLossCheck(
  gpu: GPU,
  device: GPUDevice,
  format: GPUTextureFormat,
  canvases: readonly HTMLCanvasElement[],
): Promise<{ check: ProbeCheck; device: GPUDevice | null }> {
  device.destroy()
  const reason = await Promise.race([
    device.lost.then((info) => info.reason ?? 'unknown'),
    new Promise<null>((resolve) => setTimeout(() => resolve(null), STEP_TIMEOUT_MS)),
  ])
  if (reason === null) {
    return { check: { id: 'deviceLoss', status: 'fail', notes: [{ code: 'lostUnreported' }] }, device: null }
  }
  const lost: ProbeNote = { code: 'lost', params: { reason } }
  const started = performance.now()
  const adapter = await gpu.requestAdapter()
  if (!adapter) {
    return { check: { id: 'deviceLoss', status: 'fail', notes: [lost, { code: 'noAdapter' }] }, device: null }
  }
  const next = await adapter.requestDevice()
  const drawing = await drawCanvases(next, format, canvases, 1)
  return {
    check: drawingCheck('deviceLoss', drawing, canvases.length, [lost], performance.now() - started),
    device: next,
  }
}

/**
 * Runs every check on `canvases`, which the page shows so the colors can be seen as well as
 * read. One check at a time, so that no step's time includes another's work.
 */
export async function runProbe(canvases: readonly HTMLCanvasElement[]): Promise<ProbeRun> {
  const done = new Map<ProbeCheckId, ProbeCheck>()
  const record = (check: ProbeCheck) => done.set(check.id, check)
  const report: ProbeReport = {
    userAgent: navigator.userAgent,
    secureContext: window.isSecureContext,
    devicePixelRatio: window.devicePixelRatio,
    hardwareConcurrency: navigator.hardwareConcurrency ?? 0,
    features: [],
    limits: {},
    checks: [],
  }
  let live: GPUDevice | null = null
  const finish = (): ProbeRun => {
    report.checks = PROBE_CHECKS.map((id) => done.get(id) ?? skipped(id))
    return { report, dispose: () => live?.destroy() }
  }

  record({ id: 'float16', status: 'Float16Array' in globalThis ? 'pass' : 'fail' })

  // Absent on a browser without WebGPU, and on any page that is not a secure context,
  // whatever the types say.
  const gpu = navigator.gpu as GPU | undefined
  if (!gpu) {
    record({ id: 'gpu', status: 'fail', notes: [{ code: window.isSecureContext ? 'noGpu' : 'insecureContext' }] })
    return finish()
  }
  record({ id: 'gpu', status: 'pass' })
  const format = gpu.getPreferredCanvasFormat()
  report.canvasFormat = format

  let started = performance.now()
  let adapter: GPUAdapter | null = null
  let failure: string | undefined
  try {
    adapter = await gpu.requestAdapter()
  } catch (error) {
    failure = errorText(error)
  }
  if (!adapter) {
    record({ id: 'adapter', status: 'fail', notes: failure ? undefined : [{ code: 'noAdapter' }], detail: failure })
    // The worker asks for an adapter of its own, so it is checked whatever the page's answer.
    record(await workerCheck())
    return finish()
  }
  record({ id: 'adapter', status: 'pass', ms: performance.now() - started })
  // `info` is missing on a browser that still has only `requestAdapterInfo()`.
  const info = adapter.info as GPUAdapterInfo | undefined
  if (info) {
    report.adapter = {
      vendor: info.vendor,
      architecture: info.architecture,
      device: info.device,
      description: info.description,
    }
  }
  report.features = [...adapter.features].sort()
  report.limits = pickLimits(adapter.limits)

  started = performance.now()
  let device: GPUDevice
  try {
    device = await adapter.requestDevice()
  } catch (error) {
    record({ id: 'device', status: 'fail', detail: errorText(error) })
    record(await workerCheck())
    return finish()
  }
  live = device
  record({ id: 'device', status: 'pass', ms: performance.now() - started })

  record(drawingCheck('canvases', await drawCanvases(device, format, canvases, 0), canvases.length))
  record(await workerCheck())

  try {
    const recovery = await deviceLossCheck(gpu, device, format, canvases)
    live = recovery.device
    record(recovery.check)
  } catch (error) {
    live = null
    record({ id: 'deviceLoss', status: 'fail', detail: errorText(error) })
  }
  return finish()
}
