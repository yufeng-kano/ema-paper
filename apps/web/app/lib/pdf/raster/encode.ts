/**
 * The draw list the GPU side draws (docs/pdf-viewer.md § Stage 4: the WebGPU core): strips as Vello's 24-byte
 * strip instances, their coverage bytes and the image paints as data textures, and the runs that share a
 * texture and a filter (raster/protocol.ts `DrawList`).
 *
 * Ported from Vello 0.4.0 (vello_gpu, git 570f0c4): render/common/mod.rs 276-301, draw.rs 397-421, with the
 * segment visitor of vello_common strip.rs 116-196, Apache-2.0 OR MIT, Copyright 2025 the Vello Authors,
 * THIRD_PARTY_NOTICES § 8.
 * Changed: no rect strips; paint type 7 is an image mask; data textures always 4096 wide; runs by texture and
 * filter.
 */
import type { Mat6 } from '../scene/matrix.ts'
import {
  DATA_ROW_BYTES, DATA_TEXTURE_WIDTH, INSTANCE_WORDS, MAX_DATA_ROWS, PAINT_TYPE_SHIFT, PaintType, RUN_WORDS,
  type DrawList,
} from './protocol.ts'
import { FILL_GAP, type Strips } from './strips.ts'

/** Exact rounding of x / 255 for 0 <= x <= 65025. */
export function div255(x: number): number {
  return (x + 128 + ((x + 128) >> 8)) >> 8
}

/** Premultiplied RGBA8 with r in the low byte (`unpack4x8unorm` order), from `0xRRGGBB` and an alpha in [0, 1]. */
export function premulRgba8(rgb: number, alpha: number): number {
  const a8 = Math.round(alpha * 255)
  const r = div255(((rgb >>> 16) & 0xff) * a8)
  const g = div255(((rgb >>> 8) & 0xff) * a8)
  const b = div255((rgb & 0xff) * a8)
  return (r | g << 8 | b << 16 | a8 << 24) >>> 0
}

/** Grows by doubling: instances, alpha bytes, paint words, runs. `finish` returns exactly sized, fresh arrays. */
export class ListBuilder {
  #width: number
  #height: number
  #instances = new Uint32Array(INSTANCE_WORDS * 1024)
  #instanceCount = 0
  #alphas = new Uint8Array(DATA_ROW_BYTES)
  #alphaCount = 0
  // One buffer seen as words and as f32, for the paint texels' f32 bits.
  #paintWords = new Uint32Array(4 * 1024)
  #paintFloats = new Float32Array(this.#paintWords.buffer)
  #paintCount = 0
  #runs = new Uint32Array(RUN_WORDS * 64)
  #runCount = 0

  constructor(width: number, height: number) {
    this.#width = width
    this.#height = height
  }

