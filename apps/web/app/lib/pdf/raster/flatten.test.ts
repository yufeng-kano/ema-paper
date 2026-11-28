import { describe, expect, it } from 'vitest'
import { IDENTITY } from '../scene/matrix.ts'
import { DRAW_OPS } from '../scene/ops.ts'
import { Lines } from './buffers.ts'
import { flattenFill, TOLERANCE } from './flatten.ts'

const { moveTo, lineTo, curveTo, quadraticCurveTo, closePath } = DRAW_OPS

/** xorshift64, uniform in [0, 1). */
function random(seed: bigint): () => number {
  let state = seed
  return () => {
    state ^= (state << 13n) & 0xffff_ffff_ffff_ffffn
    state ^= state >> 7n
    state ^= (state << 17n) & 0xffff_ffff_ffff_ffffn
    return Number(state >> 11n) / 2 ** 53
  }
}

function linesOf(path: number[], width = 4096, height = 4096, m = IDENTITY): number[][] {
  const out = new Lines()
  flattenFill(path, 0, path.length, m, width, height, out)
  return Array.from({ length: out.count }, (_, i) => Array.from(out.data.subarray(4 * i, 4 * i + 4)))
}

function distanceToSegment(px: number, py: number, [x0, y0, x1, y1]: number[]): number {
  const dx = x1! - x0!
  const dy = y1! - y0!
  const length2 = dx * dx + dy * dy
  const t = length2 > 0 ? Math.min(1, Math.max(0, ((px - x0!) * dx + (py - y0!) * dy) / length2)) : 0
  return Math.hypot(px - x0! - t * dx, py - y0! - t * dy)
}

/** The largest distance from the curve, sampled densely, to the lines it was flattened into. */
function flatteningError(curve: number[], lines: number[][]): number {
  const [x0, y0] = curve
  const points = curve.length / 2
  let worst = 0
  for (let k = 0; k <= 1000; k += 1) {
    const t = k / 1000
    const mt = 1 - t
    // Bernstein weights of the quad or the cubic.
    const weights = points === 3 ? [mt * mt, 2 * mt * t, t * t] : [mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t]
    let px = 0
    let py = 0
    weights.forEach((w, j) => {
      px += w * curve[2 * j]!
      py += w * curve[2 * j + 1]!
    })
    worst = Math.max(worst, Math.min(...lines.map((line) => distanceToSegment(px, py, line))))
  }
  expect([lines[0]![0], lines[0]![1]]).toEqual([Math.fround(x0!), Math.fround(y0!)])
  return worst
}

