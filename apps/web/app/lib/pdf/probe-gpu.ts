/// <reference types="@webgpu/types" />

/**
 * The WebGPU device check's browser half (docs/pdf-viewer.md § The device check). It answers,
 * on the device it runs on, the questions the page renderer's design rests on:
 *
 * - is there WebGPU at all, and how long do an adapter and a device take;
 * - does the GPU draw what it is told, read back from the GPU itself;
 * - can several canvases draw from one device at once (one canvas per visible page);
 * - can a canvas be read through a 2D canvas, and what is left of it afterwards;
 * - what a worker can do: draw, hand a frame over, draw a canvas of the page;
 * - after a device is lost, does a new one draw again without a reload.
 *
 * What a canvas shows on the screen no check can read. So each way of drawing has canvases of
 * its own (`PROBE_SURFACES`), no way touches another's, and a screenshot says the rest.
 *
 * Nothing here can run in node, so nothing here is unit tested; what can be is in `probe.ts`.
 */
import {
  PROBE_CHECKS,
  PROBE_COLORS,
  PROBE_SURFACES,
  errorText,
  pickLimits,
  pixelMatches,
  readVerdict,
  rgbaText,
  skipped,
  type ProbeCheck,
  type ProbeCheckId,
  type ProbeNote,
  type ProbeReport,
  type ProbeSurface,
} from './probe'
import {
  PROBE_CANVAS_SIZE,
  clearTo,
  configure,
  drawAndRead,
  plainTexture,
  within,
  type ProbeWorkerReply,
  type ProbeWorkerRequest,
} from './probe-draw'

/** How long to wait for a frame to be shown: a hidden tab runs no frames at all. */
const PRESENT_TIMEOUT_MS = 1000

/** The page's canvases: the five drawn once, and one for each other way of drawing. */
export type ProbeCanvases = { once: readonly HTMLCanvasElement[] } & Record<ProbeSurface, HTMLCanvasElement>

export interface ProbeRun {
  report: ProbeReport
  /** Stops the canvas that is redrawn every frame, ends the worker and releases the devices. */
  dispose: () => void
}

/** The color a one-canvas surface is drawn in: `PROBE_COLORS` at its place in `PROBE_SURFACES`. */
function surfaceColor(surface: ProbeSurface): readonly number[] {
  return PROBE_COLORS[PROBE_SURFACES.indexOf(surface)]!
}

