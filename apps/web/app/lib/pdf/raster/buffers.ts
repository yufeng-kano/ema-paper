/**
 * The growable buffers the rasteriser fills per job (docs/pdf-viewer.md § Stage 4: the WebGPU core):
 * flattened lines for the tiler, and paths in pdf.js's DrawOPS encoding for the stroker's outlines.
 * Each grows by doubling and keeps its memory on `clear`, so a job allocates them once.
 */
import { DRAW_OPS } from '../scene/ops.ts'

const INITIAL_LINES = 256
const INITIAL_PATH_ENTRIES = 1024

/** Flattened lines, 4 f32 each: x0 y0 x1 y1 in target px (Vello's `Line`). Grows by doubling; `clear` keeps the memory. */
export class Lines {
  data = new Float32Array(4 * INITIAL_LINES)
  count = 0

  /** Stored as f32, as Vello's lines are: the tiler and the strip coverage read exactly these values. */
  push(x0: number, y0: number, x1: number, y1: number): void {
    const at = 4 * this.count
    if (at + 4 > this.data.length) {
      const grown = new Float32Array(2 * this.data.length)
      grown.set(this.data)
      this.data = grown
    }
    this.data[at] = x0
    this.data[at + 1] = y0
    this.data[at + 2] = x1
    this.data[at + 3] = y1
    this.count += 1
  }

  clear(): void {
    this.count = 0
  }
}

/** A path in pdf.js DrawOPS (0 moveTo, 1 lineTo, 2 curveTo, 3 quadraticCurveTo, 4 closePath), f64. */
export class PathBuffer {
  data = new Float64Array(INITIAL_PATH_ENTRIES)
  length = 0

  moveTo(x: number, y: number): void {
    this.#reserve(3)
    this.data[this.length] = DRAW_OPS.moveTo
    this.data[this.length + 1] = x
    this.data[this.length + 2] = y
    this.length += 3
  }

  lineTo(x: number, y: number): void {
    this.#reserve(3)
    this.data[this.length] = DRAW_OPS.lineTo
    this.data[this.length + 1] = x
    this.data[this.length + 2] = y
    this.length += 3
  }

  curveTo(x1: number, y1: number, x2: number, y2: number, x3: number, y3: number): void {
    this.#reserve(7)
    const at = this.length
    this.data[at] = DRAW_OPS.curveTo
    this.data[at + 1] = x1
    this.data[at + 2] = y1
    this.data[at + 3] = x2
    this.data[at + 4] = y2
    this.data[at + 5] = x3
    this.data[at + 6] = y3
    this.length += 7
  }

  close(): void {
    this.#reserve(1)
    this.data[this.length] = DRAW_OPS.closePath
    this.length += 1
  }

  clear(): void {
    this.length = 0
  }

  #reserve(entries: number): void {
    if (this.length + entries <= this.data.length) {
      return
    }
    const grown = new Float64Array(2 * this.data.length)
    grown.set(this.data.subarray(0, this.length))
    this.data = grown
  }
}
