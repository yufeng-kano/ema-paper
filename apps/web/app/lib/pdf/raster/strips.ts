/**
 * The strip renderer of the sparse-strip rasteriser (docs/pdf-viewer.md § Stage 4: the WebGPU core): sorted
 * tiles to strips, each with 16 coverage bytes per 4 x 4 tile, and the filled gaps between them.
 *
 * Ported from Vello 0.4.0 (vello_common, git 570f0c4): strip.rs 16-28, 198-738, Apache-2.0 OR MIT,
 * Copyright 2025 the Vello Authors, THIRD_PARTY_NOTICES § 8.
 * Changed: typed arrays, scalar loops, doubles.
 */
import type { Lines } from './buffers.ts'
import type { Tiles } from './tiles.ts'

/** Bit 31 of a strip's second word: the gap between it and the strip before it in its row is filled. */
export const FILL_GAP = 0x8000_0000
/** The x of the sentinel strip that ends a run. */
export const SENTINEL_X = 0xffff

/**
 * Strips, 2 u32 each: `y << 16 | x` in px (y a multiple of 4) and `alphaIdx | fillGap << 31`. Their coverage
 * bytes are column-major within a tile: byte `4 * column + row` from the strip's `alphaIdx`, so a strip's
 * columns are contiguous and its width is the next strip's `alphaIdx` less its own, over 4.
 */
export class Strips {
  data = new Uint32Array(2 * 1024)
  count = 0
  alphas = new Uint8Array(16 * 1024)
  alphaCount = 0

  clear(): void {
    this.count = 0
    this.alphaCount = 0
  }

  push(x: number, y: number, alphaIdx: number, fillGap: boolean): void {
    const at = 2 * this.count
    if (at + 2 > this.data.length) {
      const grown = new Uint32Array(2 * this.data.length)
      grown.set(this.data)
      this.data = grown
    }
    this.data[at] = (y << 16 | x) >>> 0
    this.data[at + 1] = (alphaIdx | (fillGap ? FILL_GAP : 0)) >>> 0
    this.count += 1
  }

  /** Makes room for `n` more coverage bytes and returns the index of the first. */
  reserveAlphas(n: number): number {
    const at = this.alphaCount
    if (at + n > this.alphas.length) {
      let size = 2 * this.alphas.length
      while (size < at + n) {
        size *= 2
      }
      const grown = new Uint8Array(size)
      grown.set(this.alphas.subarray(0, at))
      this.alphas = grown
    }
    this.alphaCount = at + n
    return at
  }
}

// The 4 x 4 accumulated area of the current location (column-major, as the bytes), the winding at its right
// edge per pixel row, and one line's carry across a tile; reused by every call.
const locationWinding = new Float64Array(16)
const accumulatedWinding = new Float64Array(4)
const lineCarry = new Float64Array(4)

/**
 * Vello's `render`: the strips of `t`'s sorted tiles appended to `out`, then one sentinel strip when any was
 * made. `t` comes from `makeTiles` over the same `lines`, then `sortTiles`.
 */
