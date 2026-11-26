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
  pickLimits,
  pixelMatches,
  skipped,
  type ProbeCheck,
  type ProbeCheckId,
  type ProbeReport,
} from './probe'
import { PROBE_CANVAS_SIZE, clearTo, errorText, type ProbeWorkerReply, type ProbeWorkerRequest } from './probe-draw'

/** How long the worker and the lost-device promise may take before the check gives up. */
const STEP_TIMEOUT_MS = 8000

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

function nextFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()))
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
): Promise<{ drawn: number; afterPresent: boolean; error?: string }> {
  const colors = canvases.map((_, index) => PROBE_COLORS[(index + offset) % PROBE_COLORS.length]!)
  const matched = canvases.map(() => false)
  let error: string | undefined
  for (const [index, canvas] of canvases.entries()) {
    try {
      canvas.width = PROBE_CANVAS_SIZE
      canvas.height = PROBE_CANVAS_SIZE
      const context = canvas.getContext('webgpu')
      if (!context) {
        error ??= 'canvas has no webgpu context'
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
    await nextFrame()
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
  return { drawn: matched.filter(Boolean).length, afterPresent, error }
}

function canvasesCheck(
  id: ProbeCheckId,
  result: { drawn: number; afterPresent: boolean; error?: string },
  total: number,
  extra: { ms?: number; prefix?: string } = {},
): ProbeCheck {
  const parts = [`${result.drawn} of ${total} drawn`]
  if (extra.prefix) {
    parts.unshift(extra.prefix)
  }
  if (result.afterPresent) {
    parts.push('read after present')
  }
  if (result.error) {
    parts.push(result.error)
  }
  // No canvas to draw on is a failed check, not five of five.
  const status = total > 0 && result.drawn === total ? 'pass' : 'fail'
  return { id, status, ms: extra.ms, detail: parts.join('; ') }
}

/** Whether a worker can draw with WebGPU into an `OffscreenCanvas` and hand the frame back. */
function workerCheck(): Promise<ProbeCheck> {
  return new Promise((resolve) => {
    const started = performance.now()
    let worker: Worker
    try {
      worker = new Worker(new URL('./probe.worker.ts', import.meta.url), { type: 'module' })
    } catch (error) {
      resolve({ id: 'worker', status: 'fail', detail: errorText(error) })
      return
    }
    const finish = (check: ProbeCheck) => {
      clearTimeout(timer)
      worker.terminate()
      resolve(check)
    }
    const timer = setTimeout(() => finish({ id: 'worker', status: 'fail', detail: 'timed out' }), STEP_TIMEOUT_MS)
    worker.onerror = (event) => finish({ id: 'worker', status: 'fail', detail: event.message || 'worker failed to start' })
    worker.onmessage = (event: MessageEvent<ProbeWorkerReply>) => {
      const { bitmap, error } = event.data
      if (!bitmap) {
        finish({ id: 'worker', status: 'fail', detail: error ?? 'no frame' })
        return
      }
      try {
        const drawn = pixelMatches(centerPixel(bitmap), PROBE_COLORS[0]!)
        finish({
          id: 'worker',
          status: drawn ? 'pass' : 'fail',
          ms: performance.now() - started,
          detail: drawn ? undefined : 'the frame is not the color drawn',
        })
      } catch (caught) {
        finish({ id: 'worker', status: 'fail', detail: errorText(caught) })
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
    return { check: { id: 'deviceLoss', status: 'fail', detail: 'device.lost never resolved' }, device: null }
  }
  const started = performance.now()
  const adapter = await gpu.requestAdapter()
  if (!adapter) {
    return {
      check: { id: 'deviceLoss', status: 'fail', detail: `lost: ${reason}; requestAdapter() returned null` },
      device: null,
    }
  }
  const next = await adapter.requestDevice()
  const result = await drawCanvases(next, format, canvases, 1)
  return {
    check: canvasesCheck('deviceLoss', result, canvases.length, {
      ms: performance.now() - started,
      prefix: `lost: ${reason}`,
    }),
    device: next,
  }
}

/** Runs every check on `canvases`, which the page shows so the colors can be seen as well as read. */
export async function runProbe(canvases: readonly HTMLCanvasElement[]): Promise<ProbeRun> {
  const done = new Map<ProbeCheckId, ProbeCheck>()
  const record = (check: ProbeCheck) => done.set(check.id, check)
  const report: ProbeReport = {
    userAgent: navigator.userAgent,
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

  // Absent on a browser without WebGPU, whatever the types say.
  const gpu = navigator.gpu as GPU | undefined
  if (!gpu) {
    record({ id: 'gpu', status: 'fail', detail: 'navigator.gpu is missing' })
    return finish()
  }
  record({ id: 'gpu', status: 'pass' })
  const format = gpu.getPreferredCanvasFormat()
  report.canvasFormat = format

  // The worker asks for an adapter of its own, so it is checked whatever the page's answers.
  const worker = workerCheck()

  let started = performance.now()
  let adapter: GPUAdapter | null = null
  let failure = 'requestAdapter() returned null'
  try {
    adapter = await gpu.requestAdapter()
  } catch (error) {
    failure = errorText(error)
  }
  if (!adapter) {
    record({ id: 'adapter', status: 'fail', detail: failure })
    record(await worker)
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
    record(await worker)
    return finish()
  }
  live = device
  record({ id: 'device', status: 'pass', ms: performance.now() - started })

  record(canvasesCheck('canvases', await drawCanvases(device, format, canvases, 0), canvases.length))
  record(await worker)

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
