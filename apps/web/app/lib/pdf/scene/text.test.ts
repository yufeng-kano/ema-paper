import { describe, expect, it } from 'vitest'
import type { FontInput } from './adapter.ts'
import { IDENTITY, type Box4, type Mat6 } from './matrix.ts'
import { GlyphFlag, SceneBuilder, type FontRecord } from './scene.ts'
import { initialState, setFont, type GState } from './state.ts'
import { missingFontCss, placeGlyphs, type GlyphBounds } from './text.ts'

/** Every glyph's outline is the same box: half an em wide, 0.7 em tall, on the baseline. */
const BOUNDS: GlyphBounds = () => [0, 0, 0.5, 0.7]

const INPUT: FontInput = { loadedName: 'F', missingFile: false, isType3Font: false, isInvalidPDFjsFont: false,
  vertical: false, remeasure: false, bold: false, black: false, italic: false, fallbackName: 'serif' }

function record(over: Partial<FontRecord> = {}): FontRecord {
  return { loadedName: 'F', kind: 'outline', css: null, vertical: false, remeasure: false,
    fontMatrix: [0.001, 0, 0, 0.001, 0, 0], bbox: null, procs: {}, ...over }
}

/** A glyph 500 units wide, so 5 pt at size 10. */
function glyph(fontChar: string, over: Record<string, unknown> = {}) {
  return { fontChar, unicode: fontChar, accent: null, width: 500, isSpace: false, isInFont: true, ...over }
}

/** Text space is page space, size 10, unless a test says otherwise. */
function state(ctm: Readonly<Mat6> = IDENTITY): GState {
  const s = initialState(ctm)
  setFont(s.text, 0, 10)
  return s
}

function place(items: unknown[], s: GState, font = record(), input = INPUT, bounds = BOUNDS) {
  const b = new SceneBuilder(0, 'stamp', { width: 612, height: 792 })
  const run = placeGlyphs(items, s, font, input, b, bounds)
  const { glyphs, strings } = b.finish()
  const origins = Array.from(glyphs.x, (x, i) => [x, glyphs.y[i]!])
  return { run, glyphs, strings, origins }
}

function closeTo(actual: number[][], expected: number[][]) {
  expect(actual.length).toBe(expected.length)
  actual.forEach((point, i) => point.forEach((v, k) => expect(v).toBeCloseTo(expected[i]![k]!, 4)))
}

