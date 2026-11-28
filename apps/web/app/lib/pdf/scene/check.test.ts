import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { adaptPage } from './adapter.ts'
import { buildReport, diffGrids, diffText, dilate, GRID, gridFromRecorded, gridFromScene, ORIGIN_TOLERANCE, reportLines,
  type GridDiff, type TextDiff } from './check.ts'
import { RENDERER_STAGE } from './coverage.ts'
import { decodeFixture, fixtureInput } from './fixture.ts'
import type { Box4, Mat6 } from './matrix.ts'
import { OPS } from './ops.ts'
import { ClipKind, Flag, GlyphFlag, Kind, NoteReason, SceneBuilder, sceneBytes, type FontRecord, type PageScene } from './scene.ts'

/** One grid cell per point. */
const SQUARE = { width: GRID, height: GRID }
const LETTER = { width: 612, height: 792 }
/** `page.getViewport({ scale: 1 })`'s transform for an unrotated Letter page. */
const LETTER_VIEW: Mat6 = [1, 0, 0, -1, 0, 792]
const AT = { flags: 0, clip: -1, xform: 0 }
const ALL_VISIBLE = new Uint8Array(0)

/** pdf.js's boxes for a list of ops: each an op code, its arguments and the four bytes recorded for it. */
function recordedOps(...ops: [fn: number, args: unknown, box: number[]][]) {
  return { buffer: new Uint8Array(ops.flatMap(([, , box]) => box)).buffer, fnArray: ops.map(([fn]) => fn),
    argsArray: ops.map(([, args]) => args) }
}

/** pdf.js's boxes for filled paths. */
function recorded(...boxes: number[][]) {
  return recordedOps(...boxes.map((box): [number, unknown, number[]] => [OPS.constructPath, [OPS.fill, [null], null], box]))
}

/** The covered cells as "x,y", in grid order. */
function cells(grid: Uint8Array): string[] {
  const out: string[] = []
  grid.forEach((v, i) => {
    if (v) out.push(`${i % GRID},${Math.floor(i / GRID)}`)
  })
  return out
}

function grid(...covered: [number, number][]): Uint8Array {
  const g = new Uint8Array(GRID * GRID)
  for (const [x, y] of covered) g[y * GRID + x] = 1
  return g
}

function fontRecord(kind: FontRecord['kind']): FontRecord {
  return { loadedName: 'F', kind, css: null, vertical: false, remeasure: false, fontMatrix: [0.001, 0, 0, 0.001, 0, 0],
    bbox: null, procs: {} }
}

function path(b: SceneBuilder, box: Box4, flags: number = Flag.Fill): number {
  const row = b.leaf(Kind.Path, 0, { ...AT, flags, fill: b.solid(0) })
  b.setBox(row, box)
  return row
}

/** A `Text` row of one font, a glyph per entry: its Unicode (empty for none), origin and glyph flags. */
function text(b: SceneBuilder, font: number, flags: number, glyphs: [string, number, number, number?][], box?: Box4): number {
  let first = -1
  for (const [unicode, x, y, glyphFlags = 0] of glyphs) {
    const id = b.glyph({ font, code: 0, x, y, width: 500, unicode: unicode ? b.string(unicode) : -1, flags: glyphFlags })
    if (first < 0) first = id
  }
  const row = b.leaf(Kind.Text, 0, { ...AT, flags, fill: flags & Flag.Fill ? b.solid(0) : -1, d0: first, d1: glyphs.length })
  if (box) b.setBox(row, box)
  return row
}

/** One glyph per character, 6 pt apart on the baseline y = 92. */
function line(chars: string, x = 72): [string, number, number][] {
  return Array.from(chars, (ch, i) => [ch, x + 6 * i, 92])
}

function finished(b: SceneBuilder): PageScene {
  b.markTreeEnd()
  return b.finish()
}

/** A scene with no text, for boxes recorded without a showText. */
const NO_TEXT = finished(new SceneBuilder(0, 's', SQUARE))

