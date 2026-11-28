/**
 * Offset curves of a cubic Bézier, both sides at once, for the stroker (docs/pdf-viewer.md § Stage 4: the WebGPU
 * core), and the curve helpers they need.
 *
 * Ported from kurbo git 6bc7521 (unreleased, after 0.13.1): offset.rs 114-1828, Apache-2.0 OR MIT,
 * THIRD_PARTY_NOTICES § 9; with what it calls elsewhere in kurbo: cubicbez.rs 426-431 (`cusp_speed_squared`),
 * 439-511 (`regularize_cusp_at`, `detect_cusp`), 530-593 (`may_have_slow_point`, `zero_handle_factor`), 610-672
 * (`slow_points`), 761-780 (`eval`, `subsegment`) and 818-826 (`deriv`); quadbez.rs 217-222 and 257-262 (`eval`,
 * `deriv`); line.rs 109-120 (`eval`, `subsegment`); bezpath.rs 1326-1356 (`PathSeg::tangents`); common.rs 190-290
 * (`solve_cubic`, `solve_quadratic`); vec2.rs and point.rs (the arithmetic used).
 * Changed: scalar TypeScript; results into a PathBuffer; the cubic is passed as 8 numbers; points and vectors are one
 * class; fused multiply-adds are a multiply and an add; Rust's NaN-ignoring min and max are written out; the
 * single-point assertion throws in every build; the test-only counters are one exported object.
 */
import type { PathBuffer } from './buffers.ts'

/** Rust's f64::max: a NaN operand gives the other one. */
function fmax(a: number, b: number): number {
  return a > b || Number.isNaN(b) ? a : b
}

/** Rust's f64::min: a NaN operand gives the other one. */
function fmin(a: number, b: number): number {
  return a < b || Number.isNaN(b) ? a : b
}

/** Rust's f64::copysign for the signs this file meets (a NaN sign reads as positive). */
function copysign(magnitude: number, sign: number): number {
  return sign < 0 || Object.is(sign, -0) ? -Math.abs(magnitude) : Math.abs(magnitude)
}

/** Rust's f64::signum: ±1 for ±0, NaN for NaN. */
export function signum(x: number): number {
  if (Number.isNaN(x)) {
    return NaN
  }
  return x > 0 || (x === 0 && 1 / x > 0) ? 1 : -1
}

/** kurbo's Point and Vec2 in one: the arithmetic kurbo uses, with its rounding (division multiplies by 1/s). */
export class Vec2 {
  static readonly ZERO = new Vec2(0, 0)

  constructor(readonly x: number, readonly y: number) {}

  add(o: Vec2): Vec2 {
    return new Vec2(this.x + o.x, this.y + o.y)
  }

  sub(o: Vec2): Vec2 {
    return new Vec2(this.x - o.x, this.y - o.y)
  }

  mul(s: number): Vec2 {
    return new Vec2(this.x * s, this.y * s)
  }

  div(s: number): Vec2 {
    return this.mul(1 / s)
  }

  neg(): Vec2 {
    return new Vec2(-this.x, -this.y)
  }

  dot(o: Vec2): number {
    return this.x * o.x + this.y * o.y
  }

  cross(o: Vec2): number {
    return this.x * o.y - this.y * o.x
  }

  hypot2(): number {
    return this.dot(this)
  }

  /** kurbo avoids the library hypot here: sqrt of the square, so every engine agrees. */
  hypot(): number {
    return Math.sqrt(this.hypot2())
  }

  normalize(): Vec2 {
    return this.div(this.hypot())
  }

  turn90(): Vec2 {
    return new Vec2(-this.y, this.x)
  }

  lerp(o: Vec2, t: number): Vec2 {
    return this.add(o.sub(this).mul(t))
  }

  midpoint(o: Vec2): Vec2 {
    return new Vec2(0.5 * (this.x + o.x), 0.5 * (this.y + o.y))
  }

  equals(o: Vec2): boolean {
    return this.x === o.x && this.y === o.y
  }

  isFinite(): boolean {
    return Number.isFinite(this.x) && Number.isFinite(this.y)
  }
}

export class Line {
  constructor(readonly p0: Vec2, readonly p1: Vec2) {}

  eval(t: number): Vec2 {
    return this.p0.lerp(this.p1, t)
  }

  subsegment(t0: number, t1: number): Line {
    return new Line(this.eval(t0), this.eval(t1))
  }
}

export class QuadBez {
  constructor(readonly p0: Vec2, readonly p1: Vec2, readonly p2: Vec2) {}

  eval(t: number): Vec2 {
    const mt = 1 - t
    return this.p0.mul(mt * mt).add(this.p1.mul(mt * 2).add(this.p2.mul(t)).mul(t))
  }

  deriv(): Line {
    return new Line(this.p1.sub(this.p0).mul(2), this.p2.sub(this.p1).mul(2))
  }
}

export class CubicBez {
  constructor(readonly p0: Vec2, readonly p1: Vec2, readonly p2: Vec2, readonly p3: Vec2) {}

  eval(t: number): Vec2 {
    const mt = 1 - t
    return this.p0.mul(mt * mt * mt)
      .add(this.p1.mul(mt * mt * 3).add(this.p2.mul(mt * 3).add(this.p3.mul(t)).mul(t)).mul(t))
  }

  subsegment(t0: number, t1: number): CubicBez {
    const p0 = this.eval(t0)
    const p3 = this.eval(t1)
    const d = this.deriv()
    const scale = (t1 - t0) * (1 / 3)
    return new CubicBez(p0, p0.add(d.eval(t0).mul(scale)), p3.sub(d.eval(t1).mul(scale)), p3)
  }

  deriv(): QuadBez {
    return new QuadBez(this.p1.sub(this.p0).mul(3), this.p2.sub(this.p1).mul(3), this.p3.sub(this.p2).mul(3))
  }

  equals(o: CubicBez): boolean {
    return this.p0.equals(o.p0) && this.p1.equals(o.p1) && this.p2.equals(o.p2) && this.p3.equals(o.p3)
  }

  /** kurbo PathSeg::tangents: the endpoint tangents, robust to a control point on an endpoint. */
  tangents(): [Vec2, Vec2] {
    const EPS = 1e-12
    const d01 = this.p1.sub(this.p0)
    let d0 = d01
    if (!(d01.hypot2() > EPS)) {
      const d02 = this.p2.sub(this.p0)
      d0 = d02.hypot2() > EPS ? d02 : this.p3.sub(this.p0)
    }
    const d23 = this.p3.sub(this.p2)
    let d1 = d23
    if (!(d23.hypot2() > EPS)) {
      const d13 = this.p3.sub(this.p1)
      d1 = d13.hypot2() > EPS ? d13 : this.p3.sub(this.p0)
    }
    return [d0, d1]
  }

  /**
   * An upper bound on |C'|² at a minimum of the speed for which a cusp might be detected: there the curvature is
   * |C''| / |C'|², above 1 / dimension where |C'|² <= |C''| dimension, and |C''| peaks at an endpoint.
   */
  cuspSpeedSquared(dimension: number): number {
    const d01 = this.p1.sub(this.p0)
    const d12 = this.p2.sub(this.p1)
    const d23 = this.p3.sub(this.p2)
    return 6 * Math.sqrt(fmax(d12.sub(d01).hypot2(), d23.sub(d12).hypot2())) * dimension
  }

  /** The curve with its control points moved so its derivative does not vanish inside it; null if none moved. */
  regularizeCuspAt(dimension: number, minima: readonly number[]): CubicBez | null {
    const cuspType = this.detectCusp(dimension, minima)
    if (cuspType === null) {
      return null
    }
    let { p1, p2 } = this
    // A control point too near its endpoint is displaced along the tangent.
    const d01 = this.p1.sub(this.p0)
    const d01h = d01.hypot()
    const d23 = this.p3.sub(this.p2)
    const d23h = d23.hypot()
    if (cuspType === 'loop') {
      p1 = p1.add(d01.mul(dimension / d01h))
      p2 = p2.sub(d23.mul(dimension / d23h))
      return new CubicBez(this.p0, p1, p2, this.p3)
    }
    // Never make a control distance smaller than the dimension.
    const nudge0 = d01h > 2 * dimension
    const nudge1 = d23h > 2 * dimension
    if (nudge0) {
      p1 = p1.sub(d01.mul(dimension / d01h))
    }
    if (nudge1) {
      p2 = p2.add(d23.mul(dimension / d23h))
    }
    return nudge0 || nudge1 ? new CubicBez(this.p0, p1, p2, this.p3) : null
  }

