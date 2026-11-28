/**
 * The op coverage table (docs/pdf-viewer.md § The WebGPU renderer, *The op coverage table*): for each of
 * pdf.js's op codes, whether the adapter turns it into scene data and from which stage it is drawn,
 * whether pdf.js's Canvas 2D ignores it too, or whether `getOperatorList()` never emits it. It is the
 * one list; the report's pending features come from `FEATURE_STAGE` below.
 *
 * "Never" is read from pdf.js 6.5.438's worker: its evaluator folds or rewrites those ops before
 * `addOp` (build/pdf.worker.mjs 35381-35859), and the grouped and repeated image ops come only from
 * the render list's optimizer, not from the display-intent list the adapter reads.
 */

import { OP_NAMES, type OpName } from './ops.ts'
import { ClipKind, Flag, Kind, PaintKind, type PageScene } from './scene.ts'

export const OpClass = { Handled: 'handled', Ignored: 'ignored', NeverEmitted: 'never' } as const
export type OpClass = (typeof OpClass)[keyof typeof OpClass]

interface OpCoverage { class: OpClass; stage: 4 | 5 | 6 | null }

const IGNORED: OpCoverage = { class: OpClass.Ignored, stage: null }
const NEVER: OpCoverage = { class: OpClass.NeverEmitted, stage: null }
const STAGE_4: OpCoverage = { class: OpClass.Handled, stage: 4 }
const STAGE_5: OpCoverage = { class: OpClass.Handled, stage: 5 }
const STAGE_6: OpCoverage = { class: OpClass.Handled, stage: 6 }

/** By name, so the type checker holds every name of `OPS` to a class. */
const BY_NAME: Readonly<Record<OpName, OpCoverage>> = {
  // Resolved by scene-input.ts before the walk.
  dependency: IGNORED,
  setLineWidth: STAGE_4,
  setLineCap: STAGE_4,
  setLineJoin: STAGE_4,
  setMiterLimit: STAGE_4,
  setDash: STAGE_4,
  // No-ops in pdf.js's Canvas 2D (build/pdf.mjs 10290-10291).
  setRenderingIntent: IGNORED,
  setFlatness: IGNORED,
  // Its style and alpha keys are drawn from stage 4, `Font` from 5, `BM`, `SMask` and `TR` from 6.
  setGState: STAGE_4,
  save: STAGE_4,
  restore: STAGE_4,
  transform: STAGE_4,
  // Folded into the next constructPath's buffer (W 35766-35774).
  moveTo: NEVER,
  lineTo: NEVER,
  curveTo: NEVER,
  curveTo2: NEVER,
  curveTo3: NEVER,
  closePath: NEVER,
  rectangle: NEVER,
  // Only ever constructPath's first argument (W 35775-35801).
  stroke: NEVER,
  closeStroke: NEVER,
  fill: NEVER,
  eoFill: NEVER,
  fillStroke: NEVER,
  eoFillStroke: NEVER,
  closeFillStroke: NEVER,
  closeEOFillStroke: NEVER,
  endPath: NEVER,
  clip: STAGE_4,
  eoClip: STAGE_4,
  beginText: STAGE_5,
  endText: STAGE_5,
  setCharSpacing: STAGE_5,
  setWordSpacing: STAGE_5,
  setHScale: STAGE_5,
  setLeading: STAGE_5,
  setFont: STAGE_5,
  setTextRenderingMode: STAGE_5,
  setTextRise: STAGE_5,
  moveText: STAGE_5,
  setLeadingMoveText: STAGE_5,
  setTextMatrix: STAGE_5,
  nextLine: STAGE_5,
  showText: STAGE_5,
  // Rewritten into showText, nextLine and the spacing ops (W 35483-35519).
  showSpacedText: NEVER,
  nextLineShowText: NEVER,
  nextLineSetSpacingShowText: NEVER,
  // A no-op in pdf.js's Canvas 2D (build/pdf.mjs 11479).
  setCharWidth: IGNORED,
  setCharWidthAndBounds: STAGE_5,
  // Colour spaces are consumed, and every colour becomes an RGB, pattern or transparent op (W 35523-35658).
  setStrokeColorSpace: NEVER,
  setFillColorSpace: NEVER,
  setStrokeColor: NEVER,
  setStrokeColorN: STAGE_6,
  setFillColor: NEVER,
  setFillColorN: STAGE_6,
  setStrokeGray: NEVER,
  setFillGray: NEVER,
  setStrokeRGBColor: STAGE_4,
  setFillRGBColor: STAGE_4,
  setStrokeCMYKColor: NEVER,
  setFillCMYKColor: NEVER,
  shadingFill: STAGE_6,
  // An inline image reaches the list as one image op.
  beginInlineImage: NEVER,
  beginImageData: NEVER,
  endInlineImage: NEVER,
  // Expanded into form or image ops (W 35381-35448).
  paintXObject: NEVER,
  // Dropped (W 35806-35810).
  markPoint: NEVER,
  markPointProps: NEVER,
  // Only balances the marked-content stack.
  beginMarkedContent: IGNORED,
  // An `OptionalContent` container for "OC"; other tags only balance the stack.
  beginMarkedContentProps: STAGE_4,
  endMarkedContent: STAGE_4,
  beginCompat: NEVER,
  endCompat: NEVER,
  paintFormXObjectBegin: STAGE_4,
  paintFormXObjectEnd: STAGE_4,
  beginGroup: STAGE_6,
  endGroup: STAGE_6,
  beginAnnotation: STAGE_4,
  endAnnotation: STAGE_4,
  paintImageMaskXObject: STAGE_4,
  // The render list's optimizer makes these (W 5538-5686); the display-intent list has none.
  paintImageMaskXObjectGroup: NEVER,
  paintImageXObject: STAGE_4,
  paintInlineImageXObject: STAGE_4,
  paintInlineImageXObjectGroup: NEVER,
  paintImageXObjectRepeat: NEVER,
  paintImageMaskXObjectRepeat: NEVER,
  paintSolidColorImageMask: STAGE_4,
  constructPath: STAGE_4,
  setStrokeTransparent: STAGE_4,
  setFillTransparent: STAGE_4,
  // A traced Type 3 image mask arrives as constructPath's paint op, never on its own (W 16377, 34636-34640).
  rawFillPath: NEVER,
}

