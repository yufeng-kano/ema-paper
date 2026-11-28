import { describe, expect, it } from 'vitest'
import type { Mat6 } from '../scene/matrix.ts'
import { composeList, mipChain, patternTexture, type Texture } from './compose.ts'
import { DATA_ROW_BYTES, DATA_TEXTURE_WIDTH, Filter, NO_SLOT, PAINT_TYPE_SHIFT, PaintType, type DrawList } from './protocol.ts'

/** A draw list written out by hand: instances as 6 words, runs as 4, paints as (UV map, z, level). */
function list(width: number, height: number, instances: number[][], runs: number[][],
  paints: [Mat6, number, number][] = [], alphaBytes: number[] = []): DrawList {
  const alphas = new Uint8Array(DATA_ROW_BYTES)
  alphas.set(alphaBytes)
  const words = new Uint32Array(4 * DATA_TEXTURE_WIDTH)
  const floats = new Float32Array(words.buffer)
  for (const [i, [uv, z, lod]] of paints.entries()) {
    floats.set(uv, 8 * i)
    words[8 * i + 6] = z
    floats[8 * i + 7] = lod
  }
  return { width, height, instances: new Uint32Array(instances.flat()), alphas, paints: words, runs: new Uint32Array(runs.flat()) }
}

/** One 4-px instance row covering x0..x0 + width at y 0, filled (no alpha column). */
const filled = (x0: number, width: number, payload: number, paint = 0) => [x0, width, 0, payload, paint, 0]