  /** Whether a cusp sharper than 1 / dimension sits at one of the minima of the speed, and of which kind. */
  detectCusp(dimension: number, minima: readonly number[]): 'loop' | 'double inflection' | null {
    const d01 = this.p1.sub(this.p0)
    const d23 = this.p3.sub(this.p2)
    // A zero-length handle is a vanishing endpoint derivative, which is not regularized; the displacements divide
    // by the handle lengths.
    if (d01.hypot2() === 0 || d23.hypot2() === 0) {
      return null
    }
    const q = this.deriv()
    for (const t of minima) {
      // |C''| / |C'|² is robust at an exact cusp, where C' is rounding error that may be parallel to C''.
      const d = q.eval(t)
      const d2 = q.deriv().eval(t)
      if (d.hypot2() <= d2.hypot() * dimension && d.hypot2() < CUSP_MAX_WIDTH * CUSP_MAX_WIDTH * d2.hypot2()) {
        const d02 = this.p2.sub(this.p0)
        const d03 = this.p3.sub(this.p0)
        const det012 = d01.cross(d02)
        const det013 = d01.cross(d03)
        const det023 = d02.cross(d03)
        const a = 3 * det012 + det023 - 2 * det013
        const b = -3 * det012 + det013
        const c = det012
        return b * b - 4 * a * c > 0 ? 'double inflection' : 'loop'
      }
    }
    return null
  }

  /**
   * A cheap conservative test, which nearly every curve fails, of whether the speed |C'| may have a small interior
   * minimum: |C'| < maxWidth |C''| (a sharp turn), or |C'|² <= |C''| dimension and |C'| < CUSP_MAX_WIDTH |C''| (a
   * cusp to regularize), with |C''| its larger endpoint value, against the smallest Bernstein coefficient of |C'|².
   */
  mayHaveSlowPoint(maxWidth: number, dimension: number): boolean {
    // The control points of the hodograph over 3.
    const q0 = this.p1.sub(this.p0)
    const q1 = this.p2.sub(this.p1)
    const q2 = this.p3.sub(this.p2)
    // With c2 the square of |C''| / 6, the limit on |C'|² / 9 is `sharp c2` for a sharp turn, and for a cusp both
    // `narrow c2` and the value whose square is `cusp c2`.
    const sharp = 4 * maxWidth * maxWidth
    const narrow = 4 * CUSP_MAX_WIDTH * CUSP_MAX_WIDTH
    const cusp = (4 / 9) * dimension * dimension
    if (q0.equals(Vec2.ZERO) || q2.equals(Vec2.ZERO)) {
      const [a, m] = zeroHandleFactor(q0, q1, q2)
      const aa = a.hypot2()
      const am = a.dot(m)
      const mm = m.hypot2()
      // The larger root of |a|² + 3 a.m t + 2 |m|² t² must lie in (0, 1). Written so that a NaN gives false.
      if (!(am < 0 && 9 * am * am > 8 * aa * mm && -3 * am < 4 * mm && aa + 3 * am + 2 * mm > 0)) {
        return false
      }
      // The quadratic's minimum is at -3 a.m / (4 |m|²), and |a + t m|² is least at -a.m / |m|² below 1, where it
      // is (|a|² |m|² - (a.m)²) / |m|², and otherwise at 1.
      const [smallest, below] = -am < mm ? [aa * mm - am * am, mm] : [aa + 2 * am + mm, 1]
      // A lower bound on |C'|² / 9 at the minimum, as a numerator and denominator.
      const bound = 9 * am * am * smallest
      const denominator = 16 * mm * mm * below
      // C'' / 6 is a / 2 at the end with the zero-length handle, and a / 2 + m at the other.
      const c2 = fmax(0.25 * aa, 0.25 * aa + am + mm)
      return !(bound > sharp * c2 * denominator
        && (bound > narrow * c2 * denominator || bound * bound > cusp * c2 * denominator * denominator))
    }
    const q0q0 = q0.hypot2()
    const q1q1 = q1.hypot2()
    const q2q2 = q2.hypot2()
    const q0q1 = q0.dot(q1)
    const q1q2 = q1.dot(q2)
    const q0q2 = q0.dot(q2)
    // The Bernstein coefficients of |C'|² / 9.
    const smallest = fmin(fmin(fmin(fmin(q0q0, q0q1), (2 * q1q1 + q0q2) * (1 / 3)), q1q2), q2q2)
    const c2 = fmax(q0q0 - 2 * q0q1 + q1q1, q1q1 - 2 * q1q2 + q2q2)
    return !(smallest > sharp * c2 && (smallest > narrow * c2 || smallest * smallest > cusp * c2))
  }

  /**
   * The interior parameters where the speed has a local minimum whose square may be as small as `speedSquared`,
   * increasing; at most two, possibly with others the caller tests. A minimum is a root of C'.C'' where it turns
   * from negative to positive.
   */
  slowPoints(speedSquared: number): number[] {
    const minima: number[] = []
    const q0 = this.p1.sub(this.p0)
    const q1 = this.p2.sub(this.p1)
    const q2 = this.p3.sub(this.p2)
    const limit = speedSquared * (1 / 9)
    if (q0.equals(Vec2.ZERO) || q2.equals(Vec2.ZERO)) {
      // C' / 3 = t (a + t m), so C'.C'' is t times |a|² + 3 a.m t + 2 |m|² t², and a minimum is at its larger root.
      const [a, m] = zeroHandleFactor(q0, q1, q2)
      const aa = a.hypot2()
      const am = a.dot(m)
      const mm = m.hypot2()
      const discriminant = 9 * am * am - 8 * aa * mm
      // Both roots are positive only if a.m is negative; a NaN gives no minimum. With both handles zero the root is
      // 1 up to rounding; the caller discards a point just below 1.
      if (am < 0 && discriminant > 0) {
        const t = (-3 * am + Math.sqrt(discriminant)) / (4 * mm)
        if (t < 1 && t * t * a.add(m.mul(t)).hypot2() <= limit) {
          minima.push(q0.equals(Vec2.ZERO) ? t : 1 - t)
        }
      }
      return minima
    }
    const q0q1 = q0.dot(q1)
    const q1q2 = q1.dot(q2)
    const q0q2 = q0.dot(q2)
    const q0q0 = q0.hypot2()
    const q1q1 = q1.hypot2()
    const q2q2 = q2.hypot2()
    // The Bernstein coefficients of C'.C'', times a positive constant. By the variation diminishing property a
    // rising root needs a negative coefficient followed by a positive one.
    const b = [3 * (q0q1 - q0q0), q0q2 - 3 * q0q1 + 2 * q1q1, 3 * q1q2 - 2 * q1q1 - q0q2, 3 * (q2q2 - q1q2)] as const
    let negative = false
    let rising = false
    for (const x of b) {
      negative ||= x < 0
      rising ||= negative && x > 0
    }
    if (!rising) {
      return minima
    }
    // With a nearly zero-length handle a root lies very close to an end, and the solver may return it just inside.
    const INSIDE = 1e-12
    const c0 = b[0]
    const c1 = 3 * (b[1] - b[0])
    const c2 = 3 * (b[0] - 2 * b[1] + b[2])
    const c3 = b[3] - 3 * b[2] + 3 * b[1] - b[0]
    for (const t of solveCubic(c0, c1, c2, c3)) {
      const slope = c1 + t * (2 * c2 + t * 3 * c3)
      if (t > INSIDE && t < 1 - INSIDE && slope > 0 && minima.length < 2) {
        minima.push(t)
      }
    }
    return minima.sort((x, y) => x - y)
  }
}

/** With a zero-length handle the hodograph over 3 is t (a + t m), t measured from that end: a and m. */
function zeroHandleFactor(q0: Vec2, q1: Vec2, q2: Vec2): [Vec2, Vec2] {
  const a = q1.mul(2)
  return [a, q0.equals(Vec2.ZERO) ? q2.sub(a) : q0.sub(a)]
}

/** The largest width |C'| / |C''|, in parameter units, of a minimum of the speed treated as a cusp (cubicbez.rs 63). */
const CUSP_MAX_WIDTH = 0.03

/** Real roots of c0 + c1 x + c2 x² + c3 x³ = 0 (Blinn, "How to Solve a Cubic Equation"). */
function solveCubic(c0: number, c1: number, c2: number, c3: number): number[] {
  const c3Recip = 1 / c3
  const ONETHIRD = 1 / 3
  const scaledC2 = c2 * (ONETHIRD * c3Recip)
  const scaledC1 = c1 * (ONETHIRD * c3Recip)
  const scaledC0 = c0 * c3Recip
  if (!(Number.isFinite(scaledC0) && Number.isFinite(scaledC1) && Number.isFinite(scaledC2))) {
    // The cubic coefficient is zero or nearly so.
    return solveQuadratic(c0, c1, c2)
  }
  const e0 = -scaledC2 * scaledC2 + scaledC1
  const e1 = -scaledC1 * scaledC2 + scaledC0
  const e2 = scaledC2 * scaledC0 - scaledC1 * scaledC1
  const discriminant = 4 * e0 * e2 - e1 * e1
  const depressed = -2 * scaledC2 * e0 + e1
  if (discriminant < 0) {
    const sq = Math.sqrt(-0.25 * discriminant)
    const r = -0.5 * depressed
    return [Math.cbrt(r + sq) + Math.cbrt(r - sq) - scaledC2]
  }
  if (discriminant === 0) {
    const t1 = copysign(Math.sqrt(-e0), depressed)
    return [t1 - scaledC2, -2 * t1 - scaledC2]
  }
  const th = Math.atan2(Math.sqrt(discriminant), -depressed) * ONETHIRD
  const thSin = Math.sin(th)
  const thCos = Math.cos(th)
  const ss3 = thSin * Math.sqrt(3)
  const t = 2 * Math.sqrt(-e0)
  return [t * thCos - scaledC2, t * (0.5 * (-thCos + ss3)) - scaledC2, t * (0.5 * (-thCos - ss3)) - scaledC2]
}

