/**
 * kurbo's own tests of offset.rs (git 6bc7521, its `mod tests` from line 1829), in the same order and with the same
 * seeds, curves and limits. kurbo measures some results with a Fréchet oracle when built with `std`; here every
 * result is measured the way kurbo's build without `std` measures it, by the sampled one-sided distances below.
 * The surveys kurbo marks `#[ignore]`, which print counts and assert nothing, are left out.
 */
import { describe, expect, it } from 'vitest'
import { PathBuffer } from './buffers.ts'
import {
  CubicBez, CubicOffset, drawArc, hairlineBound, hullBound, NO_TIPS, OFFSET_LIMITS, offsetCounters, offsetCubic,
  offsetCubicPair, onePoint, smallPieceBound, Vec2,
} from './offset.ts'
import type { OffsetRec } from './offset.ts'

const { MAX_DEPTH, ROUND_ULPS } = OFFSET_LIMITS

/** kurbo's test Rng: xorshift64, uniform in [0, 1). */
class Rng {
  #state: bigint

  constructor(seed: bigint) {
    this.#state = seed
  }

  next(): number {
    const mask = 0xffff_ffff_ffff_ffffn
    this.#state ^= (this.#state << 13n) & mask
    this.#state ^= this.#state >> 7n
    this.#state ^= (this.#state << 17n) & mask
    return Number(this.#state >> 11n) / 2 ** 53
  }

  cubic(): CubicBez {
    const point = () => new Vec2(this.next() * 100, this.next() * 100)
    return new CubicBez(point(), point(), point(), point())
  }
}

function cubic(points: readonly (readonly [number, number])[]): CubicBez {
  const [p0, p1, p2, p3] = points.map(([x, y]) => new Vec2(x, y))
  return new CubicBez(p0!, p1!, p2!, p3!)
}

function eight(c: CubicBez): [number, number, number, number, number, number, number, number] {
  return [c.p0.x, c.p0.y, c.p1.x, c.p1.y, c.p2.x, c.p2.y, c.p3.x, c.p3.y]
}

function offset(c: CubicBez, d: number, tolerance: number): PathBuffer {
  const result = new PathBuffer()
  offsetCubic(eight(c), d, tolerance, result)
  return result
}

/** The cubics of a result, which is a moveTo followed by curveTos. */
function segments(path: PathBuffer): CubicBez[] {
  const out: CubicBez[] = []
  const data = path.data
  expect(data[0]).toBe(0)
  let last = new Vec2(data[1]!, data[2]!)
  for (let i = 3; i < path.length; i += 7) {
    expect(data[i]).toBe(2)
    const p3 = new Vec2(data[i + 5]!, data[i + 6]!)
    out.push(new CubicBez(last, new Vec2(data[i + 1]!, data[i + 2]!), new Vec2(data[i + 3]!, data[i + 4]!), p3))
    last = p3
  }
  return out
}

/** kurbo's test-only `regularize_cusp`: the reference the results near a cusp are measured against. */
function regularizeCusp(c: CubicBez, dimension: number): CubicBez {
  if (!c.mayHaveSlowPoint(0, dimension)) {
    return c
  }
  return c.regularizeCuspAt(dimension, c.slowPoints(c.cuspSpeedSquared(dimension))) ?? c
}

/** The state for a single piece, as `offsetRec` would receive it. The offset by `d` is side 0. */
function setup(c: CubicBez, d: number): [CubicOffset, OffsetRec] {
  const co = new CubicOffset(c, d, 0, NO_TIPS)
  return [co, co.root(c)]
}

/** The smallest and largest cusp value over a piece, by sampling. */
function cuspRange(co: CubicOffset): [number, number] {
  let min = Infinity
  let max = -Infinity
  for (let i = 0; i <= 64; i += 1) {
    const cusp = co.curvatureTimesD(i / 64) + 1
    min = Math.min(min, cusp)
    max = Math.max(max, cusp)
  }
  return [min, max]
}

function distance(a: Vec2, b: Vec2): number {
  return a.sub(b).hypot()
}

/** The distance between an approximation and the offset of `c` at equal parameter. */
function equalParameterDistance(c: CubicBez, approx: CubicBez, d: number): number {
  const c1 = c.deriv()
  let worst = 0
  for (let i = 0; i <= 400; i += 1) {
    const t = i / 400
    const n = c1.eval(t).normalize().turn90()
    worst = Math.max(worst, distance(approx.eval(t), c.eval(t).add(n.mul(d))))
  }
  return worst
}

/** An allowance for rounding in the bounds and in the measurement. */
function slack(d: number): number {
  return 1e-9 * Math.abs(d) + 1e-11
}

/** Visits pieces of random cubics, at several offset distances. */
function forEachPiece(f: (piece: CubicBez, d: number) => void): void {
  const rng = new Rng(0x2545_f491_4f6c_dd1dn)
  for (let k = 0; k < 40; k += 1) {
    const c = rng.cubic()
    for (const n of [2, 4, 8, 16]) {
      for (let i = 0; i < n; i += 1) {
        const piece = c.subsegment(i / n, (i + 1) / n)
        for (const d of [0.3, -0.3, 3, -3, 30, -30]) {
          f(piece, d)
        }
      }
    }
  }
}

/**
 * The largest distance from a point of the result to the true offset of `c`. The offset is sampled densely, and
 * around every local minimum of the sampled distance the nearest point is refined by a ternary search.
 */
function resultError(c: CubicBez, d: number, result: PathBuffer): number {
  const N = 2000
  // Just inside the endpoints, where the derivative may vanish and the normal is defined only as a limit.
  const END_EPSILON = 1e-9
  const c1 = c.deriv()
  const offsetAt = (s: number): Vec2 => {
    const clamped = Math.min(Math.max(s, END_EPSILON), 1 - END_EPSILON)
    return c.eval(clamped).add(c1.eval(clamped).normalize().turn90().mul(d))
  }
  const samples = Array.from({ length: N + 1 }, (_, j) => offsetAt(j / N))
  const dist = new Float64Array(N + 1)
  let worst = 0
  for (const seg of segments(result)) {
    for (let i = 0; i <= 20; i += 1) {
      const p = seg.eval(i / 20)
      samples.forEach((s, j) => {
        dist[j] = distance(p, s)
      })
      let nearest = Infinity
      for (let j = 0; j <= N; j += 1) {
        if (!((j === 0 || dist[j]! <= dist[j - 1]!) && (j === N || dist[j]! <= dist[j + 1]!))) {
          continue
        }
        let lo = Math.max(j - 1, 0) / N
        let hi = Math.min(j + 1, N) / N
        for (let k = 0; k < 60; k += 1) {
          const s1 = lo + (hi - lo) / 3
          const s2 = hi - (hi - lo) / 3
          if (distance(p, offsetAt(s1)) < distance(p, offsetAt(s2))) {
            hi = s2
          } else {
            lo = s1
          }
        }
        nearest = Math.min(nearest, distance(p, offsetAt(0.5 * (lo + hi))))
      }
      worst = Math.max(worst, nearest)
    }
  }
  return worst
}

/**
 * The distance from p to the cubic, in place of kurbo's `ParamCurveNearest`: sampled, then refined by a ternary
 * search around every local minimum of the samples.
 */
function nearestDistance(c: CubicBez, px: number, py: number): number {
  const N = 32
  const x0 = c.p0.x
  const y0 = c.p0.y
  const x1 = c.p1.x
  const y1 = c.p1.y
  const x2 = c.p2.x
  const y2 = c.p2.y
  const x3 = c.p3.x
  const y3 = c.p3.y
  const at = (t: number): number => {
    const mt = 1 - t
    const a = mt * mt * mt
    const b = 3 * mt * mt * t
    const e = 3 * mt * t * t
    const f = t * t * t
    return Math.hypot(a * x0 + b * x1 + e * x2 + f * x3 - px, a * y0 + b * y1 + e * y2 + f * y3 - py)
  }
  const dist = Array.from({ length: N + 1 }, (_, j) => at(j / N))
  let best = Infinity
  for (let j = 0; j <= N; j += 1) {
    if (!((j === 0 || dist[j]! <= dist[j - 1]!) && (j === N || dist[j]! <= dist[j + 1]!))) {
      continue
    }
    let lo = Math.max(j - 1, 0) / N
    let hi = Math.min(j + 1, N) / N
    for (let k = 0; k < 50; k += 1) {
      const s1 = lo + (hi - lo) / 3
      const s2 = hi - (hi - lo) / 3
      if (at(s1) < at(s2)) {
        hi = s2
      } else {
        lo = s1
      }
    }
    best = Math.min(best, dist[j]!, at(0.5 * (lo + hi)))
  }
  return best
}

/** The offset of `c` at s, with the end tangents where the derivative vanishes. */
function offsetPoint(c: CubicBez, d: number, s: number): Vec2 {
  const [limit0, limit1] = c.tangents()
  let v = c.deriv().eval(s)
  if (!(v.hypot2() > 1e-300)) {
    v = s < 0.5 ? limit0 : limit1
  }
  return c.eval(s).add(v.normalize().turn90().mul(d))
}

/**
 * The largest distance from the offset of `c` by `d`, sampled at `nOff` + 1 parameters, to the result, and the
 * parameter where it is reached. Only the segments whose boxes are nearer than the best distance so far are measured.
 */
function offsetToResult(c: CubicBez, d: number, result: PathBuffer, nOff: number): [number, number] {
  const cubics = segments(result)
  const boxes = cubics.map((cb) => {
    const xs = [cb.p0.x, cb.p1.x, cb.p2.x, cb.p3.x]
    const ys = [cb.p0.y, cb.p1.y, cb.p2.y, cb.p3.y]
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)] as const
  })
  let worst: [number, number] = [0, 0]
  let previous = 0
  for (let i = 0; i <= nOff; i += 1) {
    const s = i / nOff
    const p = offsetPoint(c, d, s)
    let best = Infinity
    if (previous < cubics.length) {
      best = nearestDistance(cubics[previous]!, p.x, p.y)
    }
    cubics.forEach((cb, k) => {
      const [bx0, by0, bx1, by1] = boxes[k]!
      const dx = Math.max(bx0 - p.x, p.x - bx1, 0)
      const dy = Math.max(by0 - p.y, p.y - by1, 0)
      if (k === previous || dx * dx + dy * dy >= best * best) {
        return
      }
      const found = nearestDistance(cb, p.x, p.y)
      if (found < best) {
        best = found
        previous = k
      }
    })
    if (best > worst[0]) {
      worst = [best, s]
    }
  }
  return worst
}

