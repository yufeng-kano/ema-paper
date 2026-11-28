/**
 * Glyph placement for `showText` (docs/pdf-viewer.md § The WebGPU renderer, *Text*): where each glyph
 * of a run lands in page space, the run's em-to-page matrix, and how far the run moves the text
 * position.
 *
 * Ported from pdf.js 6.5.438, src/display/canvas.js (`CanvasGraphics` `showText`, `paintChar`,
 * `showType3Text`, `setFont`'s CSS string; build/pdf.mjs 11247-11478, 11148-11222, 11099-11110),
 * Apache-2.0, Copyright 2024 Mozilla Foundation (THIRD_PARTY_NOTICES § 1). Changed: glyphs are
 * recorded instead of drawn, so what needs the browser's `measureText` (the `remeasure` squeeze or
 * centring, an `isRotated` glyph's centring) is left to the stage that draws, which keeps each glyph's
 * PDF width and flags for it; the box covers the centring's move. docs/pdf-viewer.md § What is taken
 * from where.
 */

import type { FontInput } from './adapter.ts'
import { apply, boxThrough, multiply, union, type Box4, type Mat6 } from './matrix.ts'
import { GlyphFlag, type FontRecord, type SceneBuilder } from './scene.ts'
import type { GState } from './state.ts'

/**
 * A glyph's outline box in em space, or null when pdf.js has no outline for it. For a Type 3 font the
 * second argument is the glyph's `operatorListId` and the box is in glyph space.
 */
export type GlyphBounds = (loadedName: string, fontChar: string) => Box4 | null

interface PlacedRun {
  /** The first glyph's id, or -1 when the run placed none. */
  glyphStart: number
  glyphCount: number
  /** Em (Type 3: glyph space) to page, its translation 0: each glyph's origin is its own. */
  xform: Mat6
  /** The page-space hull of the glyphs that are drawn, or null when none is. */
  box: Box4 | null
  /** The distance the run moved `text.x` (or `text.y` for vertical text), as pdf.js applies it. */
  advance: number
  /** Type 3: the `operatorListId`s the run uses, each once, in order. */
  procs: string[]
}

/** A `showText` item that is not a number, after the structured clone (pdf.js `Glyph`, W 25246-25264). */
interface Glyph {
  fontChar: string
  unicode: string
  accent: { fontChar: string; offset: { x: number; y: number } } | null
  width: number
  vmetric?: readonly number[] | null
  operatorListId?: string | null
  isSpace: boolean
  isInFont: boolean
  isRotated?: boolean
}

/**
 * Places one showText's items. `xform` is the run's em-to-page linear part; each glyph's origin is in
 * page space, rise included. It updates `text.x` / `text.y` as pdf.js does (build/pdf.mjs 11412-11416).
 */