/** Every code from 1 to 94, by code. The codes pdf.js leaves unassigned (78, 79, 82) are never emitted. */
export const OP_COVERAGE: Readonly<Record<number, OpCoverage>> = Object.fromEntries(
  // Code 0 is no op.
  OP_NAMES.map((name, code) => [code, name === undefined ? NEVER : BY_NAME[name]]).slice(1),
)

export const Feature = {
  Fill: 'fill',
  Stroke: 'stroke',
  Clip: 'clip',
  Image: 'image',
  ImageMask: 'imageMask',
  Glyph: 'glyph',
  MissingFontGlyph: 'missingFontGlyph',
  Type3Glyph: 'type3Glyph',
  TextClip: 'textClip',
  Axial: 'axial',
  Radial: 'radial',
  Mesh: 'mesh',
  Tiling: 'tiling',
  Group: 'group',
  SoftMask: 'softMask',
  Knockout: 'knockout',
  Blend: 'blend',
  Transfer: 'transfer',
  Alpha: 'alpha',
  OptionalContent: 'optionalContent',
  Annotation: 'annotation',
} as const
export type Feature = (typeof Feature)[keyof typeof Feature]

/** The stage the WebGPU renderer is at: a feature drawn from a later stage is pending in the report. */
export const RENDERER_STAGE = 3

/** The stage that first draws each feature. */
export const FEATURE_STAGE: Readonly<Record<Feature, 4 | 5 | 6>> = {
  fill: 4,
  stroke: 4,
  clip: 4,
  image: 4,
  imageMask: 4,
  alpha: 4,
  optionalContent: 4,
  annotation: 4,
  glyph: 5,
  missingFontGlyph: 5,
  type3Glyph: 5,
  textClip: 5,
  axial: 6,
  radial: 6,
  mesh: 6,
  tiling: 6,
  group: 6,
  softMask: 6,
  knockout: 6,
  blend: 6,
  transfer: 6,
}

/**
 * How often a scene uses each feature, counting every row, out-of-tree ones included: paths filled and
 * stroked, clip nodes, images, glyphs of painted text by the kind of their font, shadings filled or
 * used as a paint (by their IR's type), tiling paints, containers, and rows whose composite blends,
 * maps through a transfer or is not opaque.
 */
export function featureCounts(scene: PageScene): Record<Feature, number> {
  const counts = Object.fromEntries(Object.values(Feature).map((f) => [f, 0])) as Record<Feature, number>
  const { kind, flags, fill, stroke, comp, d0, d1 } = scene.obj
  const { paints, comps } = scene
  // pdf.js's shading IR: ["RadialAxial", "axial" | "radial", …] or ["Mesh", …] (W 28007-28108).
  const countShading = (index: number): void => {
    const ir = scene.shadings[index] as readonly unknown[] | undefined
    if (ir?.[0] === 'Mesh') counts.mesh++
    else if (ir?.[0] === 'RadialAxial') counts[ir[1] === 'radial' ? 'radial' : 'axial']++
  }
  const countPaint = (paint: number): void => {
    if (paint < 0) return
    if (paints.kind[paint] === PaintKind.Shading) countShading(paints.ref[paint]!)
    else if (paints.kind[paint] === PaintKind.Tiling) counts.tiling++
  }

  for (let row = 0; row < scene.count; row++) {
    const f = flags[row]!
    switch (kind[row]) {
      case Kind.Path:
        if (f & Flag.Fill) counts.fill++
        if (f & Flag.Stroke) counts.stroke++
        break
      case Kind.Text: {
        // Invisible and clip-only runs paint no glyph.
        if (!(f & (Flag.Fill | Flag.Stroke)) || d1[row]! === 0) break
        const fontKind = scene.fonts[scene.glyphs.font[d0[row]!]!]?.kind
        if (fontKind === 'outline') counts.glyph += d1[row]!
        else if (fontKind === 'missing') counts.missingFontGlyph += d1[row]!
        else if (fontKind === 'type3') counts.type3Glyph += d1[row]!
        break
      }
      case Kind.Image:
        counts.image++
        break
      case Kind.ImageMask:
        counts.imageMask++
        break
      case Kind.Shading:
        countShading(d0[row]!)
        break
      case Kind.Group:
        counts.group++
        if (f & Flag.Knockout) counts.knockout++
        break
      case Kind.SoftMask:
        counts.softMask++
        break
      case Kind.OptionalContent:
        counts.optionalContent++
        break
      case Kind.Annotation:
        counts.annotation++
        break
    }
    if (f & Flag.Fill) countPaint(fill[row]!)
    if (f & Flag.Stroke) countPaint(stroke[row]!)
    const c = comp[row]!
    if (comps.blend[c] !== 0) counts.blend++
    if (comps.transfer[c]! >= 0) counts.transfer++
    if (comps.fillAlpha[c]! < 1 || comps.strokeAlpha[c]! < 1) counts.alpha++
  }
  for (const clipKind of scene.clips.kind) {
    if (clipKind === ClipKind.Path) counts.clip++
    else counts.textClip++
  }
  return counts
}
