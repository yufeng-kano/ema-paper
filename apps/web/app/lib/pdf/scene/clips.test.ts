import { describe, expect, it } from 'vitest'
import { ClipInterner, pathKey } from './clips.ts'
import { EMPTY_BOX } from './matrix.ts'

// `0 x y` moveTo, `1 x y` lineTo, `4` closePath: pdf.js's DrawOPS stream for a rectangle.
const SQUARE = [0, 0, 0, 1, 10, 0, 1, 10, 10, 1, 0, 10, 4]
const WIDE = [0, 0, 0, 1, 50, 0, 1, 50, 5, 1, 0, 5, 4]

describe('a path’s content key', () => {
  it('is the same for the same values wherever they sit', () => {
    expect(pathKey([9, 9, ...SQUARE], 2, SQUARE.length)).toBe(pathKey(Float32Array.from(SQUARE), 0, SQUARE.length))
  })

  it('differs for another path, and for the same values cut shorter', () => {
    expect(pathKey(SQUARE, 0, SQUARE.length)).not.toBe(pathKey(WIDE, 0, WIDE.length))
    expect(pathKey(SQUARE, 0, SQUARE.length)).not.toBe(pathKey(SQUARE, 0, SQUARE.length - 1))
  })

  it('reads the values as Float32, as the scene stores them', () => {
    expect(pathKey([0.1], 0, 1)).toBe(pathKey(Float32Array.of(0.1), 0, 1))
  })
})

describe('interning clip nodes', () => {
  it('gives the same stack, interned twice, one id', () => {
    const clips = new ClipInterner()
    const key = pathKey(SQUARE, 0, SQUARE.length)
    const outer = clips.intern(-1, 0, false, 0, 13, 0, key, [0, 0, 10, 10])
    // The second copy of the path sits elsewhere in the pool; the content is what counts.
    expect(clips.intern(-1, 0, false, 13, 13, 0, key, [0, 0, 10, 10])).toBe(outer)
    const inner = clips.intern(outer, 1, false, 0, 4, 1, 'glyphs', [2, 2, 8, 8])
    expect(clips.intern(outer, 1, false, 0, 4, 1, 'glyphs', [2, 2, 8, 8])).toBe(inner)
    expect(clips.columns().parent).toEqual(Int32Array.of(-1, outer))
  })

  it('gives the same path under another parent, rule or matrix another id', () => {
    const clips = new ClipInterner()
    const key = pathKey(SQUARE, 0, SQUARE.length)
    const a = clips.intern(-1, 0, false, 0, 13, 0, key, [0, 0, 10, 10])
    const b = clips.intern(-1, 0, false, 0, 13, 0, pathKey(WIDE, 0, WIDE.length), [0, 0, 50, 5])
    const ids = [
      a,
      b,
      clips.intern(a, 0, false, 0, 13, 0, key, [0, 0, 10, 10]),
      clips.intern(b, 0, false, 0, 13, 0, key, [0, 0, 10, 10]),
      clips.intern(-1, 0, true, 0, 13, 0, key, [0, 0, 10, 10]),
      clips.intern(-1, 0, false, 0, 13, 1, key, [0, 0, 10, 10]),
      clips.intern(-1, 1, false, 0, 13, 0, key, [0, 0, 10, 10]),
    ]
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('keeps the first range it was given, and the rule and matrix', () => {
    const clips = new ClipInterner()
    const id = clips.intern(-1, 0, true, 26, 13, 3, 'k', [0, 0, 1, 1])
    clips.intern(-1, 0, true, 39, 13, 3, 'k', [0, 0, 1, 1])
    const c = clips.columns()
    expect([c.kind[id], c.evenOdd[id], c.d0[id], c.d1[id], c.xform[id]]).toEqual([0, 1, 26, 13, 3])
    expect(c.parent.length).toBe(1)
  })
})

describe('a clip node’s box', () => {
  it('is null for no clip', () => {
    expect(new ClipInterner().box(-1)).toBeNull()
  })

  it('is its own box at the top, and the intersection with its parent’s below', () => {
    const clips = new ClipInterner()
    const outer = clips.intern(-1, 0, false, 0, 13, 0, 'outer', [0, 0, 100, 50])
    expect(clips.box(outer)).toEqual([0, 0, 100, 50])
    const inner = clips.intern(outer, 0, false, 13, 13, 0, 'inner', [80, 40, 200, 200])
    expect(clips.box(inner)).toEqual([80, 40, 100, 50])
    expect(Array.from(clips.columns().box)).toEqual([0, 0, 100, 50, 80, 40, 100, 50])
  })

  it('gets an id with an empty box when nothing is left, and stays empty below', () => {
    const clips = new ClipInterner()
    const outer = clips.intern(-1, 0, false, 0, 13, 0, 'outer', [0, 0, 10, 10])
    const apart = clips.intern(outer, 0, false, 13, 13, 0, 'apart', [20, 20, 30, 30])
    expect(apart).toBeGreaterThan(outer)
    expect(clips.box(apart)).toEqual([...EMPTY_BOX])
    const below = clips.intern(apart, 0, false, 26, 13, 0, 'below', [0, 0, 10, 10])
    expect(clips.box(below)).toEqual([...EMPTY_BOX])
  })

  it('grows past its first capacity', () => {
    const clips = new ClipInterner()
    let parent = -1
    for (let i = 0; i < 200; i++) parent = clips.intern(parent, 0, false, i, 1, 0, `p${i}`, [0, 0, 1000 - i, 1000 - i])
    expect(parent).toBe(199)
    const c = clips.columns()
    expect(c.parent.length).toBe(200)
    expect(c.parent[199]).toBe(198)
    expect(Array.from(c.box.subarray(199 * 4))).toEqual([0, 0, 801, 801])
  })
})
