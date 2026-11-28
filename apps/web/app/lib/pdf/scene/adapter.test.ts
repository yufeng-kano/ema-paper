import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { adaptPage, type FontInput, type PageInput } from './adapter.ts'
import { decodeFixture, fixtureInput } from './fixture.ts'
import { OPS } from './ops.ts'
import { BLEND_MODES, ClipKind, Flag, isContainer, Kind, kindName, NoteReason, PaintKind, StyleInherit,
  type PageScene } from './scene.ts'

/** A US Letter page at scale 1: pdf.js's viewport flips y. */
const VIEW = { width: 612, height: 792, transform: [1, 0, 0, -1, 0, 792] as [number, number, number, number, number, number] }

const OUTLINE: FontInput = { loadedName: 'F1', missingFile: false, isType3Font: false, isInvalidPDFjsFont: false,
  vertical: false, remeasure: false, bold: false, black: false, italic: false, fallbackName: 'serif',
  fontMatrix: [0.001, 0, 0, 0.001, 0, 0], bbox: [0, -200, 1000, 800] }

/** constructPath's arguments as pdf.js delivers them: the DrawOPS buffer of a rectangle and its min-max. */
function rect(paintOp: number, x0: number, y0: number, x1: number, y1: number): [number, unknown] {
  const buffer = new Float32Array([0, x0, y0, 1, x1, y0, 1, x1, y1, 1, x0, y1, 4])
  return [OPS.constructPath, [paintOp, [buffer], new Float32Array([x0, y0, x1, y1])]]
}

function glyph(fontChar: string, width: number, over: Record<string, unknown> = {}) {
  return { fontChar, unicode: fontChar, accent: null, width, isSpace: false, isInFont: true, ...over }
}

function page(ops: [number, unknown][], over: Partial<PageInput> = {}): PageInput {
  return {
    page: 0,
    stamp: 'stamp',
    view: VIEW,
    fnArray: ops.map(([fn]) => fn),
    argsArray: ops.map(([, args]) => args),
    font: () => null,
    glyphBounds: () => [0, 0, 0.5, 0.7],
    image: () => null,
    shading: () => null,
    ...over,
  }
}

function adapt(input: PageInput): PageScene {
  return adaptPage(input).finish().scene
}

function box(scene: PageScene, row: number): number[] {
  return Array.from(scene.obj.bbox.subarray(row * 4, row * 4 + 4))
}

function xform(scene: PageScene, row: number): number[] {
  const id = scene.obj.xform[row]!
  return Array.from(scene.xforms.subarray(id * 6, id * 6 + 6))
}

function notes(scene: PageScene): [number, number][] {
  return Array.from(scene.notes.row, (row, i) => [row, scene.notes.reason[i]!])
}

/** Every row's `parent` and `end` agree with a pre-order walk, in the tree and after it. */
function expectWellFormed(scene: PageScene) {
  const { kind, parent, end, flags } = scene.obj
  const stack: number[] = []
  for (let row = 0; row < scene.count; row++) {
    while (stack.length > 0 && end[stack.at(-1)!]! <= row) stack.pop()
    if (row === scene.treeEnd) expect(stack).toEqual([])
    expect(parent[row]).toBe(stack.at(-1) ?? -1)
    expect(Boolean(flags[row]! & Flag.OutOfTree)).toBe(row >= scene.treeEnd)
    if (isContainer(kind[row]!)) stack.push(row)
    else expect(end[row]).toBe(row + 1)
  }
}

/** The scene with each typed array as its bytes, so two scenes compare byte for byte. */
function bytes(scene: PageScene): string {
  return JSON.stringify(scene, (_key, value: unknown) =>
    ArrayBuffer.isView(value) ? Array.from(new Uint8Array(value.buffer, value.byteOffset, value.byteLength)) : value)
}

describe('paths', () => {
  it('make a filled and a stroked row with their page-space boxes and their own copy of the path', () => {
    const fill = rect(OPS.fill, 10, 20, 110, 70)
    const line = new Float32Array([0, 0, 0, 1, 100, 0])
    const scene = adapt(page([
      [OPS.setFillRGBColor, ['#ff0000']],
      fill,
      [OPS.setLineWidth, [4]],
      [OPS.setMiterLimit, [1]],
      [OPS.constructPath, [OPS.stroke, [line], new Float32Array([0, 0, 100, 0])]],
      [OPS.constructPath, [OPS.endPath, [null], null]],
    ]))
    expectWellFormed(scene)
    expect(scene.count).toBe(2)
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Path, Kind.Path])
    expect(scene.obj.flags[0]! & (Flag.Fill | Flag.Stroke)).toBe(Flag.Fill)
    expect(scene.obj.flags[1]! & (Flag.Fill | Flag.Stroke)).toBe(Flag.Stroke)
    expect(scene.paints.rgb[scene.obj.fill[0]!]).toBe(0xff0000)
    expect(scene.paints.rgb[scene.obj.stroke[1]!]).toBe(0)
    expect(scene.styles.width[scene.obj.style[1]!]).toBe(4)
    expect(Array.from(scene.obj.op)).toEqual([1, 4])

    // y flips under the view; the stroke grows by half its width.
    expect(box(scene, 0)).toEqual([10, 722, 110, 772])
    expect(box(scene, 1)).toEqual([-2, 790, 102, 794])
    expect(xform(scene, 0)).toEqual(VIEW.transform)

    const [d0, d1] = [scene.obj.d0[0]!, scene.obj.d1[0]!]
    const buffer = ((fill[1] as unknown[])[1] as Float32Array[])[0]!
    expect(Array.from(scene.pathData.subarray(d0, d0 + d1))).toEqual(Array.from(buffer))
    buffer[1] = 999
    expect(scene.pathData[d0 + 1]).toBe(10)
  })

  it('grow a stroke’s box by half its width times the miter limit', () => {
    const line = new Float32Array([0, 0, 0, 1, 100, 0])
    const scene = adapt(page([
      [OPS.setLineWidth, [4]],
      [OPS.constructPath, [OPS.stroke, [line], new Float32Array([0, 0, 100, 0])]],
    ]))
    // The default miter limit, 10: 2 × 10 on every side.
    expect(box(scene, 0)).toEqual([-20, 772, 120, 812])
  })

  it('paint under the old clip and clip only after, as pdf.js’s consumePath does', () => {
    const scene = adapt(page([
      [OPS.clip, null],
      rect(OPS.fill, 0, 0, 100, 100),
      rect(OPS.fill, 50, 50, 150, 150),
    ]))
    expect(scene.obj.clip[0]).toBe(-1)
    expect(box(scene, 0)).toEqual([0, 692, 100, 792])
    expect(scene.obj.clip[1]).toBe(0)
    expect(Array.from(scene.clips.box)).toEqual([0, 692, 100, 792])
    expect(box(scene, 1)).toEqual([50, 692, 100, 742])
  })

  it('follow nested saves and restores of the matrix and the clip', () => {
    const scene = adapt(page([
      [OPS.save, null],
      [OPS.transform, [2, 0, 0, 2, 0, 0]],
      [OPS.save, null],
      [OPS.clip, null],
      rect(OPS.endPath, 0, 0, 10, 10),
      rect(OPS.fill, 0, 0, 100, 100),
      [OPS.restore, null],
      rect(OPS.fill, 0, 0, 100, 100),
      [OPS.restore, null],
      rect(OPS.fill, 0, 0, 100, 100),
    ]))
    expect(scene.count).toBe(3)
    expect(Array.from(scene.obj.clip)).toEqual([0, -1, -1])
    expect(box(scene, 0)).toEqual([0, 772, 20, 792])
    expect(box(scene, 1)).toEqual([0, 592, 200, 792])
    expect(box(scene, 2)).toEqual([0, 692, 100, 792])
    expect(xform(scene, 0)).toEqual([2, 0, 0, -2, 0, 792])
    expect(xform(scene, 1)).toEqual([2, 0, 0, -2, 0, 792])
    expect(xform(scene, 2)).toEqual(VIEW.transform)
  })
})