/**
 * Real roots of c0 + c1 x + c2 x² = 0, increasing. Nearly linear, it gives the root without the quadratic term;
 * with every coefficient zero, a single 0.
 */
export function solveQuadratic(c0: number, c1: number, c2: number): number[] {
  const sc0 = c0 * (1 / c2)
  const sc1 = c1 * (1 / c2)
  if (!Number.isFinite(sc0) || !Number.isFinite(sc1)) {
    // c2 is zero or very small: a linear equation.
    const root = -c0 / c1
    if (Number.isFinite(root)) {
      return [root]
    }
    return c0 === 0 && c1 === 0 ? [0] : []
  }
  const arg = sc1 * sc1 - 4 * sc0
  let root1: number
  if (!Number.isFinite(arg)) {
    // sc1² overflowed: one root from sc1 x + x² = 0, the other as sc0 / root1.
    root1 = -sc1
  } else {
    if (arg < 0) {
      return []
    }
    if (arg === 0) {
      return [-0.5 * sc1]
    }
    // math.stackexchange.com/questions/866331
    root1 = -0.5 * (sc1 + copysign(Math.sqrt(arg), sc1))
  }
  const root2 = sc0 / root1
  if (Number.isFinite(root2)) {
    return root2 > root1 ? [root1, root2] : [root2, root1]
  }
  return [root1]
}

/** The minimum distance of a subdivision point from either end of the piece, as a share of its parameter range. */
const SPLIT_MARGIN = 1e-6
/** The rounding allowance added to every bound, in ulps of the largest coordinate; see `accepts`. */
const ROUND_ULPS = 8
/**
 * The recursion depth at which a piece is emitted whether or not it was accepted. Near a sharp turn the offset
 * traces an arc of radius |d| over a tiny parameter interval, which bisection takes many levels to resolve; kurbo
 * found the value on a corpus of random curves.
 */
const MAX_DEPTH = 12
/** No cusp value is ever smaller than this in magnitude. */
const CUSP_EPSILON = 1e-12
/** The largest parameter distance from an end where the derivative vanishes to the offset's adjacent cusp. */
const STUB_MAX_PARAM = 0.05
/** The largest estimated stub, as a share of the tolerance, that is smoothed over rather than fitted. */
const STUB_MAX_EXTENT = 1 / 8
/** The smallest determinant for which a linear fit is tried: sin(theta), singular when straight or at a half turn. */
const DET_EPSILON = 1e-9
/** Side 0 is the offset by d, side 1 by -d. */
const SIDE_SIGNS = [1, -1] as const
/** The largest width of a tip, in parameter units. */
const TIP_MAX_WIDTH = 0.01
/**
 * The largest width of a sharp turn, in parameter units: the samples of the estimate are an eighth of the piece
 * apart, so a narrower turn can fall between them.
 */
const TURN_MAX_WIDTH = 0.03
/** The root subdivision points beside a tip, in widths of the tip. */
const TIP_REACH = 8
/** The same for a sharp turn that is not a tip. */
const TURN_REACH = 4
/** The largest ratio of the two ends' distances from a tip for which a piece is split at its midpoint. */
const TIP_SPLIT_RATIO = 2
/** The dimension below which a cusp of the source is regularized, as a share of the tolerance. */
const DIM_TUNE = 0.25

/** The output paths of the two sides; null for a side that is not wanted. */
type Outputs = [PathBuffer | null, PathBuffer | null]

/** Counted for the tests: pieces emitted at the maximum depth without being accepted. */
export const offsetCounters = { outOfDepth: 0 }

type Cubic8 = readonly [number, number, number, number, number, number, number, number]

function cubicOf(c: Cubic8): CubicBez {
  return new CubicBez(new Vec2(c[0], c[1]), new Vec2(c[2], c[3]), new Vec2(c[4], c[5]), new Vec2(c[6], c[7]))
}

/**
 * kurbo offset_cubic: the offset of c by d into `result` (cleared first), starting with a moveTo. d = 0 reproduces
 * the curve. For a nonzero d the control points must not all coincide.
 */
export function offsetCubic(c: Cubic8, d: number, tolerance: number, result: PathBuffer): void {
  const cubic = cubicOf(c)
  result.clear()
  if (d === 0) {
    result.moveTo(c[0], c[1])
    result.curveTo(c[2], c[3], c[4], c[5], c[6], c[7])
    return
  }
  assertNotPoint(cubic)
  const [tan0] = cubic.tangents()
  const start = cubic.p0.add(tan0.normalize().turn90().mul(d))
  result.moveTo(start.x, start.y)
  offsetSides(cubic, d, tolerance, [result, null])
}

/** kurbo offset_cubic_pair: appends the offsets of c by +d and -d as curveTo segments to paths that already have a current point. */
export function offsetCubicPair(c: Cubic8, d: number, tolerance: number, positive: PathBuffer, negative: PathBuffer): void {
  if (d === 0) {
    positive.curveTo(c[2], c[3], c[4], c[5], c[6], c[7])
    negative.curveTo(c[2], c[3], c[4], c[5], c[6], c[7])
    return
  }
  const cubic = cubicOf(c)
  assertNotPoint(cubic)
  offsetSides(cubic, d, tolerance, [positive, negative])
}

/** A curve that is one point has no normal: every piece would be rejected to the maximum depth, not finite. */
function assertNotPoint(c: CubicBez): void {
  if (c.p0.equals(c.p1) && c.p1.equals(c.p2) && c.p2.equals(c.p3)) {
    throw new Error('offset of a cubic that is a single point')
  }
}

function offsetSides(c: CubicBez, d: number, tolerance: number, out: Outputs): void {
  const sides: [boolean, boolean] = [out[0] !== null, out[1] !== null]
  if (!c.mayHaveSlowPoint(TURN_MAX_WIDTH, tolerance * DIM_TUNE)) {
    const co = new CubicOffset(c, d, tolerance, NO_TIPS)
    co.offsetRec(co.root(c), sides, out)
    return
  }
  // The curve may hold a sharp turn: regularize it and split it at its turns first.
  const [regularized, tips] = regularizeAndSplitTips(c, tolerance)
  const co = new CubicOffset(regularized, d, tolerance, tips.tips)
  const root = co.root(c)
  if (tips.splits.length === 0) {
    co.offsetRec(root, sides, out)
    return
  }
  // The pieces of the root subdivision are at depth 1, which `split` gives them; depth 0 is the whole curve.
  let rest = root
  for (const t of tips.splits) {
    const qT = co.q.eval(t)
    // At an exact cusp of the source there is no tangent, so no subdivision there.
    if (!(qT.hypot2() > 0)) {
      continue
    }
    const [piece, after] = split(rest, t, qT.normalize(), co.curvatureTimesD(t))
    co.offsetRec({ ...piece, depth: 1 }, sides, out)
    rest = after
  }
  co.offsetRec({ ...rest, depth: 1 }, sides, out)
}

/** The tips of a curve, and its root subdivision points for its tips and other sharp turns. */
interface TipSplits {
  /** The parameters of the tips; NaN where there is none. */
  tips: [number, number]
  /** The root subdivision points, increasing; at most 6. */
  splits: number[]
}

/** The tips of a curve that has none. */
export const NO_TIPS: readonly [number, number] = [NaN, NaN]

/**
 * The regularized curve and its root subdivision: at each sharp turn (an interior minimum of |C'| narrower than
 * TURN_MAX_WIDTH in w = |C'| / |C''|), and TURN_REACH widths (TIP_REACH for a tip) on either side. A turn inside a
 * piece is invisible to the samples and to the guards, which compare the cusp values at the ends.
 */
function regularizeAndSplitTips(c: CubicBez, tolerance: number): [CubicBez, TipSplits] {
  const dimension = tolerance * DIM_TUNE
  const minima = c.slowPoints(fmax(c.cuspSpeedSquared(dimension), turnSpeedSquared(c)))
  if (minima.length === 0) {
    return [c, { tips: [NaN, NaN], splits: [] }]
  }
  const regularized = c.regularizeCuspAt(dimension, minima)
  if (regularized === null) {
    return [c, tipSplitsAt(c, minima)]
  }
  // Regularization moved the control points, and with them the minima of the speed.
  return [regularized, tipSplitsAt(regularized, regularized.slowPoints(turnSpeedSquared(regularized)))]
}

/** An upper bound on |C'|² at a sharp turn: there |C'| < TURN_MAX_WIDTH |C''|, and |C''| peaks at an endpoint. */
function turnSpeedSquared(c: CubicBez): number {
  const c2Start = c.p2.sub(c.p1).sub(c.p1.sub(c.p0))
  const c2End = c.p3.sub(c.p2).sub(c.p2.sub(c.p1))
  return 36 * TURN_MAX_WIDTH * TURN_MAX_WIDTH * fmax(c2Start.hypot2(), c2End.hypot2())
}