export function renderStrips(t: Tiles, lines: Lines, evenOdd: boolean, out: Strips): void {
  const rows = t.rows
  const hasCulled = t.culled
  // Strips are tile aligned, so the row-closing strip sits at the width rounded up to a tile.
  const viewportWidth = Math.min(Math.ceil(t.width / 4) * 4, 0xffff)
  const stripStart = out.count
  if (!hasCulled && t.count === 0) {
    return
  }
  const shouldFill = evenOdd ? (winding: number) => winding % 2 !== 0 : (winding: number) => winding !== 0
  const data = lines.data

  const emitSentinel = (): void => {
    if (out.count > stripStart) {
      out.push(SENTINEL_X, out.data[2 * (out.count - 1)]! >>> 16, out.alphaCount, false)
    }
  }

  // Rows that lines left of the target wind but no tile reaches: a solid strip from 0 to the row's end.
  const emitCulledBackground = (start: number, end: number): void => {
    for (let row = start; row < end; row += 1) {
      if (t.active[row] !== 0 && shouldFill(t.coarse[row]!)) {
        out.push(0, 4 * row, out.alphaCount, false)
        out.alphas.fill(255, out.reserveAlphas(16), out.alphaCount)
        out.push(viewportWidth, 4 * row, out.alphaCount, true)
      }
    }
  }

  // A row whose first tile is not at x = 0 but that lines left of the target wind: a solid strip at 0 first.
  // The row's winding then starts from the coarse winding, plus the partial one when the tile is at x = 0.
  const emitCaptiveStrip = (y: number, isLeftViewport: boolean): number => {
    const coarse = t.coarse[y]!
    if (shouldFill(coarse) && !isLeftViewport) {
      out.push(0, 4 * y, out.alphaCount, false)
      out.alphas.fill(255, out.reserveAlphas(16), out.alphaCount)
    }
    for (let k = 0; k < 4; k += 1) {
      accumulatedWinding[k] = coarse + (isLeftViewport ? t.partial[4 * y + k]! : 0)
    }
    return coarse
  }

  // The winding a line crossing a tile's top edge adds, accumulated along the row.
  let windingDelta = 0
  let prevX = SENTINEL_X
  let prevY = SENTINEL_X
  if (t.count > 0) {
    const xy = t.data[2 * t.order[0]!]!
    prevX = xy & 0xffff
    prevY = xy >>> 16
  }
  locationWinding.fill(0)
  accumulatedWinding.fill(0)
  const leftViewport = prevX === 0
  if (hasCulled) {
    emitCulledBackground(0, Math.min(prevY, rows))
    if (t.count === 0) {
      emitSentinel()
      return
    }
    windingDelta = emitCaptiveStrip(prevY, leftViewport)
    for (let i = 0; i < 16; i += 1) {
      locationWinding[i] = accumulatedWinding[i & 3]!
    }
  }

  // The strip being built.
  let stripX = 4 * prevX
  let stripY = 4 * prevY
  let stripAlpha = out.alphaCount
  let stripGap = shouldFill(windingDelta) && !leftViewport

  for (let index = 0; index <= t.count; index += 1) {
    // The sentinel tile after the last one flushes the last location and strip.
    let tileX = SENTINEL_X
    let tileY = SENTINEL_X
    let lineIndex = 0
    let winding = 0
    if (index < t.count) {
      const at = 2 * t.order[index]!
      const xy = t.data[at]!
      tileX = xy & 0xffff
      tileY = xy >>> 16
      lineIndex = t.data[at + 1]! >>> 5
      winding = (t.data[at + 1]! >>> 4) & 1
    }

    const sameLocation = prevY === tileY && prevX === tileX
    if (!sameLocation) {
      const at = out.reserveAlphas(16)
      const alphas = out.alphas
      for (let i = 0; i < 16; i += 1) {
        const area = locationWinding[i]!
        // The winding can exceed 1, hence the clamp; the Uint8Array store truncates, as Rust's `as u32` does.
        alphas[at + i] = evenOdd
          ? Math.min(255 * Math.abs(-2 * Math.floor(area * 0.5 + 0.5) + area) + 0.5, 255)
          : Math.min(Math.abs(area) * 255 + 0.5, 255)
      }
      for (let i = 0; i < 16; i += 1) {
        locationWinding[i] = accumulatedWinding[i & 3]!
      }
    }

    if (!sameLocation && !(prevY === tileY && prevX + 1 === tileX)) {
      out.push(stripX, stripY, stripAlpha, stripGap)
      const isSentinel = index === t.count
      const tileLeftViewport = tileX === 0
      if (prevY !== tileY) {
        // The row's winding left at its end becomes a strip at its end, which fills the gap before it.
        if (windingDelta !== 0) {
          out.push(viewportWidth, 4 * prevY, out.alphaCount, shouldFill(windingDelta))
        }
        if (hasCulled && !isSentinel) {
          emitCulledBackground(prevY + 1, tileY)
          windingDelta = emitCaptiveStrip(tileY, tileLeftViewport)
        } else {
          windingDelta = 0
          accumulatedWinding.fill(0)
        }
        for (let i = 0; i < 16; i += 1) {
          locationWinding[i] = accumulatedWinding[i & 3]!
        }
      } else {
        // Not needed mathematically; it stops float error accumulating along the row.
        accumulatedWinding.fill(windingDelta)
      }
      if (isSentinel) {
        break
      }
      stripX = 4 * tileX
      stripY = 4 * tileY
      stripAlpha = out.alphaCount
      stripGap = shouldFill(windingDelta) && !tileLeftViewport
    }
    prevX = tileX
    prevY = tileY

    const tileLeft = 4 * tileX
    const tileTop = 4 * tileY
    const p0x = data[4 * lineIndex]! - tileLeft
    const p0y = data[4 * lineIndex + 1]! - tileTop
    const p1x = data[4 * lineIndex + 2]! - tileLeft
    const p1y = data[4 * lineIndex + 3]! - tileTop
    // Horizontal lines change no winding.
    if (p0y === p1y) {
      continue
    }
    // Rust's `signum` of a nonzero difference: lines going up add winding, lines going down take it away.
    const sign = p0y - p1y > 0 ? 1 : -1
    const down = p0y < p1y
    const topY = down ? p0y : p1y
    const topX = down ? p0x : p1x
    const bottomY = down ? p1y : p0y
    const bottomX = down ? p1x : p0x
    // Infinite for a vertical line; then xSlope is 0.
    const ySlope = (bottomY - topY) / (bottomX - topX)
    const xSlope = 1 / ySlope
    windingDelta += sign * winding
    const base = topY * -xSlope + topX
    lineCarry.fill(0)
    for (let x = 0; x < 4; x += 1) {
      for (let k = 0; k < 4; k += 1) {
        const ymin = topY > k ? topY : k
        const ymax = bottomY < k + 1 ? bottomY : k + 1
        // A vertical line on a pixel edge makes NaN (0 times infinity), which must take ymin:
        // Vello's `max_if_first_nan_take_second`, so the operand order matters.
        let left = (x - topX) * ySlope + topY
        left = left > ymin ? left : ymin
        left = left < ymax ? left : ymax
        let right = (x + 1 - topX) * ySlope + topY
        right = right > ymin ? right : ymin
        right = right < ymax ? right : ymax
        const leftYx = left * xSlope + base
        const rightYx = right * xSlope + base
        const h = Math.abs(right - left)
        // The trapezoid between the line and the pixel's right edge.
        const area = h * ((rightYx + leftYx) * -0.5 + (x + 1))
        locationWinding[4 * x + k]! += area * sign + lineCarry[k]!
        lineCarry[k]! += h * sign
      }
    }
    for (let k = 0; k < 4; k += 1) {
      accumulatedWinding[k]! += lineCarry[k]!
    }
  }

  if (hasCulled) {
    emitCulledBackground(Math.min(prevY + 1, rows), rows)
  }
  emitSentinel()
}
