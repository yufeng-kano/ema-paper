/// <reference types="@webgpu/types" />

/**
 * The device check's worker (docs/pdf-viewer.md § The device check): whether WebGPU can draw
 * into an `OffscreenCanvas` off the main thread, which is where the viewer's geometry and its
 * page tiles are meant to be prepared.
 *
 * The app's type check has the DOM library, not the worker one, so the worker's globals are
 * reached through the names both share: `addEventListener`, `postMessage`, `navigator`.
 */
import { PROBE_CANVAS_SIZE, clearTo, errorText, type ProbeWorkerReply, type ProbeWorkerRequest } from './probe-draw'

async function draw(request: ProbeWorkerRequest): Promise<ProbeWorkerReply> {
  const gpu = navigator.gpu
  if (!gpu) {
    return { error: 'navigator.gpu is missing in a worker' }
  }
  if (typeof OffscreenCanvas === 'undefined') {
    return { error: 'OffscreenCanvas is missing' }
  }
  const adapter = await gpu.requestAdapter()
  if (!adapter) {
    return { error: 'requestAdapter() returned null in a worker' }
  }
  const device = await adapter.requestDevice()
  try {
    const canvas = new OffscreenCanvas(PROBE_CANVAS_SIZE, PROBE_CANVAS_SIZE)
    const context = canvas.getContext('webgpu')
    if (!context) {
      return { error: 'OffscreenCanvas has no webgpu context' }
    }
    context.configure({ device, format: gpu.getPreferredCanvasFormat(), alphaMode: 'opaque' })
    clearTo(device, context, request.color)
    return { bitmap: canvas.transferToImageBitmap() }
  } finally {
    device.destroy()
  }
}

addEventListener('message', (event: MessageEvent<ProbeWorkerRequest>) => {
  void draw(event.data)
    .catch((error: unknown): ProbeWorkerReply => ({ error: errorText(error) }))
    .then((reply) => {
      postMessage(reply, { transfer: reply.bitmap ? [reply.bitmap] : [] })
    })
})