describe('containers', () => {
  it('give a form its matrix and clip its contents to its bbox', () => {
    const scene = adapt(page([
      [OPS.paintFormXObjectBegin, [new Float32Array([1, 0, 0, 1, 100, 0]), new Float32Array([0, 0, 50, 50])]],
      rect(OPS.fill, 0, 0, 100, 100),
      [OPS.paintFormXObjectEnd, []],
      rect(OPS.fill, 0, 0, 10, 10),
    ]))
    expectWellFormed(scene)
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Form, Kind.Path, Kind.Path])
    expect(Array.from(scene.obj.end)).toEqual([2, 2, 3])
    expect(Array.from(scene.obj.parent)).toEqual([-1, 0, -1])
    expect(xform(scene, 0)).toEqual([1, 0, 0, -1, 100, 792])
    expect(Array.from(scene.obj.clip)).toEqual([-1, 0, -1])
    expect(box(scene, 1)).toEqual([100, 742, 150, 792])
    expect(box(scene, 0)).toEqual([100, 742, 150, 792])
    expect(box(scene, 2)).toEqual([0, 782, 10, 792])
  })

  it('make a group and a soft mask, and mask what follows SMask true until the restore', () => {
    const group = { matrix: null, bbox: new Float32Array([0, 0, 200, 200]), smask: null, isolated: true, knockout: false,
      needsIsolation: true, hasSoftMask: false, isGray: false }
    const mask = { matrix: null, bbox: new Float32Array([0, 0, 612, 792]), isolated: false, knockout: false,
      smask: { subtype: 'Luminosity', backdrop: '#808080' }, needsIsolation: true, hasSoftMask: true, isGray: false }
    const scene = adapt(page([
      [OPS.setGState, [[['ca', 0.5]]]],
      [OPS.beginGroup, [group]],
      [OPS.paintFormXObjectBegin, [null, null]],
      rect(OPS.fill, 0, 0, 300, 300),
      [OPS.paintFormXObjectEnd, []],
      [OPS.endGroup, [group]],
      [OPS.beginGroup, [mask]],
      [OPS.paintFormXObjectBegin, [null, null]],
      rect(OPS.fill, 0, 0, 10, 10),
      [OPS.paintFormXObjectEnd, []],
      [OPS.endGroup, [mask]],
      [OPS.save, null],
      [OPS.setGState, [[['SMask', true]]]],
      rect(OPS.fill, 0, 0, 20, 20),
      [OPS.restore, null],
      rect(OPS.fill, 0, 0, 30, 30),
    ]))
    expectWellFormed(scene)
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Group, Kind.Form, Kind.Path, Kind.SoftMask, Kind.Form, Kind.Path,
      Kind.Path, Kind.Path])
    const { comp, flags } = scene.obj
    // The group is composited at the alpha it began under; its contents start opaque in its layer.
    expect(scene.comps.fillAlpha[comp[0]!]).toBe(0.5)
    expect(comp[2]).toBe(0)
    expect(flags[0]! & (Flag.Isolated | Flag.NeedsIsolation | Flag.Knockout)).toBe(Flag.Isolated | Flag.NeedsIsolation)
    expect(box(scene, 2)).toEqual([0, 592, 200, 792])
    // Luminosity, backdrop grey.
    expect([scene.obj.d0[3], scene.obj.d1[3]]).toEqual([1, 0x808080])
    expect(scene.comps.mask[comp[6]!]).toBe(3)
    expect(scene.comps.fillAlpha[comp[6]!]).toBe(0.5)
    expect(scene.comps.mask[comp[7]!]).toBe(-1)
  })

  it('keep a pending clip for a group’s first path, which a form’s bbox drops, as pdf.js ends the path for one only', () => {
    const group = { matrix: null, bbox: new Float32Array([0, 0, 200, 200]), smask: null, isolated: false, knockout: false,
      needsIsolation: false }
    const scene = adapt(page([
      [OPS.clip, null],
      [OPS.beginGroup, [group]],
      rect(OPS.endPath, 0, 0, 10, 10),
      rect(OPS.fill, 0, 0, 100, 100),
      [OPS.endGroup, [group]],
      [OPS.clip, null],
      [OPS.paintFormXObjectBegin, [null, new Float32Array([0, 0, 200, 200])]],
      rect(OPS.fill, 0, 0, 100, 100),
      [OPS.paintFormXObjectEnd, []],
    ]))
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Group, Kind.Path, Kind.Form, Kind.Path])
    // In the group the `n` path took the clip; in the form the fill paints.
    expect(box(scene, 1)).toEqual([0, 782, 10, 792])
    expect(box(scene, 3)).toEqual([0, 692, 100, 792])
  })

  it('map a group’s bbox through the group’s matrix', () => {
    const group = { matrix: [1, 0, 0, 1, 100, 0], bbox: new Float32Array([0, 0, 50, 50]), smask: null, isolated: false,
      knockout: false, needsIsolation: false }
    const scene = adapt(page([[OPS.beginGroup, [group]], rect(OPS.fill, 0, 0, 200, 200), [OPS.endGroup, [group]]]))
    expect(box(scene, 1)).toEqual([100, 742, 150, 792])
  })

  it('start a group’s layer at stroke alpha 1, and give a group inside a knockout group a layer of its own', () => {
    const isolated = { matrix: null, bbox: null, smask: null, isolated: true, knockout: false, needsIsolation: true }
    const knockout = { ...isolated, isolated: false, knockout: true }
    // Drawn in place anywhere else: it needs no isolation.
    const plain = { ...isolated, isolated: false, needsIsolation: false }
    const scene = adapt(page([
      [OPS.setGState, [[['CA', 0.4]]]],
      [OPS.beginGroup, [isolated]],
      rect(OPS.stroke, 0, 0, 10, 10),
      [OPS.endGroup, [isolated]],
      [OPS.setGState, [[['CA', 1]]]],
      [OPS.beginGroup, [knockout]],
      [OPS.setGState, [[['TR', [new Uint8Array(256)]]]]],
      [OPS.beginGroup, [plain]],
      rect(OPS.fill, 0, 0, 10, 10),
      [OPS.endGroup, [plain]],
      [OPS.endGroup, [knockout]],
    ]))
    expectWellFormed(scene)
    const { comp } = scene.obj
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Group, Kind.Path, Kind.Group, Kind.Group, Kind.Path])
    expect([scene.comps.strokeAlpha[comp[0]!], scene.comps.strokeAlpha[comp[1]!]]).toEqual([expect.closeTo(0.4, 6), 1])
    // The inner group is composited with the transfer it began under; its layer starts with none.
    expect([scene.comps.transfer[comp[3]!], scene.comps.transfer[comp[4]!]]).toEqual([0, -1])
  })

  it('composite masked content with the blend in force at the SMask key, in either key order', () => {
    const mask = { matrix: null, bbox: null, isolated: false, knockout: false, smask: { subtype: 'Alpha', backdrop: null },
      needsIsolation: true, hasSoftMask: true, isGray: false }
    const masked = (entries: [string, unknown][]): [number, unknown][] => [
      [OPS.beginGroup, [mask]],
      rect(OPS.fill, 0, 0, 10, 10),
      [OPS.endGroup, [mask]],
      [OPS.save, null],
      [OPS.setGState, [entries]],
      rect(OPS.fill, 0, 0, 20, 20),
      [OPS.restore, null],
    ]
    const scene = adapt(page([...masked([['SMask', true], ['BM', 'multiply']]), ...masked([['BM', 'multiply'], ['SMask', true]])]))
    const { comp } = scene.obj
    expect(Array.from(scene.obj.kind)).toEqual([Kind.SoftMask, Kind.Path, Kind.Path, Kind.SoftMask, Kind.Path, Kind.Path])
    expect([scene.comps.mask[comp[2]!], scene.comps.mask[comp[5]!]]).toEqual([0, 3])
    // pdf.js: a BM after the SMask key only reaches the scratch canvas the masked content is drawn into.
    expect([BLEND_MODES[scene.comps.blend[comp[2]!]!], BLEND_MODES[scene.comps.blend[comp[5]!]!]]).toEqual(['source-over', 'multiply'])
  })

  it('reset the state for an annotation and clip its contents to its rect', () => {
    const scene = adapt(page([
      [OPS.transform, [3, 0, 0, 3, 0, 0]],
      [OPS.clip, null],
      rect(OPS.endPath, 0, 0, 1, 1),
      [OPS.beginAnnotation, ['12R', [10, 10, 50, 50], [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 5, 5], false, null]],
      rect(OPS.fill, 0, 0, 100, 100),
      [OPS.endAnnotation, []],
    ]))
    expectWellFormed(scene)
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Annotation, Kind.Path])
    expect(scene.strings[scene.obj.d0[0]!]).toBe('12R')
    // The page's matrix and clip are gone; the rect is the only clip.
    expect(xform(scene, 1)).toEqual([1, 0, 0, -1, 5, 787])
    const clip = scene.obj.clip[1]!
    expect(scene.clips.parent[clip]).toBe(-1)
    expect(box(scene, 1)).toEqual([10, 742, 50, 782])
  })
})

