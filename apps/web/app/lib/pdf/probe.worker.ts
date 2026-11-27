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
  PROBE_CANVAS_SIZE,
  clearTo,
  configure,
  drawAndRead,
  plainTexture,
  within,
  type ProbeWorkerReply,
  type ProbeWorkerRequest,
} from './probe-draw'

/**
 * A frame of a fresh `OffscreenCanvas` as each way of making a bitmap gives it: copied and
 * handed over in the task that drew it, and handed over once the GPU has finished the drawing.
 */
async function bitmaps(device: GPUDevice, format: GPUTextureFormat, color: readonly number[]): Promise<ProbeWorkerReply> {
  if (typeof OffscreenCanvas === 'undefined') {
    return { bitmapNote: 'noOffscreenCanvas' }
  }
  const canvas = new OffscreenCanvas(PROBE_CANVAS_SIZE, PROBE_CANVAS_SIZE)
  const context = configure(canvas, device, format)
  if (!context) {
    return { bitmapNote: 'noContext' }
  }
  // Both in the task that drew: the frame is the canvas's only until the browser takes it.
  clearTo(device, context.getCurrentTexture(), color)
  let bitmapError: string | undefined
  const copy = createImageBitmap(canvas).catch((error: unknown) => {
    bitmapError = errorText(error)
    return undefined
  })
  const bitmapTransfer = canvas.transferToImageBitmap()
  const bitmapCopy = await copy

  // The same hand-over, late: a frame that only this one gets right was taken before it was drawn.
  let bitmapAfterWork: ImageBitmap | undefined
  try {
    clearTo(device, context.getCurrentTexture(), color)
    await within(device.queue.onSubmittedWorkDone())
    bitmapAfterWork = canvas.transferToImageBitmap()
  } catch (error) {
    bitmapError ??= errorText(error)
  }
  return { bitmapTransfer, bitmapCopy, bitmapAfterWork, bitmapError }
}

async function run(request: ProbeWorkerRequest): Promise<ProbeWorkerReply> {
  const gpu = navigator.gpu
  if (!gpu) {
    return { note: 'noGpu' }
  }
  const started = performance.now()
  const adapter = await gpu.requestAdapter()
  if (!adapter) {
    return { note: 'noAdapter' }
  }
  // Never destroyed here: it keeps the page's canvas drawn, and the page ends the worker,
  // which releases it.
  const device = await adapter.requestDevice()
  const format = gpu.getPreferredCanvasFormat()

  const texture = plainTexture(device)
  const reply: ProbeWorkerReply = { texture: await drawAndRead(device, texture, request.color) }
  reply.ms = performance.now() - started
  texture.destroy()

  Object.assign(
    reply,
    await bitmaps(device, format, request.color).catch((error: unknown) => ({ bitmapError: errorText(error) })),
  )

  if (request.canvas) {
    const context = configure(request.canvas, device, format)
    reply.canvas = context
      ? await drawAndRead(device, context.getCurrentTexture(), request.canvasColor)
      : { note: 'noContext' }
  }
  return reply
}

addEventListener('message', (event: MessageEvent<ProbeWorkerRequest>) => {
  void run(event.data)
    .catch((error: unknown): ProbeWorkerReply => ({ error: errorText(error) }))
    .then((reply) => {
      const transfer = [reply.bitmapTransfer, reply.bitmapCopy, reply.bitmapAfterWork].filter((bitmap) => bitmap !== undefined)
      postMessage(reply, { transfer })
    })
})