function tipSplitsAt(c: CubicBez, minima: readonly number[]): TipSplits {
  // Subdivision points closer together than this are merged.
  const MIN_GAP = 1e-14
  const q = c.deriv()
  const result: TipSplits = { tips: [NaN, NaN], splits: [] }
  let nTips = 0
  let last = 0
  for (const t of minima) {
    const speedSquared = q.eval(t).hypot2()
    const widthSquared = speedSquared / q.deriv().eval(t).hypot2()
    // Written so that a NaN is not a turn.
    if (!(widthSquared < TURN_MAX_WIDTH * TURN_MAX_WIDTH)) {
      continue
    }
    let reach = TURN_REACH
    if (widthSquared < TIP_MAX_WIDTH * TIP_MAX_WIDTH) {
      result.tips[nTips] = t
      nTips += 1
      reach = TIP_REACH
    }
    reach *= Math.sqrt(widthSquared)
    for (const at of [t - reach, t, t + reach]) {
      if (at > last + MIN_GAP && at < 1 - MIN_GAP) {
        result.splits.push(at)
        last = at
      }
    }
  }
  return result
}

/** One end of a curve or a piece: Start at the smaller parameter, End at the larger. */
const START = 0
const END = 1
type EndpointSide = typeof START | typeof END

/** A piece of the source curve in the recursive subdivision. */
export interface OffsetRec {
  t0: number
  t1: number
  /** Unit tangents at t0 and t1. */
  utan0: Vec2
  utan1: Vec2
  /** Cusp values (curvature times the offset distance, plus one) at t0 and t1, per side. */
  cusp0: [number, number]
  cusp1: [number, number]
  depth: number
}

/** The two pieces of a split at t, where the unit tangent is utanT and the curvature times d is kappaD. */
function split(rec: OffsetRec, t: number, utanT: Vec2, kappaD: number): [OffsetRec, OffsetRec] {
  return [
    { ...rec, cusp0: [...rec.cusp0], t1: t, utan1: utanT, cusp1: [kappaD + 1, -kappaD + 1], depth: rec.depth + 1 },
    { ...rec, cusp1: [...rec.cusp1], t0: t, utan0: utanT, cusp0: [kappaD + 1, -kappaD + 1], depth: rec.depth + 1 },
  ]
}

/** An approximation to the offset of one piece, A = C + d D, with its delta D. */
export interface Fit {
  approx: CubicBez
  /** D's control points are vectors. */
  delta: CubicBez
  /** A handle was clamped to zero length, so the approximation differs from the one the parameters describe. */
  clamped: boolean
}

/** The decision on a fit: accepted, and where to split it (local parameter) if the estimate found two cusps inside. */
interface Acceptance {
  accepted: boolean
  split: number | null
}

/** The state for the offset curves of one cubic, on one side or both. */
export class CubicOffset {
  /** The derivative of c. */
  readonly q: QuadBez
  /** c0 + c1 t + c2 t² is the cross product of the second and first derivatives, times d. */
  readonly c0: number
  readonly c1: number
  readonly c2: number
  /** Whether the offset has a stub smoothed over rather than fitted, by end, then by side; see `endCusp`. */
  readonly smoothedStub: [[boolean, boolean], [boolean, boolean]] = [[false, false], [false, false]]

  /** `c` has been regularized; `d` is side 0's distance; `tips` are NaN where the curve has fewer than two. */
  constructor(readonly c: CubicBez, readonly d: number, readonly tolerance: number, readonly tips: readonly [number, number]) {
    const q = c.deriv()
    const d2 = 2 * d
    const p1xp0 = q.p1.cross(q.p0)
    const p2xp0 = q.p2.cross(q.p0)
    const p2xp1 = q.p2.cross(q.p1)
    this.q = q
    this.c0 = d2 * p1xp0
    this.c1 = d2 * (p2xp0 - 2 * p1xp0)
    this.c2 = d2 * (p2xp1 - p2xp0 + p1xp0)
  }

  /** The piece that is the whole curve, with the end tangents of `c`, the curve before regularization. */
  root(c: CubicBez): OffsetRec {
    const [tan0, tan1] = c.tangents()
    const cusp0 = this.endCusp(START)
    const cusp1 = this.endCusp(END)
    return { t0: 0, t1: 1, utan0: tan0.normalize(), utan1: tan1.normalize(), cusp0, cusp1, depth: 0 }
  }

  /**
   * The source's curvature at t times d. One plus it is side 0's cusp value and one minus it side 1's: the offset
   * has a cusp where it crosses zero, where the radius of curvature equals the offset distance.
   */
  curvatureTimesD(t: number): number {
    const ds2 = this.q.eval(t).hypot2()
    return ((this.c2 * t + this.c1) * t + this.c0) / (ds2 * Math.sqrt(ds2))
  }

  /**
   * Both sides' cusp values at t = 0 or t = 1. Where the derivative vanishes there, the cusp value is infinite with
   * the sign of c2 on side 0: on the inside the offset has a cusp near the end and runs back to it (a stub). Only its
   * sign matters, so its magnitude is taken as 1; it is reported negative, so the cusp is found and the stub fitted,
   * unless the stub is too small to matter, which is recorded for `handles`.
   */
  endCusp(end: EndpointSide): [number, number] {
    const [tan, y] = end === START ? [this.q.p0, this.c0] : [this.q.p2, this.c0 + this.c1 + this.c2]
    const TAN_DIST_EPSILON = 1e-12
    const tanDist = tan.hypot()
    if (tanDist >= TAN_DIST_EPSILON) {
      const rsqrt = 1 / tanDist
      const kappaD = y * (rsqrt * rsqrt * rsqrt)
      return [kappaD + 1, -kappaD + 1]
    }
    const cusp: [number, number] = [1, 1]
    // The side on the inside of the curve, if there is one.
    if (this.c2 !== 0 && !Number.isNaN(this.c2)) {
      const side = this.c2 > 0 ? 1 : 0
      if (this.smoothsStub(end)) {
        this.smoothedStub[end][side] = true
      } else {
        cusp[side] = -1
      }
    }
    return cusp
  }

  /**
   * Whether the stub at an end where the derivative vanishes is small enough to smooth over. To leading order the
   * offset moves |q1| u² - d k u along the end tangent, k = (q1 × q_other) / (2 |q1|²): it turns back at
   * u = d k / (2 |q1|), having travelled (d k)² / (4 |q1|). A smoothed stub keeps the cusp value 1, but its fit keeps
   * a handle pointing back along the stub, where clamping it would fall back to arc drawing, several times out.
   */
  smoothsStub(end: EndpointSide): boolean {
    const q1 = this.q.p1
    const other = end === START ? this.q.p2 : this.q.p0
    // With m = |d (q1 × q_other)| the cusp is at u = m / (4 |q1|³) and the stub's extent m² / (16 |q1|⁵), both
    // compared without division.
    const len2 = q1.hypot2()
    const len = Math.sqrt(len2)
    const m = Math.abs(this.d * q1.cross(other))
    return len2 > 0 && m <= 4 * STUB_MAX_PARAM * len2 * len
      && m * m <= 16 * STUB_MAX_EXTENT * this.tolerance * len2 * len2 * len
  }

  /** Whether `side` has a smoothed stub at the given end of `rec`, which is then also an end of the curve. */
  smoothedAt(rec: OffsetRec, end: EndpointSide, side: number): boolean {
    const atCurveEnd = end === START ? rec.t0 === 0 : rec.t1 === 1
    return atCurveEnd && this.smoothedStub[end][side]!
  }

  distance(side: number): number {
    return SIDE_SIGNS[side as 0 | 1] * this.d
  }