describe('text', () => {
  it('makes one row per showText, its glyphs placed in page space', () => {
    const scene = adapt(page([
      [OPS.dependency, ['F1']],
      [OPS.setFont, ['F1', 12]],
      [OPS.beginText, null],
      [OPS.setTextMatrix, [new Float32Array([1, 0, 0, 1, 72, 700])]],
      [OPS.showText, [[glyph('H', 600), -100, glyph('i', 300)]]],
      [OPS.endText, null],
    ], { font: (name) => (name === 'F1' ? OUTLINE : null) }))
    expect(scene.count).toBe(1)
    expect(scene.obj.kind[0]).toBe(Kind.Text)
    expect(scene.obj.flags[0]! & Flag.Fill).toBe(Flag.Fill)
    expect([scene.obj.d0[0], scene.obj.d1[0]]).toEqual([0, 2])
    // H at the line's start; i after 600 units and the TJ's 100, at 12 pt.
    expect(scene.glyphs.x[0]).toBe(72)
    expect(scene.glyphs.x[1]).toBeCloseTo(80.4, 4)
    expect(Array.from(scene.glyphs.y)).toEqual([92, 92])
    expect(Array.from(scene.glyphs.unicode, (id) => scene.strings[id])).toEqual(['H', 'i'])
    expect(xform(scene, 0).map((v) => v + 0)).toEqual([12, 0, 0, -12, 0, 0])
    const [x0, y0, x1, y1] = box(scene, 0)
    expect([x0, x1, y1]).toEqual([72, expect.closeTo(86.4, 4), 92])
    expect(y0).toBeCloseTo(83.6, 4)
    expect(scene.fonts[0]).toMatchObject({ loadedName: 'F1', kind: 'outline', css: null })
  })

  it('turns text in mode 7 into a clip at endText', () => {
    const scene = adapt(page([
      [OPS.setFont, ['F1', 10]],
      [OPS.setTextRenderingMode, [7]],
      [OPS.beginText, null],
      [OPS.showText, [[glyph('X', 500)]]],
      [OPS.endText, null],
      rect(OPS.fill, 0, 0, 100, 100),
    ], { font: () => OUTLINE }))
    expect(scene.obj.flags[0]! & (Flag.ClipOnly | Flag.Fill)).toBe(Flag.ClipOnly)
    const clip = scene.obj.clip[1]!
    expect(scene.clips.kind[clip]).toBe(ClipKind.Text)
    expect([scene.clips.d0[clip], scene.clips.d1[clip]]).toEqual([0, 1])
    expect(Array.from(scene.clipRows)).toEqual([0])
    // The glyph's outline: 5 × 7 pt on the baseline at the page's bottom left.
    expect(box(scene, 1)).toEqual([0, 785, 5, 792])
  })

  it('makes a text clip of the runs shown in a clip mode only, its box the union of theirs', () => {
    const scene = adapt(page([
      [OPS.setFont, ['F1', 10]],
      [OPS.beginText, null],
      [OPS.setTextRenderingMode, [4]],
      [OPS.showText, [[glyph('a', 500)]]],
      [OPS.setTextRenderingMode, [0]],
      [OPS.showText, [[glyph('b', 500)]]],
      [OPS.setTextRenderingMode, [7]],
      [OPS.showText, [[glyph('c', 500)]]],
      [OPS.endText, null],
      rect(OPS.fill, 0, 0, 100, 100),
    ], { font: () => OUTLINE }))
    const clip = scene.obj.clip[3]!
    expect(scene.clips.kind[clip]).toBe(ClipKind.Text)
    const [d0, d1] = [scene.clips.d0[clip]!, scene.clips.d1[clip]!]
    // pdf.js clips to the glyphs painted with its add-to-path flag (build/pdf.mjs 11211-11220): not b's.
    expect(Array.from(scene.clipRows.subarray(d0, d0 + d1))).toEqual([0, 2])
    // a at x 0 and c at x 10, each half an em wide at 10 pt.
    expect(box(scene, 3)).toEqual([0, 785, 15, 792])
  })

  it('strokes text with its width in em units, as pdf.js does, whether the CTM or the text matrix scales it', () => {
    const scene = adapt(page([
      [OPS.setLineWidth, [1]],
      [OPS.setFont, ['F1', 10]],
      [OPS.setTextRenderingMode, [1]],
      [OPS.save, null],
      [OPS.transform, [2, 0, 0, 2, 0, 0]],
      [OPS.beginText, null],
      [OPS.moveText, [10, 10]],
      [OPS.showText, [[glyph('X', 500)]]],
      [OPS.endText, null],
      [OPS.restore, null],
      [OPS.beginText, null],
      [OPS.setTextMatrix, [new Float32Array([2, 0, 0, 2, 120, 20])]],
      [OPS.showText, [[glyph('X', 500)]]],
      [OPS.endText, null],
      [OPS.setLineWidth, [0]],
      [OPS.beginText, null],
      [OPS.showText, [[glyph('X', 500)]]],
      [OPS.endText, null],
    ], { font: () => OUTLINE }))
    // One em-to-page matrix, 20 pt per em, for the first two runs; pdf.js strokes the first 2 pt wide and
    // the second 1 pt (build/pdf.mjs 11295-11301, 11194).
    expect(xform(scene, 0).map((v) => v + 0)).toEqual([20, 0, 0, -20, 0, 0])
    expect(xform(scene, 1).map((v) => v + 0)).toEqual([20, 0, 0, -20, 0, 0])
    const width = (row: number) => scene.styles.width[scene.obj.style[row]!]!
    expect(width(0)).toBeCloseTo(0.1, 6)
    expect(width(1)).toBeCloseTo(0.05, 6)
    // Width 0 is pdf.js's single device pixel.
    expect(width(2)).toBe(0)
    // Each box is the outline's grown by half the width times the miter limit 10, at 20 pt per em.
    expect(box(scene, 0)[0]).toBeCloseTo(20 - 10, 4)
    expect(box(scene, 1)[0]).toBeCloseTo(120 - 5, 4)
  })

  it('leaves to a Type 3 glyph’s text what the glyph’s procedure does not set, as pdf.js runs it in that state', () => {
    const line = [OPS.stroke, [new Float32Array([0, 100, 100, 1, 900, 900])], new Float32Array([100, 100, 900, 900])]
    const plain = { fnArray: [OPS.setCharWidth, OPS.constructPath], argsArray: [[1000, 0], line] }
    const own = { fnArray: [OPS.setCharWidth, OPS.setLineWidth, OPS.setLineCap, OPS.constructPath],
      argsArray: [[1000, 0], [5], [1], line] }
    const shaded = { fnArray: [OPS.setCharWidth, OPS.setFillColorN, OPS.constructPath],
      argsArray: [[1000, 0], ['Shading', 'pattern_p0_1', [1, 0, 0, 1, 5, 5]], rect(OPS.fill, 0, 0, 500, 500)[1]] }
    const type3: FontInput = { ...OUTLINE, loadedName: 'T3', isType3Font: true,
      type3Procs: new Map<string, { fnArray: number[]; argsArray: unknown[] }>([['plain', plain], ['own', own], ['shaded', shaded]]) }
    const axial = ['RadialAxial', 'axial', null, [[0, '#000000'], [1, '#ffffff']], [0, 0], [1, 0], null, null]
    const scene = adapt(page([
      [OPS.setStrokeRGBColor, ['#ff0000']],
      [OPS.setLineWidth, [40]],
      [OPS.setDash, [[30, 10], 0]],
      [OPS.setFont, ['T3', 20]],
      [OPS.beginText, null],
      [OPS.showText, [['plain', 'own', 'shaded'].map((id) => glyph(id, 1000, { operatorListId: id }))]],
      [OPS.endText, null],
    ], { font: () => type3, shading: (id) => (id === 'pattern_p0_1' ? axial : null) }))
    expectWellFormed(scene)
    expect(notes(scene)).toEqual([])
    const { stroke, style } = scene.obj
    const { styles } = scene
    // The text row carries what its glyphs' procedures take: the stroke paint and the style in force.
    expect(scene.paints.rgb[stroke[0]!]).toBe(0xff0000)
    expect([styles.width[style[0]!], styles.dashCount[style[0]!], styles.inherit[style[0]!]]).toEqual([40, 2, 0])

    const [plainPath, ownPath, shadedPath] = ['plain', 'own', 'shaded'].map((id) => scene.fonts[0]!.procs[id]! + 1)
    expect(stroke[plainPath!]).toBe(-1)
    expect(styles.inherit[style[plainPath!]!]).toBe(StyleInherit.Width | StyleInherit.Cap | StyleInherit.Join
      | StyleInherit.Miter | StyleInherit.Dash)
    const ownStyle = style[ownPath!]!
    expect([styles.width[ownStyle], styles.cap[ownStyle], styles.inherit[ownStyle]])
      .toEqual([5, 1, StyleInherit.Join | StyleInherit.Miter | StyleInherit.Dash])
    // A stroke of the text's width, which the procedure cannot know, may reach anywhere it is not clipped.
    expect(box(scene, plainPath!)).toEqual([-Infinity, -Infinity, Infinity, Infinity])
    // The text's own box holds it: 40 / 2 × 10 glyph units past the line on each side, at 0.02 pt each.
    expect(box(scene, 0)[0]).toBeCloseTo(-2, 4)
    // A pattern set in a procedure is against the base transform of the text that shows the glyph.
    const paint = scene.obj.fill[shadedPath!]!
    expect(scene.paints.kind[paint]).toBe(PaintKind.Shading)
    expect(Array.from(scene.xforms.subarray(scene.paints.xform[paint]! * 6, scene.paints.xform[paint]! * 6 + 6)))
      .toEqual([1, 0, 0, 1, 5, 5])
  })

  it('boxes a Type 3 glyph where pdf.js paints it: a gs line width, a form’s matrix and bbox, clipped shadings, d1', () => {
    const proc = (...ops: [number, unknown][]) => ({ fnArray: [OPS.setCharWidth, ...ops.map(([fn]) => fn)],
      argsArray: [[1000, 0], ...ops.map(([, args]) => args)] })
    const procs = new Map([
      ['width', proc([OPS.setGState, [[['LW', 100]]]],
        [OPS.constructPath, [OPS.stroke, [new Float32Array([0, 200, 200, 1, 800, 800])], new Float32Array([200, 200, 800, 800])]])],
      ['form', proc([OPS.paintFormXObjectBegin, [[1, 0, 0, 1, 300, 300], [0, 0, 400, 400]]], rect(OPS.fill, 0, 0, 500, 500),
        [OPS.paintFormXObjectEnd, null])],
      ['clipped', proc([OPS.save, null], [OPS.clip, null], rect(OPS.endPath, 300, 300, 700, 700), [OPS.shadingFill, ['pattern_p0_1']],
        [OPS.restore, null], rect(OPS.fill, 0, 0, 100, 100))],
      ['bounds', proc([OPS.setCharWidthAndBounds, [1000, 0, 100, 100, 900, 900]], rect(OPS.fill, 450, 450, 550, 2000))],
      ['shading', proc([OPS.shadingFill, ['pattern_p0_1']])],
    ])
    const type3: FontInput = { ...OUTLINE, loadedName: 'T3', isType3Font: true, type3Procs: procs }
    // At size 1000 a glyph unit is a point, and the run's origin is the page's bottom left: y is 792 - y.
    const ops: [number, unknown][] = [[OPS.setFont, ['T3', 1000]]]
    for (const id of procs.keys()) ops.push([OPS.beginText, null], [OPS.showText, [[glyph(id, 1000, { operatorListId: id })]]], [OPS.endText, null])
    const scene = adapt(page([...ops, [OPS.save, null], [OPS.clip, null], rect(OPS.endPath, 10, 10, 20, 20), ...ops.slice(-3),
      [OPS.restore, null]], { font: () => type3, shading: () => ['RadialAxial', 'axial', null, [[0, '#000000'], [1, '#ffffff']],
      [0, 0], [1, 0], null, null] }))
    expect(notes(scene)).toEqual([])
    // A stroke 100 wide, padded by half its width times the miter limit of 10.
    expect(box(scene, 0)).toEqual([-300, 792 - 1300, 1300, 792 + 300])
    expect(box(scene, 1)).toEqual([300, 792 - 700, 700, 792 - 300])
    expect(box(scene, 2)).toEqual([0, 792 - 700, 700, 792])
    // The d1 bounds, which pdf.js records whatever the glyph paints inside them.
    expect(box(scene, 3)).toEqual([100, 792 - 900, 900, 792 - 100])
    // A shading with no clip in the glyph fills the clip of the text.
    expect(box(scene, 4)).toEqual([-Infinity, -Infinity, Infinity, Infinity])
    expect(box(scene, 5)).toEqual([10, 772, 20, 782])
  })

  it('boxes a Type 3 glyph without an image pdf.js could not decode, which paints nothing', () => {
    const procs = new Map(['img_p0_1', 'img_p0_2'].map((id) => [id, { fnArray: [OPS.setCharWidth, OPS.save, OPS.transform,
      OPS.paintImageXObject, OPS.restore, OPS.constructPath], argsArray: [[1000, 0], null, [1000, 0, 0, 1000, 0, 0], [id, 1, 1], null,
      rect(OPS.fill, 0, 0, 100, 100)[1]] }]))
    const type3: FontInput = { ...OUTLINE, loadedName: 'T3', isType3Font: true, type3Procs: procs }
    const ops: [number, unknown][] = [[OPS.setFont, ['T3', 1000]]]
    for (const id of procs.keys()) ops.push([OPS.beginText, null], [OPS.showText, [[glyph(id, 1000, { operatorListId: id })]]], [OPS.endText, null])
    // img_p0_2 did not decode.
    const scene = adapt(page(ops, { font: () => type3, image: (id) => (id === 'img_p0_1' ? { width: 4, height: 4 } : null) }))
    expect(box(scene, 0)).toEqual([0, 792 - 1000, 1000, 792])
    expect(box(scene, 1)).toEqual([0, 792 - 100, 100, 792])
  })
})

