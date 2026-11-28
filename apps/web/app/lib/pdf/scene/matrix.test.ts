import { describe, expect, it } from 'vitest'
import { apply, boxThrough, EMPTY_BOX, IDENTITY, intersect, invert, maxScale, multiply, union, type Mat6 } from './matrix.ts'

const SCALE_2: Mat6 = [2, 0, 0, 2, 0, 0]
const MOVE_10_20: Mat6 = [1, 0, 0, 1, 10, 20]
const ROTATE_90: Mat6 = [0, 1, -1, 0, 0, 0]

function close(a: readonly number[], b: readonly number[]) {
  expect(a.length).toBe(b.length)
  a.forEach((v, i) => expect(v).toBeCloseTo(b[i]!, 12))
}

describe('multiplying matrices', () => {
  it('applies the right one first, as pdf.js’s Util.transform does', () => {
    // Move, then scale: the move is scaled too.
    expect(multiply(SCALE_2, MOVE_10_20)).toEqual([2, 0, 0, 2, 20, 40])
    // Scale, then move: the move is not.
    expect(multiply(MOVE_10_20, SCALE_2)).toEqual([2, 0, 0, 2, 10, 20])
    const [x, y] = apply(multiply(SCALE_2, MOVE_10_20), 1, 1)
    expect(apply(SCALE_2, ...apply(MOVE_10_20, 1, 1))).toEqual([x, y])
  })

  it('leaves a matrix as it was under the identity', () => {
    const m: Mat6 = [0.5, 0.25, -1, 3, 7, -9]
    expect(multiply(IDENTITY, m)).toEqual(m)
    expect(multiply(m, IDENTITY)).toEqual(m)
  })
})

describe('inverting a matrix', () => {
  it('gives the matrix that takes every point back', () => {
    const m: Mat6 = [0.5, 0.25, -1, 3, 7, -9]
    close(multiply(m, invert(m)!), IDENTITY)
    close(apply(invert(m)!, ...apply(m, 4, -6)), [4, -6])
  })

  it('answers null for a matrix that flattens the plane', () => {
    expect(invert([1, 2, 2, 4, 5, 6])).toBeNull()
    expect(invert([0, 0, 0, 0, 0, 0])).toBeNull()
  })
})

describe('a box through a matrix', () => {
  it('is the hull of the four corners under a rotation', () => {
    // A 90° turn sends (x, y) to (-y, x).
    expect(boxThrough(ROTATE_90, [10, 20, 30, 60])).toEqual([-60, 10, -20, 30])
  })

  it('is the hull under a 45° turn, wider than the box', () => {
    const c = Math.SQRT1_2
    close(boxThrough([c, c, -c, c, 0, 0], [0, 0, 1, 1]), [-c, 0, c, 2 * c])
  })

  it('stays empty for an empty box, even under a matrix with zeros', () => {
    expect(boxThrough(ROTATE_90, EMPTY_BOX)).toEqual([...EMPTY_BOX])
  })
})

describe('the largest scale of a matrix', () => {
  it('is the scale of a uniform scale', () => {
    expect(maxScale([3, 0, 0, 3, 50, 50])).toBeCloseTo(3, 12)
  })

  it('is 1 under a 90° rotation, where max(|a|, |d|) would read 0', () => {
    expect(maxScale(ROTATE_90)).toBeCloseTo(1, 12)
    expect(maxScale([0, 2, -2, 0, 0, 0])).toBeCloseTo(2, 12)
  })

  it('is the larger singular value of a shear', () => {
    // [[1, 1], [0, 1]] has singular values (1 ± √5) / 2 in magnitude: the golden ratio and its inverse.
    expect(maxScale([1, 0, 1, 1, 0, 0])).toBeCloseTo((1 + Math.sqrt(5)) / 2, 12)
  })

  it('is the larger axis of a non-uniform scale', () => {
    expect(maxScale([0.5, 0, 0, 4, 0, 0])).toBeCloseTo(4, 12)
  })
})

describe('intersecting and joining boxes', () => {
  it('gives the overlap, or null when there is none', () => {
    expect(intersect([0, 0, 10, 10], [5, -5, 20, 6])).toEqual([5, 0, 10, 6])
    expect(intersect([0, 0, 10, 10], [11, 0, 20, 10])).toBeNull()
    expect(intersect([0, 0, 10, 10], [0, 11, 10, 20])).toBeNull()
  })

  it('keeps boxes that only touch, as a line', () => {
    expect(intersect([0, 0, 10, 10], [10, 0, 20, 10])).toEqual([10, 0, 10, 10])
  })

  it('treats the empty box as holding nothing', () => {
    expect(intersect(EMPTY_BOX, [0, 0, 10, 10])).toBeNull()
    expect(intersect([0, 0, 10, 10], EMPTY_BOX)).toBeNull()
    expect(union([...EMPTY_BOX], [1, 2, 3, 4])).toEqual([1, 2, 3, 4])
    expect(union([1, 2, 3, 4], EMPTY_BOX)).toEqual([1, 2, 3, 4])
  })

  it('grows the first box in place to hold the second', () => {
    const a: [number, number, number, number] = [0, 0, 1, 1]
    const joined = union(a, [-1, 0.5, 0.5, 3])
    expect(joined).toBe(a)
    expect(a).toEqual([-1, 0, 1, 3])
  })
})
