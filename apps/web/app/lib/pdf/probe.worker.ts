/// <reference types="@webgpu/types" />

/**
 * The device check's worker (docs/pdf-viewer.md § The device check): whether WebGPU can draw
 * into an `OffscreenCanvas` off the main thread, which is where the viewer's geometry and its
 * page tiles are meant to be prepared.
 *
 * The app's type check has the DOM library, not the worker one, so the worker's globals are
 * reached through the names both share: `addEventListener`, `postMessage`, `navigator`.
 */
import { errorText } from './probe'
import { PROBE_CANVAS_SIZE, clearTo, type ProbeWorkerReply, type ProbeWorkerRequest } from './probe-draw'

async function draw(request: ProbeWorkerRequest): Promise<ProbeWorkerReply> {
  const gpu = navigator.gpu
  if (!gpu) {
    return { note: 'noGpu' }
  }
  if (typeof OffscreenCanvas === 'undefined') {
    return { note: 'noOffscreenCanvas' }
  }
  const started = performance.now()
  const adapter = await gpu.requestAdapter()
  if (!adapter) {
    return { note: 'noAdapter' }
  }
  // Never destroyed here: the page reads the frame first and then ends the worker, which
  // releases the device with it.
  const device = await adapter.requestDevice()
  const canvas = new OffscreenCanvas(PROBE_CANVAS_SIZE, PROBE_CANVAS_SIZE)
  const context = canvas.getContext('webgpu')
  if (!context) {
    return { note: 'noContext' }
  }
  context.configure({ device, format: gpu.getPreferredCanvasFormat(), alphaMode: 'opaque' })
  clearTo(device, context, request.color)
  const bitmap = canvas.transferToImageBitmap()
  return { bitmap, ms: performance.now() - started }
}

addEventListener('message', (event: MessageEvent<ProbeWorkerRequest>) => {
  void draw(event.data)
    .catch((error: unknown): ProbeWorkerReply => ({ error: errorText(error) }))
    .then((reply) => {
      postMessage(reply, { transfer: reply.bitmap ? [reply.bitmap] : [] })
    })
})