describe('images', () => {
  it('record an image mask’s size as its op gives it', () => {
    const scene = adapt(page([[OPS.paintImageMaskXObject, [{ data: 'mask_p0_1', width: 8, height: 4 }]]],
      { image: (id) => (id === 'mask_p0_1' ? { width: 8, height: 4 } : null) }))
    expect(scene.images).toEqual([{ id: 'mask_p0_1', width: 8, height: 4, pdfWidth: 8, pdfHeight: 4 }])
  })
})

describe('missing inputs', () => {
  it('note a run of a font pdf.js cannot use, which it draws as one string in a CSS font', () => {
    const invalid: FontInput = { ...OUTLINE, loadedName: 'Arial', isInvalidPDFjsFont: true, fallbackName: 'sans-serif' }
    const scene = adapt(page([
      [OPS.setFont, ['g_d0_f1', 10]],
      [OPS.beginText, null],
      [OPS.showText, [[glyph('A', 500)]]],
      [OPS.endText, null],
    ], { font: () => invalid }))
    expect(scene.fonts[0]).toMatchObject({ kind: 'invalid', css: 'normal normal "Arial", sans-serif' })
    expect(notes(scene)).toEqual([[0, NoteReason.InvalidFont]])
  })

  it('make rows marked not drawn, with the reason, and go on', () => {
    const scene = adapt(page([
      [OPS.setFont, ['g_d0_f1', 10]],
      [OPS.beginText, null],
      [OPS.showText, [[glyph('A', 500)]]],
      [OPS.endText, null],
      [OPS.paintImageXObject, ['img_p0_1', 8, 8]],
      [OPS.shadingFill, ['pattern_p0_1']],
      [OPS.moveTo, null],
      rect(OPS.fill, 0, 0, 1, 1),
    ], { font: () => 'Error: the font could not be read' }))
    expect(Array.from(scene.obj.kind)).toEqual([Kind.Text, Kind.Image, Kind.Shading, Kind.Path, Kind.Path])
    expect(notes(scene)).toEqual([[0, NoteReason.FontFailed], [1, NoteReason.ImageMissing],
      [2, NoteReason.ShadingMissing], [3, NoteReason.UnexpectedOp]])
    for (const row of [0, 1, 2, 3]) expect(scene.obj.flags[row]! & Flag.NotDrawn).toBe(Flag.NotDrawn)
    expect(scene.obj.flags[4]! & Flag.NotDrawn).toBe(0)
    expect(scene.fonts[0]).toMatchObject({ kind: 'failed', fontMatrix: [0.001, 0, 0, 0.001, 0, 0] })
    expect(scene.opCounts[OPS.moveTo]).toBe(1)
  })
})