describe('placing glyphs', () => {
  it('puts each glyph at the text position in page space, rise included', () => {
    // The view flips y; the text matrix moves the line to (100, 700).
    const s = state([1, 0, 0, -1, 0, 792])
    s.text.matrix = [1, 0, 0, 1, 100, 700]
    s.text.rise = 3
    const { origins, run } = place([glyph('A'), glyph('B')], s)
    expect(origins).toEqual([[100, 89], [105, 89]])
    // Em to page: size 10, the text space's y up turned into the page's y down.
    expect(run.xform).toEqual([10, 0, 0, -10, 0, 0])
    expect(s.text.x).toBe(10)
  })

  it('adds the char spacing after every glyph', () => {
    const s = state()
    s.text.charSpacing = 2
    const { origins, run } = place([glyph('A'), glyph('B'), glyph('C')], s)
    expect(origins).toEqual([[0, 0], [7, 0], [14, 0]])
    expect(run.advance).toBe(21)
    expect(s.text.x).toBe(21)
  })

  it('adds the word spacing after a space only', () => {
    const s = state()
    s.text.wordSpacing = 3
    const { origins, glyphs } = place([glyph('A'), glyph(' ', { isSpace: true }), glyph('B')], s)
    expect(origins).toEqual([[0, 0], [5, 0], [13, 0]])
    expect(glyphs.flags[1]).toBe(GlyphFlag.Space)
    expect(s.text.x).toBe(18)
  })

  it('scales the positions and the advance by Tz', () => {
    const s = state()
    s.text.hScale = 0.5
    const { origins, run } = place([glyph('A'), glyph('B')], s)
    expect(origins).toEqual([[0, 0], [2.5, 0]])
    expect(run.xform).toEqual([5, 0, 0, 10, 0, 0])
    expect(s.text.x).toBe(5)
  })

  it('moves by a TJ number in thousandths of an em, against the writing direction', () => {
    const s = state()
    const { origins } = place([glyph('A'), -250, glyph('B'), 500, glyph('C')], s)
    // A, then 5 + 2.5; B advances to 12.5, then 500 takes 5 back.
    expect(origins).toEqual([[0, 0], [7.5, 0], [7.5, 0]])
    expect(s.text.x).toBe(12.5)
  })

  it('writes right to left and flips the glyphs for a negative size', () => {
    const s = initialState(IDENTITY)
    setFont(s.text, 0, -10)
    const { origins, run } = place([glyph('A'), glyph('B')], s)
    expect(origins).toEqual([[0, 0], [-5, 0]])
    expect(run.xform).toEqual([-10, 0, 0, -10, 0, 0])
    expect(s.text.x).toBe(-10)
  })

  it('turns the char spacing with the direction for a negative size', () => {
    const s = initialState(IDENTITY)
    setFont(s.text, 0, -10)
    s.text.charSpacing = 2
    // pdf.js advances by width × size / 1000 + spacing × direction (build/pdf.mjs 11406), here 5 - 2.
    const { origins } = place([glyph('A'), glyph('B')], s)
    expect(origins).toEqual([[0, 0], [-3, 0]])
    expect(s.text.x).toBe(-6)
  })

  it('places vertical glyphs by their metrics and advances down', () => {
    const s = state()
    s.text.charSpacing = 1
    const vertical = record({ vertical: true })
    const vmetric = [-1000, 250, 880]
    const { origins, run } = place([glyph('A', { vmetric }), glyph('B', { vmetric })], s, vertical, { ...INPUT, vertical: true })
    // Origin (-vx, x + vy) at 0.01 pt per unit; each glyph advances 10 - Tc down the line.
    closeTo(origins, [[-2.5, -8.8], [-2.5, -17.8]])
    expect(run.advance).toBe(-18)
    expect(s.text.y).toBe(-18)
    expect(s.text.x).toBe(0)
  })

  it('stores the width pdf.js remeasures a glyph against: a vertical one’s advance unless it is rotated', () => {
    const vertical = record({ vertical: true })
    const input = { ...INPUT, vertical: true }
    const vmetric = [-900, 250, 880]
    const upright = place([glyph('A', { vmetric })], state(), vertical, input)
    expect(upright.glyphs.width[0]).toBe(900)
    const rotated = place([glyph('A', { vmetric, isRotated: true })], state(), vertical, input)
    expect(rotated.glyphs.width[0]).toBe(500)
    expect(place([glyph('A')], state()).glyphs.width[0]).toBe(500)
  })

  it('places an accent as its own glyph at its offset, without advancing', () => {
    const s = state()
    const accented = glyph('a', { accent: { fontChar: '´', offset: { x: 0.1, y: 0.2 } } })
    const { origins, glyphs } = place([accented], s)
    expect(origins).toEqual([[0, 0], [1, 2]])
    expect(Array.from(glyphs.flags)).toEqual([0, GlyphFlag.Accent])
    expect([glyphs.unicode[1], glyphs.width[1], glyphs.code[1]]).toEqual([-1, 0, 0xb4])
    expect(s.text.x).toBe(5)
  })

  it('advances past a glyph that is not in the font, and draws it only for a font with no file', () => {
    const s = state()
    const { glyphs, run } = place([glyph('A', { isInFont: false }), glyph('B')], s)
    expect(Array.from(glyphs.flags)).toEqual([GlyphFlag.NotInFont, 0])
    // Only B is drawn: its outline at x 5.
    expect(run.box).toEqual([5, 0, 10, 7])
    expect(s.text.x).toBe(10)

    const missing = place([glyph('A', { isInFont: false })], state(), record({ kind: 'missing' }), { ...INPUT, missingFile: true })
    expect(missing.glyphs.flags[0]).toBe(0)
  })

  it('draws nothing and moves nothing at size 0, but keeps the glyphs for the text', () => {
    const s = initialState(IDENTITY)
    setFont(s.text, 0, 0)
    s.text.x = 4
    const { origins, run, strings, glyphs } = place([glyph('A'), 300, glyph('B')], s)
    expect(origins).toEqual([[4, 0], [4, 0]])
    expect(run.box).toBeNull()
    expect(s.text.x).toBe(4)
    expect(Array.from(glyphs.unicode, (id) => strings[id])).toEqual(['A', 'B'])
  })

  it('flags a rotated glyph and leaves its turn to the stage that measures it', () => {
    const s = state()
    const vmetric = [-1000, 250, 880]
    const { origins, glyphs, run } = place([glyph('あ', { vmetric, isRotated: true })], s,
      record({ kind: 'missing', vertical: true }), { ...INPUT, missingFile: true, vertical: true })
    expect(glyphs.flags[0]).toBe(GlyphFlag.Rotated)
    closeTo(origins, [[-2.5, -8.8]])
    // A square about the origin, since the glyph turns about it.
    const box = run.box!
    expect(box[2] - box[0]).toBeCloseTo(box[3] - box[1], 6)
  })

  it('gives a glyph with no outline the font’s bbox as its box', () => {
    const s = state()
    const font = record({ kind: 'missing', bbox: [-100, -200, 900, 800] })
    const { run } = place([glyph('A')], s, font, { ...INPUT, missingFile: true }, () => null)
    expect(run.box).toEqual([-1, -2, 9, 8])
  })

  it('widens a remeasured glyph’s box along the line by half its width, as far as pdf.js may move it to centre it', () => {
    const font = record({ kind: 'missing', remeasure: true, bbox: [-100, -200, 900, 800] })
    // 500 units at size 10: pdf.js moves the glyph by up to 500 / 2000 em, 2.5 pt.
    const { run } = place([glyph('A')], state(), font, { ...INPUT, missingFile: true, remeasure: true }, () => null)
    expect(run.box).toEqual([-1, -2, 11.5, 8])
  })

  it('places Type 3 glyphs through the font matrix, its translation included', () => {
    const s = state()
    const font = record({ kind: 'type3', fontMatrix: [0.001, 0, 0, 0.001, 0.1, 0.2] })
    const procs = new Map([['a', { fnArray: [], argsArray: [] }]])
    const glyphBox: Box4 = [0, 0, 500, 700]
    const { origins, glyphs, strings, run } = place(
      [glyph('a', { operatorListId: 'a' }), glyph('b', { operatorListId: 'b' })], s, font,
      { ...INPUT, isType3Font: true, type3Procs: procs }, (_name, id) => (id === 'a' ? glyphBox : null))
    expect(origins).toEqual([[1, 2], [6, 2]])
    expect(Array.from(glyphs.code, (id) => strings[id])).toEqual(['a', 'b'])
    // `b` has no procedure: not drawn, still advanced.
    expect(Array.from(glyphs.flags)).toEqual([0, GlyphFlag.NotInFont])
    expect(run.procs).toEqual(['a', 'b'])
    expect(run.xform).toEqual([0.01, 0, 0, 0.01, 0, 0])
    expect(run.box).toEqual([1, 2, 6, 9])
    expect(s.text.x).toBe(10)
  })

  it('moves a Type 3 run by a TJ number in thousandths of the size, against the writing direction', () => {
    const font = record({ kind: 'type3' })
    const input = { ...INPUT, isType3Font: true, type3Procs: new Map([['a', { fnArray: [], argsArray: [] }]]) }
    const s = state()
    const { origins } = place([glyph('a', { operatorListId: 'a' }), -500, glyph('a', { operatorListId: 'a' })], s, font, input)
    // 5 for the glyph, then 5 more for -500 (build/pdf.mjs 11452-11454).
    expect(origins).toEqual([[0, 0], [10, 0]])
    expect(s.text.x).toBe(15)
  })
})

