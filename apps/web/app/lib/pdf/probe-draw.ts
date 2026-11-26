/// <reference types="@webgpu/types" />

/**
 * The one drawing the device check makes, shared by the page and its worker
 * (docs/pdf-viewer.md § The device check): a canvas cleared to one opaque color.
 */

import type { ProbeNoteCode } from './probe'

/** The side of every check canvas, in device pixels. */
export const PROBE_CANVAS_SIZE = 64

/** What the page asks its worker to draw. */
export interface ProbeWorkerRequest {
  color: readonly number[]
}

/** The worker's answer: the frame it drew and how long that took, or why it could not. */
export interface ProbeWorkerReply {
  bitmap?: ImageBitmap
  /** From asking for an adapter to the frame drawn, timed in the worker: no script load in it. */
  ms?: number
  note?: ProbeNoteCode
  error?: string
}

/** Clears the context's current texture to `color` (RGBA bytes) and submits the pass. */
export function clearTo(device: GPUDevice, context: GPUCanvasContext, color: readonly number[]): void {
  const encoder = device.createCommandEncoder()
  const pass = encoder.beginRenderPass({
    colorAttachments: [
      {
        view: context.getCurrentTexture().createView(),
        clearValue: { r: color[0]! / 255, g: color[1]! / 255, b: color[2]! / 255, a: color[3]! / 255 },
        loadOp: 'clear',
        storeOp: 'store',
      },
    ],
  })
  pass.end()
  device.queue.submit([encoder.finish()])
}