/** A page that reaches every kind of row, Type 3 glyphs and a tiling cell included. */
function richPage(): PageInput {
  const procA = {
    fnArray: [OPS.setCharWidthAndBounds, OPS.constructPath],
    argsArray: [[500, 0, 0, 0, 500, 700], rect(OPS.fill, 0, 0, 600, 600)[1]],
  }
  const type3: FontInput = { ...OUTLINE, loadedName: 'T3', isType3Font: true, type3Procs: new Map([['a', procA]]) }
  // Larger than its 10 × 10 bbox, which cuts it.
  const cell = { fnArray: [OPS.constructPath], argsArray: [rect(OPS.fill, 0, 0, 15, 15)[1]] }
  const shading = ['RadialAxial', 'axial', null, [[0, '#000000'], [1, '#ffffff']], [0, 0], [1, 0], null, null]
  return page([
    [OPS.dependency, ['F1', 'T3']],
    [OPS.setFont, ['F1', 12]],
    [OPS.beginText, null],
    [OPS.setTextMatrix, [new Float32Array([1, 0, 0, 1, 72, 700])]],
    [OPS.showText, [[glyph('H', 600), -100, glyph('i', 300)]]],
    [OPS.setFont, ['T3', 10]],
    [OPS.showText, [[glyph('a', 500, { operatorListId: 'a' }), glyph('a', 500, { operatorListId: 'a' })]]],
    [OPS.endText, null],
    [OPS.beginMarkedContentProps, ['OC', { type: 'OCG', id: '5R' }]],
    [OPS.setFillColorN, ['TilingPattern', null, cell, [1, 0, 0, 1, 0, 0], [0, 0, 10, 10], 10, 10, 1, 1, false]],
    rect(OPS.fill, 0, 0, 100, 100),
    [OPS.endMarkedContent, []],
    [OPS.setFillColorN, ['Shading', 'pattern_p0_1', null]],
    rect(OPS.fill, 200, 200, 300, 300),
    [OPS.shadingFill, ['pattern_p0_1']],
    [OPS.save, null],
    [OPS.transform, [50, 0, 0, 50, 300, 300]],
    [OPS.paintImageXObject, ['img_p0_1', 4, 4]],
    [OPS.restore, null],
    [OPS.beginAnnotation, ['12R', [10, 10, 50, 50], [1, 0, 0, 1, 0, 0], [1, 0, 0, 1, 0, 0], false, null]],
    rect(OPS.fill, 0, 0, 100, 100),
    [OPS.endAnnotation, []],
  ], {
    font: (name) => (name === 'F1' ? OUTLINE : name === 'T3' ? type3 : null),
    image: (id) => (id === 'img_p0_1' ? { width: 4, height: 4 } : null),
    shading: (id) => (id === 'pattern_p0_1' ? shading : null),
  })
}

