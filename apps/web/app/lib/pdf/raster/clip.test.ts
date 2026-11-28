import { describe, expect, it } from 'vitest'
import type { PageScene } from '../scene/scene.ts'
import { Lines } from './buffers.ts'
import { ClipCache, intersectStrips, type StripRun } from './clip.ts'
import { composeList } from './compose.ts'
import { ListBuilder } from './encode.ts'
import { NO_SLOT } from './protocol.ts'
import { renderStrips, Strips } from './strips.ts'
import { makeTiles, sortTiles, Tiles } from './tiles.ts'

const WIDTH = 64
const HEIGHT = 48

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

const tiles = new Tiles()

/** The strips of closed polygons, appended to `store`. */
function rasterise(polygons: readonly (readonly number[])[], evenOdd: boolean, store: Strips): StripRun {
  const lines = new Lines()
  for (const points of polygons) {
    for (let i = 0; i < points.length; i += 2) {
      const j = (i + 2) % points.length
      lines.push(points[i]!, points[i + 1]!, points[j]!, points[j + 1]!)
    }
  }
  makeTiles(lines, WIDTH, HEIGHT, tiles)
  sortTiles(tiles)
  const start = store.count
  renderStrips(tiles, lines, evenOdd, store)
  return { store, start, end: store.count }
}

/** A run's coverage per pixel, drawn in opaque black over white. */
function coverage(run: StripRun): Uint8Array {
  const builder = new ListBuilder(WIDTH, HEIGHT)
  builder.strips(run.store, run.start, run.end, 0xff00_0000, 0, NO_SLOT, 0)
  const rgba = composeList(builder.finish(), [])
  const out = new Uint8Array(WIDTH * HEIGHT)
  for (let i = 0; i < out.length; i += 1) {
    out[i] = 255 - rgba[4 * i]!
  }
  return out
}

/** A seeded polygon around and across the target's edges, sometimes with a rectangle on the tile grid. */
function seededPolygons(next: () => number): number[][] {
  const point = () => [next() * 120 - 40, next() * 90 - 25]
  const polygon = point()
  const vertices = 3 + Math.floor(next() * 5)
  for (let v = 0; v < vertices; v += 1) {
    polygon.push(...point())
  }
  const polygons = [polygon]
  if (next() < 0.3) {
    const x = 4 * Math.floor(next() * 16)
    const y = 4 * Math.floor(next() * 12)
    polygons.push([x, y, x + 16, y, x + 16, y + 12, x, y + 12])
  }
  return polygons
}

const rectangle = (x0: number, y0: number, x1: number, y1: number) => [[x0, y0, x1, y0, x1, y1, x0, y1]]

describe('the strip intersection', () => {
  it('multiplies the coverage of 600 seeded pairs exactly as (a * b + 255) >> 8', () => {
    const next = random(7_031_964)
    let differences = 0
    let met = 0
    for (let trial = 0; trial < 600; trial += 1) {
      const a = rasterise(seededPolygons(next), next() < 0.5, new Strips())
      const b = rasterise(seededPolygons(next), next() < 0.5, new Strips())
      const out = new Strips()
      intersectStrips(a, b, out)
      met += out.count > 0 ? 1 : 0
      const got = coverage({ store: out, start: 0, end: out.count })
      const coverageA = coverage(a)
      const coverageB = coverage(b)
      for (let i = 0; i < got.length; i += 1) {
        if (got[i] !== (coverageA[i]! * coverageB[i]! + 255) >> 8) {
          differences += 1
        }
      }
    }
    expect(differences).toBe(0)
    // Most pairs meet (559 of 600 with this seed), so the product is tested, not the empty case.
    expect(met).toBeGreaterThan(500)
  })

  it('appends nothing, not even a sentinel, when the two do not meet', () => {
    const out = new Strips()
    // Apart in rows, and in the same rows apart in columns.
    intersectStrips(rasterise(rectangle(2, 2, 10, 10), false, new Strips()), rasterise(rectangle(30, 30, 40, 40), false, new Strips()), out)
    intersectStrips(rasterise(rectangle(2, 2, 10, 10), false, new Strips()), rasterise(rectangle(30, 2, 40, 10), false, new Strips()), out)
    intersectStrips(rasterise(rectangle(2, 2, 10, 10), false, new Strips()), { store: new Strips(), start: 0, end: 0 }, out)
    expect(out.count).toBe(0)
    expect(out.alphaCount).toBe(0)
  })

  it('can write into the store of the run it reads, past its capacity', () => {
    const store = new Strips()
    const a = rasterise(rectangle(-20, -20, 100, 100), false, store)
    const b = rasterise([[3.5, 1.25, 60.75, 9.5, 20.25, 46.5]], false, store)
    const separate = new Strips()
    intersectStrips(a, b, separate)
    // Full to the last strip, so that the intersection's first push moves the store's arrays.
    while (2 * (store.count + 1) <= store.data.length) {
      store.push(0, 0, store.alphaCount, false)
    }
    const capacity = store.data.length
    const start = store.count
    intersectStrips(a, b, store)
    expect(store.data.length).toBeGreaterThan(capacity)
    expect(coverage({ store, start, end: store.count })).toEqual(coverage({ store: separate, start: 0, end: separate.count }))
  })
})

describe('the clip cache', () => {
  /** Clip nodes 0 (a rectangle), 1 (0 ∩ a triangle), 2 (1 ∩ a rectangle beside it: nothing) and 3 (2 ∩ anything). */
  const shapes = [rectangle(4, 4, 40, 40), [[0, 0, 60, 20, 10, 46]], rectangle(44, 0, 60, 48), rectangle(0, 0, 64, 48)]
  const scene = {
    clips: {
      parent: Int32Array.of(-1, 0, 1, 2, -1),
      kind: Uint8Array.of(0, 0, 0, 0, 1),
      evenOdd: Uint8Array.of(0, 0, 0, 0, 0),
      d0: Int32Array.of(0, 1, 2, 3, 0),
      d1: Int32Array.of(1, 2, 3, 4, 0),
      xform: Int32Array.of(0, 0, 0, 0, 0),
    },
  } as unknown as PageScene

  it('builds each chain once, parent ∩ own, and gives null when it clips everything', () => {
    const asked: number[] = []
    const cache = new ClipCache(scene, (pathStart, _end, _xform, evenOdd, out) => {
      asked.push(pathStart)
      const run = rasterise(shapes[pathStart]!, evenOdd, out)
      return run.end > run.start ? run : null
    })
    const one = cache.get(1)
    expect(one).not.toBeNull()
    const own = coverage(rasterise(shapes[1]!, false, new Strips()))
    const parent = coverage(rasterise(shapes[0]!, false, new Strips()))
    expect(coverage(one!)).toEqual(own.map((value, i) => (value * parent[i]! + 255) >> 8))
    expect(cache.get(1)).toBe(one)
    expect(cache.get(0)).not.toBeNull()
    expect(asked).toEqual([0, 1])

    expect(cache.get(2)).toBeNull()
    // Under a chain that clips everything, the own path is never rasterised.
    expect(cache.get(3)).toBeNull()
    expect(asked).toEqual([0, 1, 2])

    expect(() => cache.get(4)).toThrow(/text clip/)
    cache.clear()
    cache.get(1)
    expect(asked).toEqual([0, 1, 2, 0, 1])
  })
})
