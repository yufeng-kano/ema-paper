/**
 * Affine helpers for the page scene (docs/pdf-viewer.md § The WebGPU renderer, *The page scene*),
 * and the two shapes every scene module shares: a matrix and a box.
 *
 * Ported from pdf.js 6.5.438, src/shared/util.js (`Util.transform`, `applyTransform`,
 * `inverseTransform`, `axialAlignedBoundingBox`, `singularValueDecompose2dScale`, `intersect`;
 * build/pdf.mjs 557-664), Apache-2.0, Copyright 2024 Mozilla Foundation (THIRD_PARTY_NOTICES § 1).
 * Changed: pure functions that return new values instead of writing into their arguments (but
 * `union`), `invert` answers null for a singular matrix, and `intersect` takes boxes already
 * ordered min to max. docs/pdf-viewer.md § What is taken from where.
 */

/** `[a, b, c, d, e, f]`, pdf.js's order: x' = a·x + c·y + e, y' = b·x + d·y + f. */
export type Mat6 = [number, number, number, number, number, number]

/** `[minX, minY, maxX, maxY]`. */
export type Box4 = [number, number, number, number]

export const IDENTITY: Readonly<Mat6> = Object.freeze([1, 0, 0, 1, 0, 0])

/**
 * The box that holds nothing: intersected with any box it gives null, and a union leaves the other
 * box as it was. pdf.js starts a path's min-max from the same values, so an empty path reads the same.
 */
export const EMPTY_BOX: Readonly<Box4> = Object.freeze([Infinity, Infinity, -Infinity, -Infinity])

/** `m · n`: `n` applied first, then `m`, as pdf.js's `Util.transform(m, n)`. */
export function multiply(m: Readonly<Mat6>, n: Readonly<Mat6>): Mat6 {
  return [
    m[0] * n[0] + m[2] * n[1],
    m[1] * n[0] + m[3] * n[1],
    m[0] * n[2] + m[2] * n[3],
    m[1] * n[2] + m[3] * n[3],
    m[0] * n[4] + m[2] * n[5] + m[4],
    m[1] * n[4] + m[3] * n[5] + m[5],
  ]
}

export function apply(m: Readonly<Mat6>, x: number, y: number): [number, number] {
  return [x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]]
}

export function invert(m: Readonly<Mat6>): Mat6 | null {
  const d = m[0] * m[3] - m[1] * m[2]
  if (d === 0) return null
  return [m[3] / d, -m[1] / d, -m[2] / d, m[0] / d, (m[2] * m[5] - m[4] * m[3]) / d, (m[4] * m[1] - m[5] * m[0]) / d]
}

/** The axis-aligned hull of `box`'s four corners under `m`. */
export function boxThrough(m: Readonly<Mat6>, box: Readonly<Box4>): Box4 {
  // Infinite corners would give NaN under a zero entry of `m`.
  if (box[0] > box[2] || box[1] > box[3]) return [...EMPTY_BOX]
  // pdf.js's order of operations, so a box matches the one its Canvas 2D records to the last bit.
  const [x0, y0, x1, y1] = box
  const left = m[0] * x0 + m[4]
  const right = m[0] * x1 + m[4]
  const top = m[3] * y0 + m[5]
  const bottom = m[3] * y1 + m[5]
  const xs = [left + m[2] * y0, right + m[2] * y1, left + m[2] * y1, right + m[2] * y0]
  const ys = [top + m[1] * x0, bottom + m[1] * x1, top + m[1] * x1, bottom + m[1] * x0]
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)]
}

/**
 * The largest singular value of `m`'s linear part: how far one unit can stretch under it. Unlike
 * `max(|a|, |d|)` it is right for a rotation or a shear. A singular value of 0 reads as 1, as in pdf.js,
 * so a degenerate matrix never makes a width or a tolerance 0.
 */
export function maxScale(m: Readonly<Mat6>): number {
  const a = m[0] ** 2 + m[1] ** 2
  const b = m[0] * m[2] + m[1] * m[3]
  const c = m[2] ** 2 + m[3] ** 2
  const first = (a + c) / 2
  const second = Math.sqrt(first ** 2 - (a * c - b ** 2))
  return Math.sqrt(first + second || 1)
}

/**
 * The overlap of two boxes, or null when there is none. Boxes that only touch overlap in a line, as in
 * pdf.js. pdf.js also reorders each box's corners first; scene boxes are always min to max, and
 * reordering would turn `EMPTY_BOX` into the whole plane.
 */
export function intersect(a: Readonly<Box4>, b: Readonly<Box4>): Box4 | null {
  const minX = Math.max(a[0], b[0])
  const maxX = Math.min(a[2], b[2])
  if (minX > maxX) return null
  const minY = Math.max(a[1], b[1])
  const maxY = Math.min(a[3], b[3])
  return minY > maxY ? null : [minX, minY, maxX, maxY]
}

/** Grows `a` to hold `b`, in place, and returns `a`. */
export function union(a: Box4, b: Readonly<Box4>): Box4 {
  a[0] = Math.min(a[0], b[0])
  a[1] = Math.min(a[1], b[1])
  a[2] = Math.max(a[2], b[2])
  a[3] = Math.max(a[3], b[3])
  return a
}