/**
 * The largest distance from a point of `result` to the offset of `c` by `d`, to within half of `spacing`: the
 * offset is bisected until its points are no further apart than `spacing`, so the arc it traces about a sharp turn
 * of the source is resolved, and a loop whose ends are close is too.
 */
function resultToOffset(c: CubicBez, d: number, result: PathBuffer, spacing: number): number {
  const N = 64
  const pending: [number, number][] = Array.from({ length: N }, (_, k) => [(N - 1 - k) / N, (N - k) / N])
  const points = [offsetPoint(c, d, 0)]
  for (let next = pending.pop(); next !== undefined; next = pending.pop()) {
    const [s0, s1] = next
    const p0 = offsetPoint(c, d, s0)
    const p1 = offsetPoint(c, d, s1)
    const mid = 0.5 * (s0 + s1)
    const bulge = distance(offsetPoint(c, d, mid), p0.midpoint(p1))
    if ((distance(p0, p1) > spacing || bulge > spacing) && s1 - s0 > 1e-13) {
      pending.push([mid, s1], [s0, mid])
    } else {
      points.push(p1)
    }
  }
  let worst = 0
  for (const seg of segments(result)) {
    for (let i = 0; i <= 32; i += 1) {
      const p = seg.eval(i / 32)
      let nearest = Infinity
      for (const point of points) {
        nearest = Math.min(nearest, point.sub(p).hypot2())
      }
      worst = Math.max(worst, Math.sqrt(nearest))
    }
  }
  return worst
}