export function placeGlyphs(items: readonly unknown[], state: GState, font: FontRecord, fontInput: FontInput,
  b: SceneBuilder, bounds: GlyphBounds): PlacedRun {
  const text = state.text
  const fontIndex = text.font
  const fontSize = text.fontSize
  const fm = font.fontMatrix
  const direction = text.fontDirection
  // `textHScale` in pdf.js already carries the direction.
  const hScale = text.hScale * direction
  // Text space, for the run: CTM · text matrix, then translate(x, y + rise) per glyph below.
  const base = multiply(state.ctm, text.matrix)
  const run: PlacedRun = { glyphStart: -1, glyphCount: 0, xform: [0, 0, 0, 0, 0, 0], box: null, advance: 0, procs: [] }

  const add = (code: number, x: number, y: number, width: number, unicode: string, flags: number): void => {
    const id = b.glyph({ font: fontIndex, code, x, y, width, unicode: unicode ? b.string(unicode) : -1, flags })
    if (run.glyphStart < 0) run.glyphStart = id
    run.glyphCount++
  }
  const grow = (glyphToPage: Mat6, em: Readonly<Box4>): void => {
    // A Type 3 glyph that fills a shading with no clip covers the plane, which a matrix would turn into NaN.
    const box: Box4 = em.every(Number.isFinite) ? boxThrough(glyphToPage, em) : [-Infinity, -Infinity, Infinity, Infinity]
    run.box = run.box === null ? box : union(run.box, box)
  }

  if (font.kind === 'type3') {
    // showType3Text: translate(x, y + rise) · scale(hScale, direction), then per glyph scale(size) · fontMatrix.
    // No y flip here: glyph space is y up like text space.
    run.xform = multiply([base[0], base[1], base[2], base[3], 0, 0],
      [hScale * fontSize * fm[0], direction * fontSize * fm[1], hScale * fontSize * fm[2], direction * fontSize * fm[3], 0, 0])
    const spacingDir = font.vertical ? 1 : -1
    let x = 0
    for (const item of items) {
      if (typeof item === 'number') {
        x += spacingDir * item * fontSize / 1000
        continue
      }
      const glyph = item as Glyph
      const id = glyph.operatorListId ?? ''
      const hasProc = fontInput.type3Procs?.has(id) === true
      const [ox, oy] = apply(base, text.x + hScale * (x + fontSize * fm[4]), text.y + text.rise + direction * fontSize * fm[5])
      add(b.string(id), ox, oy, glyph.width,
        glyph.unicode, (glyph.isSpace ? GlyphFlag.Space : 0) | (hasProc ? 0 : GlyphFlag.NotInFont))
      if (!run.procs.includes(id)) run.procs.push(id)
      const em = hasProc && fontSize !== 0 ? bounds(font.loadedName, id) : null
      if (em !== null) grow([...run.xform.slice(0, 4), ox, oy] as Mat6, em)
      if (fontSize !== 0) x += glyph.width * fm[0] * fontSize + ((glyph.isSpace ? text.wordSpacing : 0) + text.charSpacing) * direction
    }
    run.advance = x * hScale
    text.x += run.advance
    return run
  }

  // paintChar draws the em-unit path at translate(x, y) · scale(size, -size), under scale(hScale, ∓1).
  const yFlip = direction > 0 ? -1 : 1
  run.xform = multiply([base[0], base[1], base[2], base[3], 0, 0], [hScale * fontSize, 0, 0, -yFlip * fontSize, 0, 0])
  const origin = (x: number, y: number): [number, number] => apply(base, text.x + hScale * x, text.y + text.rise + yFlip * y)
  const k = fontSize * fm[0]

  if (fontSize === 0) {
    // pdf.js draws nothing and moves nothing (build/pdf.mjs 11264-11267); the glyphs stay for the text check.
    const [ox, oy] = origin(0, 0)
    for (const item of items) {
      if (typeof item === 'number') continue
      const glyph = item as Glyph
      add(glyph.fontChar.codePointAt(0) ?? 0, ox, oy, glyph.width, glyph.unicode, glyph.isSpace ? GlyphFlag.Space : 0)
    }
    return run
  }

  if (font.kind === 'invalid') {
    // One fillText of the run's Unicode at the run's origin, advanced by the widths alone (build/pdf.mjs
    // 11314-11332). pdf.js adds a TJ number's undefined width too, which makes the position NaN; numbers
    // are skipped here instead.
    let x = 0
    for (const item of items) {
      if (typeof item === 'number') continue
      const glyph = item as Glyph
      const [ox, oy] = origin(x, 0)
      add(glyph.fontChar.codePointAt(0) ?? 0, ox, oy, glyph.width, glyph.unicode, glyph.isSpace ? GlyphFlag.Space : 0)
      grow([...run.xform.slice(0, 4), ox, oy] as Mat6, missingEmBox(font, glyph.width * fm[0], false))
      x += glyph.width * k
    }
    run.advance = x * hScale
    text.x += run.advance
    return run
  }

  const vertical = font.vertical
  const spacingDir = vertical ? 1 : -1
  const hasOutlines = font.kind === 'outline'
  let x = 0
  for (const item of items) {
    if (typeof item === 'number') {
      x += spacingDir * item * fontSize / 1000
      continue
    }
    const glyph = item as Glyph
    const spacing = (glyph.isSpace ? text.wordSpacing : 0) + text.charSpacing
    let width = glyph.width
    let gx: number
    let gy: number
    if (vertical) {
      // A vertical glyph always has its metric in 6.5; PDF's default (DW2 [880 -1000]) covers a broken one.
      const vmetric = glyph.vmetric ?? [-1000, glyph.width / 2, 880]
      gx = -vmetric[1]! * k
      gy = x + vmetric[2]! * k
      width = -vmetric[0]!
    } else {
      gx = x
      gy = 0
    }
    // An `isInFont: false` glyph of a font with outlines is not drawn but still advances (build/pdf.mjs 11391).
    const drawn = glyph.isInFont || font.kind === 'missing'
    const flags = (glyph.isSpace ? GlyphFlag.Space : 0) | (glyph.isRotated ? GlyphFlag.Rotated : 0)
      | (drawn ? 0 : GlyphFlag.NotInFont)
    const [ox, oy] = origin(gx, gy)
    // The width pdf.js remeasures the glyph against: a vertical glyph's advance, unless it is rotated
    // (build/pdf.mjs 11371).
    add(glyph.fontChar.codePointAt(0) ?? 0, ox, oy, glyph.isRotated ? glyph.width : width, glyph.unicode, flags)
    if (drawn) {
      const em = hasOutlines ? bounds(font.loadedName, glyph.fontChar) : missingEmBox(font, glyph.width * fm[0], glyph.isRotated === true)
      // pdf.js centres a remeasured glyph the system font draws narrower in its width, moving it along the
      // line by (its width - the measured width) / 2000 em, at most half its width (build/pdf.mjs
      // 11370-11385). The measured width is known only where the glyph is drawn, so the box covers the whole
      // move; a wider glyph pdf.js squeezes into its width where text is antialiased, as in a browser.
      if (em !== null && font.kind === 'missing' && font.remeasure && !glyph.isRotated && width > 0) em[2] += width / 2000
      if (em !== null) grow([...run.xform.slice(0, 4), ox, oy] as Mat6, em)
      if (glyph.accent) {
        // The accent is its own glyph at `offset × size` from the base glyph (build/pdf.mjs 11399-11403).
        const ax = gx + fontSize * glyph.accent.offset.x
        const ay = gy - fontSize * glyph.accent.offset.y
        const [aox, aoy] = origin(ax, ay)
        add(glyph.accent.fontChar.codePointAt(0) ?? 0, aox, aoy, 0, '', GlyphFlag.Accent)
        const accentEm = hasOutlines ? bounds(font.loadedName, glyph.accent.fontChar) : missingEmBox(font, 0, false)
        if (accentEm !== null) grow([...run.xform.slice(0, 4), aox, aoy] as Mat6, accentEm)
      }
    }
    x += vertical ? width * k - spacing * direction : width * k + spacing * direction
  }
  if (vertical) {
    run.advance = -x
    text.y -= x
  } else {
    run.advance = x * hScale
    text.x += run.advance
  }
  return run
}