  /**
   * Offsets one piece for the sides in `sides`, subdividing it as far as the harder side needs. A side that accepts
   * the piece emits it and drops out. The subdivision point depends on the source only, so the sides left share the
   * halves, except where a side's offset has a cusp in a piece it rejects: that side goes on alone, split at the cusp.
   */
  offsetRec(rec: OffsetRec, pending: readonly [boolean, boolean], out: Outputs): void {
    // Each call has its own copy, as Rust passes the array by value: a half's decisions are not the other's.
    const sides: [boolean, boolean] = [pending[0], pending[1]]
    const seg = this.piece(rec)
    const emit = (side: number, approx: CubicBez): void => {
      out[side]?.curveTo(approx.p1.x, approx.p1.y, approx.p2.x, approx.p2.y, approx.p3.x, approx.p3.y)
    }

    // Arc drawing is tried first, on the bounds that need nothing but the fit; see `acceptArc`.
    const triesArc = this.triesArc(rec)
    const isSmall = seg.p3.sub(seg.p0).hypot2() <= this.tolerance * this.tolerance
    const arc: { params: [number, number] | null } = { params: null }
    if (triesArc || isSmall) {
      const params = drawArc(rec.utan0, rec.utan1)
      arc.params = params
      const hull: { known: boolean; bound: number | null } = { known: false, bound: null }
      for (let side = 0; side < 2; side += 1) {
        if (sides[side]) {
          const approx = this.acceptArc(side, seg, rec, params, triesArc, isSmall, hull)
          if (approx !== null) {
            emit(side, approx)
            sides[side] = false
          }
        }
      }
      if (!sides[0] && !sides[1]) {
        return
      }
    }

    // An inflection is the cusp value crossing one. There the end tangents may be parallel, where one-point is
    // singular, so two-point linear is used; otherwise one-point, of higher order and controlling the slip too.
    const hasInflection = [0, 1].some((side) => (rec.cusp0[side]! - 1) * (rec.cusp1[side]! - 1) < 0)
    const linearFit = hasInflection ? twoPoint(seg, rec) : onePoint(seg, rec)
    const isTwoPoint = hasInflection && linearFit !== null
    // At the maximum depth an approximation is emitted even though it was not accepted.
    const atLimit = rec.depth >= MAX_DEPTH

    // The samples depend on the side only through D, the same on both sides unless a handle was clamped, so the
    // second side usually finds the samples it needs.
    const first = sides[0] ? 0 : 1
    const fit = this.fit(first, seg, rec, linearFit, arc)
    let samples = new Samples(seg, fit.delta, this.d)
    this.decide(first, rec, fit, samples, isTwoPoint, atLimit, sides, out, emit)
    if (first === 0 && sides[1]) {
      const secondFit = this.fit(1, seg, rec, linearFit, arc)
      if (!secondFit.delta.equals(fit.delta)) {
        samples = new Samples(seg, secondFit.delta, this.d)
      }
      this.decide(1, rec, secondFit, samples, isTwoPoint, atLimit, sides, out, emit)
    }
    if (!sides[0] && !sides[1]) {
      return
    }
    const [t, utan] = this.findSubdivisionPoint(rec)
    const [rec0, rec1] = split(rec, t, utan, this.curvatureTimesD(t))
    this.offsetRec(rec0, sides, out)
    this.offsetRec(rec1, sides, out)
  }

  /** The block `offset_rec` runs for each side in turn: accept, split at a cusp, emit at the limit, or split at a sample. */
  decide(side: number, rec: OffsetRec, fit: Fit, samples: Samples, isTwoPoint: boolean, atLimit: boolean,
    sides: [boolean, boolean], out: Outputs, emit: (side: number, approx: CubicBez) => void): void {
    const acceptance = this.acceptFit(side, rec, fit, samples, isTwoPoint)
    let { accepted } = acceptance
    if (accepted && this.atStub(side, rec)) {
      accepted = !this.overshootsStub(side, rec, fit.approx)
    }
    if (!accepted && rec.cusp0[side]! * rec.cusp1[side]! < 0) {
      sides[side] = false
      this.splitAtCusp(side, rec, out)
    } else if (accepted || atLimit) {
      if (!accepted) {
        offsetCounters.outOfDepth += 1
      }
      emit(side, fit.approx)
      sides[side] = false
    } else if (acceptance.split !== null) {
      if (this.splitAtSample(side, rec, acceptance.split, out)) {
        sides[side] = false
      }
    }
  }

  /**
   * `side`'s cusp value times |C'|³, and its derivative in t: the cusp value's sign without its pole where the
   * derivative vanishes, better suited to Newton's method.
   */
  scaledCuspAndDeriv(side: number, t: number): [number, number] {
    const q = this.q.eval(t)
    const qDeriv = this.q.deriv().eval(t)
    const m = q.hypot2()
    const mDeriv = 2 * q.dot(qDeriv)
    const sign = SIDE_SIGNS[side as 0 | 1]
    const n = sign * ((this.c2 * t + this.c1) * t + this.c0)
    const nDeriv = sign * (2 * this.c2 * t + this.c1)
    const speed = Math.sqrt(m)
    return [n + m * speed, nDeriv + 1.5 * speed * mDeriv]
  }

  /**
   * A zero of `side`'s cusp value in [a, b], where s times it is negative at a and positive at b: Newton's method on
   * `scaledCuspAndDeriv`, safeguarded by bisection, to about 1e-12 in t.
   */
  findCusp(side: number, a: number, b: number, s: number): number {
    const EPS = 1e-12
    let t = 0.5 * (a + b)
    let stepOld = b - a
    for (let i = 0; i < 100; i += 1) {
      const [rawY, rawDy] = this.scaledCuspAndDeriv(side, t)
      const y = s * rawY
      const dy = s * rawDy
      if (y > 0) {
        b = t
      } else if (y < 0) {
        a = t
      } else {
        return t
      }
      if (b - a <= 2 * EPS) {
        break
      }
      const step = y / dy
      // A step this small ends the search wherever it leads: Newton's method usually approaches from one side, and
      // its last steps are lost to rounding; treating them as leaving the bracket would bisect the whole bracket.
      if (Math.abs(step) < EPS) {
        const x = t - step
        return x < a ? a : x > b ? b : x
      }
      const newton = t - step
      // Bisect if the Newton step leaves the bracket or converges no faster than bisection would.
      if (newton > a && newton < b && Math.abs(2 * y) <= Math.abs(stepOld * dy)) {
        stepOld = Math.abs(step)
        t = newton
      } else {
        stepOld = 0.5 * (b - a)
        t = 0.5 * (a + b)
      }
    }
    return 0.5 * (a + b)
  }

  /**
   * Splits a piece at the cusp of `side`'s offset inside it and offsets both halves for that side alone, without
   * counting the split toward the depth limit. Just before and after the cusp the cusp value is a signed
   * infinitesimal, CUSP_EPSILON with the sign of the adjacent end.
   */
  splitAtCusp(side: number, rec: OffsetRec, out: Outputs): void {
    const only: [boolean, boolean] = [false, false]
    only[side] = true
    const a = rec.t0
    const b = rec.t1
    const t = this.findCusp(side, a, b, signum(rec.cusp1[side]!))
    // A root at an end means the sign there is spurious, as where the derivative vanishes and the stub was smoothed
    // over: the whole piece is past the cusp. That end takes the other one's sign instead of a zero-length split.
    const margin = SPLIT_MARGIN * (b - a)
    if (t <= a + margin || t >= b - margin) {
      const moved: OffsetRec = { ...rec, cusp0: [...rec.cusp0], cusp1: [...rec.cusp1] }
      if (t <= a + margin) {
        moved.cusp0[side] = copysign(CUSP_EPSILON, rec.cusp1[side]!)
      } else {
        moved.cusp1[side] = copysign(CUSP_EPSILON, rec.cusp0[side]!)
      }
      this.offsetRec(moved, only, out)
      return
    }
    // The derivative may vanish near an end of the piece, where the solver may converge: use the end tangent then.
    const UTAN_EPSILON = 1e-12
    const qT = this.q.eval(t)
    const utan = qT.hypot2() >= UTAN_EPSILON ? qT.normalize() : t - a < b - t ? rec.utan0 : rec.utan1
    const [rec0, rec1] = split(rec, t, utan, this.curvatureTimesD(t))
    rec0.cusp1[side] = copysign(CUSP_EPSILON, rec.cusp0[side]!)
    rec1.cusp0[side] = copysign(CUSP_EPSILON, rec.cusp1[side]!)
    rec0.depth = Math.max(rec.depth, 1)
    rec1.depth = rec0.depth
    this.offsetRec(rec0, only, out)
    this.offsetRec(rec1, only, out)
  }

  /**
   * Splits a piece for `side` alone at the local parameter u of a sample whose cusp value has the sign opposite to
   * both ends, so each half holds a detectable sign change. False, leaving the piece to the shared subdivision, if
   * the derivative vanishes there.
   */
  splitAtSample(side: number, rec: OffsetRec, u: number, out: Outputs): boolean {
    const UTAN_EPSILON = 1e-12
    const t = rec.t0 + u * (rec.t1 - rec.t0)
    const qT = this.q.eval(t)
    if (qT.hypot2() < UTAN_EPSILON) {
      return false
    }
    const only: [boolean, boolean] = [false, false]
    only[side] = true
    const [rec0, rec1] = split(rec, t, qT.normalize(), this.curvatureTimesD(t))
    this.offsetRec(rec0, only, out)
    this.offsetRec(rec1, only, out)
    return true
  }

  /** A piece's fit on one side: the linear fit, or arc drawing if there is none or it would clamp a handle. */
  fit(side: number, seg: CubicBez, rec: OffsetRec, linearFit: [number, number] | null,
    arc: { params: [number, number] | null }): Fit {
    if (linearFit !== null && !this.clamps(side, seg, rec, linearFit[0], linearFit[1])) {
      return this.apply(side, seg, rec, linearFit[0], linearFit[1])
    }
    arc.params ??= drawArc(rec.utan0, rec.utan1)
    return this.apply(side, seg, rec, arc.params[0], arc.params[1])
  }