/** A curve whose speed has a minimum near the cusp set by `gap`, at a random parameter. */
function nearCusp(rng: Rng, gap: number): CubicBez {
  const tCusp = 0.15 + 0.7 * rng.next()
  const randomVector = () => new Vec2(rng.next() * 300 - 150, rng.next() * 300 - 150)
  const a = randomVector()
  const b = randomVector()
  const dir = new Vec2(rng.next() - 0.5, rng.next() - 0.5).normalize()
  // The hodograph in the power basis, then its control points, and finally those of the curve.
  const pow2 = b
  const pow1 = a.sub(b.mul(tCusp))
  const pow0 = dir.mul(3 * gap).sub(a.mul(tCusp))
  const q0 = pow0
  const q1 = pow0.add(pow1.mul(0.5))
  const q2 = pow0.add(pow1).add(pow2)
  const p0 = new Vec2(rng.next() * 100, rng.next() * 100)
  const p1 = p0.add(q0.div(3))
  const p2 = p1.add(q1.div(3))
  return new CubicBez(p0, p1, p2, p2.add(q2.div(3)))
}

function isFinitePath(path: PathBuffer): boolean {
  return path.data.subarray(0, path.length).every(Number.isFinite)
}

describe('offset', () => {
  it('keeps the hull and hairline bounds strict', () => {
    let nHull = 0
    forEachPiece((piece, d) => {
      const [co, rec] = setup(piece, d)
      // These bounds hold regardless of the cusp values, and for any fit.
      const [arcA, arcB] = drawArc(rec.utan0, rec.utan1)
      const fits = [co.apply(0, piece, rec, arcA, arcB)]
      const linear = onePoint(piece, rec)
      if (linear !== null) {
        fits.push(co.apply(0, piece, rec, linear[0], linear[1]))
      }
      for (const fit of fits) {
        const measured = equalParameterDistance(piece, fit.approx, d)
        const hull = hullBound(piece, rec, fit.delta, d)
        if (hull !== null) {
          expect(hull).toBeGreaterThanOrEqual(measured - slack(d))
          nHull += 1
        }
        expect(hairlineBound(fit.delta, d)).toBeGreaterThanOrEqual(measured - slack(d))
      }
    })
    expect(nHull).toBeGreaterThan(1000)
  })

  it('keeps the small-piece bound strict', () => {
    // Pieces around the minimum of the speed of curves with a near-cusp.
    const rng = new Rng(4242n)
    let nChecked = 0
    for (let k = 0; k < 200; k += 1) {
      // A hairpin: the inner control points lie beyond the endpoints.
      const p0 = new Vec2(rng.next() * 10, rng.next() * 10)
      const p3 = p0.add(new Vec2(rng.next() * 2 - 1, rng.next() * 2 - 1))
      const far = new Vec2(40 + rng.next() * 40, rng.next() * 20)
      const spread = new Vec2(rng.next() - 0.5, rng.next() - 0.5).mul(0.2)
      const c = new CubicBez(p0, p3.add(far).add(spread), p0.add(far).sub(spread), p3)
      const q = c.deriv()
      let tSlow = 0.5
      for (let i = 0; i <= 1000; i += 1) {
        const t = i / 1000
        if (q.eval(t).hypot2() < q.eval(tSlow).hypot2()) {
          tSlow = t
        }
      }
      for (const width of [1e-2, 3e-3, 1e-3]) {
        const t0 = Math.max(tSlow - width * rng.next(), 0)
        const t1 = Math.min(t0 + width, 1)
        const piece = c.subsegment(t0, t1)
        for (const d of [1, -1, 5, -5]) {
          const [co, rec] = setup(piece, d)
          const [a, b] = drawArc(rec.utan0, rec.utan1)
          const fit = co.apply(0, piece, rec, a, b)
          const bound = smallPieceBound(piece, rec, co.hodographMiddle(rec), fit, d)
          if (bound === null) {
            continue
          }
          const result = new PathBuffer()
          result.moveTo(fit.approx.p0.x, fit.approx.p0.y)
          result.curveTo(fit.approx.p1.x, fit.approx.p1.y, fit.approx.p2.x, fit.approx.p2.y, fit.approx.p3.x,
            fit.approx.p3.y)
          expect(bound).toBeGreaterThanOrEqual(resultError(piece, d, result) - slack(d))
          nChecked += 1
        }
      }
    }
    expect(nChecked).toBeGreaterThan(200)
  }, 60_000)

  it('draws the arc as the trigonometric form does', () => {
    const rng = new Rng(12345n)
    for (let k = 0; k < 100; k += 1) {
      const a0 = rng.next() * 6
      const a1 = rng.next() * 6
      const utan0 = new Vec2(Math.cos(a0), Math.sin(a0))
      const utan1 = new Vec2(Math.cos(a1), Math.sin(a1))
      const th = Math.atan2(utan1.cross(utan0), utan1.dot(utan0))
      const expected = (2 / 3) / (1 + Math.cos(0.5 * th)) * 2 * Math.sin(0.5 * th)
      const [a, b] = drawArc(utan0, utan1)
      expect(Math.abs(a - expected)).toBeLessThan(1e-12)
      expect(b).toBe(-a)
    }
  })

  it('keeps the default result within the tolerance with a hidden cusp pair', () => {
    // The cusp value is close to 1 at both ends of this piece of a random cubic but varies widely inside: with d of
    // -10 the offset has a pair of cusps that the signs at the ends do not reveal.
    const c = cubic([[79.057_384_592_444_27, 48.466_008_655_533_074], [12.503_273_981_653_885, 44.382_261_036_215_43],
      [52.908_321_475_974_894, 47.634_604_733_008_06], [28.539_084_055_419_305, 44.786_787_416_066_95]])
    const [co] = setup(c, -10)
    expect(cuspRange(co)[0]).toBeLessThan(0)
    const tolerance = 0.25
    const result = offset(c, -10, tolerance)
    expect(resultError(regularizeCusp(c, tolerance * 0.25), -10, result)).toBeLessThanOrEqual(1.25 * tolerance)
  }, 60_000)

  it('keeps results near cusps of the offset within the tolerance, in both directions', () => {
    // The first two have a sharp turn, with cusps of the inner offset on either side of it. The third has a
    // vanishing derivative at its end, with a stub small enough to be smoothed over, whose approximation once ran
    // past the endpoint by more than twice the tolerance.
    const cases: [CubicBez, number][] = [
      [cubic([[50.70379356982498, 97.31579361970653], [54.408713681427315, 99.62040910077242],
        [46.59558023356715, 104.59659647574499], [63.10848861740551, 72.84200738122556]]), 1],
      [cubic([[68.19282651769628, 64.02157963849669], [65.63887693529813, 102.62679821651291],
        [76.9955138820981, 109.10380160425157], [71.3351350259059, 103.23868143174104]]), -5],
      [cubic([[48.091559827693544, 96.6665041125934], [8.427831923733642, 28.106428183666686],
        [30.525434722905253, 68.48298765242757], [30.525434722905253, 68.48298765242757]]), -30],
    ]
    const tolerance = 0.01
    for (const [c, d] of cases) {
      const regularized = regularizeCusp(c, 0.25 * tolerance)
      const result = offset(c, d, tolerance)
      expect(offsetToResult(regularized, d, result, 4000)[0]).toBeLessThanOrEqual(1.1 * tolerance)
      const spacing = tolerance / 8
      expect(resultToOffset(regularized, d, result, spacing)).toBeLessThanOrEqual(1.1 * tolerance + 0.5 * spacing)
    }
  }, 60_000)

  it('keeps results with tips within the tolerance and the depth', () => {
    // A near-cusp of the source inside a piece is invisible to the estimate, whose samples straddle it.
    const rng = new Rng(2026n)
    for (const gap of [1e-2, 1e-5, 0]) {
      for (let k = 0; k < 6; k += 1) {
        const c = nearCusp(rng, gap)
        for (const d of [1, -1, 5]) {
          for (const tolerance of [0.25, 1e-2, 1e-4]) {
            const regularized = regularizeCusp(c, 0.25 * tolerance)
            offsetCounters.outOfDepth = 0
            const result = offset(c, d, tolerance)
            expect(offsetCounters.outOfDepth).toBe(0)
            expect(offsetToResult(regularized, d, result, 4000)[0]).toBeLessThanOrEqual(1.1 * tolerance)
          }
        }
      }
    }
  }, 120_000)

  it('keeps results with sharp turns within the tolerance', () => {
    // Turns too broad to be a tip, at a tolerance of half the offset distance, which earlier versions accepted
    // whole or nearly so.
    const cases: [readonly (readonly [number, number])[], number][] = [
      [[[70.48094071239159, 69.67513157564085], [60.395361225140846, 21.14518938682526],
        [82.70372826510773, 73.15404192150442], [80.36747186021297, 63.619310802492066]], -0.5],
      [[[14.940246361937215, 24.323404923761505], [16.969975647298806, 57.145266278455665],
        [53.36354627583025, 30.428145078542368], [51.880625747832134, 33.42931795389501]], 0.5],
      [[[0.9055224166763853, 85.56012487359405], [64.81963576741242, 27.466766809783483],
        [62.88663400875335, 2.0243310497624845], [18.286893883228384, 65.35842253157391]], -0.5],
    ]
    for (const [points, d] of cases) {
      const c = cubic(points)
      const tolerance = 0.25
      expect(offsetToResult(c, d, offset(c, d, tolerance), 4000)[0]).toBeLessThanOrEqual(1.1 * tolerance)
    }
  })

  it('does not exhaust the depth at tips', () => {
    // Pieces beside the tip of a regularized cusp are a few ulps across, with very small speeds.
    const rng = new Rng(7n)
    for (const gap of [1e-6, 0]) {
      let nOutOfDepth = 0
      let nSegments = 0
      for (let k = 0; k < 40; k += 1) {
        const c = nearCusp(rng, gap)
        for (const d of [1, -1, 5, -5]) {
          offsetCounters.outOfDepth = 0
          const result = offset(c, d, 1e-4)
          nOutOfDepth += offsetCounters.outOfDepth
          nSegments += segments(result).length
        }
      }
      expect(nOutOfDepth).toBeLessThanOrEqual(5)
      expect(nSegments).toBeLessThan(3500)
    }
  })

  it('keeps the default result within the tolerance with a vanishing end derivative', () => {
    // The cusp near such an end, on the inside, was once never found, and the fit spanned it.
    const cases: [CubicBez, number, number][] = [
      [cubic([[71.988_963_612_376_5, 58.039_513_772_261_74], [28.277_259_840_239_01, 75.409_677_717_394_56],
        [76.641_865_945_822_9, 47.690_545_505_945_88], [76.641_865_945_822_9, 47.690_545_505_945_88]]), 0.3, 0.25],
      [cubic([[20.284_875_574_871_297, 73.667_977_655_445_3], [20.284_875_574_871_297, 73.667_977_655_445_3],
        [60.096_863_426_480_81, 22.650_499_256_665_98], [45.303_963_035_391_234, 80.413_901_500_895_16]]), 30, 0.01],
    ]
    for (const [c, d, tolerance] of cases) {
      expect(offsetToResult(c, d, offset(c, d, tolerance), 20_000)[0]).toBeLessThanOrEqual(1.25 * tolerance)
    }
  }, 60_000)

  it('asserts against the offset of a point, on one side or both', () => {
    // A curve whose control points coincide has no normal: every piece was rejected down to the maximum depth.
    const c: [number, number, number, number, number, number, number, number] = [5, 5, 5, 5, 5, 5, 5, 5]
    expect(() => offsetCubic(c, 1, 0.25, new PathBuffer())).toThrow('single point')
    const positive = new PathBuffer()
    const negative = new PathBuffer()
    positive.moveTo(5, 5)
    negative.moveTo(5, 5)
    expect(() => offsetCubicPair(c, 1, 0.25, positive, negative)).toThrow('single point')
  })

  it('does not regularize small curves', () => {
    // Small quarter circles stroked with a half-width much larger than their radius, common in some PDF files, were
    // taken for cusps and regularized, which made a sharp turn they were then split at.
    for (const r of [0.01, 0.02, 0.04, 0.06]) {
      const k = 0.552_284_749_8 * r
      const c = cubic([[r, 0], [r, k], [k, r], [0, r]])
      for (const d of [0.5, -0.5]) {
        expect(segments(offset(c, d, 0.25))).toHaveLength(1)
      }
    }
  })

  it('offsets the curves of open kurbo issues finitely and within the tolerance', () => {
    // #617 and #344 have a vanishing end derivative, which once gave non-finite points on the inner side; #615 has
    // an exact cusp at its midpoint, which was once missed.
    const cases: [CubicBez, number[], number[]][] = [
      [cubic([[0.5, -3], [-0.25, -0.5], [0, 0], [0, 0]]), [-0.6, 0.6], [5e-5, 0.02]],
      [cubic([[51, 0], [-0.085_937_5, 161.640_625], [0, 164], [0, 164]]), [-8, 8], [0.01, 1e-4]],
      [cubic([[1.600_000_000_000_000_8, 310.787_665_198_237_9], [5.406_167_400_881_057, 306.981_497_797_356_84],
        [5.406_167_400_881_058, 310.787_665_198_237_9], [1.6, 306.981_497_797_356_84]]), [-0.6, 0.6], [0.02, 1e-4]],
    ]
    for (const [c, ds, tolerances] of cases) {
      for (const d of ds) {
        for (const tolerance of tolerances) {
          const result = offset(c, d, tolerance)
          expect(isFinitePath(result)).toBe(true)
          expect(offsetToResult(c, d, result, 20_000)[0]).toBeLessThanOrEqual(tolerance)
        }
      }
    }
  }, 120_000)

  it('gives the same pair as one side at a time, bit for bit', () => {
    // The two-sided recursion shares work but makes the same decisions.
    const rng = new Rng(98765n)
    let nSegments = 0
    for (let k = 0; k < 80; k += 1) {
      const c = rng.cubic()
      for (const d of [0.3, 3, 30]) {
        for (const tolerance of [0.25, 1e-2, 1e-4]) {
          const expectPos = offset(c, d, tolerance)
          const expectNeg = offset(c, -d, tolerance)
          const pos = new PathBuffer()
          const neg = new PathBuffer()
          pos.moveTo(expectPos.data[1]!, expectPos.data[2]!)
          neg.moveTo(expectNeg.data[1]!, expectNeg.data[2]!)
          offsetCubicPair(eight(c), d, tolerance, pos, neg)
          expect(Array.from(pos.data.subarray(0, pos.length))).toEqual(Array.from(expectPos.data.subarray(0, expectPos.length)))
          expect(Array.from(neg.data.subarray(0, neg.length))).toEqual(Array.from(expectNeg.data.subarray(0, expectNeg.length)))
          nSegments += (pos.length - 3) / 7 + 1 + (neg.length - 3) / 7 + 1
        }
      }
    }
    expect(nSegments).toBeGreaterThan(5000)
  }, 60_000)

  it('covers rounding at large coordinates with the allowance', () => {
    // Far from the origin the approximation's control points are rounded by about an ulp, which the bounds,
    // computed for the exact D, do not see. The arc is drawn and bounded there, then translated back to the origin,
    // where the subtraction is exact, and measured.
    const rng = new Rng(31337n)
    let nChecked = 0
    for (let k = 0; k < 20; k += 1) {
      const c = rng.cubic()
      for (let i = 0; i < 32; i += 1) {
        const piece = c.subsegment(i / 32, (i + 1) / 32)
        for (const amount of [1e6, 1e9]) {
          const shift = new Vec2(amount, amount)
          const far = new CubicBez(piece.p0.add(shift), piece.p1.add(shift), piece.p2.add(shift), piece.p3.add(shift))
          for (const d of [1, -1]) {
            const [co, rec] = setup(far, d)
            const [a, b] = drawArc(rec.utan0, rec.utan1)
            const fit = co.apply(0, far, rec, a, b)
            const bound = hullBound(far, rec, fit.delta, d)
            if (bound === null) {
              continue
            }
            const back = (p: Vec2) => p.sub(shift)
            const result = new PathBuffer()
            result.moveTo(back(fit.approx.p0).x, back(fit.approx.p0).y)
            result.curveTo(back(fit.approx.p1).x, back(fit.approx.p1).y, back(fit.approx.p2).x, back(fit.approx.p2).y,
              back(fit.approx.p3).x, back(fit.approx.p3).y)
            const source = new CubicBez(back(far.p0), back(far.p1), back(far.p2), back(far.p3))
            let largest = 0
            for (const p of [far.p0, far.p1, far.p2, far.p3, fit.approx.p0, fit.approx.p1, fit.approx.p2, fit.approx.p3]) {
              largest = Math.max(largest, Math.abs(p.x), Math.abs(p.y))
            }
            const allowance = ROUND_ULPS * Number.EPSILON * largest
            expect(bound + allowance).toBeGreaterThanOrEqual(resultError(source, d, result))
            nChecked += 1
          }
        }
      }
    }
    expect(nChecked).toBeGreaterThan(500)
  }, 120_000)

  it('offsets a straight line exactly, in one segment', () => {
    const result = offset(cubic([[0, 0], [10, 0], [20, 0], [30, 0]]), 5, 1e-6)
    const [seg, ...rest] = segments(result)
    expect(rest).toEqual([])
    for (const p of [seg!.p1, seg!.p2, seg!.p3]) {
      expect(Math.abs(p.y - 5)).toBeLessThan(1e-12)
    }
  })

  it('survives degenerate input', () => {
    // Repeated and collinear control points, very small and very large curves, and offset distances far larger than
    // the curve: only finite output of reasonable length is checked.
    const rng = new Rng(99n)
    const result = new PathBuffer()
    for (let i = 0; i < 2000; i += 1) {
      let c = rng.cubic()
      switch (i % 8) {
        case 0:
          c = new CubicBez(c.p0, c.p0, c.p2, c.p3)
          break
        case 1:
          c = new CubicBez(c.p0, c.p1, c.p3, c.p3)
          break
        case 2:
          c = new CubicBez(c.p0, c.p0, c.p3, c.p3)
          break
        case 3: {
          const p1 = c.p0.lerp(c.p3, rng.next() * 2 - 0.5)
          c = new CubicBez(c.p0, p1, c.p0.lerp(c.p3, rng.next() * 2 - 0.5), c.p3)
          break
        }
        case 4:
          c = new CubicBez(c.p0, c.p1, c.p1, c.p3)
          break
        case 5:
          c = new CubicBez(c.p0, c.p1, c.p2, c.p0)
          break
        case 6:
          c = new CubicBez(c.p0.mul(1e-6), c.p1.mul(1e-6), c.p2.mul(1e-6), c.p3.mul(1e-6))
          break
      }
      for (const d of [1e-3, -0.5, 7, -300, 1e5]) {
        for (const tolerance of [1, 1e-2, 1e-6]) {
          offsetCubic(eight(c), d, tolerance, result)
          // The pieces of the root subdivision, at most seven, start at depth 1.
          expect((result.length - 3) / 7 + 1).toBeLessThanOrEqual(4 << MAX_DEPTH)
          expect(isFinitePath(result)).toBe(true)
        }
      }
    }
  }, 120_000)

  it('offsets a pathological curve, starting with a moveTo', () => {
    const curve = cubic([[-1236.3746269978635, 152.17981429574826], [-1175.18662093517, 108.04721798590596],
      [-1152.142883879584, 105.76260301083356], [-1151.842639804639, 105.73040758939104]])
    const result = offset(curve, 3603.7267536453924, 0.1)
    expect(result.data[0]).toBe(0)
  })

  it('does not recurse forever on a degenerate straight curve', () => {
    const c = cubic([[1096.2962962962963, 593.90243902439033], [1043.6213991769548, 593.90243902439033],
      [1030.4526748971193, 593.90243902439033], [1056.7901234567901, 593.90243902439033]])
    expect(offset(c, -0.5, 0.1).length).toBeGreaterThan(3)
  })

  it('reproduces the curve for a zero distance', () => {
    const c = [1, 2, 3, 5, 7, 4, 9, 9] as const
    const result = new PathBuffer()
    offsetCubic(c, 0, 0.25, result)
    expect(Array.from(result.data.subarray(0, result.length))).toEqual([0, 1, 2, 2, 3, 5, 7, 4, 9, 9])
  })
})