describe('the grid of pdf.js’s recorded boxes', () => {
  it('marks min to max, both included, as pdf.js reads a max c as (c + 1) / 256', () => {
    const covered = cells(gridFromRecorded(recorded([10, 20, 12, 21]), NO_TEXT))
    expect(covered).toEqual(['10,20', '11,20', '12,20', '10,21', '11,21', '12,21'])
  })

  it('skips EMPTY_BBOX, and covers nothing for a box pdf.js left with its min above its max', () => {
    expect(cells(gridFromRecorded(recorded([255, 255, 0, 0], [30, 30, 29, 40], [30, 30, 40, 29]), NO_TEXT))).toEqual([])
  })

  it('is the union of every op, in any order, up to the grid’s last cell', () => {
    const ops = [[0, 0, 1, 1], [254, 255, 255, 255], [1, 1, 2, 1]]
    const forward = gridFromRecorded(recorded(...ops), NO_TEXT)
    expect(cells(forward)).toEqual(['0,0', '1,0', '0,1', '1,1', '2,1', '254,255', '255,255'])
    expect(gridFromRecorded(recorded(...ops.reverse()), NO_TEXT)).toEqual(forward)
  })

  it('covers nothing for a page with no ops', () => {
    expect(cells(gridFromRecorded(recorded(), NO_TEXT))).toEqual([])
  })

  it('reads the drawing ops’ own boxes, not the hulls of their dependents that state ops hold', () => {
    const hull = [0, 0, 100, 100]
    const boxes = recordedOps(
      [OPS.save, null, hull],
      [OPS.dependency, ['g_d0_f1'], hull],
      [OPS.setFillRGBColor, ['#000000'], hull],
      [OPS.beginGroup, [{}], hull],
      [OPS.constructPath, [OPS.stroke, [null], null], [10, 10, 10, 10]],
      [OPS.showText, [[]], [20, 20, 20, 20]],
      [OPS.paintImageXObject, ['img_p0_1', 4, 4], [30, 30, 30, 30]],
      [OPS.paintInlineImageXObject, [{}], [40, 40, 40, 40]],
      [OPS.paintImageMaskXObject, [{}], [50, 50, 50, 50]],
      [OPS.paintSolidColorImageMask, null, [60, 60, 60, 60]],
      [OPS.shadingFill, ['pattern_p0_1'], [70, 70, 70, 70]],
      [OPS.endGroup, [{}], hull],
      [OPS.restore, null, hull],
    )
    const b = new SceneBuilder(0, 's', SQUARE)
    text(b, b.font(fontRecord('missing')), Flag.Fill, [['A', 20, 20]])
    expect(cells(gridFromRecorded(boxes, finished(b)))).toEqual(['10,10', '20,20', '30,30', '40,40', '50,50', '60,60', '70,70'])
  })

  it('reads no box of a showText whose run pdf.js records as its whole clip box, matching each to its Text row', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    const missing = b.font(fontRecord('missing'))
    text(b, b.font(fontRecord('outline')), Flag.Fill, [['A', 10, 10]])
    text(b, missing, Flag.Fill, [['B', 20, 20]])
    text(b, missing, Flag.Fill | Flag.AddToClip, [['C', 30, 30]])
    text(b, missing, Flag.ClipOnly, [['D', 40, 40]])
    const scene = finished(b)
    const showText = (box: number[]): [number, unknown, number[]] => [OPS.showText, [[]], box]
    const boxes = recordedOps(showText([0, 0, 255, 255]), showText([20, 20, 20, 20]), showText([0, 0, 255, 255]),
      showText([0, 0, 255, 255]))
    expect(cells(gridFromRecorded(boxes, scene))).toEqual(['20,20'])
    // A list with more or fewer showText ops than the scene has Text rows was not the scene's.
    expect(() => gridFromRecorded(recordedOps(showText([20, 20, 20, 20])), scene)).toThrow()
    expect(() => gridFromRecorded(boxes, NO_TEXT)).toThrow()
  })

  it('reads no box of a path that takes a pending clip or ends in n, neither of which draws', () => {
    const boxes = recordedOps(
      [OPS.clip, null, [0, 0, 100, 100]],
      [OPS.constructPath, [OPS.fill, [null], null], [10, 10, 10, 10]],
      [OPS.constructPath, [OPS.endPath, [null], null], [20, 20, 20, 20]],
      [OPS.constructPath, [OPS.fill, [null], null], [30, 30, 30, 30]],
    )
    expect(cells(gridFromRecorded(boxes, NO_TEXT))).toEqual(['30,30'])
  })

  it('refuses boxes recorded for another list', () => {
    expect(() => gridFromRecorded({ ...recorded([0, 0, 1, 1]), fnArray: [] }, NO_TEXT)).toThrow()
  })
})

