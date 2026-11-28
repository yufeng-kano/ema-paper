/**
 * The tiler of the sparse-strip rasteriser (docs/pdf-viewer.md § Stage 4: the WebGPU core): the 4 x 4 tiles
 * each line of a path crosses, and the winding of the lines left of the target, which make no tiles.
 *
 * Ported from Vello 0.4.0 (vello_common, git 570f0c4): tile.rs 45-206, 514-933, Apache-2.0 OR MIT,
 * Copyright 2025 the Vello Authors, THIRD_PARTY_NOTICES § 8.
 * Changed: typed arrays; one byte per row for the active set; one Float64Array key sort; Rust's NaN-ignoring
 * min/max and saturating casts written out; the arithmetic in doubles, not f32.
 */
import type { Lines } from './buffers.ts'

/** The winding bit of a tile's mask: the line crosses the tile's top edge. */
const W = 0b10000
/** The line index sits above the 5 mask bits of a u32. */
const MAX_LINES_PER_PATH = 1 << 27
/** `Tile::new_clamped`: u16::MAX / 4, so that tile coordinates times 4 stay in u16. */
const MAX_TILE_COORD = 16383
/** The sort key keeps the tile's index in its low 24 bits. */
const MAX_TILES = 1 << 24
const INDEX_SCALE = 2 ** 24

/**
 * One path's tiles, 2 u32 each in generation order: `y << 16 | x` and `line << 5 | mask` (the analytic tiler
 * sets only the winding bit). `order` holds the tile indices sorted by `sortTiles`. `partial`, `coarse` and
 * `active` are Vello's `CulledWindings` per tile row, `culled` whether any line was left of the target.
 */
export class Tiles {
  data = new Uint32Array(2 * 1024)
  count = 0
  keys = new Float64Array(1024)
  order = new Uint32Array(1024)
  /** 4 per tile row: the fractional winding of each pixel row from lines left of the target. */
  partial = new Float64Array(0)
  coarse = new Int32Array(0)
  active = new Uint8Array(0)
  culled = false
  width = 0
  height = 0
  rows = 0
}

/** Rust's `f32::min`: a NaN operand yields the other one. */
function rmin(a: number, b: number): number {
  return a < b || b !== b ? a : b
}

/** Rust's `f32::max`: a NaN operand yields the other one. */
function rmax(a: number, b: number): number {
  return a > b || b !== b ? a : b
}

/** Rust's `f32 as u16`: truncation, saturating at 0 and 65535, NaN to 0. */
function u16(v: number): number {
  return v > 0 ? (v < 65535 ? Math.trunc(v) : 65535) : 0
}

function pushTile(t: Tiles, x: number, y: number, line: number, mask: number): void {
  const at = 2 * t.count
  if (at + 2 > t.data.length) {
    const grown = new Uint32Array(2 * t.data.length)
    grown.set(t.data)
    t.data = grown
  }
  t.data[at] = (y << 16 | x) >>> 0
  t.data[at + 1] = (line << 5 | mask) >>> 0
  t.count += 1
}

/**
 * `calc_fractional_coverage` added into a row's partial winding: each pixel row's share of the segment
 * [top, bottom) in tile units, times `dir`, less `doubleCount` where the coarse winding already holds it.
 */
function addPartial(t: Tiles, row: number, top: number, bottom: number, dir: number, doubleCount: number): void {
  const start = (top - row) * 4
  const end = (bottom - row) * 4
  for (let k = 0; k < 4; k += 1) {
    const coverage = Math.max(Math.min(k + 1, end) - Math.max(k, start), 0)
    t.partial[4 * row + k] = coverage * dir + (t.partial[4 * row + k]! - doubleCount)
  }
}

