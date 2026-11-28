/**
 * A filled path flattened into lines for the strip rasteriser (docs/pdf-viewer.md § Stage 4: the WebGPU core).
 *
 * Ported from Vello 0.4.0 (vello_common, git 570f0c4): flatten.rs 178-286, flatten_simd.rs 49-261 and 645-683,
 * Apache-2.0 OR MIT, THIRD_PARTY_NOTICES § 8; with kurbo git 6bc7521 (unreleased, after 0.13.1) quadbez.rs 57-100
 * and 204-221, bezpath.rs 622-700, cubicbez.rs 87-106, 760-779, 818-826 and 993-1009, line.rs 162-185, § 9.
 * Changed: reads DrawOPS with the matrix applied while reading, and a drawing op with no current point starts a
 * subpath at its first point, as Path2D does; doubles, lines stored as f32; kurbo's scalar subdivision, not
 * flatten_simd's lanes; no SIMD; a NaN in any point read drops the path.
 */
import type { Mat6 } from '../scene/matrix.ts'
import { DRAW_OPS } from '../scene/ops.ts'
import type { Lines } from './buffers.ts'

/** Device px; Vello's tolerance. */
export const TOLERANCE = 0.25
const SQRT_TOL = 0.5
const TOL_2 = TOLERANCE * TOLERANCE
/** The share of the tolerance a cubic's quadratic approximation may use (kurbo bezpath.rs 555). */
const TO_QUAD_TOL = 0.1
/** Vello's bound on the quadratics per cubic; only reached under very large transforms. */
const MAX_QUADS = 16
/** n^6 for n = 1..16: Vello's `estimate` of ceil(err^(1/6)) without a pow, so every engine agrees. */
const QUAD_LUT = [1, 64, 729, 4096, 15625, 46656, 117649, 262144, 531441, 1000000, 1771561, 2985984, 4826809, 7529536,
  11390625, 16777216]
const D_POWI_4 = 0.67 * 0.67 * (0.67 * 0.67)

/** One cubic's quadratics, 6 doubles each, and their subdivision parameters a0 a2 u0 uscale val. */
const quads = new Float64Array(6 * MAX_QUADS)
const params = new Float64Array(5 * MAX_QUADS)

/** An approximation to the integral of (1 + 4x²)^-0.25. */
function approxParabolaIntegral(x: number): number {
  return x / (1 - 0.67 + Math.sqrt(Math.sqrt(D_POWI_4 + 0.25 * x * x)))
}

function approxParabolaInvIntegral(x: number): number {
  const B = 0.39
  return x * (1 - B + Math.sqrt(B * B + 0.25 * x * x))
}

/** Rust's f64::signum: ±1 for ±0, NaN for NaN. */
function signum(x: number): number {
  if (Number.isNaN(x)) {
    return NaN
  }
  return x > 0 || (x === 0 && 1 / x > 0) ? 1 : -1
}

/** kurbo QuadBez::estimate_subdiv: the map of the quad onto y = x², into params[5k, 5k + 5). */
function estimateSubdiv(x0: number, y0: number, x1: number, y1: number, x2: number, y2: number, sqrtTol: number,
  k: number): void {
  const d01x = x1 - x0
  const d01y = y1 - y0
  const d12x = x2 - x1
  const d12y = y2 - y1
  const ddx = d01x - d12x
  const ddy = d01y - d12y
  const cross = (x2 - x0) * ddy - (y2 - y0) * ddx
  const p0 = (d01x * ddx + d01y * ddy) * (1 / cross)
  const p2 = (d12x * ddx + d12y * ddy) * (1 / cross)
  const scale = Math.abs(cross / (Math.sqrt(ddx * ddx + ddy * ddy) * (p2 - p0)))
  const a0 = approxParabolaIntegral(p0)
  const a2 = approxParabolaIntegral(p2)
  let val = 0
  if (Number.isFinite(scale)) {
    const da = Math.abs(a2 - a0)
    const sqrtScale = Math.sqrt(scale)
    if (signum(p0) === signum(p2)) {
      val = da * sqrtScale
    } else {
      // The segment holds the curvature maximum (a cusp of the parabola map).
      const xmin = sqrtTol / sqrtScale
      val = (sqrtTol * da) / approxParabolaIntegral(xmin)
    }
  }
  const u0 = approxParabolaInvIntegral(a0)
  const u2 = approxParabolaInvIntegral(a2)
  params[5 * k] = a0
  params[5 * k + 1] = a2
  params[5 * k + 2] = u0
  params[5 * k + 3] = 1 / (u2 - u0)
  params[5 * k + 4] = val
}