describe('a whole page', () => {
  it('puts the Type 3 glyph and the tiling cell after the tree, each once', () => {
    const scene = adapt(richPage())
    expectWellFormed(scene)
    expect(notes(scene)).toEqual([])
    const kinds = Array.from(scene.obj.kind)
    expect(kinds.slice(0, scene.treeEnd)).toEqual([Kind.Text, Kind.Text, Kind.OptionalContent, Kind.Path, Kind.Path,
      Kind.Shading, Kind.Image, Kind.Annotation, Kind.Path])
    expect(kinds.slice(scene.treeEnd)).toEqual([Kind.Type3Glyph, Kind.Path, Kind.TilingCell, Kind.Path])

    const glyphRow = scene.treeEnd
    const t3 = scene.fonts.findIndex((f) => f.loadedName === 'T3')
    expect(scene.fonts[t3]!.procs).toEqual({ a: glyphRow })
    expect([scene.obj.d0[glyphRow], scene.strings[scene.obj.d1[glyphRow]!]]).toEqual([t3, 'a'])
    // The glyph's own path is in glyph space, cut to its d1 bounds, and paints in its text's colour.
    expect(box(scene, glyphRow + 1)).toEqual([0, 0, 500, 600])
    expect(scene.obj.fill[glyphRow + 1]).toBe(-1)
    // On the page the run starts where the F1 run ended (72 + 12) and each glyph is its d1 box, which
    // pdf.js records whatever the glyph paints in it, at 0.01 pt per unit, 5 pt apart.
    const [x0, y0, x1, y1] = box(scene, 1)
    expect([x0, x1, y1]).toEqual([84, 94, 92])
    expect(y0).toBeCloseTo(85, 4)

    const cellRow = scene.treeEnd + 2
    expect(scene.tilings[0]!.cell).toBe(cellRow)
    expect(scene.obj.d0[cellRow]).toBe(0)
    expect(scene.obj.op[cellRow + 1]).toBe(0)
    expect(box(scene, cellRow + 1)).toEqual([0, 0, 10, 10])
    expect(scene.ocProps).toEqual([{ type: 'OCG', id: '5R' }])
    expect(scene.images).toEqual([{ id: 'img_p0_1', width: 4, height: 4, pdfWidth: 4, pdfHeight: 4 }])
    expect(box(scene, 6)).toEqual([300, 442, 350, 492])
  })

  it('is the same, byte for byte, when adapted twice', () => {
    expect(bytes(adapt(richPage()))).toBe(bytes(adapt(richPage())))
  })

  it('is the same when it stops after every op as when it runs in one step', () => {
    const input = richPage()
    const stepper = adaptPage(input)
    let steps = 1
    while (!stepper.step(() => true)) steps++
    expect(steps).toBeGreaterThanOrEqual(input.fnArray.length)
    expect(bytes(stepper.finish().scene)).toBe(bytes(adapt(input)))
  })
})

const fixturesDir = join(import.meta.dirname, 'fixtures')
const fixtures = new Map(readdirSync(fixturesDir).filter((name) => name.endsWith('.oplist.json')).sort()
  .map((name) => [name.slice(0, -'.oplist.json'.length), decodeFixture(readFileSync(join(fixturesDir, name), 'utf8'))]))

function fixture(name: string) {
  const f = fixtures.get(name)
  if (f === undefined) throw new Error(`no fixture ${name}`)
  return f
}

/**
 * The rows each stored page was written to produce (scripts/pdf-fixtures/documents.ts), by kind: in the
 * page tree, then out of it.
 */