  /**
   * One instance per alpha segment and per filled gap of `s[start, end)` (a run ending in its sentinel), clipped to
   * the target's tiles; the coverage bytes used are copied. `payload` is a solid's premultiplied colour; an image or
   * mask paint takes each segment's own `x | y << 16` instead, which the vertex shader maps through the UV map.
   */
  strips(s: Strips, start: number, end: number, payload: number, paint: number, slot: number, filter: number): void {
    const columns = Math.ceil(this.#width / 4)
    const rows = Math.ceil(this.#height / 4)
    const image = paint >>> PAINT_TYPE_SHIFT !== PaintType.Solid
    const first = this.#instanceCount
    const data = s.data
    for (let i = start; i + 1 < end; i += 1) {
      const xy = data[2 * i]!
      const x = xy & 0xffff
      const y = xy >>> 16
      if (y >> 2 >= rows) {
        break
      }
      const alpha = data[2 * i + 1]! & ~FILL_GAP
      const nextXy = data[2 * i + 2]!
      const nextWord = data[2 * i + 3]!
      // `Strip::width_to`, in tiles: the columns of coverage bytes between this strip and the next.
      const tiles = Math.max(0, ((nextWord & ~FILL_GAP) >>> 2) - (alpha >>> 2)) >>> 2
      const tileX0 = x >>> 2
      const tileX1 = tileX0 + tiles
      const x1 = Math.min(tileX1, columns)
      if (tileX0 < x1) {
        const n = 16 * (x1 - tileX0)
        if (this.#alphaCount + n > this.#alphas.length) {
          let size = 2 * this.#alphas.length
          while (size < this.#alphaCount + n) {
            size *= 2
          }
          const grown = new Uint8Array(size)
          grown.set(this.#alphas.subarray(0, this.#alphaCount))
          this.#alphas = grown
        }
        this.#alphas.set(s.alphas.subarray(alpha, alpha + n), this.#alphaCount)
        this.#instance(4 * tileX0, y, 4 * (x1 - tileX0), 4 * (x1 - tileX0), this.#alphaCount / 4, image ? (4 * tileX0 | y << 16) : payload, paint)
        this.#alphaCount += n
      }
      if ((nextWord & FILL_GAP) !== 0 && nextXy >>> 16 === y) {
        const gapEnd = Math.min((nextXy & 0xffff) >>> 2, columns)
        if (tileX1 < gapEnd) {
          this.#instance(4 * tileX1, y, 4 * (gapEnd - tileX1), 0, 0, image ? (4 * tileX1 | y << 16) : payload, paint)
        }
      }
    }
    const count = this.#instanceCount - first
    if (count === 0) {
      return
    }
    const last = RUN_WORDS * (this.#runCount - 1)
    if (this.#runCount > 0 && this.#runs[last + 2] === slot && this.#runs[last + 3] === filter) {
      this.#runs[last + 1]! += count
      return
    }
    if (RUN_WORDS * (this.#runCount + 1) > this.#runs.length) {
      const grown = new Uint32Array(2 * this.#runs.length)
      grown.set(this.#runs)
      this.#runs = grown
    }
    this.#runs.set([first, count, slot, filter], RUN_WORDS * this.#runCount)
    this.#runCount += 1
  }

  /** Appends the two texels of an image or mask paint: the UV map's f32 bits, `z` and the level. Returns the first's index. */
  imagePaint(uv: Readonly<Mat6>, z: number, lod: number): number {
    if (4 * (this.#paintCount + 2) > this.#paintWords.length) {
      const grown = new Uint32Array(2 * this.#paintWords.length)
      grown.set(this.#paintWords)
      this.#paintWords = grown
      this.#paintFloats = new Float32Array(grown.buffer)
    }
    const at = 4 * this.#paintCount
    this.#paintFloats[at] = uv[0]
    this.#paintFloats[at + 1] = uv[1]
    this.#paintFloats[at + 2] = uv[2]
    this.#paintFloats[at + 3] = uv[3]
    this.#paintFloats[at + 4] = uv[4]
    this.#paintFloats[at + 5] = uv[5]
    this.#paintWords[at + 6] = z >>> 0
    this.#paintFloats[at + 7] = lod
    const texel = this.#paintCount
    this.#paintCount += 2
    return texel
  }

  finish(): DrawList {
    const alphaRows = Math.max(1, Math.ceil(this.#alphaCount / DATA_ROW_BYTES))
    const paintRows = Math.max(1, Math.ceil(this.#paintCount / DATA_TEXTURE_WIDTH))
    if (alphaRows > MAX_DATA_ROWS || paintRows > MAX_DATA_ROWS) {
      throw new Error(`the draw list needs ${Math.max(alphaRows, paintRows)} data texture rows, more than ${MAX_DATA_ROWS}`)
    }
    const alphas = new Uint8Array(alphaRows * DATA_ROW_BYTES)
    alphas.set(this.#alphas.subarray(0, this.#alphaCount))
    const paints = new Uint32Array(paintRows * 4 * DATA_TEXTURE_WIDTH)
    paints.set(this.#paintWords.subarray(0, 4 * this.#paintCount))
    return {
      width: this.#width,
      height: this.#height,
      instances: this.#instances.slice(0, INSTANCE_WORDS * this.#instanceCount),
      alphas,
      paints,
      runs: this.#runs.slice(0, RUN_WORDS * this.#runCount),
    }
  }

  #instance(x: number, y: number, width: number, dense: number, column: number, payload: number, paint: number): void {
    const at = INSTANCE_WORDS * this.#instanceCount
    if (at + INSTANCE_WORDS > this.#instances.length) {
      const grown = new Uint32Array(2 * this.#instances.length)
      grown.set(this.#instances)
      this.#instances = grown
    }
    const words = this.#instances
    words[at] = (x | y << 16) >>> 0
    words[at + 1] = (width | dense << 16) >>> 0
    words[at + 2] = column
    words[at + 3] = payload >>> 0
    words[at + 4] = paint >>> 0
    words[at + 5] = 0
    this.#instanceCount += 1
  }
}