describe('flattenFill', () => {
  /** The flattening errors of `count` seeded curves of `points` control points that `keep` accepts, sorted. */
  function errors(seed: bigint, points: number, count: number, keep: (curve: number[]) => boolean): number[] {
    const next = random(seed)
    const found: number[] = []
    while (found.length < count) {
      // Sizes from a few px to a few thousand, so the subdivision counts range widely.
      const size = 4 * 2 ** (next() * 9.5)
      const curve = Array.from({ length: 2 * points }, () => 100 + next() * size)
      if (!keep(curve)) {
        continue
      }
      const path = [moveTo, curve[0]!, curve[1]!, points === 3 ? quadraticCurveTo : curveTo, ...curve.slice(2)]
      // The last line is the implicit close back to the start.
      found.push(flatteningError(curve, linesOf(path).slice(0, -1)))
    }
    return found.sort((x, y) => x - y)
  }

  /** Whether a cubic's control polygon is convex, so the curve has no inflection, loop or cusp. */
  function convex(c: number[]): boolean {
    const turn = (i: number, j: number, k: number) => (c[2 * j]! - c[2 * i]!) * (c[2 * k + 1]! - c[2 * j + 1]!)
      - (c[2 * j + 1]! - c[2 * i + 1]!) * (c[2 * k]! - c[2 * j]!)
    const turns = [turn(0, 1, 2), turn(1, 2, 3), turn(0, 1, 3), turn(0, 2, 3)]
    return turns.every((x) => x > 0) || turns.every((x) => x < 0)
  }

  // kurbo's subdivision is an estimate, not a bound ("attempts to bound this distance ... but this is not absolutely
  // guaranteed", bezpath.rs 589-592): measured on these seeds, quads reach 1.01 times the tolerance, convex cubics
  // 1.16, and cubics through an inflection or a loop, whose quadratics' estimates the fractional subdivision
  // combines, 2.4 (1.1 at the 95th percentile).
  it('keeps seeded quads within the tolerance of their lines', () => {
    expect(errors(0x2545_f491_4f6c_dd1dn, 3, 1000, () => true).at(-1)).toBeLessThanOrEqual(1.05 * TOLERANCE)
  })

  it('keeps seeded convex cubics within the tolerance of their lines', () => {
    expect(errors(0x9e37_79b9_7f4a_7c15n, 4, 1000, convex).at(-1)).toBeLessThanOrEqual(1.2 * TOLERANCE)
  })

  it('keeps seeded cubics with inflections and loops near the tolerance of their lines', () => {
    const found = errors(0x1234_5678_9abc_def1n, 4, 1000, (curve) => !convex(curve))
    expect(found[Math.floor(0.95 * found.length)]).toBeLessThanOrEqual(1.15 * TOLERANCE)
    expect(found.at(-1)).toBeLessThanOrEqual(4 * TOLERANCE)
  })

  it('subdivides a quad by kurbo\'s scalar map t(u), not the mirrored 1 - t(1 - u)', () => {
    // An asymmetric quad: its curvature peaks near the start.
    const [x0, y0, x1, y1, x2, y2] = [10, 10, 30, 400, 600, 420]
    const lines = linesOf([moveTo, x0, y0, quadraticCurveTo, x1, y1, x2, y2]).slice(0, -1)
    // The parabola map of the quad (raphlinus.github.io, "Flattening quadratic Béziers").
    const integral = (x: number) => x / (0.33 + Math.sqrt(Math.sqrt(0.67 ** 4 + 0.25 * x * x)))
    const inverse = (x: number) => x * (0.61 + Math.sqrt(0.39 * 0.39 + 0.25 * x * x))
    const ddx = x1 - x0 - (x2 - x1)
    const ddy = y1 - y0 - (y2 - y1)
    const cross = (x2 - x0) * ddy - (y2 - y0) * ddx
    const a0 = integral(((x1 - x0) * ddx + (y1 - y0) * ddy) / cross)
    const a2 = integral(((x2 - x1) * ddx + (y2 - y1) * ddy) / cross)
    const t = (u: number) => (inverse(a0 + (a2 - a0) * u) - inverse(a0)) / (inverse(a2) - inverse(a0))
    const at = (s: number) => [(1 - s) ** 2 * x0 + 2 * (1 - s) * s * x1 + s * s * x2, (1 - s) ** 2 * y0 + 2 * (1 - s) * s * y1 + s * s * y2]
    const n = lines.length
    expect(n).toBeGreaterThan(8)
    for (let k = 1; k < n; k += 1) {
      const [x, y] = [lines[k]![0]!, lines[k]![1]!]
      const [ex, ey] = at(t(k / n))
      const [mx, my] = at(1 - t(1 - k / n))
      expect(Math.hypot(x - ex!, y - ey!)).toBeLessThan(1e-3)
      expect(Math.hypot(x - mx!, y - my!)).toBeGreaterThan(1)
    }
  })

  it('takes a quad whose end is at the parabola\'s vertex as no cusp (Rust\'s signum of +0 is 1)', () => {
    // x2 of the parabola map is +0 here: d12 is perpendicular to d01 - d12. Math.sign would read 0 and take the
    // cusp branch, which asks for three lines instead of two.
    expect(linesOf([moveTo, 0, 0, quadraticCurveTo, 1, 18, 2, 18])).toHaveLength(3)
  })

  it('culls a curve right of, above or below the target, and keeps one line for one wholly left', () => {
    const quad = (dx: number, dy: number) => [moveTo, 50 + dx, 10 + dy, quadraticCurveTo, 80 + dx, 50 + dy, 50 + dx, 90 + dy,
      closePath]
    const cubic = (dx: number, dy: number) => [moveTo, 50 + dx, 10 + dy, curveTo, 90 + dx, 10 + dy, 90 + dx, 90 + dy,
      50 + dx, 90 + dy, closePath]
    for (const shape of [quad, cubic]) {
      expect(linesOf(shape(0, 0), 100, 100).length).toBeGreaterThan(4)
      expect(linesOf(shape(200, 0), 100, 100)).toEqual([])
      expect(linesOf(shape(0, -200), 100, 100)).toEqual([])
      expect(linesOf(shape(0, 200), 100, 100)).toEqual([])
      // Left of the target only the winding matters: the curve is its chord, and the close is kept.
      expect(linesOf(shape(-200, 0), 100, 100)).toEqual([[-150, 10, -150, 90], [-150, 90, -150, 10]])
    }
  })

  it('culls lines outside the target into moves and keeps the lines that cross it', () => {
    // A rectangle that straddles the target's right edge: its right side and the parts above go.
    const lines = linesOf([moveTo, 50, -20, lineTo, 150, -20, lineTo, 150, 50, lineTo, 50, 50, closePath], 100, 100)
    expect(lines).toEqual([[150, 50, 50, 50], [50, 50, 50, -20]])
  })

  it('drops a path that holds a NaN', () => {
    expect(linesOf([moveTo, 0, 0, lineTo, 10, 10, curveTo, 20, NaN, 30, 10, 40, 40])).toEqual([])
    expect(linesOf([moveTo, 0, 0, lineTo, 10, 10, lineTo, 0, 10], 100, 100, [1, 0, 0, NaN, 0, 0])).toEqual([])
  })

  it('closes each subpath implicitly', () => {
    expect(linesOf([moveTo, 0, 0, lineTo, 10, 0, lineTo, 10, 10, moveTo, 20, 20, lineTo, 30, 20, lineTo, 30, 30])).toEqual([
      [0, 0, 10, 0], [10, 0, 10, 10], [10, 10, 0, 0],
      [20, 20, 30, 20], [30, 20, 30, 30], [30, 30, 20, 20],
    ])
    // An explicit close adds the same line once, and a close at the start adds none.
    expect(linesOf([moveTo, 0, 0, lineTo, 10, 0, lineTo, 10, 10, closePath])).toEqual([[0, 0, 10, 0], [10, 0, 10, 10], [10, 10, 0, 0]])
    expect(linesOf([moveTo, 0, 0, lineTo, 10, 0, lineTo, 0, 0])).toEqual([[0, 0, 10, 0], [10, 0, 0, 0]])
  })

  it('applies the matrix while reading and stores the lines as f32', () => {
    const lines = linesOf([moveTo, 1, 2, lineTo, 3, 5], 100, 100, [2, 0, 0, -3, 0.1, 50])
    expect(lines).toEqual([[2.1, 44, 6.1, 35], [6.1, 35, 2.1, 44]].map((line) => line.map(Math.fround)))
  })

  it('starts a subpath where a drawing op finds no current point, as Path2D does', () => {
    expect(linesOf([lineTo, 0, 0, lineTo, 10, 0, lineTo, 10, 10])).toEqual([[0, 0, 10, 0], [10, 0, 10, 10], [10, 10, 0, 0]])
    // A curve with no current point starts at its first control point.
    const lines = linesOf([curveTo, 0, 0, 0, 0, 10, 0])
    expect(lines).toEqual([[0, 0, 10, 0], [10, 0, 0, 0]])
  })
})