/** kurbo QuadBez::determine_subdiv_t: maps u in 0..1 to the quad's parameter. */
function subdivT(k: number, u: number): number {
  const a = params[5 * k]! + (params[5 * k + 1]! - params[5 * k]!) * u
  return (approxParabolaInvIntegral(a) - params[5 * k + 2]!) * params[5 * k + 3]!
}

/** kurbo Line::nearest's squared distance from (px, py) to the segment (ax, ay)-(bx, by). */
function distanceSqToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax
  const dy = by - ay
  const vx = px - ax
  const vy = py - ay
  const t = (dx * vx + dy * vy) / (dx * dx + dy * dy)
  // Rust's max(0).min(1): a NaN or -Infinity (a zero-length segment) clamps to 0, +Infinity to 1.
  const u = t > 0 ? (t < 1 ? t : 1) : 0
  const ex = vx - u * dx
  const ey = vy - u * dy
  return ex * ex + ey * ey
}

/**
 * Lines of path[start, end) (DrawOPS) under m (user -> target px), culled to [0, 0, width, height] as Vello's fill
 * callback does: right of, above (top rounded down to a tile row) or below the target becomes a moveTo; wholly left
 * becomes one straight line carrying winding only; subpaths close implicitly. A NaN anywhere drops the path.
 * `out` is cleared first.
 */