describe('the CPU compositor', () => {
  it('rounds once per layer, as the GPU blends into an 8-bit target', () => {
    // Premultiplied r g b a = 150, 0, 0, 200, at coverage 100/255, over white:
    // r = floor(255 * (100/255 * 150/255 + 1 * (1 - 100/255 * 200/255)) + 0.5) = floor(235.39 + 0.5) = 235,
    // g = b = floor(176.57 + 0.5) = 177. Rounding the source first, then the blend, would give r = 236.
    const colour = 150 | 200 << 24
    const once = composeList(list(4, 4, [[0, 4 | 4 << 16, 0, colour, 0, 0]], [[0, 1, NO_SLOT, 0]], [], new Array(16).fill(100)), [])
    expect(Array.from(once.subarray(0, 4))).toEqual([235, 177, 177, 255])
    // The same colour again in a filled gap: r = floor(150 + 235 * 55/255 + 0.5) = 201, g = floor(177 * 55/255 + 0.5) = 38.
    const twice = composeList(list(4, 4, [[0, 4 | 4 << 16, 0, colour, 0, 0], filled(0, 4, colour)], [[0, 2, NO_SLOT, 0]], [],
      new Array(16).fill(100)), [])
    expect(Array.from(twice.subarray(0, 4))).toEqual([201, 38, 38, 255])
    // Outside every instance the target stays white.
    expect(Array.from(composeList(list(8, 4, [filled(0, 4, colour)], [[0, 1, NO_SLOT, 0]]), []).subarray(16, 20))).toEqual([255, 255, 255, 255])
  })

  it('builds the mip chain to 1 x 1 with max(1, size >> level), each 2 x 2 average rounded', () => {
    expect(mipChain(patternTexture(13, 6, 1)).map(({ width, height }) => [width, height])).toEqual([[13, 6], [6, 3], [3, 1], [1, 1]])
    expect(mipChain(patternTexture(1, 5, 2)).map(({ width, height }) => [width, height])).toEqual([[1, 5], [1, 2], [1, 1]])
    const square: Texture = { width: 2, height: 2, data: Uint8Array.of(10, 1, 0, 255, 20, 2, 0, 255, 30, 3, 0, 255, 41, 4, 0, 255) }
    // (10 + 20 + 30 + 41) / 4 = 25.25 and (1 + 2 + 3 + 4) / 4 = 2.5, rounded to nearest.
    expect(Array.from(mipChain(square)[1]!.data)).toEqual([25, 3, 0, 255])
  })

  it('samples bilinearly at a texel centre exactly that texel, and the level a paint names', () => {
    const chain = mipChain(patternTexture(4, 4, 3))
    const image = PaintType.Image << PAINT_TYPE_SHIFT
    // u = px / 4 and v = py / 4: each pixel centre is a texel centre.
    const uv: Mat6 = [0.25, 0, 0, 0.25, 0, 0]
    const draw = (filter: number, lod: number) => composeList(list(4, 4, [filled(0, 4, 0, image)], [[0, 1, 0, filter]], [[uv, 255, lod]]), [chain])
    const linear = draw(Filter.Linear, 0)
    expect(linear).toEqual(draw(Filter.Nearest, 0))
    for (let i = 0; i < 16; i += 1) {
      const texel = chain[0]!.data.subarray(4 * i, 4 * i + 4)
      for (let channel = 0; channel < 3; channel += 1) {
        expect(linear[4 * i + channel]).toBe(Math.floor(255 * (texel[channel]! / 255 + 1 - texel[3]! / 255) + 0.5))
      }
    }
    // Level 1 is 2 x 2: the top left four pixels all show its first texel.
    const level = chain[1]!.data
    const one = draw(Filter.Nearest, 1)
    for (const i of [0, 1, 4, 5]) {
      expect(one[4 * i]).toBe(Math.floor(255 * (level[0]! / 255 + 1 - level[3]! / 255) + 0.5))
    }
  })

  it('clamps nearest sampling to the edge texels, and scales images by opacity and masks by their colour', () => {
    const texture: Texture = { width: 2, height: 1, data: Uint8Array.of(255, 0, 0, 255, 0, 0, 128, 128) }
    const image = PaintType.Image << PAINT_TYPE_SHIFT
    // u = (px - 2) / 4: pixels 0 and 1 lie left of the image, 6 and 7 right of it.
    const uv: Mat6 = [0.25, 0, 0, 0, -0.5, 0.5]
    const edges = composeList(list(8, 4, [filled(0, 8, 0, image)], [[0, 1, 0, Filter.Nearest]], [[uv, 255, 0]]), [[texture]])
    const reds = Array.from({ length: 8 }, (_, x) => edges[4 * x])
    expect(reds).toEqual([255, 255, 255, 255, 127, 127, 127, 127])
    expect(edges[4 * 7 + 2]).toBe(255)

    // Opacity 102/255 = 0.4 on the opaque red texel: g = floor(255 * (1 - 0.4) + 0.5) = 153.
    const faint = composeList(list(4, 4, [filled(0, 4, 0, image)], [[0, 1, 0, Filter.Nearest]], [[[0, 0, 0, 0, 0.25, 0.5], 102, 0]]), [[texture]])
    expect(Array.from(faint.subarray(0, 4))).toEqual([255, 153, 153, 255])
    // A mask paints its colour (premultiplied 0, 0, 200, 200) times the texel's alpha, 128/255 here:
    // r = floor(255 * (1 - 200/255 * 128/255) + 0.5) = floor(154.6 + 0.5) = 155, b = floor(255 * (200/255 * 128/255) + 154.6 + 0.5) = 255.
    const mask = PaintType.Mask << PAINT_TYPE_SHIFT
    const masked = composeList(list(4, 4, [filled(0, 4, 0, mask)], [[0, 1, 0, Filter.Nearest]], [[[0, 0, 0, 0, 0.75, 0.5], 0xc8c8_0000, 0]]), [[texture]])
    expect(Array.from(masked.subarray(0, 4))).toEqual([155, 155, 255, 255])
  })

  it('skips the runs of a slot without a texture', () => {
    const image = PaintType.Image << PAINT_TYPE_SHIFT
    const drawn = composeList(list(4, 4, [filled(0, 4, 0, image), filled(0, 4, 0xff00_00ff)], [[0, 1, 0, 0], [1, 1, NO_SLOT, 0]],
      [[[0, 0, 0, 0, 0.5, 0.5], 255, 0]]), [null])
    expect(Array.from(drawn.subarray(0, 4))).toEqual([255, 0, 0, 255])
  })
})
