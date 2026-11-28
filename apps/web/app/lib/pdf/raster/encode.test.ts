import { describe, expect, it } from 'vitest'
import { div255, ListBuilder, premulRgba8 } from './encode.ts'
import {
  DATA_ROW_BYTES, DATA_TEXTURE_WIDTH, Filter, INSTANCE_WORDS, MAX_DATA_ROWS, NO_SLOT, PAINT_TYPE_SHIFT, PaintType, RUN_WORDS,
} from './protocol.ts'
import { SENTINEL_X, Strips } from './strips.ts'

/** Strips from [x, y, tiles, fillGap] in order, each tile's 16 bytes counting up from `firstByte`, then the sentinel. */
function stripsOf(entries: [number, number, number, boolean][], firstByte = 1): Strips {
  const strips = new Strips()
  let byte = firstByte
  for (const [x, y, tiles, fillGap] of entries) {
    strips.push(x, y, strips.alphaCount, fillGap)
    const at = strips.reserveAlphas(16 * tiles)
    for (let i = 0; i < 16 * tiles; i += 1) {
      strips.alphas[at + i] = byte++ & 0xff
    }
  }
  strips.push(SENTINEL_X, entries.at(-1)![1], strips.alphaCount, false)
  return strips
}

function instancesOf(words: Uint32Array): number[][] {
  const out: number[][] = []
  for (let i = 0; i < words.length; i += INSTANCE_WORDS) {
    out.push(Array.from(words.subarray(i, i + INSTANCE_WORDS)))
  }
  return out
}

