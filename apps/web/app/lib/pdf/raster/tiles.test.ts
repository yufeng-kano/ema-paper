import { describe, expect, it } from 'vitest'
import { Lines } from './buffers.ts'
import { makeTiles, sortTiles, Tiles } from './tiles.ts'

const WINDING = 0b10000

function tilesOf(t: Tiles): [x: number, y: number, line: number, mask: number][] {
  const out: [number, number, number, number][] = []
  for (let i = 0; i < t.count; i += 1) {
    const xy = t.data[2 * i]!
    const word = t.data[2 * i + 1]!
    out.push([xy & 0xffff, xy >>> 16, word >>> 5, word & 0b11111])
  }
  return out
}

function lines(...coordinates: number[][]): Lines {
  const out = new Lines()
  for (const [x0, y0, x1, y1] of coordinates) {
    out.push(x0!, y0!, x1!, y1!)
  }
  return out
}

describe('the tiler', () => {
  it('gives a line one tile per row it crosses, with the winding bit where it crosses a tile’s top', () => {
    const t = new Tiles()
    makeTiles(lines([5, 1, 5, 9], [9, 6, 15, 7]), 64, 48, t)
    expect(tilesOf(t)).toEqual([[1, 0, 0, 0], [1, 1, 0, WINDING], [1, 2, 0, WINDING], [2, 1, 1, 0], [3, 1, 1, 0]])
    expect(t.culled).toBe(false)
  })

  it('keeps a line left of the target as winding of its rows only, and resets that on the next path', () => {
    const t = new Tiles()
    // Downwards, so -1, from pixel row 2 to 14: rows 1 and 2 whole, the first and the last in part.
    makeTiles(lines([-3, 2, -3, 14]), 64, 48, t)
    expect(t.count).toBe(0)
    expect(t.culled).toBe(true)
    expect(Array.from(t.coarse.subarray(0, 5))).toEqual([0, -1, -1, -1, 0])
    expect(Array.from(t.active.subarray(0, 5))).toEqual([1, 1, 1, 1, 0])
    // Relative to each row's coarse winding: the last row's coarse -1 is undone below the line's end.
    expect(Array.from(t.partial.subarray(0, 16))).toEqual([0, 0, -1, -1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1])

    makeTiles(lines([5, 1, 5, 9]), 64, 48, t)
    expect(t.culled).toBe(false)
    expect(t.coarse.every((winding) => winding === 0)).toBe(true)
    expect(t.partial.every((winding) => winding === 0)).toBe(true)
    expect(t.active.every((row) => row === 0)).toBe(true)
  })

  it('tiles only the visible part of a line that crosses the left edge', () => {
    const t = new Tiles()
    makeTiles(lines([-10, 1, 10, 11]), 64, 48, t)
    expect(t.culled).toBe(true)
    expect(t.active[0]).toBe(1)
    expect(t.active[1]).toBe(1)
    expect(tilesOf(t).map(([x, y]) => `${x},${y}`)).toEqual(['0,1', '1,1', '1,2', '2,2'])
  })

  it('drops lines right of, above and below the target, and horizontal lines on a tile row boundary', () => {
    const t = new Tiles()
    makeTiles(lines([70, 5, 80, 20], [5, -10, 20, -2], [5, 50, 20, 60], [2, 8, 30, 8]), 64, 48, t)
    expect(t.count).toBe(0)
    expect(t.culled).toBe(false)
  })

  it('sorts by row, then column, ties in the order the tiles were made', () => {
    const t = new Tiles()
    const at = [[3, 1], [0, 2], [3, 1], [1, 1], [16383, 0], [0, 16383], [2, 1]]
    for (const [i, [x, y]] of at.entries()) {
      t.data[2 * i] = (y! << 16 | x!) >>> 0
      t.data[2 * i + 1] = i << 5
    }
    t.count = at.length
    sortTiles(t)
    expect(Array.from(t.order.subarray(0, t.count))).toEqual([4, 3, 6, 0, 2, 1, 5])
  })

  it('refuses 2^24 tiles, which the sort key cannot index', () => {
    const t = new Tiles()
    t.count = 2 ** 24
    expect(() => sortTiles(t)).toThrow(/tiles/)
  })
})