  /** Whether a piece's fit is accepted on one side by the estimate at the samples. */
  acceptFit(side: number, rec: OffsetRec, fit: Fit, samples: Samples, isTwoPoint: boolean): Acceptance {
    const cusp0 = rec.cusp0[side]!
    const cusp1 = rec.cusp1[side]!
    const limit = this.tolerance / (ESTIMATE_SAFETY * Math.abs(this.d))
    const sideSign = SIDE_SIGNS[side as 0 | 1]
    // A piece holding a cusp of the offset is judged by the distance at equal parameter, still a bound across the
    // cusp; it accepts a swallowtail or stub small against the tolerance, and a piece it rejects is split there.
    if (cusp0 * cusp1 < 0) {
      return { accepted: samples.equalParameterWithin(limit), split: null }
    }
    // The osculating estimate is trusted where its model holds: the curvature times d varies little over the piece,
    // no sample is at a cusp, and a two-point fit does not slip far.
    const cuspSign = copysign(1, cusp0)
    if (this.triesOsculating(cusp0, cusp1)) {
      // Only the two-point fit leaves the slip free; limiting it for the others sent most pieces with a vanishing
      // end derivative to the fallback.
      const maxSlip = isTwoPoint && !fit.clamped ? OSCULATING_MAX_SLIP : Infinity
      const result = samples.osculating(sideSign, limit, maxSlip, cuspSign)
      if (result.kind === 'decided') {
        return { accepted: result.accepted, split: null }
      }
      // A sample's cusp value has the sign opposite to both ends: two cusps at least, split at that sample.
      if (result.kind === 'cusp') {
        return { accepted: false, split: result.u }
      }
    }
    const fallback = samples.osculating(sideSign, limit, OSCULATING_MAX_SLIP, cuspSign)
    const accepted = this.acceptFallback(cusp0, cusp1) && fallback.kind === 'decided' && fallback.accepted
    return { accepted, split: null }
  }

  /** Whether a piece starts or ends, on `side`, at a smoothed stub; see `overshootsStub`. */
  atStub(side: number, rec: OffsetRec): boolean {
    return this.smoothedAt(rec, START, side) || this.smoothedAt(rec, END, side)
  }

  /**
   * Whether the approximation of a piece at a smoothed stub runs past that end by more than the tolerance. Its
   * handle points back along the stub; the estimate continues the offset past the end and does not see it.
   * Conservative: a part beyond the end along the handle counts as overshoot.
   */
  overshootsStub(side: number, rec: OffsetRec, approx: CubicBez): boolean {
    const runsPast = (c: CubicBez): boolean => {
      // With the other two control points behind the end along the handle, the overshoot is at most 4/9 of it.
      const handle = c.p2.sub(c.p3)
      const behind = c.p0.sub(c.p3).dot(handle) <= 0 && c.p1.sub(c.p3).dot(handle) <= 0
      const short = (4 / 9) * (4 / 9) * handle.hypot2() <= this.tolerance * this.tolerance
      return !(behind && short) && overshoot(c) > this.tolerance
    }
    const reversed = new CubicBez(approx.p3, approx.p2, approx.p1, approx.p0)
    return (this.smoothedAt(rec, START, side) && runsPast(reversed)) || (this.smoothedAt(rec, END, side) && runsPast(approx))
  }

  /**
   * Whether `acceptArc` is worth trying on a piece whose chord is not small: the hull bound needs the end normals
   * times d within the tolerance of each other, and the hairline bound a thin enough stroke.
   */
  triesArc(rec: OffsetRec): boolean {
    return this.d * this.d * rec.utan1.sub(rec.utan0).hypot2() <= this.tolerance * this.tolerance
      || 2 * Math.abs(this.d) <= this.tolerance
  }

  /**
   * Arc drawing for a piece on one side, if a strict geometric bound accepts it: the hairline bound for a thin
   * stroke, the hull bound for a piece that hardly turns, and, for a chord under the tolerance (at or near the tip
   * of a near-cusp), the small-piece bound. With no handle clamped the hull bound is the same on both sides, so
   * `hull` keeps it for the other.
   */
  acceptArc(side: number, seg: CubicBez, rec: OffsetRec, arc: [number, number], triesArc: boolean, isSmall: boolean,
    hull: { known: boolean; bound: number | null }): CubicBez | null {
    const d = this.distance(side)
    const arcFit = this.apply(side, seg, rec, arc[0], arc[1])
    const within = (bound: number | null): boolean => bound !== null && this.accepts(bound, seg, arcFit.approx)
    const cachedHull = (): number | null => {
      if (!hull.known) {
        hull.bound = hullBound(seg, rec, arcFit.delta, d)
        hull.known = true
      }
      return hull.bound
    }
    const accepted = (triesArc
      && ((2 * Math.abs(d) <= this.tolerance && within(hairlineBound(arcFit.delta, d)))
        || within(arcFit.clamped ? hullBound(seg, rec, arcFit.delta, d) : cachedHull())))
      || (isSmall && within(smallPieceBound(seg, rec, this.hodographMiddle(rec), arcFit, d)))
    return accepted ? arcFit.approx : null
  }

  /**
   * Whether to trust the osculating estimate: the cusp value has one sign at both ends and its magnitude varies by
   * at most OSCULATING_MAX_CUSP_RATIO. Negative at both ends, the piece is past a cusp and the estimate holds with
   * the signs reversed.
   */
  triesOsculating(cusp0: number, cusp1: number): boolean {
    const cuspMin = fmin(Math.abs(cusp0), Math.abs(cusp1))
    const cuspMax = fmax(Math.abs(cusp0), Math.abs(cusp1))
    // An end made by a split at a cusp has CUSP_EPSILON, to which the ratio does not apply.
    return cusp0 * cusp1 > 0 && (cuspMax <= OSCULATING_MAX_CUSP_RATIO * cuspMin || cuspMin === CUSP_EPSILON)
  }

  /**
   * Whether a piece `triesOsculating` excludes, or whose estimate was not trusted, may still be accepted with the
   * slip limit on every fit: a looser ratio, FALLBACK_MAX_CUSP_RATIO, applied only with both cusp values positive.
   */
  acceptFallback(cusp0: number, cusp1: number): boolean {
    // With the curvature times d concentrated at one end, so is the error, and it can fall between the samples.
    const cuspMin = fmin(cusp0, cusp1)
    const cuspMax = fmax(cusp0, cusp1)
    if (cuspMin > 0 && cuspMin !== CUSP_EPSILON && cuspMax > FALLBACK_MAX_CUSP_RATIO * cuspMin) {
      return false
    }
    // The estimate needs one sign of the cusp value at both ends.
    return cusp0 * cusp1 > 0
  }

  /**
   * Whether `bound` on the error of `approx` puts it within the tolerance, allowing for the rounding of its control
   * points in absolute coordinates, which no bound on D sees: a few ulps of the largest coordinate.
   */
  accepts(bound: number, seg: CubicBez, approx: CubicBez): boolean {
    let largest = 0
    for (const p of [seg.p0, seg.p1, seg.p2, seg.p3, approx.p0, approx.p1, approx.p2, approx.p3]) {
      largest = fmax(fmax(largest, Math.abs(p.x)), Math.abs(p.y))
    }
    return bound + ROUND_ULPS * Number.EPSILON * largest <= this.tolerance
  }

  /** The piece of the source `rec` covers. */
  piece(rec: OffsetRec): CubicBez {
    return rec.depth === 0 ? this.c : this.c.subsegment(rec.t0, rec.t1)
  }

  /**
   * The approximation on one side for the parameters (a, b): D's control points are n0, n0 + a utan0, n1 + b utan1
   * and n1. A handle that would point against the offset's direction of travel is clamped to zero length.
   */
  apply(side: number, seg: CubicBez, rec: OffsetRec, a: number, b: number): Fit {
    const d = this.distance(side)
    const n0 = rec.utan0.turn90()
    const n1 = rec.utan1.turn90()
    const p0 = seg.p0.add(n0.mul(d))
    const p3 = seg.p3.add(n1.mul(d))
    const [l0, l1, clamp0, clamp1] = this.handles(side, seg, rec, a, b)
    // D directly, not by subtracting control points of A and C, whose coordinates may dwarf the offset distance.
    let p1 = p0.add(rec.utan0.mul(l0))
    let delta1 = n0.add(rec.utan0.mul(a))
    if (clamp0) {
      p1 = p0
      delta1 = p1.sub(seg.p1).div(d)
    }
    let p2 = p3.sub(rec.utan1.mul(l1))
    let delta2 = n1.add(rec.utan1.mul(b))
    if (clamp1) {
      p2 = p3
      delta2 = p2.sub(seg.p2).div(d)
    }
    return { approx: new CubicBez(p0, p1, p2, p3), delta: new CubicBez(n0, delta1, delta2, n1), clamped: clamp0 || clamp1 }
  }

