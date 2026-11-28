/**
 * How an image is sampled: pdf.js's smoothing rule and how far its pre-halving would go
 * (docs/pdf-viewer.md § Stage 4: the WebGPU core), and the texture an image is uploaded as.
 *
 * Ported from pdf.js 6.5.438 (build/pdf.mjs 632-644, 9872-9879, 10106-10160: `Util.singularValueDecompose2dScale`,
 * `getImageSmoothingEnabled`, `CanvasGraphics#_scaleImage`), Apache-2.0, Copyright 2024 Mozilla Foundation,
 * THIRD_PARTY_NOTICES § 1.
 * Changed: the halving count, not the halved canvas; the smaller count of the two axes for one mip chain.
 */
import { invert, type Mat6 } from '../scene/matrix.ts'
import { MAX_TEXTURE_SIDE } from './protocol.ts'

/** pdf.js's PixelsPerInch.PDF_TO_CSS_UNITS, CSS / PDF, divided first as pdf.js does. */
const PDF_TO_CSS_UNITS = 96 / 72

/** pdf.js Util.singularValueDecompose2dScale (B 632-644): the two scale factors of m's 2x2 part. */
export function singularValues(m: Readonly<Mat6>): [number, number] {
  const a = m[0] ** 2 + m[1] ** 2
  const b = m[0] * m[2] + m[1] * m[3]
  const c = m[2] ** 2 + m[3] ** 2
  const first = (a + c) / 2
  const second = Math.sqrt(first ** 2 - (a * c - b ** 2))
  // pdf.js's `|| 1`: a zero (or NaN) square becomes 1, so a degenerate matrix reads as unscaled.
  return [Math.sqrt(first + second || 1), Math.sqrt(first - second || 1)]
}

/**
 * pdf.js getImageSmoothingEnabled (B 9872-9879): linear when /Interpolate, or when both singular values of
 * `deviceFromImage` (device px per delivered image px) are <= Math.fround(dpr * 96 / 72); nearest otherwise.
 * `dpr` is devicePixelRatio (OutputScale.pixelRatio, B 911-913), never the target's own scale.
 */
export function smoothing(deviceFromImage: Readonly<Mat6>, interpolate: boolean, dpr: number): boolean {
  if (interpolate) {
    return true
  }
  const [first, second] = singularValues(deviceFromImage)
  const actualScale = Math.fround(dpr * PDF_TO_CSS_UNITS)
  return first <= actualScale && second <= actualScale
}

/**
 * The mip level pdf.js's _scaleImage (B 10106-10160) would reach: with inv = invert(deviceFromTexture),
 * widthScale = max(hypot(inv[0], inv[1]), 1), heightScale = max(hypot(inv[2], inv[3]), 1), count the halvings of
 * each axis by pdf.js's loop (size = ceil(size / 2), scale /= size / newSize, while scale > 2 and size > 1), and
 * return the smaller count, clamped to mipLevelCount(width, height) - 1.
 */
export function mipLevel(deviceFromTexture: Readonly<Mat6>, width: number, height: number): number {
  const inverse = invert(deviceFromTexture)
  // pdf.js's inverse of a singular matrix is NaN throughout, and a NaN scale never halves.
  if (inverse === null) {
    return 0
  }
  let widthScale = Math.max(Math.hypot(inverse[0], inverse[1]), 1)
  let heightScale = Math.max(Math.hypot(inverse[2], inverse[3]), 1)
  let paintWidth = width
  let paintHeight = height
  let widthSteps = 0
  let heightSteps = 0
  while ((widthScale > 2 && paintWidth > 1) || (heightScale > 2 && paintHeight > 1)) {
    if (widthScale > 2 && paintWidth > 1) {
      const newWidth = Math.ceil(paintWidth / 2)
      widthScale /= paintWidth / newWidth
      paintWidth = newWidth
      widthSteps += 1
    }
    if (heightScale > 2 && paintHeight > 1) {
      const newHeight = Math.ceil(paintHeight / 2)
      heightScale /= paintHeight / newHeight
      paintHeight = newHeight
      heightSteps += 1
    }
  }
  // One chain halves both axes together, so the less minified axis decides: the other comes out
  // sharper than pdf.js's, never blurrier.
  return Math.min(widthSteps, heightSteps, mipLevelCount(width, height) - 1)
}

/** floor(log2(max(w, h))) + 1, for whole sizes from 1: the bit length, exact where Math.log2 may round. */
export function mipLevelCount(width: number, height: number): number {
  return 32 - Math.clz32(Math.max(width, height))
}

/** The texture for a delivered image: unchanged when both sides fit MAX_TEXTURE_SIDE, else scaled to fit, aspect kept, sides floored, at least 1. */
export function textureSize(width: number, height: number): { width: number; height: number } {
  if (width <= MAX_TEXTURE_SIDE && height <= MAX_TEXTURE_SIDE) {
    return { width, height }
  }
  const longest = Math.max(width, height)
  // Whole numbers multiplied, then divided once, so the long side lands on MAX_TEXTURE_SIDE exactly and
  // the floor never drops a side that a scale factor would have put a hair under a whole number.
  const fit = (side: number) => Math.max(1, Math.floor((side * MAX_TEXTURE_SIDE) / longest))
  return { width: fit(width), height: fit(height) }
}
