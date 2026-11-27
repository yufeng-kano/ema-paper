/// <reference types="@webgpu/types" />

/**
 * The device check's worker (docs/pdf-viewer.md § The device check): what WebGPU can do off
 * the main thread, where the viewer's geometry and its page tiles are meant to be prepared.
 * Three separate answers, since a browser may give one without the others:
 *
 * - a texture drawn and read back, all inside the worker;
 * - an `OffscreenCanvas` frame handed to the page as a bitmap, three ways;
 * - one of the page's own canvases, handed over, drawn here.
 *
 * The app's type check has the DOM library, not the worker one, so the worker's globals are
 * reached through the names both share: `addEventListener`, `postMessage`, `navigator`.
 */
import { errorText } from './probe'
import {
  BITMAP_WAYS,
  PROBE_CANVAS_SIZE,
  TimedOut,
  clearTo,
  configure,
  drawAndRead,
  failedRead,
  plainTexture,
  within,
  type BitmapWay,
  type ProbeWorkerReply,
  type ProbeWorkerRequest,
} from './probe-draw'

/** Sends the page one part of the answer, its bitmaps handed over, not copied. */
function post(part: ProbeWorkerReply): void {
  postMessage(part, { transfer: BITMAP_WAYS.map((way) => part[way]).filter((bitmap) => bitmap !== undefined) })
}

/**
 * A frame of an `OffscreenCanvas` as each way of making a bitmap gives it. Each way has a
 * canvas of its own, drawn for it, so no way reads what another left behind.
 */
async function bitmaps(device: GPUDevice, format: GPUTextureFormat, color: readonly number[]): Promise<ProbeWorkerReply> {
  if (typeof OffscreenCanvas === 'undefined') {
    return { bitmapNote: 'noOffscreenCanvas' }
  }
  /** A new canvas with one frame drawn, in this task. */
  const drawn = (): OffscreenCanvas | null => {
    const canvas = new OffscreenCanvas(PROBE_CANVAS_SIZE, PROBE_CANVAS_SIZE)
    const context = configure(canvas, device, format)
    if (!context) {
      return null
    }
    clearTo(device, context.getCurrentTexture(), color)
    return canvas
  }
  const ways: Record<BitmapWay, (canvas: OffscreenCanvas) => ImageBitmap | Promise<ImageBitmap>> = {
    // In the task that drew: the frame is the canvas's only until the browser takes it.
    bitmapTransfer: (canvas) => canvas.transferToImageBitmap(),
    bitmapCopy: (canvas) => {
      const copy = createImageBitmap(canvas)
      return within(copy).catch((error: unknown) => {
        // A copy that comes after its time has no reader left.
        void copy.then((bitmap) => bitmap.close()).catch(() => undefined)
        throw error
      })
    },
    // The same hand-over, late: a frame that only this way gets right was taken before it was drawn.
    bitmapAfterWork: async (canvas) => {
      await within(device.queue.onSubmittedWorkDone())
      return canvas.transferToImageBitmap()
    },
  }
  const reply: ProbeWorkerReply = {}
  for (const way of BITMAP_WAYS) {
    try {
      const canvas = drawn()
      if (!canvas) {
        return { ...reply, bitmapNote: 'noContext' }
      }
      reply[way] = await ways[way](canvas)
    } catch (error) {
      if (error instanceof TimedOut) {
        reply.bitmapTimedOut = [...(reply.bitmapTimedOut ?? []), way]
      } else {
        reply.bitmapErrors = { ...reply.bitmapErrors, [way]: errorText(error) }
      }
    }
  }
  return reply
}

/** Runs the stages in order and posts each one's answer as it ends. */
async function run(request: ProbeWorkerRequest): Promise<void> {
  const gpu = navigator.gpu
  if (!gpu) {
    return post({ note: 'noGpu' })
  }
  const started = performance.now()
  // Never destroyed here: it keeps the page's canvas drawn, and the page ends the worker,
  // which releases it.
  const device = await within(gpu.requestAdapter().then((adapter) => adapter?.requestDevice()))
  if (!device) {
    return post({ note: 'noAdapter' })
  }
  const format = gpu.getPreferredCanvasFormat()

  const texture = plainTexture(device)
  const read = await drawAndRead(device, texture, request.color)
  post({ texture: read, ms: performance.now() - started })
  texture.destroy()

  post(await bitmaps(device, format, request.color))

  if (request.canvas) {
    try {
      const context = configure(request.canvas, device, format)
      post({
        canvas: context ? await drawAndRead(device, context.getCurrentTexture(), request.canvasColor) : { note: 'noContext' },
      })
    } catch (error) {
      post({ canvas: failedRead(error) })
    }
  }
}

addEventListener('message', (event: MessageEvent<ProbeWorkerRequest>) => {
  void run(event.data)
    .catch((error: unknown) => post(error instanceof TimedOut ? { note: 'timedOut' } : { error: errorText(error) }))
    .then(() => post({ done: true }))
})