export function flattenFill(path: ArrayLike<number>, start: number, end: number, m: Readonly<Mat6>,
  width: number, height: number, out: Lines): void {
  out.clear()
  // Vello rounds the top down to a strip row so no intermediate tile is lost; a target starts at row 0.
  const left = 0
  const top = 0
  const right = width
  const bottom = height
  // Vello's fill callback: the start of the line it emits next, as f32.
  let penX = 0
  let penY = 0
  let isNan = false
  const moveTo = (x: number, y: number): void => {
    penX = Math.fround(x)
    penY = Math.fround(y)
  }
  const lineTo = (x: number, y: number): void => {
    const fx = Math.fround(x)
    const fy = Math.fround(y)
    out.push(penX, penY, fx, fy)
    penX = fx
    penY = fy
  }
  // A segment wholly right of, above or below the target adds neither coverage nor winding.
  const culled = (minX: number, minY: number, maxY: number): boolean => minX > right || maxY < top || minY > bottom

  let started = false
  let startX = 0
  let startY = 0
  let lastX = 0
  let lastY = 0
  const beginSubpath = (x: number, y: number): void => {
    if (started && (lastX !== startX || lastY !== startY)
      && !culled(Math.min(lastX, startX), Math.min(lastY, startY), Math.max(lastY, startY))) {
      lineTo(startX, startY)
    }
    started = true
    startX = lastX = x
    startY = lastY = y
    moveTo(x, y)
  }

  const [a, b, c, d, e, f] = m
  let i = start
  while (i < end) {
    const op = path[i]!
    if (op === DRAW_OPS.closePath) {
      i += 1
      if (started && (lastX !== startX || lastY !== startY)) {
        if (culled(Math.min(lastX, startX), Math.min(lastY, startY), Math.max(lastY, startY))) {
          moveTo(startX, startY)
        } else {
          lineTo(startX, startY)
        }
        // After a close the next segment starts at the closed subpath's start (kurbo, Path2D).
        lastX = startX
        lastY = startY
      }
      continue
    }
    if (op !== DRAW_OPS.moveTo && op !== DRAW_OPS.lineTo && op !== DRAW_OPS.curveTo
      && op !== DRAW_OPS.quadraticCurveTo) {
      // pdf.js skips an unknown op code alone and reads on.
      i += 1
      continue
    }
    const ux = path[i + 1]!
    const uy = path[i + 2]!
    const x1 = ux * a + uy * c + e
    const y1 = ux * b + uy * d + f
    isNan ||= Number.isNaN(x1) || Number.isNaN(y1)
    if (op === DRAW_OPS.moveTo || !started) {
      beginSubpath(x1, y1)
      if (op === DRAW_OPS.moveTo || op === DRAW_OPS.lineTo) {
        i += 3
        continue
      }
    }
    if (op === DRAW_OPS.lineTo) {
      i += 3
      if (culled(Math.min(lastX, x1), Math.min(lastY, y1), Math.max(lastY, y1))) {
        moveTo(x1, y1)
      } else {
        lineTo(x1, y1)
      }
      lastX = x1
      lastY = y1
      continue
    }
    const vx = path[i + 3]!
    const vy = path[i + 4]!
    const x2 = vx * a + vy * c + e
    const y2 = vx * b + vy * d + f
    isNan ||= Number.isNaN(x2) || Number.isNaN(y2)
    const x0 = lastX
    const y0 = lastY
    if (op === DRAW_OPS.quadraticCurveTo) {
      i += 5
      if (culled(Math.min(x0, x1, x2), Math.min(y0, y1, y2), Math.max(y0, y1, y2))) {
        moveTo(x2, y2)
      } else if (Math.max(x0, x1, x2) < left
        || distanceSqToSegment(x1, y1, x0, y0, x2, y2) <= 4 * TOL_2) {
        // Wholly left only carries winding, and a control point within 2·tol of the chord keeps the curve within
        // tol of it (the bound is half the control point's distance).
        lineTo(x2, y2)
      } else {
        estimateSubdiv(x0, y0, x1, y1, x2, y2, SQRT_TOL, 0)
        const count = Math.ceil((0.5 / SQRT_TOL) * params[4]!)
        // `as usize` takes NaN to 0, then max(1).
        const n = count >= 1 ? count : 1
        for (let k = 1; k < n; k += 1) {
          const t = subdivT(0, k / n)
          const mt = 1 - t
          lineTo(x0 * (mt * mt) + (x1 * (mt * 2) + x2 * t) * t, y0 * (mt * mt) + (y1 * (mt * 2) + y2 * t) * t)
        }
        lineTo(x2, y2)
      }
      lastX = x2
      lastY = y2
      continue
    }
    const wx = path[i + 5]!
    const wy = path[i + 6]!
    const x3 = wx * a + wy * c + e
    const y3 = wx * b + wy * d + f
    isNan ||= Number.isNaN(x3) || Number.isNaN(y3)
    i += 7
    if (culled(Math.min(x0, x1, x2, x3), Math.min(y0, y1, y2, y3), Math.max(y0, y1, y2, y3))) {
      moveTo(x3, y3)
    } else if (Math.max(x0, x1, x2, x3) < left
      || Math.max(distanceSqToSegment(x1, y1, x0, y0, x3, y3), distanceSqToSegment(x2, y2, x0, y0, x3, y3))
      <= (16 / 9) * TOL_2) {
      // The curve stays within 3/4 of its farther control point's distance from the chord.
      lineTo(x3, y3)
    } else {
      flattenCubic(x0, y0, x1, y1, x2, y2, x3, y3, lineTo)
      // The end point exactly, so the subpath stays watertight.
      lineTo(x3, y3)
    }
    lastX = x3
    lastY = y3
  }
  if (started && (lastX !== startX || lastY !== startY)
    && !culled(Math.min(lastX, startX), Math.min(lastY, startY), Math.max(lastY, startY))) {
    lineTo(startX, startY)
  }
  // A path holding NaN is ill-defined (Vello ignores it).
  if (isNan) {
    out.clear()
  }
}

/**
 * kurbo's scalar flatten of a cubic (bezpath.rs 656-698): split into quadratics by `to_quads` (cubicbez.rs 87-106,
 * 993-1009), their count clamped as Vello's, then points spread evenly over the sum of the quads' parabola
 * integrals. Emits the interior points only.
 */