describe('the scene’s grid', () => {
  it('quantises a box with pdf.js’s floor and ceil, so it reads as pdf.js would record it', () => {
    const b = new SceneBuilder(0, 's', { width: 512, height: 256 })
    // x in 2 pt cells: floor(10.5) = 10 and ceil(12) = 12; y: floor(20.5) = 20 and ceil(21.2) = 22.
    path(b, [21, 20.5, 24, 21.2])
    expect(gridFromScene(finished(b), ALL_VISIBLE)).toEqual(gridFromRecorded(recorded([10, 20, 12, 22]), NO_TEXT))
  })

  it('clamps to the grid as pdf.js’s byte coordinates do', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    path(b, [-10, -10, 300, 0.5])
    const covered = cells(gridFromScene(finished(b), ALL_VISIBLE))
    expect(covered.length).toBe(256 * 2)
    expect(covered.at(-1)).toBe('255,1')
  })

  it('marks leaves, not containers, and nothing out of the page tree', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    const form = b.open(Kind.Form, 0, AT)
    b.setBox(form, [0, 0, 100, 100])
    path(b, [10, 10, 11, 11])
    b.close(form)
    b.markTreeEnd()
    const glyph = b.open(Kind.Type3Glyph, 0, AT)
    path(b, [50, 50, 60, 60])
    b.close(glyph)
    expect(cells(gridFromScene(b.finish(), ALL_VISIBLE))).toEqual(['10,10', '11,10', '10,11', '11,11'])
  })

  it('leaves out an inactive row and every row of an inactive container', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    const alone = path(b, [10, 10, 11, 10])
    const form = b.open(Kind.Form, 0, AT)
    const group = b.open(Kind.Group, 0, AT)
    path(b, [40, 40, 41, 40])
    b.close(group)
    b.close(form)
    const scene = finished(b)
    expect(cells(gridFromScene(scene, ALL_VISIBLE))).toEqual(['10,10', '11,10', '40,40', '41,40'])
    scene.obj.flags[alone]! &= ~Flag.Active
    scene.obj.flags[form]! &= ~Flag.Active
    expect(cells(gridFromScene(scene, ALL_VISIBLE))).toEqual([])
    scene.obj.flags[form]! |= Flag.Active
    expect(cells(gridFromScene(scene, ALL_VISIBLE))).toEqual(['40,40', '41,40'])
  })

  it('marks only paths in hidden optional content, which pdf.js records before it checks visibility', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    const missing = b.font(fontRecord('missing'))
    const oc = b.open(Kind.OptionalContent, 0, { ...AT, d0: b.ocProps({ type: 'OCG', id: '1R' }), d1: 0 })
    path(b, [10, 10, 11, 10])
    b.setBox(b.leaf(Kind.Image, 0, { ...AT, d0: 0, d1: 0 }), [30, 30, 31, 30])
    text(b, missing, Flag.Fill, [['A', 50, 50]], [50, 50, 51, 50])
    b.setBox(b.leaf(Kind.Shading, 0, { ...AT, d0: 0, d1: 0 }), [70, 70, 71, 70])
    b.close(oc)
    const scene = finished(b)
    expect(cells(gridFromScene(scene, new Uint8Array([0])))).toEqual(['10,10', '11,10'])
    const all = ['10,10', '11,10', '30,30', '31,30', '50,50', '51,50', '70,70', '71,70']
    expect(cells(gridFromScene(scene, new Uint8Array([1])))).toEqual(all)
    // Props the check was given no visibility for are drawn.
    expect(cells(gridFromScene(scene, ALL_VISIBLE))).toEqual(all)
  })

  it('marks a soft mask’s content and a transparent paint, as pdf.js records both', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    const mask = b.open(Kind.SoftMask, 0, { ...AT, d0: 1, d1: 0 })
    path(b, [10, 10, 11, 10])
    b.close(mask)
    b.setBox(b.leaf(Kind.Path, 0, { ...AT, flags: Flag.Fill, fill: b.transparent() }), [30, 30, 31, 30])
    expect(cells(gridFromScene(finished(b), ALL_VISIBLE))).toEqual(['10,10', '11,10', '30,30', '31,30'])
  })

  it('marks no run pdf.js records as its whole clip box: an outline font’s, or one that adds to the clip', () => {
    for (const [kind, flags] of [['outline', Flag.Fill], ['outline', Flag.Invisible], ['missing', Flag.Fill | Flag.AddToClip],
      ['missing', Flag.ClipOnly]] as const) {
      const b = new SceneBuilder(0, 's', SQUARE)
      text(b, b.font(fontRecord(kind)), flags, [['A', 10, 10]], [10, 10, 11, 10])
      expect(cells(gridFromScene(finished(b), ALL_VISIBLE))).toEqual([])
    }
  })

  it('marks a run of a font without outlines by its box, and nothing when invisible', () => {
    for (const kind of ['missing', 'type3', 'invalid', 'failed'] as const) {
      const b = new SceneBuilder(0, 's', SQUARE)
      const font = b.font(fontRecord(kind))
      text(b, font, Flag.Fill, [['A', 10, 10]], [10, 10, 11, 10])
      text(b, font, Flag.Invisible, [['B', 30, 30]], [30, 30, 31, 30])
      expect(cells(gridFromScene(finished(b), ALL_VISIBLE))).toEqual(['10,10', '11,10'])
    }
  })

  it('marks a row under a text clip by the clip above it, which is all pdf.js cuts its box by', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    const rect = b.clips.intern(-1, ClipKind.Path, false, 0, 0, 0, 'rect', [100, 100, 200, 200])
    const textClip = b.clips.intern(rect, ClipKind.Text, false, 0, 1, -1, 'text', [110, 110, 120, 120])
    const inner = b.clips.intern(textClip, ClipKind.Path, false, 0, 0, 0, 'inner', [0, 0, 255, 255])
    b.setBox(b.leaf(Kind.Path, 0, { flags: Flag.Fill, clip: inner, xform: 0, fill: b.solid(0) }), [110, 110, 120, 120])
    const covered = cells(gridFromScene(finished(b), ALL_VISIBLE))
    expect([covered.length, covered[0], covered.at(-1)]).toEqual([101 * 101, '100,100', '200,200'])
    const page = new SceneBuilder(0, 's', SQUARE)
    const alone = page.clips.intern(-1, ClipKind.Text, false, 0, 1, -1, 'text', [10, 10, 20, 20])
    page.setBox(page.leaf(Kind.Path, 0, { flags: Flag.Fill, clip: alone, xform: 0, fill: page.solid(0) }), [10, 10, 20, 20])
    expect(cells(gridFromScene(finished(page), ALL_VISIBLE))).toHaveLength(GRID * GRID)
  })

  it('marks nothing for a run with no glyphs', () => {
    const b = new SceneBuilder(0, 's', SQUARE)
    b.font(fontRecord('outline'))
    b.leaf(Kind.Text, 0, { ...AT, flags: Flag.Fill, d0: -1, d1: 0 })
    expect(cells(gridFromScene(finished(b), ALL_VISIBLE))).toEqual([])
  })
})