describe('placing the glyphs of a font pdf.js could not use', () => {
  it('advances by each glyph’s width alone', () => {
    const { origins, run } = place([glyph('A'), 300, glyph('B')], state(), record({ kind: 'invalid' }),
      { ...INPUT, isInvalidPDFjsFont: true })
    expect(origins).toEqual([[0, 0], [5, 0]])
    expect(run.advance).toBe(10)
  })
})

describe('the CSS font of a font with no outline', () => {
  it('is pdf.js’s string without its size', () => {
    expect(missingFontCss({ ...INPUT, loadedName: 'Helvetica', fallbackName: 'sans-serif' }))
      .toBe('normal normal "Helvetica", sans-serif')
    expect(missingFontCss({ ...INPUT, loadedName: 'Helvetica', bold: true, italic: true, fallbackName: 'sans-serif' }))
      .toBe('italic bold "Helvetica", sans-serif')
    // Black outranks bold, as weight 900.
    expect(missingFontCss({ ...INPUT, loadedName: 'Arial', bold: true, black: true, fallbackName: 'sans-serif' }))
      .toBe('normal 900 "Arial", sans-serif')
    expect(missingFontCss({ ...INPUT, loadedName: '', fallbackName: 'serif' })).toBe('normal normal "sans-serif", serif')
  })
})