  /** The approximation's handle lengths for (a, b), and whether `apply` clamps each to zero. */
  handles(side: number, seg: CubicBez, rec: OffsetRec, a: number, b: number): [number, number, boolean, boolean] {
    const d = this.distance(side)
    const l0 = seg.p1.sub(seg.p0).dot(rec.utan0) + a * d
    const l1 = seg.p3.sub(seg.p2).dot(rec.utan1) - b * d
    // Where the cusp value is negative the offset, and its handle, run backward; at a smoothed stub the offset
    // arrives running backward whatever the cusp value.
    const flip0 = this.smoothedAt(rec, START, side) ? -1 : 1
    const flip1 = this.smoothedAt(rec, END, side) ? -1 : 1
    return [l0, l1, l0 * rec.cusp0[side]! * flip0 <= 0, l1 * rec.cusp1[side]! * flip1 <= 0]
  }

  /** Whether `apply` would clamp a handle, far cheaper than the fit itself. */
  clamps(side: number, seg: CubicBez, rec: OffsetRec, a: number, b: number): boolean {
    const [, , clamp0, clamp1] = this.handles(side, seg, rec, a, b)
    return clamp0 || clamp1
  }

  /**
   * Where to split a piece that was not accepted, and the unit tangent there: the parameter midpoint, except for a
   * piece reaching toward a tip from some distance; see `splitTowardTip`.
   */
  findSubdivisionPoint(rec: OffsetRec): [number, Vec2] {
    if (!Number.isNaN(this.tips[0])) {
      const point = this.splitTowardTip(rec)
      if (point !== null) {
        return point
      }
    }
    const t = 0.5 * (rec.t0 + rec.t1)
    const qT = this.q.eval(t)
    // Near a tip the speed is tiny but the tangent still defined, so only an exact zero is refused.
    return [t, qT.hypot2() > 0 ? qT.normalize() : rec.utan0]
  }

  /**
   * The split of a piece reaching toward a tip from some distance: beyond a few widths the curvature decays as the
   * inverse cube of the distance from the tip, so a piece spanning a large ratio of distances is split at their
   * geometric mean; the midpoint would take many levels to reach the turn.
   */
  splitTowardTip(rec: OffsetRec): [number, Vec2] | null {
    // The tip nearest the piece, the ends' distances from it and the direction away from it. A tip is never inside
    // a piece: the root subdivision holds it.
    let nearest: [number, number, number, number] | null = null
    for (const tip of this.tips) {
      let near: number
      let far: number
      let away: number
      if (tip <= rec.t0) {
        [near, far, away] = [rec.t0 - tip, rec.t1 - tip, 1]
      } else if (tip >= rec.t1) {
        [near, far, away] = [tip - rec.t1, tip - rec.t0, -1]
      } else {
        // No tip (NaN), or a tip inside the piece.
        continue
      }
      if (nearest === null || near < nearest[0]) {
        nearest = [near, far, tip, away]
      }
    }
    if (nearest === null) {
      return null
    }
    const [near, far, tip, away] = nearest
    // A piece that starts at the tip, or is not long against its distance from it, is split at its midpoint.
    if (!(near > 0 && far > TIP_SPLIT_RATIO * near)) {
      return null
    }
    const t = tip + away * Math.sqrt(near * far)
    const qT = this.q.eval(t)
    return qT.hypot2() > 0 ? [t, qT.normalize()] : null
  }

  /**
   * The middle control point of the piece's hodograph, up to scale: the derivative at its start plus half its
   * parameter length times the second derivative there. Subtracting the piece's control points would give rounding
   * error for a piece a few ulps across, as at the tip of a near-cusp.
   */
  hodographMiddle(rec: OffsetRec): Vec2 {
    return this.q.eval(rec.t0).add(this.q.deriv().eval(rec.t0).mul(0.5 * (rec.t1 - rec.t0)))
  }
}

// The fits: each gives the parameters (a, b) that `apply` takes, independent of the offset distance.

/**
 * Arc drawing: D is the standard cubic of the unit circle's arc between the end normals. Always defined, exact for
 * a straight piece; error of fourth order in the piece's length. The handle is 4/3 tan(theta / 4)
 * = 4/3 sin(theta / 2) / (1 + cos(theta / 2)), theta from utan1 to utan0.
 */
export function drawArc(utan0: Vec2, utan1: Vec2): [number, number] {
  const cosTh = utan1.dot(utan0)
  const sinTh = utan1.cross(utan0)
  const cosHalf = Math.sqrt(fmax(0.5 * (1 + cosTh), 0))
  const sinHalf = copysign(Math.sqrt(fmax(0.5 * (1 - cosTh), 0)), sinTh)
  const a = ((4 / 3) * sinHalf) / (1 + cosHalf)
  return [a, -a]
}

/**
 * One-point shape control (Yang and Huang, 1993): D(1/2) = n(1/2), so the approximation meets the offset at the
 * same parameter there; fifth order. Null when the end tangents are parallel or the midpoint derivative vanishes.
 */
export function onePoint(seg: CubicBez, rec: OffsetRec): [number, number] | null {
  const det = rec.utan0.cross(rec.utan1)
  if (Math.abs(det) < DET_EPSILON) {
    return null
  }
  const n0 = rec.utan0.turn90()
  const n1 = rec.utan1.turn90()
  const nMid = unitNormal(seg, 0.5)
  if (nMid === null) {
    return null
  }
  // D(1/2) = (n0 + n1) / 2 + 3/8 (a utan0 + b utan1) must equal nMid.
  const rhs = nMid.sub(n0.add(n1).mul(0.5)).mul(8 / 3)
  return [rhs.cross(rec.utan1) / det, rec.utan0.cross(rhs) / det]
}

/**
 * Two-point linear: D(t) · n(t) = 1 at t = 1/3 and 2/3. Well conditioned with parallel end tangents, as at an
 * inflection, where it is used; it leaves the slip free, which the vanishing curvature there makes harmless. Null
 * when singular, as for a parabola.
 */
function twoPoint(seg: CubicBez, rec: OffsetRec): [number, number] | null {
  const n0 = rec.utan0.turn90()
  const n1 = rec.utan1.turn90()
  const matrix = [[0, 0], [0, 0]]
  const rhs = [0, 0]
  for (const [i, t] of [1 / 3, 2 / 3].entries()) {
    const n = unitNormal(seg, t)
    if (n === null) {
      return null
    }
    const mt = 1 - t
    // The Bernstein basis of the four control points at t.
    const w0 = mt * mt * mt
    const w1 = 3 * mt * mt * t
    const w2 = 3 * mt * t * t
    const w3 = t * t * t
    matrix[i] = [w1 * rec.utan0.dot(n), w2 * rec.utan1.dot(n)]
    rhs[i] = 1 - n0.mul(w0 + w1).add(n1.mul(w2 + w3)).dot(n)
  }
  const [[m00, m01], [m10, m11]] = matrix as [[number, number], [number, number]]
  const det = m00 * m11 - m01 * m10
  if (Math.abs(det) < DET_EPSILON) {
    return null
  }
  return [(rhs[0]! * m11 - m01 * rhs[1]!) / det, (m00 * rhs[1]! - rhs[0]! * m10) / det]
}

/** A cubic's unit normal at t, or null where its derivative vanishes. */
function unitNormal(c: CubicBez, t: number): Vec2 | null {
  const TAN_EPSILON = 1e-12
  const tan = c.deriv().eval(t)
  const len = tan.hypot()
  if (len < TAN_EPSILON) {
    return null
  }
  return tan.turn90().div(len)
}

/**
 * A bound on the distance at equal parameter, |d| |D(t) - n(t)|, from convex hulls alone: D(t) is in its control
 * points' hull, and n(t) on the arc between the normals of the hodograph's control points, so the distance peaks at
 * a hull vertex and an arc end. First order in the turning; null if the cone spans a quarter turn or more.
 */
export function hullBound(seg: CubicBez, rec: OffsetRec, delta: CubicBez, d: number): number | null {
  const TAN_EPSILON = 1e-12
  // The directions of the hodograph's control points: the end tangents, and the middle one if nonzero.
  const directions = [rec.utan0]
  const middle = seg.p2.sub(seg.p1)
  const middleLen = middle.hypot()
  if (middleLen > TAN_EPSILON) {
    directions.push(middle.div(middleLen))
  }
  directions.push(rec.utan1)
  for (let i = 0; i < directions.length; i += 1) {
    for (let j = 0; j < i; j += 1) {
      if (!(directions[i]!.dot(directions[j]!) > 0)) {
        return null
      }
    }
  }
  let worst = 0
  for (const direction of directions) {
    const normal = direction.turn90()
    for (const vertex of [delta.p0, delta.p1, delta.p2, delta.p3]) {
      // The argument needs each vertex within a quarter turn of each normal.
      if (!(vertex.dot(normal) > 0)) {
        return null
      }
      worst = fmax(worst, vertex.sub(normal).hypot2())
    }
  }
  return Math.abs(d) * Math.sqrt(worst)
}

/**
 * A bound on the distance at equal parameter for any piece and fit, by the triangle inequality: |d| times one plus
 * D's largest control point. Never below 2 |d|, so only of use for a stroke thin against the tolerance.
 */
export function hairlineBound(delta: CubicBez, d: number): number {
  let largest = 0
  for (const p of [delta.p0, delta.p1, delta.p2, delta.p3]) {
    largest = fmax(largest, p.hypot2())
  }
  return Math.abs(d) * (Math.sqrt(largest) + 1)
}

