import { describe, expect, it } from 'vitest'
import { Lines } from './buffers.ts'
import { composeList } from './compose.ts'
import { ListBuilder } from './encode.ts'
import { NO_SLOT } from './protocol.ts'
import { renderStrips, SENTINEL_X, Strips } from './strips.ts'
import { makeTiles, sortTiles, Tiles } from './tiles.ts'

const WIDTH = 64
const HEIGHT = 48
const BLACK = 0xff00_0000

function random(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b_79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296
  }
}

/** Each closed polygon's edges, its last point joined to its first. */
function polygonLines(polygons: readonly (readonly number[])[], dx = 0, dy = 0): Lines {
  const lines = new Lines()
  for (const points of polygons) {
    for (let i = 0; i < points.length; i += 2) {
      const j = (i + 2) % points.length
      lines.push(points[i]! + dx, points[i + 1]! + dy, points[j]! + dx, points[j + 1]! + dy)
    }
  }
  return lines
}

/** The pipeline's coverage per pixel: tiles, strips, the draw list in opaque black, composited over white. */
function coverage(lines: Lines, width: number, height: number, evenOdd: boolean): Uint8Array {
  const tiles = new Tiles()
  makeTiles(lines, width, height, tiles)
  sortTiles(tiles)
  const strips = new Strips()
  renderStrips(tiles, lines, evenOdd, strips)
  const builder = new ListBuilder(width, height)
  builder.strips(strips, 0, strips.count, BLACK, 0, NO_SLOT, 0)
  const rgba = composeList(builder.finish(), [])
  const out = new Uint8Array(width * height)
  for (let i = 0; i < out.length; i += 1) {
    out[i] = 255 - rgba[4 * i]!
  }
  return out
}

// 32 rows err by up to 5/255 where two nearly horizontal edges meet inside one pixel row; 64 keep the
// reference's own error well under the 4/255 the renderer is held to.
const SAMPLE_ROWS = 64

/**
 * The exact winding reference: the signed winding (up +1, down -1, as Vello counts it) averaged over each
 * pixel, from `sampleRows` sample rows per pixel row, each integrated exactly across x, then Vello's mapping,
 * before the byte store truncates it. Not an inside test: analytic coverage integrates the winding, so where
 * windings 2 and 0 share a pixel it reads 1.
 */
function reference(lines: Lines, width: number, height: number, evenOdd: boolean, sampleRows = SAMPLE_ROWS): Float64Array {
  const out = new Float64Array(width * height)
  const d = lines.data
  const area = new Float64Array(width)
  const crossings: [number, number][] = []
  for (let y = 0; y < height; y += 1) {
    area.fill(0)
    for (let s = 0; s < sampleRows; s += 1) {
      const sy = y + (s + 0.5) / sampleRows
      crossings.length = 0
      for (let i = 0; i < lines.count; i += 1) {
        const x0 = d[4 * i]!
        const y0 = d[4 * i + 1]!
        const x1 = d[4 * i + 2]!
        const y1 = d[4 * i + 3]!
        if ((y0 <= sy) !== (y1 <= sy)) {
          crossings.push([x0 + ((sy - y0) / (y1 - y0)) * (x1 - x0), y1 < y0 ? 1 : -1])
        }
      }
      crossings.sort((a, b) => a[0] - b[0])
      let winding = 0
      for (let k = 0; k + 1 < crossings.length; k += 1) {
        winding += crossings[k]![1]
        const from = Math.max(crossings[k]![0], 0)
        const to = Math.min(crossings[k + 1]![0], width)
        for (let x = Math.floor(from); x < to; x += 1) {
          area[x]! += (winding * (Math.min(to, x + 1) - Math.max(from, x))) / sampleRows
        }
      }
    }
    for (let x = 0; x < width; x += 1) {
      const a = area[x]!
      out[y * width + x] = evenOdd
        ? Math.min(255 * Math.abs(a - 2 * Math.floor(a * 0.5 + 0.5)) + 0.5, 255)
        : Math.min(Math.abs(a) * 255 + 0.5, 255)
    }
  }
  return out
}