describe('the draw list builder', () => {
  it('makes one instance per alpha segment and per filled gap, dense = width and dense = 0', () => {
    // A strip of 2 tiles at x 8, then a filled gap to a strip of 1 tile at x 24, in the row at y 4.
    const strips = stripsOf([[8, 4, 2, false], [24, 4, 1, true]])
    const builder = new ListBuilder(64, 16)
    builder.strips(strips, 0, strips.count, 0x1122_3344, 0, NO_SLOT, Filter.Nearest)
    const { instances } = builder.finish()
    expect(instancesOf(instances)).toEqual([
      [8 | 4 << 16, 8 | 8 << 16, 0, 0x1122_3344, 0, 0],
      [16 | 4 << 16, 8, 0, 0x1122_3344, 0, 0],
      [24 | 4 << 16, 4 | 4 << 16, 8, 0x1122_3344, 0, 0],
    ])
  })

  it('clips the instances to the target’s tiles, and gives an image paint each instance’s own position', () => {
    const strips = stripsOf([[8, 4, 2, false], [24, 4, 1, true], [0, 16, 1, false]])
    // 18 px wide: 5 tile columns, the last one in part; 3 tile rows, so the row at y 16 is outside.
    const builder = new ListBuilder(18, 12)
    const paint = PaintType.Image << PAINT_TYPE_SHIFT | 6
    builder.strips(strips, 0, strips.count, 0x1122_3344, paint, 0, Filter.Linear)
    expect(instancesOf(builder.finish().instances)).toEqual([
      [8 | 4 << 16, 8 | 8 << 16, 0, 8 | 4 << 16, paint, 0],
      [16 | 4 << 16, 4, 0, 16 | 4 << 16, paint, 0],
    ])
  })

  it('copies the coverage bytes so that the shader’s addressing finds each again', () => {
    const tiles = [3, 2, 2000, 2000, 200]
    const strips = stripsOf([[0, 0, 3, false], [20, 0, 2, false], [8, 8, 2000, false], [0, 12, 2000, false], [0, 16, 200, false]], 7)
    const builder = new ListBuilder(8192, 20)
    // The first strip alone first, so that the second copy of its bytes lands at other columns than its source.
    builder.strips(strips, 0, 2, 0, 0, NO_SLOT, 0)
    builder.strips(strips, 0, strips.count, 0, 0, NO_SLOT, 0)
    const { instances, alphas } = builder.finish()
    // Over 4096 tiles of bytes: the second texture row is in use.
    expect(alphas.length).toBe(2 * DATA_ROW_BYTES)
    const texels = new Uint32Array(alphas.buffer)
    const source = tiles.map((_, i) => 16 * tiles.slice(0, i).reduce((sum, n) => sum + n, 0))
    for (const [n, strip] of [0, 0, 1, 2, 3, 4].entries()) {
      const column = instances[INSTANCE_WORDS * n + 2]!
      const dense = instances[INSTANCE_WORDS * n + 1]! >>> 16
      expect(dense).toBe(4 * tiles[strip]!)
      for (let c = 0; c < dense; c += 1) {
        for (let y = 0; y < 4; y += 1) {
          // The shader: texel (col + c) >> 2 at (texel & 4095, texel >> 12), channel (col + c) & 3, byte y.
          const index = column + c
          const texel = index >> 2
          const word = texels[4 * ((texel >> 12) * DATA_TEXTURE_WIDTH + (texel & 4095)) + (index & 3)]!
          expect((word >>> (8 * y)) & 0xff).toBe(strips.alphas[source[strip]! + 4 * c + y])
        }
      }
    }
  })

  it('breaks runs exactly where the texture slot or the filter changes, never for an empty call', () => {
    const strips = stripsOf([[0, 0, 1, false]])
    const empty = new Strips()
    const builder = new ListBuilder(64, 64)
    const image = PaintType.Image << PAINT_TYPE_SHIFT
    const calls: [number, number, number][] = [
      [0, NO_SLOT, 0], [0, NO_SLOT, 0], [image, 0, Filter.Nearest], [image, 0, Filter.Nearest], [image, 0, Filter.Linear],
      [image, 1, Filter.Linear], [0, NO_SLOT, 0],
    ]
    for (const [paint, slot, filter] of calls) {
      builder.strips(strips, 0, strips.count, 0, paint, slot, filter)
      builder.strips(empty, 0, 0, 0, paint, 2, filter)
    }
    const { runs } = builder.finish()
    expect(Array.from(runs)).toEqual([0, 2, NO_SLOT, 0, 2, 2, 0, 0, 4, 1, 0, 1, 5, 1, 1, 1, 6, 1, NO_SLOT, 0])
    expect(runs.length % RUN_WORDS).toBe(0)
  })

  it('lays out a paint as two texels: the UV map’s f32 bits, then e, f, z and the level', () => {
    const builder = new ListBuilder(64, 64)
    expect(builder.imagePaint([0.5, -0.25, 1 / 3, 2, -7.125, 0.1], 0x8040_20ff, 0)).toBe(0)
    expect(builder.imagePaint([1, 0, 0, 1, 0, 0], 200, 3)).toBe(2)
    const { paints } = builder.finish()
    const floats = new Float32Array(paints.buffer)
    expect(Array.from(floats.subarray(0, 6))).toEqual([0.5, -0.25, Math.fround(1 / 3), 2, -7.125, Math.fround(0.1)])
    expect(paints[6]).toBe(0x8040_20ff)
    expect(floats[7]).toBe(0)
    expect(paints[14]).toBe(200)
    expect(floats[15]).toBe(3)
  })

  it('pads both data textures to whole rows, at least one, in fresh arrays of their own', () => {
    const builder = new ListBuilder(8, 8)
    const first = builder.finish()
    expect(first.alphas.length).toBe(DATA_ROW_BYTES)
    expect(first.paints.length).toBe(4 * DATA_TEXTURE_WIDTH)
    expect(first.instances.length).toBe(0)
    expect(first.runs.length).toBe(0)
    builder.strips(stripsOf([[0, 0, 1, false]]), 0, 2, 0, 0, NO_SLOT, 0)
    const second = builder.finish()
    expect(second.instances.length).toBe(INSTANCE_WORDS)
    expect(second.alphas.length).toBe(DATA_ROW_BYTES)
    const buffers = new Set([first, second].flatMap((list) => [list.instances, list.alphas, list.paints, list.runs].map((array) => array.buffer)))
    expect(buffers.size).toBe(8)
  })

  it(`refuses a list whose coverage bytes need more than ${MAX_DATA_ROWS} texture rows`, () => {
    // One row of strips across 8192 px holds 32 KiB of coverage; 8193 of them pass 256 MiB by one row's worth.
    const strips = stripsOf([[0, 0, 2048, false]])
    const builder = new ListBuilder(8192, 4)
    for (let i = 0; i < 8192; i += 1) {
      builder.strips(strips, 0, strips.count, 0, 0, NO_SLOT, 0)
    }
    builder.strips(strips, 0, strips.count, 0, 0, NO_SLOT, 0)
    expect(() => builder.finish()).toThrow(/texture rows/)
  })
})

describe('the colour helpers', () => {
  it('premultiply with r in the low byte', () => {
    // r g b a = 0, 0, 128, 128.
    expect(premulRgba8(0x00_00ff, 0.5)).toBe(0x8080_0000)
    expect(premulRgba8(0xff_8040, 1)).toBe(0xff40_80ff)
    expect(premulRgba8(0x12_3456, 0)).toBe(0)
  })

  it('divide by 255 with exact rounding over 0..65025', () => {
    for (let x = 0; x <= 65_025; x += 1) {
      if (div255(x) !== Math.round(x / 255)) {
        expect(div255(x), String(x)).toBe(Math.round(x / 255))
      }
    }
  })
})
