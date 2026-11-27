/// <reference types="@webgpu/types" />

/**
 * The drawing and the reading the device check makes, shared by the page and its worker
 * (docs/pdf-viewer.md § The device check): a texture cleared to one opaque color, and its
 * middle pixel copied back out of the GPU.
 */
import { errorText, type ProbeNoteCode, type ProbeRead } from './probe'

/** The side of every check canvas and texture, in device pixels. */
export const PROBE_CANVAS_SIZE = 64

/** A row of the read-back buffer: 64 pixels of 4 bytes, already the multiple of 256 a copy needs. */
const BYTES_PER_ROW = PROBE_CANVAS_SIZE * 4

/** How long a step that waits on the GPU may take before the check gives up on it. */
export const STEP_TIMEOUT_MS = 8000

/** What the page asks its worker to do: draw in `color`, and `canvas` — one of the page's, handed over — in `canvasColor`. */
export interface ProbeWorkerRequest {
  color: readonly number[]
  canvas?: OffscreenCanvas
  canvasColor: readonly number[]
}

/** The worker's answer. Without `texture` nothing ran, and `note` or `error` says why. */
export interface ProbeWorkerReply {
  note?: ProbeNoteCode
  error?: string
  /** A texture drawn and read back in the worker, and how long from asking for an adapter to that read. */
  texture?: ProbeRead
  ms?: number
  /**
   * An `OffscreenCanvas` frame as `transferToImageBitmap()` gives it and as `createImageBitmap()`
   * copies it, both in the task that drew it, and as `transferToImageBitmap()` gives it once
   * `onSubmittedWorkDone()` has resolved.
   */
  bitmapTransfer?: ImageBitmap
  bitmapCopy?: ImageBitmap
  bitmapAfterWork?: ImageBitmap
  bitmapNote?: ProbeNoteCode
  bitmapError?: string
  /** The page's canvas, drawn by the worker and read back from the GPU there. */
  canvas?: ProbeRead
}

/** `promise`, or a rejection once `ms` have passed: a lost device answers nothing at all. */
export function within<T>(promise: Promise<T>, ms = STEP_TIMEOUT_MS): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timed out')), ms)
    promise.then(resolve, reject).finally(() => clearTimeout(timer))
  })
}

/**
 * Configures `canvas` for `device` at the check's size and returns its context, or null where
 * the browser gives none. The textures are copy sources too, so a check reads one back from the
 * GPU without going through the canvas.
 */
export function configure(
  canvas: HTMLCanvasElement | OffscreenCanvas,
  device: GPUDevice,
  format: GPUTextureFormat,
): GPUCanvasContext | null {
  canvas.width = PROBE_CANVAS_SIZE
  canvas.height = PROBE_CANVAS_SIZE
  const context = canvas.getContext('webgpu')
  context?.configure({
    device,
    format,
    alphaMode: 'opaque',
    usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC,
  })
  return context
}

/** A texture of the check's size that is no canvas's: what the GPU draws with nothing presenting it. */
export function plainTexture(device: GPUDevice): GPUTexture {
  return device.createTexture({
    size: [PROBE_CANVAS_SIZE, PROBE_CANVAS_SIZE],
    format: 'rgba8unorm',
    usage: GPUTextureUsage.RENDER_ATTACHMENT | GPUTextureUsage.COPY_SRC,
  })
}

/** Clears `texture` to `color` (RGBA bytes) and submits the pass. */
export function clearTo(device: GPUDevice, texture: GPUTexture, color: readonly number[]): void {
  const encoder = device.createCommandEncoder()
  const pass = encoder.beginRenderPass({
    colorAttachments: [
      {
        view: texture.createView(),
        clearValue: { r: color[0]! / 255, g: color[1]! / 255, b: color[2]! / 255, a: color[3]! / 255 },
        loadOp: 'clear',
        storeOp: 'store',
      },
    ],
  })
  pass.end()
  device.queue.submit([encoder.finish()])
}

/**
 * Clears `texture` to `color` and reads its middle pixel back from the GPU, as RGBA whatever
 * the texture's byte order.
 *
 * The clear and the copy are both submitted before this first waits, so a canvas's current
 * texture is read in the task that drew it, before the browser presents it and takes it away.
 * A step the browser refuses comes back as its validation message.
 */
export async function drawAndRead(device: GPUDevice, texture: GPUTexture, color: readonly number[]): Promise<ProbeRead> {
  let buffer: GPUBuffer | undefined
  try {
    device.pushErrorScope('validation')
    clearTo(device, texture, color)
    buffer = device.createBuffer({
      size: BYTES_PER_ROW * PROBE_CANVAS_SIZE,
      usage: GPUBufferUsage.COPY_DST | GPUBufferUsage.MAP_READ,
    })
    const encoder = device.createCommandEncoder()
    encoder.copyTextureToBuffer(
      { texture },
      { buffer, bytesPerRow: BYTES_PER_ROW },
      { width: PROBE_CANVAS_SIZE, height: PROBE_CANVAS_SIZE },
    )
    device.queue.submit([encoder.finish()])
    const bgra = texture.format.startsWith('bgra')
    const refused = await within(device.popErrorScope())
    if (refused) {
      return { error: refused.message }
    }
    await within(buffer.mapAsync(GPUMapMode.READ))
    const middle = (PROBE_CANVAS_SIZE / 2) * BYTES_PER_ROW + (PROBE_CANVAS_SIZE / 2) * 4
    const [first, green, third, alpha] = new Uint8Array(buffer.getMappedRange(middle, 4))
    return { pixel: bgra ? [third!, green!, first!, alpha!] : [first!, green!, third!, alpha!] }
  } catch (error) {
    return { error: errorText(error) }
  } finally {
    buffer?.destroy()
  }
}