/** The pixel at the middle of a canvas or a bitmap, read through a 2D canvas. */
function centerPixel(source: HTMLCanvasElement | ImageBitmap): number[] {
  const scratch = document.createElement('canvas')
  scratch.width = PROBE_CANVAS_SIZE
  scratch.height = PROBE_CANVAS_SIZE
  const context = scratch.getContext('2d', { willReadFrequently: true })
  if (!context) {
    throw new Error('no 2d context')
  }
  context.drawImage(source, 0, 0)
  return Array.from(context.getImageData(PROBE_CANVAS_SIZE / 2, PROBE_CANVAS_SIZE / 2, 1, 1).data)
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

/** A new adapter and a device from it: an adapter gives one device only. */
async function newDevice(gpu: GPU): Promise<GPUDevice | null> {
  const adapter = await gpu.requestAdapter()
  return adapter ? adapter.requestDevice() : null
}

/**
 * Five canvases drawn once from one device, all in one task as a viewer draws its visible
 * pages, each read back from the GPU. No 2D canvas ever reads them.
 */
async function canvasesCheck(device: GPUDevice, format: GPUTextureFormat, canvases: readonly HTMLCanvasElement[]): Promise<ProbeCheck> {
  const reads = await Promise.all(
    canvases.map(async (canvas, index) => {
      const context = configure(canvas, device, format)
      return context
        ? readVerdict(await drawAndRead(device, context.getCurrentTexture(), PROBE_COLORS[index]!), PROBE_COLORS[index]!)
        : readVerdict({ note: 'noContext' }, PROBE_COLORS[index]!)
    }),
  )
  const failed = reads.find((read) => read.status === 'fail')
  const drawn = reads.length - reads.filter((read) => read.status === 'fail').length
  return {
    id: 'canvases',
    // No canvas to draw on is a failed check, not five of five.
    status: reads.length > 0 && !failed ? 'pass' : 'fail',
    notes: [{ code: 'drawn', params: { count: drawn, total: reads.length } }, ...(failed?.notes ?? [])],
    detail: failed?.detail,
  }
}

/**
 * One canvas drawn once and read through a 2D canvas (`drawImage`), as a thumbnail or a print
 * would read a page: in the task that drew it, and again once the frame has been shown. Both
 * pixels are noted. It passes when the first read is the color drawn: a browser gives the
 * canvas a new, empty texture after showing a frame, so the second read is not held to it.
 */
async function snapshotCheck(device: GPUDevice, format: GPUTextureFormat, canvas: HTMLCanvasElement): Promise<ProbeCheck> {
  const color = surfaceColor('snapshot')
  const context = configure(canvas, device, format)
  if (!context) {
    return { id: 'snapshot', status: 'fail', notes: [{ code: 'noContext' }] }
  }
  clearTo(device, context.getCurrentTexture(), color)
  const now = centerPixel(canvas)
  await presented()
  const later = centerPixel(canvas)
  return {
    id: 'snapshot',
    status: pixelMatches(now, color) ? 'pass' : 'fail',
    notes: [
      { code: 'readNow', params: { got: rgbaText(now) } },
      { code: 'readLater', params: { got: rgbaText(later) } },
    ],
  }
}

/** Redraws `canvas` on every frame until the returned function is called. Nothing reads it: the screen is the answer. */
function drawLive(device: GPUDevice, format: GPUTextureFormat, canvas: HTMLCanvasElement): () => void {
  const context = configure(canvas, device, format)
  let frame = 0
  const draw = () => {
    try {
      clearTo(device, context!.getCurrentTexture(), surfaceColor('live'))
      frame = requestAnimationFrame(draw)
    } catch {
      // No context, or a device that is gone: the canvas stays as it is, which the screenshot shows.
    }
  }
  draw()
  return () => cancelAnimationFrame(frame)
}

/** The three checks a worker's one answer makes, and the worker, which keeps the page's canvas drawn while it lives. */
async function workerChecks(canvas: HTMLCanvasElement): Promise<{ checks: ProbeCheck[]; worker: Worker | null }> {
  const failed = (rest: Pick<ProbeCheck, 'notes' | 'detail'>): ProbeCheck[] => [{ id: 'worker', status: 'fail', ...rest }]
  let worker: Worker
  try {
    worker = new Worker(new URL('./probe.worker.ts', import.meta.url), { type: 'module' })
  } catch (error) {
    return { checks: failed({ detail: errorText(error) }), worker: null }
  }
  const request: ProbeWorkerRequest = { color: PROBE_COLORS[0]!, canvasColor: surfaceColor('worker') }
  let transferError: string | undefined
  if ('transferControlToOffscreen' in canvas) {
    try {
      request.canvas = canvas.transferControlToOffscreen()
    } catch (error) {
      transferError = errorText(error)
    }
  }
  let reply: ProbeWorkerReply
  try {
    reply = await within(
      new Promise<ProbeWorkerReply>((resolve, reject) => {
        worker.onmessage = (event: MessageEvent<ProbeWorkerReply>) => resolve(event.data)
        worker.onerror = (event) => reject(new Error(event.message || 'worker failed to start'))
        worker.postMessage(request, request.canvas ? [request.canvas] : [])
      }),
    )
  } catch (error) {
    worker.terminate()
    const timedOut = errorText(error) === 'timed out'
    return { checks: failed(timedOut ? { notes: [{ code: 'timedOut' }] } : { detail: errorText(error) }), worker: null }
  }
  if (!reply.texture) {
    worker.terminate()
    return { checks: failed({ notes: reply.note ? [{ code: reply.note }] : undefined, detail: reply.error }), worker: null }
  }

  const checks: ProbeCheck[] = [{ id: 'worker', ms: reply.ms, ...readVerdict(reply.texture, request.color) }]

  // What the page reads from each bitmap, through a 2D canvas as it would draw one.
  const notes: ProbeNote[] = []
  let transferred: number[] | undefined
  let detail = reply.bitmapError
  for (const code of ['bitmapTransfer', 'bitmapCopy', 'bitmapAfterWork'] as const) {
    const bitmap = reply[code]
    if (!bitmap) {
      continue
    }
    try {
      const pixel = centerPixel(bitmap)
      notes.push({ code, params: { got: rgbaText(pixel) } })
      if (code === 'bitmapTransfer') {
        transferred = pixel
      }
    } catch (error) {
      detail ??= errorText(error)
    } finally {
      bitmap.close()
    }
  }
  if (reply.bitmapNote) {
    notes.push({ code: reply.bitmapNote })
  }
  checks.push({
    id: 'workerBitmap',
    status: transferred && pixelMatches(transferred, request.color) ? 'pass' : 'fail',
    notes,
    detail,
  })

  checks.push(
    reply.canvas
      ? { id: 'workerCanvas', ...readVerdict(reply.canvas, request.canvasColor) }
      : { id: 'workerCanvas', status: 'fail', notes: transferError ? undefined : [{ code: 'noTransfer' }], detail: transferError },
  )
  return { checks, worker }
}

/**
 * A device of its own draws `canvas` and is destroyed; once the browser reports it lost, a new
 * adapter and device draw the canvas again in another color. The time is the recovery alone:
 * from the loss to the canvas drawn and read back.
 *
 * A destroyed device is the one loss a page can cause itself. A loss the GPU process causes
 * takes the same path from `device.lost` on, which is the part being checked. The device the
 * other canvases are drawn from is never the one destroyed.
 */
async function deviceLossCheck(
  gpu: GPU,
  format: GPUTextureFormat,
  canvas: HTMLCanvasElement,
): Promise<{ check: ProbeCheck; device: GPUDevice | null }> {
  const fail = (notes: ProbeNote[], detail?: string) => ({ check: { id: 'deviceLoss' as const, status: 'fail' as const, notes, detail }, device: null })
  const first = await newDevice(gpu)
  if (!first) {
    return fail([{ code: 'noAdapter' }])
  }
  const before = configure(canvas, first, format)
  if (!before) {
    first.destroy()
    return fail([{ code: 'noContext' }])
  }
  const drawn = readVerdict(await drawAndRead(first, before.getCurrentTexture(), PROBE_COLORS[4]!), PROBE_COLORS[4]!)
  first.destroy()
  let reason: string
  try {
    reason = (await within(first.lost)).reason ?? 'unknown'
  } catch {
    return fail([{ code: 'lostUnreported' }])
  }
  const lost: ProbeNote = { code: 'lost', params: { reason } }
  if (drawn.status === 'fail') {
    return fail([lost, ...(drawn.notes ?? [])], drawn.detail)
  }
  const started = performance.now()
  const next = await newDevice(gpu)
  if (!next) {
    return fail([lost, { code: 'noAdapter' }])
  }
  const after = configure(canvas, next, format)
  if (!after) {
    next.destroy()
    return fail([lost, { code: 'noContext' }])
  }
  const redrawn = readVerdict(await drawAndRead(next, after.getCurrentTexture(), surfaceColor('loss')), surfaceColor('loss'))
  return {
    check: {
      id: 'deviceLoss',
      status: redrawn.status,
      ms: performance.now() - started,
      notes: [lost, ...(redrawn.notes ?? [])],
      detail: redrawn.detail,
    },
    device: next,
  }
}

/**
 * Runs every check on the page's canvases. One check at a time, so that no step's time
 * includes another's work.
 */
export async function runProbe(canvases: ProbeCanvases): Promise<ProbeRun> {
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
  // What the run leaves working, for the page to release when it goes or runs again.
  const release: (() => void)[] = []
  const finish = (): ProbeRun => {
    report.checks = PROBE_CHECKS.map((id) => done.get(id) ?? skipped(id))
    return { report, dispose: () => release.forEach((step) => step()) }
  }
  // The worker asks for an adapter of its own, so it is checked whatever the page's answers.
  const checkWorker = async () => {
    const { checks, worker } = await workerChecks(canvases.worker)
    checks.forEach(record)
    if (worker) {
      release.push(() => worker.terminate())
    }
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
    await checkWorker()
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
    await checkWorker()
    return finish()
  }
  release.push(() => device.destroy())
  record({ id: 'device', status: 'pass', ms: performance.now() - started })

  const texture = plainTexture(device)
  record({ id: 'readback', ...readVerdict(await drawAndRead(device, texture, PROBE_COLORS[0]!), PROBE_COLORS[0]!) })
  texture.destroy()

  record(await canvasesCheck(device, format, canvases.once))
  try {
    record(await snapshotCheck(device, format, canvases.snapshot))
  } catch (error) {
    record({ id: 'snapshot', status: 'fail', detail: errorText(error) })
  }
  release.push(drawLive(device, format, canvases.live))
  await checkWorker()

  try {
    const recovery = await deviceLossCheck(gpu, format, canvases.loss)
    record(recovery.check)
    if (recovery.device) {
      const kept = recovery.device
      release.push(() => kept.destroy())
    }
  } catch (error) {
    record({ id: 'deviceLoss', status: 'fail', detail: errorText(error) })
  }
  return finish()
}