const WRITTEN: Record<string, [tree: Record<string, number>, outOfTree?: Record<string, number>]> = {
  // Two stars, `re B`, `v`/`y` with `b*`, two rectangles under `B*`, `b` and `s`.
  'paths.p0': [{ Path: 7 }],
  // Three caps, four joins and miters, a dash, width 0 and the round dot.
  'paths.p1': [{ Path: 10 }],
  // The fills under the rectangle, triangle and ring clips, and the rotated fill and stroke.
  'paths.p2': [{ Path: 5 }],
  'paths.p3': [{ Path: 2 }],
  // Helvetica, Times, Calibri twice, Tc and Tw, Tz and rise, TJ, a negative size, and Tm, TD, ' and ".
  'text.p0': [{ Text: 14 }],
  // Modes 0 to 7, and the fill clipped by each of modes 4 to 7.
  'text.p1': [{ Text: 8, Path: 4 }],
  // Two vertical runs and two Type 3 runs; glyph `a` fills a path, `b` a traced image mask.
  'text.p2': [{ Text: 4 }, { Type3Glyph: 2, Path: 2 }],
  // Two images, the one with an /SMask, and the inline image.
  'images.p0': [{ Image: 4 }],
  // The stencil, and the 1 x 1 stencil as a filled unit square.
  'images.p1': [{ ImageMask: 1, Path: 1 }],
  'shadings.p0': [{ Shading: 2 }],
  // The axial pattern as a fill and as a stroke.
  'shadings.p1': [{ Path: 2 }],
  // The function-based shading and the mesh, both `sh`.
  'shadings.p2': [{ Shading: 2 }],
  // Two tiling fills; the coloured cell holds two squares, the uncoloured one a triangle.
  'shadings.p3': [{ Path: 2 }, { TilingCell: 2, Path: 3 }],
  // One form filling with the axial and the coloured tiling pattern.
  'shadings.p4': [{ Form: 1, Path: 2 }, { TilingCell: 1, Path: 2 }],
  // Each group a form of two squares.
  'groups.p0': [{ Group: 2, Form: 2, Path: 4 }],
  'groups.p1': [{ Path: 3 }],
  // Three soft masks, each a form (two, one and two fills), and the three fills they mask.
  'groups.p2': [{ SoftMask: 3, Form: 3, Path: 8 }],
  // Three nested forms, the middle one in a group, each painting one path.
  'structure.p0': [{ Form: 3, Group: 1, Path: 3 }],
  // Three optional contents of one fill each, the marked text and the `/Span` fill.
  'structure.p1': [{ OptionalContent: 3, Path: 4, Text: 1 }],
  // The page's square and the annotation's appearance.
  'structure.p2': [{ Path: 2, Annotation: 1 }],
  'structure.p3': [{}],
  // Paths that end in `n` paint nothing.
  'structure.p4': [{}],
}

function kinds(scene: PageScene, from: number, to: number): Record<string, number> {
  const counts: Record<string, number> = {}
  for (let row = from; row < to; row++) {
    const name = kindName(scene.obj.kind[row]!)
    counts[name] = (counts[name] ?? 0) + 1
  }
  return counts
}

describe('every stored page', () => {
  it('has the rows it was written to produce listed here', () => {
    expect(Object.keys(WRITTEN).sort()).toEqual([...fixtures.keys()])
  })

  for (const [name, f] of fixtures) {
    describe(name, () => {
      it('adapts with nothing noted into a well-formed tree of the rows it was written to produce', () => {
        const scene = adapt(fixtureInput(f))
        expect(notes(scene)).toEqual([])
        expectWellFormed(scene)
        const [tree, outOfTree = {}] = WRITTEN[name]!
        expect(kinds(scene, 0, scene.treeEnd)).toEqual(tree)
        expect(kinds(scene, scene.treeEnd, scene.count)).toEqual(outOfTree)
      })

      it('is the same byte for byte when adapted twice, and when it stops after every op', () => {
        const once = bytes(adapt(fixtureInput(f)))
        expect(bytes(adapt(fixtureInput(f)))).toBe(once)
        const stepper = adaptPage(fixtureInput(f))
        while (!stepper.step(() => true));
        expect(bytes(stepper.finish().scene)).toBe(once)
      })
    })
  }
})