function flattenCubic(x0: number, y0: number, x1: number, y1: number, x2: number, y2: number, x3: number,
  y3: number, lineTo: (x: number, y: number) => void): void {
  const accuracy = TOLERANCE * TO_QUAD_TOL
  // The square of 36 / sqrt(3), the bound on a cubic's distance from its best quadratic per third derivative.
  const maxHypot2 = 432 * accuracy * accuracy
  const ex = 3 * x2 - x3 - (3 * x1 - x0)
  const ey = 3 * y2 - y3 - (3 * y1 - y0)
  const errDiv = (ex * ex + ey * ey) / maxHypot2
  let nQuads = MAX_QUADS
  for (let k = 0; k < MAX_QUADS; k += 1) {
    if (errDiv <= QUAD_LUT[k]!) {
      nQuads = k + 1
      break
    }
  }
  // The derivative's control points, for kurbo's CubicBez::subsegment.
  const qx0 = 3 * (x1 - x0)
  const qy0 = 3 * (y1 - y0)
  const qx1 = 3 * (x2 - x1)
  const qy1 = 3 * (y2 - y1)
  const qx2 = 3 * (x3 - x2)
  const qy2 = 3 * (y3 - y2)
  const sqrtRemainTol = SQRT_TOL * Math.sqrt(1 - TO_QUAD_TOL)
  let sum = 0
  for (let k = 0; k < nQuads; k += 1) {
    const t0 = k / nQuads
    const t1 = (k + 1) / nQuads
    let mt = 1 - t0
    const s0x = x0 * (mt * mt * mt) + (x1 * (mt * mt * 3) + (x2 * (mt * 3) + x3 * t0) * t0) * t0
    const s0y = y0 * (mt * mt * mt) + (y1 * (mt * mt * 3) + (y2 * (mt * 3) + y3 * t0) * t0) * t0
    const d0x = qx0 * (mt * mt) + (qx1 * (mt * 2) + qx2 * t0) * t0
    const d0y = qy0 * (mt * mt) + (qy1 * (mt * 2) + qy2 * t0) * t0
    mt = 1 - t1
    const s3x = x0 * (mt * mt * mt) + (x1 * (mt * mt * 3) + (x2 * (mt * 3) + x3 * t1) * t1) * t1
    const s3y = y0 * (mt * mt * mt) + (y1 * (mt * mt * 3) + (y2 * (mt * 3) + y3 * t1) * t1) * t1
    const d1x = qx0 * (mt * mt) + (qx1 * (mt * 2) + qx2 * t1) * t1
    const d1y = qy0 * (mt * mt) + (qy1 * (mt * 2) + qy2 * t1) * t1
    const scale = (t1 - t0) * (1 / 3)
    const s1x = s0x + scale * d0x
    const s1y = s0y + scale * d0y
    const s2x = s3x - scale * d1x
    const s2y = s3y - scale * d1y
    const cx = (3 * s1x - s0x + (3 * s2x - s3x)) / 4
    const cy = (3 * s1y - s0y + (3 * s2y - s3y)) / 4
    quads[6 * k] = s0x
    quads[6 * k + 1] = s0y
    quads[6 * k + 2] = cx
    quads[6 * k + 3] = cy
    quads[6 * k + 4] = s3x
    quads[6 * k + 5] = s3y
    estimateSubdiv(s0x, s0y, cx, cy, s3x, s3y, sqrtRemainTol, k)
    sum += params[5 * k + 4]!
  }
  const count = Math.ceil((0.5 * sum) / sqrtRemainTol)
  const n = count >= 1 ? count : 1
  const step = sum / n
  let i = 1
  let valSum = 0
  for (let k = 0; k < nQuads; k += 1) {
    const val = params[5 * k + 4]!
    let target = i * step
    const recipVal = 1 / val
    while (target < valSum + val) {
      const t = subdivT(k, (target - valSum) * recipVal)
      const mt = 1 - t
      const q = 6 * k
      lineTo(quads[q]! * (mt * mt) + (quads[q + 2]! * (mt * 2) + quads[q + 4]! * t) * t,
        quads[q + 1]! * (mt * mt) + (quads[q + 3]! * (mt * 2) + quads[q + 5]! * t) * t)
      i += 1
      if (i === n + 1) {
        break
      }
      target = i * step
    }
    valSum += val
  }
}