/** Points on a 1/1024 grid, so that the f32 lines shift exactly. */
const snap = (v: number) => Math.round(v * 1024) / 1024

/**
 * A seeded shape: one polygon of lines and cubics (flattened here, finely and uniformly) around and across the
 * target's left, top and right edges, sometimes on the pixel grid, sometimes with a rectangle beside it.
 */
function seededShape(next: () => number): number[][] {
  const grid = next() < 0.3
  const point = (): [number, number] => {
    const x = next() * 144 - 40
    const y = next() * 98 - 25
    return grid ? [Math.round(x), Math.round(y)] : [snap(x), snap(y)]
  }
  let [x, y] = point()
  const polygon = [x, y]
  const vertices = 3 + Math.floor(next() * 6)
  for (let v = 0; v < vertices; v += 1) {
    if (next() < 0.25) {
      const [ax, ay] = point()
      const [bx, by] = point()
      const [ex, ey] = point()
      for (let k = 1; k <= 64; k += 1) {
        const t = k / 64
        const u = 1 - t
        polygon.push(snap(u * u * u * x + 3 * u * u * t * ax + 3 * u * t * t * bx + t * t * t * ex),
          snap(u * u * u * y + 3 * u * u * t * ay + 3 * u * t * t * by + t * t * t * ey))
      }
      ;[x, y] = [ex, ey]
    } else {
      ;[x, y] = point()
      polygon.push(x, y)
    }
  }
  const shape = [polygon]
  if (next() < 0.3) {
    const [rx, ry] = point()
    shape.push([rx, ry, rx + 10, ry, rx + 10, ry + 10, rx, ry + 10])
  }
  return shape
}

