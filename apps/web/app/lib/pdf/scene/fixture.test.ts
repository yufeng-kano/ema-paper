import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { decodeFixture, FIXTURE_FORMAT, fixtureInput } from './fixture.ts'

const FIXTURES = join(import.meta.dirname, 'fixtures')

// The capture's encoding of a typed array or buffer: its bytes, little-endian, in base64.
function tagged(tag: string, view: ArrayBufferView | ArrayBuffer): { $t: string; b64: string } {
  const bytes = view instanceof ArrayBuffer ? new Uint8Array(view) : new Uint8Array(view.buffer, view.byteOffset, view.byteLength)
  return { $t: tag, b64: btoa(String.fromCharCode(...bytes)) }
}

function fixtureJson(fields: Record<string, unknown>): string {
  return JSON.stringify({
    format: FIXTURE_FORMAT, pdfjs: '6.5.438', document: 'test', page: 2, ops: {},
    view: { width: 100, height: 50, transform: [1, 0, 0, -1, 0, 50] }, fnArray: [], argsArray: [], fonts: {},
    glyphBounds: {}, images: {}, shadings: {}, text: [], ocVisible: [],
    canvas: { width: 100, height: 50, boxes: tagged('ab', new ArrayBuffer(0)) }, ...fields,
  })
}

describe('decoding a stored fixture', () => {
  it('revives each typed-array tag with its type and values', () => {
    const arrays = {
      u8: Uint8Array.of(0, 7, 255),
      u8c: Uint8ClampedArray.of(1, 128, 255),
      u16: Uint16Array.of(0, 513, 65535),
      u32: Uint32Array.of(0xffffffff, 3, 0x01020304),
      i32: Int32Array.of(-1, 0, 2147483647),
      f32: Float32Array.of(0.1, -2.5, 1e-7),
      f64: Float64Array.of(0.1, -1 / 3, 1e300),
    }
    const json = fixtureJson({ argsArray: Object.entries(arrays).map(([tag, view]) => [tagged(tag, view)]) })
    const revived = decodeFixture(json).argsArray.map((args) => (args as unknown[])[0])
    Object.values(arrays).forEach((view, i) => {
      expect(revived[i]).toBeInstanceOf(view.constructor)
      expect(revived[i]).toEqual(view)
    })
  })

  it('revives an ArrayBuffer, as pdf.js’s recorded boxes are stored', () => {
    const boxes = Uint32Array.of(0x10203040, 0xffffffff).buffer
    const { canvas } = decodeFixture(fixtureJson({ canvas: { width: 100, height: 50, boxes: tagged('ab', boxes) } }))
    expect(canvas.boxes).toBeInstanceOf(ArrayBuffer)
    expect(new Uint32Array(canvas.boxes)).toEqual(new Uint32Array(boxes))
  })

  it('revives a $map as a Map, with tagged arrays inside it revived too', () => {
    const procs = { $map: [['a', { fnArray: [91], argsArray: [[22, [tagged('f32', Float32Array.of(0, 1, 2))], null]] }], ['b', { fnArray: [], argsArray: [] }]] }
    const fonts = { g_d0_f1: { loadedName: 'g_d0_f1', isType3Font: true, type3Procs: procs } }
    const font = decodeFixture(fixtureJson({ fonts })).fonts.g_d0_f1
    if (typeof font !== 'object') throw new Error('the font did not decode')
    expect(font.type3Procs).toBeInstanceOf(Map)
    expect([...font.type3Procs!.keys()]).toEqual(['a', 'b'])
    expect((font.type3Procs!.get('a')!.argsArray[0] as unknown[])[1]).toEqual([Float32Array.of(0, 1, 2)])
  })

  it('refuses an unknown tag and another format', () => {
    expect(() => decodeFixture(fixtureJson({ argsArray: [[{ $t: 'i8', b64: '' }]] }))).toThrow(/tag/)
    expect(() => decodeFixture(fixtureJson({ format: 2 }))).toThrow(/format/)
  })
})

describe('a stored fixture as the adapter’s input', () => {
  const fixture = decodeFixture(fixtureJson({
    fnArray: [37, 44],
    fonts: { g_d0_f1: { loadedName: 'Helvetica', missingFile: true }, g_d0_f2: 'Font failed to load' },
    glyphBounds: { 'g_d0_f3_path_': [0, -0.1, 0.5, 0.7] },
    images: { img_p2_1: { width: 4, height: 3, interpolate: false } },
    shadings: { pattern_p2_1: ['RadialAxial', 'axial'] },
  }))
  const input = fixtureInput(fixture)

  it('looks fonts up by the id setFont names, a failed font as its message', () => {
    expect(input.font('g_d0_f1')).toEqual({ loadedName: 'Helvetica', missingFile: true })
    expect(input.font('g_d0_f2')).toBe('Font failed to load')
    expect(input.font('g_d0_f9')).toBeNull()
  })

  it('answers glyph boxes, images and shadings by id, and null for one never read', () => {
    expect(input.glyphBounds('g_d0_f3', '')).toEqual([0, -0.1, 0.5, 0.7])
    expect(input.glyphBounds('g_d0_f3', '')).toBeNull()
    expect(input.image('img_p2_1')).toEqual({ width: 4, height: 3, interpolate: false })
    expect(input.shading('pattern_p2_1')).toEqual(['RadialAxial', 'axial'])
    expect(input.image('img_p2_2')).toBeNull()
  })

  it('finds nothing under a name every object inherits', () => {
    expect(input.font('constructor')).toBeNull()
    expect(input.image('toString')).toBeNull()
  })

  it('carries the page, its view and its list, under a stamp naming the fixture', () => {
    expect(input.page).toBe(2)
    expect(input.view.transform).toEqual([1, 0, 0, -1, 0, 50])
    expect(Array.from(input.fnArray)).toEqual([37, 44])
    expect(input.stamp).toBe('fixture:test.p2:6.5.438')
    expect(fixtureInput(fixture, 'other').stamp).toBe('other')
  })
})

describe('the committed fixtures', () => {
  const names = readdirSync(FIXTURES).filter((name) => name.endsWith('.oplist.json')).sort()

  it('exist', () => {
    expect(names.length).toBeGreaterThan(0)
  })

  it.each(names)('%s decodes, named after its document and page, with one argument list per op', (name) => {
    const fixture = decodeFixture(readFileSync(join(FIXTURES, name), 'utf8'))
    expect(name).toBe(`${fixture.document}.p${fixture.page}.oplist.json`)
    expect(fixture.argsArray).toHaveLength(fixture.fnArray.length)
    expect(fixture.ocVisible).toHaveLength(fixture.fnArray.filter((code, i) =>
      code === fixture.ops.beginMarkedContentProps && (fixture.argsArray[i] as unknown[])[0] === 'OC').length)
    expect(fixture.canvas.boxes).toBeInstanceOf(ArrayBuffer)
  })
})