describe('what the stored pages carry', () => {
  const stored = (name: string) => adapt(fixtureInput(fixture(name)))
  const rowsOf = (scene: PageScene, kind: number) => Array.from(scene.obj.kind.keys())
    .filter((row) => row < scene.treeEnd && scene.obj.kind[row] === kind)
  const has = (scene: PageScene, flag: number) => Array.from(scene.obj.flags, (flags) => (flags & flag) !== 0)

  it('images.p0: the one image with /Interpolate', () => {
    expect(has(stored('images.p0'), Flag.Interpolate)).toEqual([false, true, false, false])
  })

  it('groups.p0: an isolated group and a knockout one', () => {
    const scene = stored('groups.p0')
    expect(rowsOf(scene, Kind.Group).map((row) => scene.obj.flags[row]! & (Flag.Isolated | Flag.Knockout)))
      .toEqual([Flag.Isolated, Flag.Knockout])
  })

  it('groups.p2: each soft mask’s subtype, backdrop and transfer map', () => {
    const scene = stored('groups.p2')
    const masks = rowsOf(scene, Kind.SoftMask)
    expect(masks.map((row) => [scene.obj.d0[row], scene.obj.d1[row]])).toEqual([[1, 0x333333], [0, 0], [1, 0]])
    const transfers = masks.map((row) => scene.comps.transfer[scene.obj.comp[row]!])
    expect(transfers).toEqual([-1, -1, 0])
    // The mask's one map serves R, G and B; alpha is never mapped.
    const maps = scene.transfers.subarray(0, 1024)
    const identity = Array.from({ length: 256 }, (_, i) => i)
    expect(Array.from(maps.subarray(0, 256))).not.toEqual(identity)
    expect(Array.from(maps.subarray(256, 512))).toEqual(Array.from(maps.subarray(0, 256)))
    expect(Array.from(maps.subarray(512, 768))).toEqual(Array.from(maps.subarray(0, 256)))
    expect(Array.from(maps.subarray(768))).toEqual(identity)
  })

  it('text.p1: each render mode’s flags, and a stroked run’s width in em units', () => {
    const scene = stored('text.p1')
    const rows = rowsOf(scene, Kind.Text)
    const mask = Flag.Fill | Flag.Stroke | Flag.Invisible | Flag.AddToClip | Flag.ClipOnly
    expect(rows.map((row) => scene.obj.flags[row]! & mask)).toEqual([Flag.Fill, Flag.Stroke, Flag.Fill | Flag.Stroke,
      Flag.Invisible, Flag.Fill | Flag.AddToClip, Flag.Stroke | Flag.AddToClip, Flag.Fill | Flag.Stroke | Flag.AddToClip,
      Flag.ClipOnly])
    // `0.5 w` at 18 pt.
    for (const row of [rows[1]!, rows[2]!, rows[5]!, rows[6]!]) {
      expect(scene.styles.width[scene.obj.style[row]!]).toBeCloseTo(0.5 / 18, 6)
    }
    for (const row of [rows[0]!, rows[3]!, rows[4]!, rows[7]!]) expect(scene.obj.style[row]).toBe(-1)
  })

  it('paths.p0: the rows filled even-odd, by f*, b* and B*', () => {
    expect(has(stored('paths.p0'), Flag.EvenOdd)).toEqual([false, true, false, true, true, false, false])
  })

  it('paths.p2: the W* clips, and the triangle clip nested in the rectangle’s', () => {
    const { clips } = stored('paths.p2')
    expect(Array.from(clips.evenOdd)).toEqual([0, 1, 1])
    expect(Array.from(clips.parent)).toEqual([-1, 0, -1])
  })

  it('paths.p1: each cap, join and miter limit, the dash and its phase', () => {
    const { styles, dashes } = stored('paths.p1')
    expect(Array.from(styles.width)).toEqual([10, 10, 10, 10, 10, 10, 2, 0, 12])
    expect(Array.from(styles.cap)).toEqual([0, 1, 2, 0, 0, 0, 0, 0, 1])
    expect(Array.from(styles.join)).toEqual([0, 0, 0, 1, 2, 0, 0, 0, 0])
    expect(Array.from(styles.miter)).toEqual([10, 10, 10, 10, 10, 1.5, 1.5, 1.5, 1.5])
    expect(Array.from(styles.dashCount)).toEqual([0, 0, 0, 0, 0, 0, 2, 0, 0])
    expect(Array.from(styles.dashPhase)).toEqual([0, 0, 0, 0, 0, 0, 2, 0, 0])
    expect(Array.from(dashes.subarray(styles.dashStart[6]!, styles.dashStart[6]! + 2))).toEqual([6, 3])
  })

  it('shadings.p3: a coloured and an uncoloured tiling, each with its cell', () => {
    const scene = stored('shadings.p3')
    expect(scene.tilings.map(({ paintType, rgb, bbox, xstep, ystep }) => ({ paintType, rgb, bbox, xstep, ystep }))).toEqual([
      { paintType: 1, rgb: -1, bbox: [0, 0, 20, 20], xstep: 20, ystep: 20 },
      { paintType: 2, rgb: 0x009900, bbox: [0, 0, 20, 20], xstep: 20, ystep: 20 },
    ])
    expect(scene.tilings.map((t) => scene.obj.kind[t.cell])).toEqual([Kind.TilingCell, Kind.TilingCell])
  })

  it('text.p0: each font’s kind, CSS and remeasure flag, and Tz in the run’s matrix', () => {
    const scene = stored('text.p0')
    expect(scene.fonts.map(({ loadedName, kind, css, remeasure }) => [loadedName, kind, css, remeasure])).toEqual([
      ['g_d1_f1', 'missing', 'normal normal "Helvetica", sans-serif', false],
      ['g_d1_f2', 'outline', null, false],
      ['g_d1_f3', 'missing', 'normal normal "Helvetica", sans-serif', true],
      ['g_d1_f4', 'missing', 'normal bold "Helvetica", sans-serif', true],
    ])
    // `150 Tz` at 12 pt.
    const wide = rowsOf(scene, Kind.Text).find((row) => scene.strings[scene.glyphs.unicode[scene.obj.d0[row]!]!] === 'T'
      && scene.strings[scene.glyphs.unicode[scene.obj.d0[row]! + 1]!] === 'z')!
    expect(xform(scene, wide)[0]).toBeCloseTo(1.5 * 12, 6)
  })
})

describe('a stored page whose input pdf.js did not deliver', () => {
  it('notes each run of a font that failed, and goes on', () => {
    const f = fixture('text.p0')
    const input = fixtureInput(f)
    const scene = adapt({ ...input, font: (id) => (id === 'g_d1_f2' ? 'Error: the Times font could not be read' : input.font(id)) })
    const failed = scene.fonts.findIndex((font) => font.loadedName === 'g_d1_f2')
    expect(scene.fonts[failed]!.kind).toBe('failed')
    // Times shows 11 of the page's 14 runs.
    const timesRows = Array.from(scene.obj.kind.keys()).filter((row) => scene.obj.kind[row] === Kind.Text
      && scene.glyphs.font[scene.obj.d0[row]!] === failed)
    expect(timesRows).toHaveLength(11)
    expect(notes(scene)).toEqual(timesRows.map((row) => [row, NoteReason.FontFailed]))
    expect(scene.count).toBe(14)
  })

  it('notes each image and shading that is missing, but not the inline image, which the list carries', () => {
    const images = adapt({ ...fixtureInput(fixture('images.p0')), image: () => null })
    expect(notes(images)).toEqual([[0, NoteReason.ImageMissing], [1, NoteReason.ImageMissing], [2, NoteReason.ImageMissing]])
    expect(images.obj.flags[3]! & (Flag.Inline | Flag.NotDrawn)).toBe(Flag.Inline)
    const shadings = adapt({ ...fixtureInput(fixture('shadings.p0')), shading: () => null })
    expect(notes(shadings)).toEqual([[0, NoteReason.ShadingMissing], [1, NoteReason.ShadingMissing]])
  })
})

describe('patterns inside a form', () => {
  // pdf.js composes a pattern's matrix with `baseTransform`, the CTM the form began under
  // (build/pdf.mjs 11591): a shading pattern's when it paints (9096), a tiling pattern's when it is set (11490).
  it('are set against the form’s matrix, not the page’s', () => {
    const scene = adapt(fixtureInput(fixture('shadings.p4')))
    const matrix = (id: number) => Array.from(scene.xforms.subarray(id * 6, id * 6 + 6))
    // The page's view, `1.5 0 0 1.5 0 0 cm`, then the form's `/Matrix [1 0 0 1 20 20]`.
    const form = [1.5, 0, 0, -1.5, 30, 210]
    expect(xform(scene, 0)).toEqual(form)
    const [axial, tiling] = [1, 2].map((row) => scene.obj.fill[row]!)
    expect([scene.paints.kind[axial!], scene.paints.kind[tiling!]]).toEqual([PaintKind.Shading, PaintKind.Tiling])
    // The axial pattern's own `/Matrix [1 0 0 1 0 40]` after the form's.
    expect(matrix(scene.paints.xform[axial!]!)).toEqual([1.5, 0, 0, -1.5, 30, 150])
    expect(matrix(scene.paints.xform[tiling!]!)).toEqual(form)
    expect(matrix(scene.tilings[0]!.xform)).toEqual(form)
  })
})