/** Vello's `make_tiles_analytic_aa` with its `CulledWindings`: `out` is reset for a width x height target first. */
export function makeTiles(lines: Lines, width: number, height: number, out: Tiles): void {
  const rows = Math.ceil(height / 4)
  if (rows !== out.rows) {
    out.partial = new Float64Array(4 * rows)
    out.coarse = new Int32Array(rows)
    out.active = new Uint8Array(rows)
    out.rows = rows
  } else if (out.culled) {
    out.partial.fill(0)
    out.coarse.fill(0)
    out.active.fill(0)
  }
  out.culled = false
  out.width = width
  out.height = height
  out.count = 0
  if (width === 0 || height === 0) {
    return
  }
  const columns = Math.ceil(width / 4)
  const data = lines.data

  // The line being tiled, read by the two row helpers below, which are made once per call.
  let line = 0
  let p0x = 0
  let p0y = 0
  let dx = 0
  let dy = 0
  let xSlope = 0
  let leftX = 0
  let rightX = 0
  let dir = 0

  const pushRowExtents = (y: number, rowLeftX: number, rowRightX: number, wStart: number, wEnd: number, wSingle: number): void => {
    const xStart = u16(rowLeftX)
    const xEnd = Math.min(u16(rowRightX), columns - 1)
    if (xStart <= xEnd) {
      pushTile(out, xStart, y, line, xStart === xEnd ? wSingle : wStart)
      for (let x = xStart + 1; x < xEnd; x += 1) {
        pushTile(out, x, y, line, 0)
      }
      if (xStart < xEnd) {
        pushTile(out, xEnd, y, line, wEnd)
      }
    }
  }

  const pushRow = (y: number, rowTopY: number, rowBottomY: number, wStart: number, wEnd: number, wSingle: number): void => {
    const rowTopX = p0x + (rowTopY - p0y) * xSlope
    const rowBottomX = p0x + (rowBottomY - p0y) * xSlope
    // A horizontal line makes NaN here (0 times an infinite slope), which Rust's min and max discard.
    const rowLeftX = rmax(rmin(rowTopX, rowBottomX), leftX)
    const rowRightX = rmin(rmax(rowTopX, rowBottomX), rightX)
    if (rowLeftX < 0) {
      out.culled = true
      if (rowRightX < 0) {
        // The line crosses the left edge, but not in this row: only its winding counts here.
        out.active[y] = 1
        const crossesTop = (wSingle & W) !== 0
        if (crossesTop) {
          out.coarse[y]! += dir
        }
        addPartial(out, y, rowTopY, rowBottomY, dir, crossesTop ? dir : 0)
        return
      }
      // The line enters the target in this row: the part left of it adds its fraction only, because the
      // clamped tiles carry the coarse winding.
      const ySlope = dy / dx
      const yIntersect = rowTopY - rowTopX * ySlope
      const offTop = rowTopX < 0 ? rowTopY : rmax(rowTopY, yIntersect)
      const offBottom = rowTopX < 0 ? rmin(rowBottomY, yIntersect) : rowBottomY
      if (offTop < offBottom) {
        out.active[y] = 1
        addPartial(out, y, offTop, offBottom, dir, 0)
      }
    }
    pushRowExtents(y, rowLeftX, rowRightX, wStart, wEnd, wSingle)
  }

  const count = Math.min(lines.count, MAX_LINES_PER_PATH)
  for (line = 0; line < count; line += 1) {
    p0x = data[4 * line]! / 4
    p0y = data[4 * line + 1]! / 4
    const p1x = data[4 * line + 2]! / 4
    const p1y = data[4 * line + 3]! / 4
    leftX = p0x < p1x ? p0x : p1x
    rightX = p0x < p1x ? p1x : p0x
    if (leftX > columns) {
      continue
    }
    const down = p0y < p1y
    const topY = down ? p0y : p1y
    const topX = down ? p0x : p1x
    const bottomY = down ? p1y : p0y
    const bottomX = down ? p1x : p0x
    const yTop = Math.min(u16(topY), rows)
    const yBottom = Math.min(u16(Math.ceil(bottomY)), rows)
    // Wholly above or below the target, or horizontal on a tile row boundary: no winding to give.
    if (yTop >= yBottom) {
      continue
    }
    dir = p0y >= p1y ? 1 : -1

    if (rightX < 0) {
      const startCulled = topY < 0
      if (!startCulled) {
        out.active[yTop] = 1
        // `<=`, not `===`, against rounding, as Vello has it.
        const atTop = topY <= yTop
        if (atTop) {
          out.coarse[yTop]! += dir
        }
        addPartial(out, yTop, topY, bottomY, dir, atTop ? dir : 0)
      }
      const yStartMiddle = startCulled ? yTop : yTop + 1
      const bottomFloor = Math.floor(bottomY)
      const yEndMiddle = Math.min(u16(bottomFloor), rows)
      for (let y = yStartMiddle; y < yEndMiddle; y += 1) {
        out.coarse[y]! += dir
        out.active[y] = 1
      }
      // The row the line ends in, unless it is the start row and that was handled above.
      if (bottomY !== bottomFloor && yEndMiddle < rows && (startCulled || yEndMiddle !== yTop)) {
        out.active[yEndMiddle] = 1
        out.coarse[yEndMiddle]! += dir
        addPartial(out, yEndMiddle, topY, bottomY, dir, dir)
      }
      out.culled = true
      continue
    }

    const sameTile = Math.floor(topY) === Math.floor(bottomY) && Math.floor(topX) === Math.floor(bottomX)
    if (sameTile) {
      pushTile(out, Math.min(Math.min(u16(leftX), columns + 1), MAX_TILE_COORD), Math.min(yTop, MAX_TILE_COORD), line,
        yTop >= topY ? W : 0)
      continue
    }
    const startCulled = topY < 0
    if (leftX === rightX) {
      // Vertical: it cannot cross the left edge within the target.
      const x = Math.min(Math.min(u16(leftX), Math.max(columns - 1, 0)), MAX_TILE_COORD)
      if (!startCulled) {
        pushTile(out, x, Math.min(yTop, MAX_TILE_COORD), line, yTop >= topY ? W : 0)
      }
      for (let y = startCulled ? yTop : yTop + 1; y < yBottom; y += 1) {
        pushTile(out, x, Math.min(y, MAX_TILE_COORD), line, W)
      }
      continue
    }

    dx = p1x - p0x
    dy = p1y - p0y
    xSlope = dx / dy
    const dxDir = bottomX >= topX ? 1 : 0
    const wStartBase = dxDir << 4
    const wEndBase = (dxDir ^ 1) << 4
    if (leftX >= 0 && rightX < columns) {
      // Wholly inside horizontally: no left-edge bookkeeping.
      if (!startCulled) {
        const rowBottomY = rmin(yTop + 1, bottomY)
        const rowBottomX = rowBottomY === bottomY ? bottomX : p0x + (rowBottomY - p0y) * xSlope
        const mask = yTop >= topY ? W : 0
        pushRowExtents(yTop, rmin(topX, rowBottomX), rmax(topX, rowBottomX), wStartBase & mask, wEndBase & mask, W & mask)
      }
      const yStart = startCulled ? yTop : yTop + 1
      if (yStart < yBottom) {
        let rowTopX = p0x + (yStart - p0y) * xSlope
        for (let y = yStart; y < yBottom; y += 1) {
          // Each row's x from the line's start, never by adding the slope up, against accumulated error.
          const rowBottomX = bottomY < y + 1 ? bottomX : p0x + (y + 1 - p0y) * xSlope
          pushRowExtents(y, rmin(rowTopX, rowBottomX), rmax(rowTopX, rowBottomX), wStartBase, wEndBase, W)
          rowTopX = rowBottomX
        }
      }
    } else {
      if (!startCulled) {
        const mask = yTop >= topY ? W : 0
        pushRow(yTop, topY, rmin(yTop + 1, bottomY), wStartBase & mask, wEndBase & mask, W & mask)
      }
      for (let y = startCulled ? yTop : yTop + 1; y < yBottom; y += 1) {
        pushRow(y, y, rmin(y + 1, bottomY), wStartBase, wEndBase, W)
      }
    }
  }
}

/**
 * Sorts the tiles by (y, x), ties in generation order. Vello sorts by (y, x, line, mask), and one line makes at
 * most one tile per location, so generation order is the same tie order. One numeric Float64Array sort of
 * `(y * 2^15 + x) * 2^24 + index`, exact in 53 bits.
 */
export function sortTiles(t: Tiles): void {
  const n = t.count
  if (n >= MAX_TILES) {
    throw new Error(`a path made ${n} tiles, the sort holds fewer than ${MAX_TILES}`)
  }
  if (t.keys.length < n) {
    t.keys = new Float64Array(2 * n)
    t.order = new Uint32Array(2 * n)
  }
  const keys = t.keys.subarray(0, n)
  for (let i = 0; i < n; i += 1) {
    const xy = t.data[2 * i]!
    keys[i] = ((xy >>> 16) * 32768 + (xy & 0xffff)) * INDEX_SCALE + i
  }
  keys.sort()
  for (let i = 0; i < n; i += 1) {
    t.order[i] = keys[i]! % INDEX_SCALE
  }
}