describe('dilate', () => {
  it('widens each cell to its eight neighbours, inside the grid', () => {
    expect(cells(dilate(grid([5, 5])))).toEqual(['4,4', '5,4', '6,4', '4,5', '5,5', '6,5', '4,6', '5,6', '6,6'])
    expect(cells(dilate(grid([0, 0], [255, 255])))).toEqual(['0,0', '1,0', '0,1', '1,1', '254,254', '255,254', '254,255', '255,255'])
  })
})

describe('the grid difference', () => {
  it('counts each side’s cells against the other side widened by a cell', () => {
    expect(diffGrids(grid([10, 10]), grid([11, 11]), SQUARE)).toEqual({ canvasCells: 1, sceneCells: 1, canvasOnly: 0, sceneOnly: 0,
      canvasOnlyBoxes: [] })
    const diff = diffGrids(grid([10, 10], [11, 10]), grid([12, 11]), SQUARE)
    expect([diff.canvasCells, diff.sceneCells, diff.canvasOnly, diff.sceneOnly]).toEqual([2, 1, 1, 0])
  })

  it('gives canvas-only cells as page-space boxes, a row’s runs merged down while they line up', () => {
    const canvas: [number, number][] = []
    for (let y = 20; y <= 23; y++) for (let x = 10; x <= 12; x++) canvas.push([x, y])
    canvas.push([100, 50], [101, 50], [30, 60], [31, 60], [32, 60], [30, 61])
    const diff = diffGrids(grid(...canvas), grid(), { width: 512, height: 256 })
    expect(diff.canvasOnly).toBe(12 + 2 + 4)
    expect(diff.canvasOnlyBoxes).toEqual([[20, 20, 26, 24], [200, 50, 204, 51], [60, 60, 66, 61], [60, 61, 62, 62]])
  })

  it('keeps at most 16 boxes, and counts every cell', () => {
    const canvas = Array.from({ length: 20 }, (_, k): [number, number] => [10 * k, 5])
    const diff = diffGrids(grid(...canvas), grid(), SQUARE)
    expect(diff.canvasOnly).toBe(20)
    expect(diff.canvasOnlyBoxes).toHaveLength(16)
  })
})