/**
 * A bound on arc drawing's error on a small piece. If the tangent turns monotonically, the source normal and arc
 * drawing's D both traverse the same arc once; pairing points of equal normal direction, the Fréchet distance is at
 * most the piece's diameter plus |d| epsilon, independent of the parametrization, which near a cusp of the source
 * turns very unevenly. Null if the tangent might not turn monotonically, turns more than about a right angle, or the
 * approximation is not plain arc drawing.
 */
export function smallPieceBound(seg: CubicBez, rec: OffsetRec, middle: Vec2, arc: Fit, d: number): number | null {
  if (arc.clamped) {
    return null
  }
  // Monotonic turning: the hodograph's control points in angular order, with the end tangents for the outer ones,
  // which may vanish, and `middle` for the middle one.
  const turn0 = rec.utan0.cross(middle)
  const turn1 = middle.cross(rec.utan1)
  const turn = rec.utan0.cross(rec.utan1)
  const allOneWay = (turn0 >= 0 && turn1 >= 0 && turn >= 0) || (turn0 <= 0 && turn1 <= 0 && turn <= 0)
  // Only turns for which the angular order is unambiguous.
  const cosTh = rec.utan0.dot(rec.utan1)
  if (!allOneWay || !(cosTh > 0) || !(rec.utan0.dot(middle) >= 0)) {
    return null
  }
  // The diameter is at most the largest distance between two control points.
  const points = [seg.p0, seg.p1, seg.p2, seg.p3]
  let diameterSquared = 0
  for (let i = 0; i < 4; i += 1) {
    for (let j = 0; j < i; j += 1) {
      diameterSquared = fmax(diameterSquared, points[i]!.sub(points[j]!).hypot2())
    }
  }
  // The standard arc approximation's largest radial error at angle theta: 2/27 sin⁶(theta / 4) / cos²(theta / 4).
  const cosHalf = Math.sqrt(0.5 * (1 + cosTh))
  const cosQuarterSquared = 0.5 * (1 + cosHalf)
  const sinQuarterSquared = 0.5 * (1 - cosHalf)
  const epsilon = ((2 / 27) * (sinQuarterSquared * sinQuarterSquared * sinQuarterSquared)) / cosQuarterSquared
  return Math.sqrt(diameterSquared) + Math.abs(d) * epsilon
}

/** How far c runs past its end point, along the direction from that point to the last control point but one. */
function overshoot(c: CubicBez): number {
  const toP2 = c.p2.sub(c.p3)
  const length = toP2.hypot()
  if (!(length > 0)) {
    return 0
  }
  const dir = toP2.div(length)
  const x = [c.p0.sub(c.p3).dot(dir), c.p1.sub(c.p3).dot(dir), length, 0] as const
  const at = (u: number): number => {
    const mu = 1 - u
    return x[0] * mu * mu * mu + 3 * mu * u * (x[1] * mu + x[2] * u) + x[3] * u * u * u
  }
  // The derivative over 3, in the power basis.
  const a = x[1] - x[0]
  const b = x[2] - x[1]
  const e = x[3] - x[2]
  let most = fmax(x[0], 0)
  for (const u of solveQuadratic(a, 2 * (b - a), a - 2 * b + e)) {
    if (u > 0 && u < 1) {
      most = fmax(most, at(u))
    }
  }
  return most
}

// The error estimate.

/**
 * The samples of the estimate, at the midpoints of 8 equal intervals, in the order in which the first sample whose
 * cusp value has the wrong sign or whose error is over the limit decides. Fewer samples missed many more errors.
 */
const SAMPLES = [0.3125, 0.6875, 0.1875, 0.8125, 0.4375, 0.5625, 0.0625, 0.9375] as const
/** The safety factor on the estimate before it meets the tolerance, fitted against Fréchet errors by kurbo. */
const ESTIMATE_SAFETY = 1.2
/**
 * The largest slip (D's tangential part) for which the estimate is trusted: the two-point fit leaves it free, and a
 * large one makes the estimate fall short. The first pass limits only two-point, the second every fit.
 */
const OSCULATING_MAX_SLIP = 0.5
/** The largest ratio of the end cusp values for which the estimate is trusted; only signs are checked inside. */
const OSCULATING_MAX_CUSP_RATIO = 3
/** The same for the fallback; above it the curvature is concentrated at one end. */
const FALLBACK_MAX_CUSP_RATIO = 64

/**
 * The estimate's quantities at each sample for a piece C and delta D: with A = C'⊥ · D, B = C' · D, K = C'' × C'
 * and s = |C'|, these are A s, s², s³, K d, K d B² / 2 and B², d side 0's distance (side 1 flips the terms in d).
 * The error in units of d against the offset's osculating parabola is A / s - 1 + K d B² / (2 s² E), E = s³ + K d
 * (s³ times the cusp value); it is within `limit` when |A s E - s² E + K d B² / 2| <= limit |s² E|, and the slip
 * within `maxSlip` when B² <= maxSlip² s². One square root per sample, no division.
 */
export class Samples {
  readonly aS: number[] = []
  readonly s2: number[] = []
  readonly s3: number[] = []
  readonly kD: number[] = []
  readonly halfKBb: number[] = []
  readonly bb: number[] = []

  constructor(seg: CubicBez, delta: CubicBez, d: number) {
    const c1 = [seg.p1.sub(seg.p0).mul(3), seg.p2.sub(seg.p1).mul(3), seg.p3.sub(seg.p2).mul(3)] as const
    const c2 = [c1[1].sub(c1[0]).mul(2), c1[2].sub(c1[1]).mul(2)] as const
    for (const t of SAMPLES) {
      const mt = 1 - t
      const dT = delta.p0.mul(mt * mt * mt).add(delta.p1.mul(3 * mt * mt * t)).add(delta.p2.mul(3 * mt * t * t))
        .add(delta.p3.mul(t * t * t))
      const q = c1[0].mul(mt * mt).add(c1[1].mul(2 * mt * t)).add(c1[2].mul(t * t))
      const qq = c2[0].mul(mt).add(c2[1].mul(t))
      const s2 = q.hypot2()
      const b = q.dot(dT)
      const s = Math.sqrt(s2)
      const a = q.turn90().dot(dT)
      const kD = qq.cross(q) * d
      this.aS.push(a * s)
      this.s2.push(s2)
      this.s3.push(s2 * s)
      this.kD.push(kD)
      this.halfKBb.push(0.5 * kD * b * b)
      this.bb.push(b * b)
    }
  }

  /** Whether the distance at equal parameter, |D - n| in units of d, is within `limit` at every sample. */
  equalParameterWithin(limit: number): boolean {
    // Its square is ((A - s)² + B²) / s², compared with both sides times s⁴.
    const limitSquared = limit * limit
    let within = true
    for (let i = 0; i < 8; i += 1) {
      const radial = this.aS[i]! - this.s2[i]!
      // Written so that a NaN rejects.
      within &&= radial * radial + this.bb[i]! * this.s2[i]! <= limitSquared * this.s2[i]! * this.s2[i]!
    }
    return within
  }

  /**
   * The osculating estimate's decision for the side of sign `sideSign`, made by the first sample whose cusp value
   * lacks the sign of `cuspSign` (two cusps inside, which the ends do not show) or whose error is over `limit`.
   * Excess slip matters only if no sample rejects: then the estimate is not trusted to accept.
   */
  osculating(sideSign: number, limit: number, maxSlip: number, cuspSign: number): Osculating {
    const e: number[] = []
    const numerator: number[] = []
    const denominator: number[] = []
    for (let i = 0; i < 8; i += 1) {
      e.push(this.s3[i]! + sideSign * this.kD[i]!)
      denominator.push(this.s2[i]! * e[i]!)
      numerator.push(this.aS[i]! * e[i]! - denominator[i]! + sideSign * this.halfKBb[i]!)
    }
    const slipScale = maxSlip * maxSlip
    let trusted = true
    for (let i = 0; i < 8; i += 1) {
      // The cusp value here is e / s3, and s3 is not negative.
      if (e[i]! * cuspSign <= 0) {
        return { kind: 'cusp', u: SAMPLES[i]! }
      }
      // Written so that a NaN rejects.
      if (!(Math.abs(numerator[i]!) <= limit * Math.abs(denominator[i]!))) {
        return { kind: 'decided', accepted: false }
      }
      trusted &&= this.bb[i]! <= slipScale * this.s2[i]!
    }
    return trusted ? { kind: 'decided', accepted: true } : { kind: 'untrusted' }
  }
}

/** `decided`: every sample within the limit and the slip (true), or one over it (false). `untrusted`: within, but
 * the slip over its limit somewhere. `cusp`: a sample's cusp value lacks the ends' sign, at local parameter u. */
type Osculating = { kind: 'decided'; accepted: boolean } | { kind: 'untrusted' } | { kind: 'cusp'; u: number }

/** The constants the tests check against kurbo's. */
export const OFFSET_LIMITS = { MAX_DEPTH, ROUND_ULPS } as const
