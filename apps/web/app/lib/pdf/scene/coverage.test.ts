import { describe, expect, it } from 'vitest'
import { Feature, FEATURE_STAGE, featureCounts, OP_COVERAGE, OpClass } from './coverage.ts'
import { OPS, OP_NAMES } from './ops.ts'
import { ClipKind, Flag, Kind, SceneBuilder, type FontRecord } from './scene.ts'

const AT = { flags: 0, clip: -1, xform: 0 }

describe('the op coverage table', () => {
  it('classes every code from 1 to 94 once, and nothing else', () => {
    expect(Object.keys(OP_COVERAGE).map(Number)).toEqual(Array.from({ length: 94 }, (_, i) => i + 1))
    expect(OP_NAMES.length).toBe(95)
    for (const entry of Object.values(OP_COVERAGE)) expect(Object.values(OpClass)).toContain(entry.class)
  })

  it('gives every handled op the stage that draws it, and no other op a stage', () => {
    for (const { class: opClass, stage } of Object.values(OP_COVERAGE)) {
      if (opClass === OpClass.Handled) expect([4, 5, 6]).toContain(stage)
      else expect(stage).toBeNull()
    }
  })

  it('marks as never what pdf.js 6.5’s worker folds, rewrites or drops, and the codes it leaves unassigned', () => {
    const never = [
      OPS.moveTo, OPS.rectangle, OPS.stroke, OPS.endPath, OPS.closeEOFillStroke, OPS.rawFillPath, OPS.showSpacedText,
      OPS.nextLineSetSpacingShowText, OPS.setFillColorSpace, OPS.setStrokeGray, OPS.setFillCMYKColor, OPS.beginInlineImage,
      OPS.endInlineImage, OPS.paintXObject, OPS.markPointProps, OPS.endCompat, OPS.paintImageMaskXObjectGroup,
      OPS.paintImageXObjectRepeat, 78, 79, 82,
    ]
    for (const code of never) expect(OP_COVERAGE[code]!.class, String(code)).toBe(OpClass.NeverEmitted)
    for (const code of [OPS.dependency, OPS.setRenderingIntent, OPS.setFlatness, OPS.setCharWidth, OPS.beginMarkedContent]) {
      expect(OP_COVERAGE[code]!.class, String(code)).toBe(OpClass.Ignored)
    }
    expect(OP_COVERAGE[OPS.constructPath]).toEqual({ class: OpClass.Handled, stage: 4 })
    expect(OP_COVERAGE[OPS.setFillTransparent]).toEqual({ class: OpClass.Handled, stage: 4 })
    expect(OP_COVERAGE[OPS.showText]).toEqual({ class: OpClass.Handled, stage: 5 })
    expect(OP_COVERAGE[OPS.setCharWidthAndBounds]).toEqual({ class: OpClass.Handled, stage: 5 })
    expect(OP_COVERAGE[OPS.shadingFill]).toEqual({ class: OpClass.Handled, stage: 6 })
    expect(OP_COVERAGE[OPS.beginGroup]).toEqual({ class: OpClass.Handled, stage: 6 })
  })

  it('gives every feature the stage that draws it', () => {
    expect(Object.keys(FEATURE_STAGE).sort()).toEqual(Object.values(Feature).sort())
    const byStage = (stage: number) => Object.values(Feature).filter((f) => FEATURE_STAGE[f] === stage)
    expect(byStage(4)).toEqual(['fill', 'stroke', 'clip', 'image', 'imageMask', 'alpha', 'optionalContent', 'annotation'])
    expect(byStage(5)).toEqual(['glyph', 'missingFontGlyph', 'type3Glyph', 'textClip'])
    expect(byStage(6)).toEqual(['axial', 'radial', 'mesh', 'tiling', 'group', 'softMask', 'knockout', 'blend', 'transfer'])
  })
})

