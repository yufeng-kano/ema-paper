import { describe, expect, it } from 'vitest'
import type { Mat6 } from '../scene/matrix.ts'
import { mipLevel, mipLevelCount, singularValues, smoothing, textureSize } from './sampling.ts'

/** An image drawn as pdf.js draws it: device px per image px `s`, y flipped, somewhere on the page. */
function scaled(sx: number, sy = sx): Mat6 {
  return [sx, 0, 0, -sy, 40, 700]
}

function rotated(s: number, radians: number): Mat6 {
  return [s * Math.cos(radians), s * Math.sin(radians), -s * Math.sin(radians), s * Math.cos(radians), 0, 0]
}

describe('singular values', () => {
  it('are the scales of the 2x2 part, whatever the rotation', () => {
    expect(singularValues([3, 0, 0, 1, 5, 5])).toEqual([3, 1])
    const [first, second] = singularValues(rotated(2, 0.7))
    expect(first).toBeCloseTo(2, 12)
    expect(second).toBeCloseTo(2, 12)
  })

  it('read a zero matrix as unscaled, as pdf.js does', () => {
    expect(singularValues([0, 0, 0, 0, 0, 0])).toEqual([1, 1])
  })
})

describe('smoothing', () => {
  it('smooths up to 96/72 device px per image px at a pixel ratio of 1, and not past it', () => {
    expect(smoothing(scaled(1.333), false, 1)).toBe(true)
    expect(smoothing(scaled(1.334), false, 1)).toBe(false)
  })

  it('includes the bound itself, pdf.js rounding it to a float', () => {
    const bound = Math.fround(96 / 72)
    expect(smoothing(scaled(bound), false, 1)).toBe(true)
    expect(smoothing(scaled(bound + 1e-9), false, 1)).toBe(false)
  })

  it('doubles the bound at a pixel ratio of 2', () => {
    expect(smoothing(scaled(1.334), false, 2)).toBe(true)
    expect(smoothing(scaled(2.666), false, 2)).toBe(true)
    expect(smoothing(scaled(2.667), false, 2)).toBe(false)
  })

  it('always smooths an image that asks to be interpolated', () => {
    expect(smoothing(scaled(10), true, 1)).toBe(true)
  })

  it('reads a rotated or stretched matrix by its singular values, not its entries', () => {
    // At 45 degrees each entry of a 1.4 scale is under the bound; the scale itself is over it.
    expect(smoothing(rotated(1.4, Math.PI / 4), false, 1)).toBe(false)
    expect(smoothing(rotated(1.3, Math.PI / 4), false, 1)).toBe(true)
    // Either axis past the bound is enough.
    expect(smoothing(scaled(1, 2), false, 1)).toBe(false)
    expect(smoothing(scaled(2, 1), false, 1)).toBe(false)
  })
})

describe('the mip level', () => {
  it('counts pdf.js’s halvings of a minified image', () => {
    expect(mipLevel(scaled(1), 1000, 1000)).toBe(0)
    expect(mipLevel(scaled(1 / 2), 1000, 1000)).toBe(0)
    expect(mipLevel(scaled(1 / 3.2), 1000, 1000)).toBe(1)
    expect(mipLevel(scaled(1 / 8), 1000, 1000)).toBe(2)
    // Magnified: never halved.
    expect(mipLevel(scaled(4), 1000, 1000)).toBe(0)
  })

  it('takes the less minified axis', () => {
    expect(mipLevel(scaled(1 / 8, 1), 1000, 1000)).toBe(0)
    expect(mipLevel(scaled(1, 1 / 8), 1000, 1000)).toBe(0)
    expect(mipLevel(scaled(1 / 8, 1 / 3.2), 1000, 1000)).toBe(1)
  })

  it('divides the scale by what each halving really shrank, as pdf.js does on an odd size', () => {
    // 5 -> 3 leaves 3.5 / (5/3) = 2.1, still over 2, so 3 -> 2 follows; halving the scale would stop at one.
    expect(mipLevel(scaled(1 / 3.5), 5, 5)).toBe(2)
  })

  it('never goes past the chain’s last level', () => {
    // pdf.js halves 3 -> 2 -> 1 twice; a 3 x 3 chain has levels 0 and 1.
    expect(mipLevel(scaled(1 / 100), 3, 3)).toBe(1)
    expect(mipLevel(scaled(1 / 100), 1, 1)).toBe(0)
  })

  it('does not halve under a singular matrix', () => {
    expect(mipLevel([0, 0, 0, 0, 0, 0], 1000, 1000)).toBe(0)
  })
})

describe('the mip level count', () => {
  it('is floor(log2) of the longer side, plus one', () => {
    expect(mipLevelCount(1, 1)).toBe(1)
    expect(mipLevelCount(2, 1)).toBe(2)
    expect(mipLevelCount(1243, 1)).toBe(11)
    expect(mipLevelCount(3, 8191)).toBe(13)
    expect(mipLevelCount(8192, 3)).toBe(14)
  })
})

describe('the texture size', () => {
  it('keeps an image that fits', () => {
    expect(textureSize(8192, 100)).toEqual({ width: 8192, height: 100 })
    expect(textureSize(1, 8192)).toEqual({ width: 1, height: 8192 })
  })

  it('scales a larger one to fit, keeping its aspect and flooring its sides', () => {
    expect(textureSize(16384, 1000)).toEqual({ width: 8192, height: 500 })
    expect(textureSize(1000, 16384)).toEqual({ width: 500, height: 8192 })
    expect(textureSize(10000, 7777)).toEqual({ width: 8192, height: 6370 })
  })

  it('never makes a side 0', () => {
    expect(textureSize(32768, 3)).toEqual({ width: 8192, height: 1 })
  })

  it('puts the long side on the limit exactly', () => {
    for (let side = 8193; side < 9000; side += 1) {
      expect(textureSize(side, 3).width).toBe(8192)
    }
  })
})