/**
 * The em box a glyph without an outline may cover: the font's bbox when it has a real one, as pdf.js's
 * recorded boxes use it, else a box generous enough for a system font's ascent, descent and overhang.
 * A rotated glyph turns about its origin, so its box is square.
 */
function missingEmBox(font: FontRecord, advance: number, rotated: boolean): Box4 {
  const bbox = font.bbox
  if (!rotated && bbox !== null && bbox[0] !== bbox[2] && bbox[1] !== bbox[3]) {
    return boxThrough(font.fontMatrix, [Math.min(bbox[0], bbox[2]), Math.min(bbox[1], bbox[3]),
      Math.max(bbox[0], bbox[2]), Math.max(bbox[1], bbox[3])])
  }
  if (rotated) {
    const side = Math.max(1.25, Math.abs(advance) + 0.25)
    return [-side, -side, side, side]
  }
  return [Math.min(0, advance) - 0.25, -0.5, Math.max(1, advance) + 0.25, 1.25]
}

/** pdf.js's CSS font string for a font with no outline, without its size (build/pdf.mjs 11099-11110). */
export function missingFontCss(f: FontInput): string {
  const italic = f.italic ? 'italic' : 'normal'
  const bold = f.black ? '900' : f.bold ? 'bold' : 'normal'
  return `${italic} ${bold} "${f.loadedName || 'sans-serif'}", ${f.fallbackName}`
}