describe('feature counts', () => {
  it('count what a scene uses, by row, paint, composite and clip', () => {
    const b = new SceneBuilder(0, 's', { width: 100, height: 100 })
    const font = (kind: FontRecord['kind']) => b.font({ loadedName: kind, kind, css: null, vertical: false, remeasure: false,
      fontMatrix: [0.001, 0, 0, 0.001, 0, 0], bbox: null, procs: {} })
    const run = (fontIndex: number, flags: number, glyphs: number) => {
      const first = b.glyph({ font: fontIndex, code: 65, x: 0, y: 0, width: 500, unicode: -1, flags: 0 })
      for (let i = 1; i < glyphs; i++) b.glyph({ font: fontIndex, code: 65, x: 0, y: 0, width: 500, unicode: -1, flags: 0 })
      b.leaf(Kind.Text, 0, { ...AT, flags, fill: flags & Flag.Fill ? b.solid(0) : -1, d0: first, d1: glyphs })
    }
    const axial = b.shading(['RadialAxial', 'axial', null, [], [0, 0], [1, 1], null, null])
    const radial = b.shading(['RadialAxial', 'radial', null, [], [0, 0], [1, 1], 0, 1])
    const mesh = b.shading(['Mesh', 4, new Float32Array(6), new Uint8Array(12), 3, [0, 0, 1, 1], null, null])
    const half = b.comp({ blend: 0, fillAlpha: 0.5, strokeAlpha: 1, mask: -1, transfer: -1 })
    const multiply = b.comp({ blend: 1, fillAlpha: 1, strokeAlpha: 1, mask: -1, transfer: b.transfer([new Uint8Array(256)]) })

    const oc = b.open(Kind.OptionalContent, 0, { ...AT, d0: 0, d1: 0 })
    b.leaf(Kind.Path, 0, { ...AT, flags: Flag.Fill, fill: b.solid(0xff0000), comp: half })
    b.leaf(Kind.Path, 0, { ...AT, flags: Flag.Fill | Flag.Stroke, fill: b.pattern(1, radial, 0), stroke: b.pattern(2, 0, 0) })
    b.close(oc)
    const group = b.open(Kind.Group, 0, { ...AT, flags: Flag.Knockout, comp: multiply })
    b.leaf(Kind.Shading, 0, { ...AT, d0: axial, d1: 0 })
    b.leaf(Kind.Shading, 0, { ...AT, d0: mesh, d1: 0 })
    b.close(group)
    const mask = b.open(Kind.SoftMask, 0, { ...AT, d0: 1, d1: 0 })
    b.leaf(Kind.Image, 0, { ...AT, d0: 0, d1: 0 })
    b.leaf(Kind.ImageMask, 0, { ...AT, flags: Flag.Fill, fill: b.solid(0), d0: 0, d1: 0 })
    b.close(mask)
    const annotation = b.open(Kind.Annotation, 0, { ...AT, d0: b.string('5R'), d1: 0 })
    run(font('outline'), Flag.Fill, 3)
    run(font('missing'), Flag.Stroke, 2)
    // Invisible and clip-only runs paint no glyph.
    run(font('outline'), Flag.Invisible, 4)
    run(font('outline'), Flag.ClipOnly | Flag.AddToClip, 5)
    b.close(annotation)
    b.markTreeEnd()
    const proc = b.open(Kind.Type3Glyph, -1, AT)
    b.leaf(Kind.Path, 0, { ...AT, flags: Flag.Fill, fill: b.solid(0) })
    b.close(proc)
    run(font('type3'), Flag.Fill, 1)
    b.clips.intern(-1, ClipKind.Path, false, 0, 4, 0, 'p', [0, 0, 10, 10])
    b.clips.intern(-1, ClipKind.Text, false, 0, 3, -1, 't', [0, 0, 10, 10])
    b.clips.intern(-1, ClipKind.Path, true, 0, 4, 0, 'q', [0, 0, 5, 5])

    expect(featureCounts(b.finish())).toEqual({
      fill: 3, stroke: 1, clip: 2, image: 1, imageMask: 1, glyph: 3, missingFontGlyph: 2, type3Glyph: 1, textClip: 1,
      axial: 1, radial: 1, mesh: 1, tiling: 1, group: 1, softMask: 1, knockout: 1, blend: 1, transfer: 1, alpha: 1,
      optionalContent: 1, annotation: 1,
    })
  })
})