describe('the text difference', () => {
  it('is equal when the characters match with whitespace removed, and compares each item’s origin', () => {
    const b = new SceneBuilder(0, 's', LETTER)
    text(b, b.font(fontRecord('missing')), Flag.Fill, [['H', 72, 92], ['i', 80, 92], [' ', 85, 92], ['t', 90, 92], ['o', 96, 92]])
    const diff = diffText(finished(b), [
      { str: 'Hi ', transform: [12, 0, 0, 12, 72, 700] },
      { str: '', transform: [1, 0, 0, 1, 0, 0] },
      { str: 't\no', transform: [12, 0, 0, 12, 90, 700] },
    ], LETTER_VIEW)
    expect(diff).toEqual({ sceneChars: 4, contentChars: 4, firstMismatch: -1, context: null, originsCompared: 2,
      originsOff: 0, maxOriginError: 0 })
  })

  it('skips accents, annotations, inactive rows and rows out of the tree, and keeps a glyph’s characters together', () => {
    const b = new SceneBuilder(0, 's', LETTER)
    const font = b.font(fontRecord('outline'))
    text(b, font, Flag.Fill, [['fi', 72, 92], ['´', 72, 85, GlyphFlag.Accent], ['n', 84, 92]])
    const annotation = b.open(Kind.Annotation, 0, { ...AT, d0: b.string('7R'), d1: 0 })
    text(b, font, Flag.Fill, [['X', 300, 92]])
    b.close(annotation)
    const inactive = text(b, font, Flag.Fill, [['Y', 300, 200]])
    // Invisible text is still the page's text.
    text(b, font, Flag.Invisible, [['z', 300, 300]])
    b.markTreeEnd()
    const glyph = b.open(Kind.Type3Glyph, 0, AT)
    text(b, font, Flag.Fill, [['Z', 0, 0]])
    b.close(glyph)
    const scene = b.finish()
    scene.obj.flags[inactive]! &= ~Flag.Active
    const diff = diffText(scene, [{ str: 'fin', transform: [12, 0, 0, 12, 72, 700] }, { str: 'z', transform: [1, 0, 0, 1, 300, 492] }],
      LETTER_VIEW)
    expect(diff.firstMismatch).toBe(-1)
    expect([diff.sceneChars, diff.originsCompared, diff.originsOff]).toEqual([4, 2, 0])
  })

  it('finds the first difference, with 20 characters on each side', () => {
    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ'
    const b = new SceneBuilder(0, 's', LETTER)
    text(b, b.font(fontRecord('outline')), Flag.Fill, line(chars))
    const scene = finished(b)
    const content = `${chars.slice(0, 30)}#${chars.slice(31)}`
    const diff = diffText(scene, [{ str: content, transform: [12, 0, 0, 12, 72, 700] }], LETTER_VIEW)
    expect(diff.firstMismatch).toBe(30)
    expect(diff.context).toEqual({ scene: chars.slice(10, 50), content: content.slice(10, 50) })
  })

  it('differs when one glyph lost its Unicode, and when one side is the start of the other', () => {
    const b = new SceneBuilder(0, 's', LETTER)
    text(b, b.font(fontRecord('outline')), Flag.Fill, line('abcd'))
    const scene = finished(b)
    expect(diffText(scene, [{ str: 'abcde', transform: [1, 0, 0, 1, 72, 700] }], LETTER_VIEW).firstMismatch).toBe(4)
    scene.glyphs.unicode[1] = -1
    const diff = diffText(scene, [{ str: 'abcd', transform: [1, 0, 0, 1, 72, 700] }], LETTER_VIEW)
    expect(diff.firstMismatch).toBe(1)
    expect(diff.context).toEqual({ scene: 'acd', content: 'abcd' })
  })

  it('counts an origin farther than the tolerance and keeps the largest error', () => {
    const b = new SceneBuilder(0, 's', LETTER)
    text(b, b.font(fontRecord('outline')), Flag.Fill, line('abc'))
    const diff = diffText(finished(b), [
      { str: 'a', transform: [1, 0, 0, 1, 72 + ORIGIN_TOLERANCE / 2, 700] },
      { str: 'b', transform: [1, 0, 0, 1, 78, 702] },
      { str: 'c', transform: [1, 0, 0, 1, 84, 700] },
    ], LETTER_VIEW)
    expect([diff.originsCompared, diff.originsOff, diff.maxOriginError]).toEqual([3, 1, 2])
  })

  it('compares no origin of a vertical font, whose text content gives the pen, not the glyph’s vertical origin', () => {
    const b = new SceneBuilder(0, 's', LETTER)
    const vertical = b.font({ ...fontRecord('missing'), vertical: true })
    // pdf.js draws each vertical glyph at the pen plus (-vmetric[1], vmetric[2]) in text space.
    text(b, vertical, Flag.Fill, [['あ', 192, 34.08], ['い', 192, 50.08]])
    text(b, b.font(fontRecord('outline')), Flag.Fill, line('ab'))
    const diff = diffText(finished(b), [
      { str: 'あい', transform: [16, 0, 0, 16, 200, 772] },
      { str: 'ab', transform: [1, 0, 0, 1, 72, 700] },
    ], LETTER_VIEW)
    expect([diff.firstMismatch, diff.originsCompared, diff.originsOff]).toEqual([-1, 1, 0])
  })

  it('compares no origin past the first difference, nor one of an item that starts with whitespace', () => {
    const b = new SceneBuilder(0, 's', LETTER)
    text(b, b.font(fontRecord('outline')), Flag.Fill, line('abcd'))
    const diff = diffText(finished(b), [
      { str: ' a', transform: [1, 0, 0, 1, 500, 0] },
      { str: 'b', transform: [1, 0, 0, 1, 78, 700] },
      { str: 'X', transform: [1, 0, 0, 1, 84, 700] },
      { str: 'd', transform: [1, 0, 0, 1, 90, 700] },
    ], LETTER_VIEW)
    expect([diff.firstMismatch, diff.originsCompared, diff.originsOff]).toEqual([2, 1, 0])
  })
})