describe('the strip renderer', () => {
  it('matches the exact winding reference on 600 seeded shapes within 4/255, and shifted with nothing culled, the same bytes but at rounding ties', () => {
    const next = random(20_240_601)
    let worst = 0
    let ties = 0
    for (let trial = 0; trial < 600; trial += 1) {
      const shape = seededShape(next)
      const evenOdd = next() < 0.5
      const lines = polygonLines(shape)
      const got = coverage(lines, WIDTH, HEIGHT, evenOdd)
      const want = reference(lines, WIDTH, HEIGHT, evenOdd)
      for (let i = 0; i < got.length; i += 1) {
        worst = Math.max(worst, Math.abs(got[i]! - Math.floor(want[i]!)))
      }
      // Moved right and down by whole tiles into a target that holds all of it: no line is culled. The culled
      // windings and the tiles reach the same area by different sums, so where the exact value sits on a byte's
      // rounding boundary (a grid shape's 0.5, or 60.0 from 7/30) their last bits may round apart; nowhere else.
      const shifted = coverage(polygonLines(shape, 64, 32), 192, 128, evenOdd)
      let fine: Float64Array | null = null
      for (let y = 0; y < HEIGHT; y += 1) {
        for (let x = 0; x < WIDTH; x += 1) {
          const i = y * WIDTH + x
          const moved = shifted[(y + 32) * 192 + x + 64]!
          if (moved !== got[i]) {
            fine ??= reference(lines, WIDTH, HEIGHT, evenOdd, 4096)
            expect(Math.abs(moved - got[i]!), `trial ${trial} at ${x}, ${y}`).toBe(1)
            expect(Math.abs(fine[i]! - Math.round(fine[i]!)), `trial ${trial} at ${x}, ${y}`).toBeLessThan(0.05)
            ties += 1
          }
        }
      }
    }
    expect(worst).toBeLessThanOrEqual(4)
    // 11 of the 1,843,200 pixels with this seed.
    expect(ties).toBeLessThan(20)
  })

  it('gives only 255 and 0 for rectangles on the pixel grid, also across the edges and around the target', () => {
    const cases: [number, number, number, number][] = [[3, 5, 21, 14], [-7, -3, 10, 6], [50, 30, 90, 70], [-10, -10, 100, 100]]
    for (const [x0, y0, x1, y1] of cases) {
      const got = coverage(polygonLines([[x0, y0, x1, y0, x1, y1, x0, y1]]), WIDTH, HEIGHT, false)
      for (let y = 0; y < HEIGHT; y += 1) {
        for (let x = 0; x < WIDTH; x += 1) {
          expect(got[y * WIDTH + x], `${x0},${y0} ${x}, ${y}`).toBe(x >= x0 && x < x1 && y >= y0 && y < y1 ? 255 : 0)
        }
      }
    }
  })

  it('gives a fractional edge its exact area, rounded to the nearest byte', () => {
    // Binary fractions, so every area times 255 is exact: 0.5 gives 128 and 0.25 gives 64.
    const span = (lo: number, hi: number, at: number) => Math.max(0, Math.min(hi, at + 1) - Math.max(lo, at))
    const cases: [number, number, number, number][] = [[2.5, 1.75, 10.25, 6.5], [-5.5, 1.5, 10.5, 6.5], [40.75, -3, 70, 9.25]]
    for (const [x0, y0, x1, y1] of cases) {
      const got = coverage(polygonLines([[x0, y0, x1, y0, x1, y1, x0, y1]]), WIDTH, HEIGHT, false)
      for (let y = 0; y < HEIGHT; y += 1) {
        for (let x = 0; x < WIDTH; x += 1) {
          expect(got[y * WIDTH + x], `${x0},${y0} ${x}, ${y}`).toBe(Math.floor(span(x0, x1, x) * span(y0, y1, y) * 255 + 0.5))
        }
      }
    }
    const half = coverage(polygonLines([[2.5, 0, 10, 0, 10, 8, 2.5, 8]]), WIDTH, HEIGHT, false)
    expect(half[2]).toBe(128)
  })

  it('keeps the fill rules apart: two overlapping rectangles wound alike, and wound opposite', () => {
    const alike = polygonLines([[2, 2, 20, 2, 20, 10, 2, 10], [10, 4, 28, 4, 28, 14, 10, 14]])
    const opposite = polygonLines([[2, 2, 2, 10, 20, 10, 20, 2], [10, 4, 28, 4, 28, 14, 10, 14]])
    const overlap = 6 * WIDTH + 15
    const once = 3 * WIDTH + 5
    expect(coverage(alike, WIDTH, HEIGHT, false)[overlap]).toBe(255)
    expect(coverage(alike, WIDTH, HEIGHT, true)[overlap]).toBe(0)
    expect(coverage(opposite, WIDTH, HEIGHT, false)[overlap]).toBe(0)
    expect(coverage(opposite, WIDTH, HEIGHT, true)[overlap]).toBe(0)
    for (const evenOdd of [false, true]) {
      expect(coverage(alike, WIDTH, HEIGHT, evenOdd)[once]).toBe(255)
    }
  })

  it('ends its strips with one sentinel at the last strip’s row, and makes none for nothing', () => {
    const lines = polygonLines([[3, 5, 21, 5, 21, 14, 3, 14]])
    const tiles = new Tiles()
    makeTiles(lines, WIDTH, HEIGHT, tiles)
    sortTiles(tiles)
    const strips = new Strips()
    renderStrips(tiles, lines, false, strips)
    const last = strips.data[2 * (strips.count - 1)]!
    expect(last & 0xffff).toBe(SENTINEL_X)
    expect(last >>> 16).toBe(strips.data[2 * (strips.count - 2)]! >>> 16)
    expect(strips.data[2 * (strips.count - 1) + 1]).toBe(strips.alphaCount)

    const outside = polygonLines([[70, 5, 90, 5, 90, 14, 70, 14]])
    makeTiles(outside, WIDTH, HEIGHT, tiles)
    sortTiles(tiles)
    strips.clear()
    renderStrips(tiles, outside, false, strips)
    expect(strips.count).toBe(0)
  })
})