describe('the report', () => {
  const stats = { slices: 1, totalMs: 3.14, longestMs: 3.14 }
  const grid0: GridDiff = { canvasCells: 18311, sceneCells: 18904, canvasOnly: 0, sceneOnly: 593, canvasOnlyBoxes: [] }
  const text0: TextDiff = { sceneChars: 28772, contentChars: 28772, firstMismatch: -1, context: null, originsCompared: 1410,
    originsOff: 0, maxOriginError: 0.2149 }

  function page() {
    const b = new SceneBuilder(14, 'stamp', LETTER)
    b.opCounts[OPS.constructPath] = 2
    b.opCounts[OPS.showText] = 1
    b.opCounts[OPS.save] = 3
    path(b, [0, 0, 10, 10])
    path(b, [0, 0, 10, 10], Flag.Stroke)
    const group = b.open(Kind.Group, 0, AT)
    text(b, b.font(fontRecord('outline')), Flag.Fill, line('abc'))
    b.close(group)
    b.leaf(Kind.Image, 0, { ...AT, d0: 0, d1: 0 })
    return b
  }

  it('says what the page holds, what a later stage draws, and how it compares, in the sample’s lines', () => {
    const scene = finished(page())
    const report = buildReport({ scene, stats, renderMs: 328.4, grid: grid0, text: text0, rendererStage: 4 })
    expect(report).toMatchObject({ page: 14, stamp: 'stamp', ops: 6, rows: 5, glyphs: 3, verdict: 'match',
      byKind: { Path: 2, Text: 1, Image: 1, Group: 1 }, pending: { glyph: 3, group: 1 }, notDrawn: {} })
    expect(reportLines(report)).toEqual([
      `page 15 · 6 ops · 5 rows · 3 glyphs · ${(sceneBytes(scene) / 1024).toFixed(1)} KB · adapter 3.1 ms in 1 slice (longest 3.1 ms) · render 328 ms`,
      'kinds: Path 2, Text 1, Image 1, Group 1',
      'pending (drawn from a later stage): glyph 3 (5), group 1 (6)',
      'not drawn: none',
      'grid: canvas 18311 cells, scene 18904, canvas-only 0, scene-only 593',
      'text: 28772 chars, equal; origins 1410 compared, 0 off (max 0.21 pt)',
      'verdict: match',
    ])
  })

  it('counts every feature after the renderer’s stage as pending', () => {
    const report = buildReport({ scene: finished(page()), stats, renderMs: null, grid: grid0, text: text0, rendererStage: 3 })
    expect(report.pending).toEqual({ fill: 1, stroke: 1, image: 1, glyph: 3, group: 1 })
    expect(reportLines(report)[2]).toBe(
      'pending (drawn from a later stage): fill 1 (4), stroke 1 (4), image 1 (4), glyph 3 (5), group 1 (6)')
    expect(reportLines(report)[0]).not.toContain('render')
  })

  it('is a mismatch for content pdf.js drew alone, a text or origin difference, or an op never emitted', () => {
    const scene = finished(page())
    const verdict = (grid: GridDiff, text: TextDiff, s = scene) =>
      buildReport({ scene: s, stats, renderMs: null, grid, text, rendererStage: 3 }).verdict
    expect(verdict({ ...grid0, canvasOnly: 1 }, text0)).toBe('mismatch')
    expect(verdict(grid0, { ...text0, firstMismatch: 3, context: { scene: 'ab', content: 'ac' } })).toBe('mismatch')
    expect(verdict(grid0, { ...text0, originsOff: 1 })).toBe('mismatch')
    expect(verdict({ ...grid0, sceneOnly: 9000 }, text0)).toBe('match')
    const b = page()
    b.note(b.leaf(Kind.Path, 9, AT), NoteReason.UnexpectedOp)
    b.note(b.leaf(Kind.Image, 10, AT), NoteReason.ImageMissing)
    const odd = buildReport({ scene: finished(b), stats, renderMs: null, grid: grid0, text: text0, rendererStage: 3 })
    expect(odd.verdict).toBe('mismatch')
    expect(odd.notDrawn).toEqual({ ImageMissing: 1, UnexpectedOp: 1 })
    expect(reportLines(odd)[3]).toBe('not drawn: ImageMissing 1, UnexpectedOp 1')
  })

  it('prints the boxes pdf.js drew alone, and the text around the first difference', () => {
    const report = buildReport({ scene: finished(page()), stats: { slices: 3, totalMs: 20, longestMs: 8 }, renderMs: null,
      grid: { ...grid0, canvasOnly: 6, canvasOnlyBoxes: [[20, 20, 26, 24], [200, 50, 204.5, 51]] },
      text: { ...text0, contentChars: 28771, firstMismatch: 120, context: { scene: 'ab"c', content: 'abd' }, originsOff: 2,
        maxOriginError: 3 }, rendererStage: 3 })
    const lines = reportLines(report)
    expect(lines[0]).toContain('adapter 20.0 ms in 3 slices (longest 8.0 ms)')
    expect(lines.slice(4)).toEqual([
      'grid: canvas 18311 cells, scene 18904, canvas-only 6, scene-only 593',
      '  canvas-only boxes (pt): 20.0 20.0 26.0 24.0; 200.0 50.0 204.5 51.0',
      'text: scene 28772 chars, content 28771, first difference at 120; origins 1410 compared, 2 off (max 3.00 pt)',
      '  scene:   "ab\\"c"',
      '  content: "abd"',
      'verdict: mismatch',
    ])
  })
})

const fixturesDir = join(import.meta.dirname, 'fixtures')
const fixtures = new Map(readdirSync(fixturesDir).filter((name) => name.endsWith('.oplist.json')).sort()
  .map((name) => [name.slice(0, -'.oplist.json'.length), decodeFixture(readFileSync(join(fixturesDir, name), 'utf8'))]))

/** A stored page adapted, with what the check compares it against: pdf.js's own boxes and text content. */
function stored(name: string) {
  const f = fixtures.get(name)!
  const { scene } = adaptPage(fixtureInput(f)).finish()
  // Every stored page's render list is its operator list (the capture checks), so its ops index the boxes.
  const canvas = gridFromRecorded({ buffer: f.canvas.boxes, fnArray: f.fnArray, argsArray: f.argsArray }, scene)
  const ocVisible = Uint8Array.from(f.ocVisible, Number)
  return {
    f,
    scene,
    grid: () => diffGrids(canvas, gridFromScene(scene, ocVisible), scene.view),
    text: () => diffText(scene, f.text, f.view.transform),
  }
}

function largestPath(scene: PageScene): number {
  let largest = -1
  let area = -1
  for (let row = 0; row < scene.treeEnd; row++) {
    if (scene.obj.kind[row] !== Kind.Path) continue
    const [x0, y0, x1, y1] = scene.obj.bbox.subarray(row * 4, row * 4 + 4)
    if ((x1! - x0!) * (y1! - y0!) > area) [largest, area] = [row, (x1! - x0!) * (y1! - y0!)]
  }
  return largest
}

describe('every stored page against pdf.js’s own drawing of it', () => {
  for (const name of fixtures.keys()) {
    it(`${name}: pdf.js drew nowhere the scene has nothing, and the text and its origins are pdf.js’s`, () => {
      const { scene, grid, text } = stored(name)
      const report = buildReport({ scene, stats: { slices: 1, totalMs: 0, longestMs: 0 }, renderMs: null, grid: grid(),
        text: text(), rendererStage: RENDERER_STAGE })
      expect(report.grid!.canvasOnly).toBe(0)
      expect(report.text!.firstMismatch).toBe(-1)
      expect(report.text!.originsOff).toBe(0)
      expect(report.verdict).toBe('match')
    })
  }

  // Exceptions where the largest path's cells are all inside other rows' too: paths.p1's strokes are
  // grown for a miter limit of 10 and cover each other, and on text.p1 every fill is under a text clip,
  // which marks the page (`gridFromScene`).
  const overlapping = new Set(['paths.p1', 'text.p1'])
  it('fails when the largest path is lost', () => {
    let pages = 0
    for (const name of fixtures.keys()) {
      const { scene, grid } = stored(name)
      const row = largestPath(scene)
      if (row < 0 || overlapping.has(name)) continue
      scene.obj.flags[row]! &= ~Flag.Active
      expect(grid().canvasOnly, name).toBeGreaterThan(0)
      pages++
    }
    expect(pages).toBe(13)
  })

  it('fails when one glyph loses its Unicode', () => {
    for (const name of ['text.p0', 'text.p1', 'text.p2', 'structure.p1']) {
      const { scene, text } = stored(name)
      const glyph = scene.glyphs.unicode.findIndex((id) => id >= 0 && scene.strings[id]!.trim() !== '')
      scene.glyphs.unicode[glyph] = -1
      expect(text().firstMismatch, name).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('what the stored pages settle about pdf.js’s boxes and text', () => {
  it('records a soft mask’s content as it is drawn into the mask', () => {
    const { f } = stored('groups.p2')
    const boxes = new Uint8Array(f.canvas.boxes)
    let inMask = 0
    const recorded: boolean[] = []
    f.fnArray.forEach((fn, op) => {
      if (fn === OPS.beginGroup && (f.argsArray[op] as [{ smask: unknown }])[0].smask) inMask++
      else if (fn === OPS.endGroup) inMask = Math.max(0, inMask - 1)
      else if (fn === OPS.constructPath && inMask > 0) recorded.push(!(boxes[op * 4] === 255 && boxes[op * 4 + 2] === 0))
    })
    // The three masks paint two, one and two fills.
    expect(recorded).toEqual([true, true, true, true, true])
  })

  it('gives a vertical glyph’s pen as its text content origin, not the origin pdf.js draws it at', () => {
    // Of the page's four runs only the two Type 3 ones are compared; the two vertical ones would be off.
    const { scene, text } = stored('text.p2')
    expect([text().originsCompared, text().originsOff]).toEqual([2, 0])
    for (const font of scene.fonts) font.vertical = false
    expect([text().originsCompared, text().originsOff]).toEqual([4, 2])
  })

  it('gives text with rise the origin pdf.js draws it at', () => {
    const { f, text } = stored('text.p0')
    expect(f.text.some((item) => item.str === 'rise')).toBe(true)
    expect(text().originsCompared).toBe(f.text.filter((item) => /^\S/u.test(item.str)).length)
  })
})
