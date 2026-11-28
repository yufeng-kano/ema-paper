/**
 * The hand-written PDFs the operator-list fixtures are captured from (docs/pdf-viewer.md § Testing).
 * Each document targets one class of operators, one page per group of cases, so that each stored
 * page holds a small, known set of ops. Nothing here is fetched, and no PDF is committed: the
 * capture script builds these bytes each time it runs.
 */

import { contentStream, dict, latin1, pdfDocument, type PdfObj } from './writer.ts'

/** Collects objects with ids in order; ids 1 and 2 are the catalog and the page tree. */
class DocumentBuilder {
  readonly #objects: PdfObj[] = []
  readonly #pages: string[] = []

  /** Adds an object and returns its reference, `N 0 R`. A stream body is the stream's dictionary. */
  add(body: string, stream?: string | Uint8Array): string {
    const id = this.#objects.length + 3
    this.#objects.push({ id, body, stream: typeof stream === 'string' ? latin1(stream) : stream })
    return `${id} 0 R`
  }

  /** A page of the given content (none for an empty page) and resources. */
  page(content: string[] | null, resources: Record<string, string>, extra: Record<string, string> = {}): void {
    const entries: Record<string, string> = { Type: '/Page', Parent: '2 0 R', MediaBox: '[0 0 240 240]', Resources: dict(resources), ...extra }
    if (content) entries.Contents = this.add('<< >>', contentStream(content))
    this.#pages.push(this.add(dict(entries)))
  }

  finish(catalog: Record<string, string> = {}): Uint8Array {
    return pdfDocument([
      { id: 1, body: dict({ Type: '/Catalog', Pages: '2 0 R', ...catalog }) },
      { id: 2, body: dict({ Type: '/Pages', Kids: `[${this.#pages.join(' ')}]`, Count: String(this.#pages.length) }) },
      ...this.#objects,
    ], 1)
  }
}

function paths(): Uint8Array {
  const b = new DocumentBuilder()
  // Fill rules and the path-building operators: one self-crossing star under each rule, then
  // `re`, `v` and `y` under the fill-and-stroke paint ops.
  b.page([
    '1 0 0 rg',
    '20 130 m 50 230 l 80 130 l 10 195 l 90 195 l h f',
    '0 0 1 rg',
    '130 130 m 160 230 l 190 130 l 120 195 l 200 195 l h f*',
    '0 0.6 0 rg 0 0 0 RG 2 w',
    '20 70 50 40 re B',
    '90 70 m 130 120 170 70 v 170 100 l 130 130 90 100 y h b*',
    '20 20 40 30 re 30 25 20 20 re B*',
    '90 20 m 140 20 l 140 50 l b',
    '160 20 m 220 20 l 220 50 l s',
  ], {})
  // Caps, joins, the miter limit, a dash with a phase, width 0 and a zero-length round-capped dot.
  b.page([
    '0 0 0 RG 10 w',
    '0 J 20 220 m 80 220 l S',
    '1 J 20 195 m 80 195 l S',
    '2 J 20 170 m 80 170 l S',
    '0 J 0 j 10 M 110 150 m 130 220 l 150 150 l S',
    '1 j 110 90 m 130 140 l 150 90 l S',
    '2 j 160 150 m 180 220 l 200 150 l S',
    '0 j 1.5 M 160 90 m 180 140 l 200 90 l S',
    '2 w [6 3] 2 d 20 140 m 90 140 l S',
    '[] 0 d 0 w 20 120 m 90 120 l S',
    '1 J 12 w 40 70 m 40 70 l S',
  ], {})
  // Clips: a rectangle, an even-odd triangle nested in it, a ring from two rectangles under `W*`,
  // and a rotated CTM.
  b.page([
    'q 20 20 100 100 re W n',
    '1 0 0 rg 0 0 240 240 re f',
    'q 40 40 m 140 40 l 90 140 l h W* n 0 0 1 rg 0 0 240 240 re f Q',
    'Q',
    'q 140 140 80 80 re 160 160 40 40 re W* n 1 0.5 0 rg 140 140 80 80 re f Q',
    'q 0.7071 0.7071 -0.7071 0.7071 180 60 cm',
    '0 0.5 0 rg -20 -20 40 40 re f 0 0 0 RG 2 w -30 -30 60 60 re S',
    'Q',
  ], {})
  // `/Rotate 90`: page space is the viewport's, so this page is 160 wide and 240 high.
  b.page([
    '1 0 0 rg 10 10 100 40 re f',
    '0 0 0 RG 3 w 10 100 m 200 140 l S',
  ], {}, { MediaBox: '[0 0 240 160]', Rotate: '90' })
  return b.finish()
}

function text(): Uint8Array {
  const b = new DocumentBuilder()
  const helvetica = b.add(dict({ Type: '/Font', Subtype: '/Type1', BaseFont: '/Helvetica', Encoding: '/WinAnsiEncoding' }))
  const times = b.add(dict({ Type: '/Font', Subtype: '/Type1', BaseFont: '/Times-Roman', Encoding: '/WinAnsiEncoding' }))
  // Calibri is neither embedded nor one of the base 14, so pdf.js has no outline for it. Its widths
  // must differ, or pdf.js takes it for a fixed-pitch font and falls back to monospace.
  const calibri = (name: string, flags: number) => b.add(dict({
    Type: '/Font', Subtype: '/TrueType', BaseFont: `/${name}`, FirstChar: '32', LastChar: '126',
    Widths: `[${Array.from({ length: 95 }, (_, i) => 400 + ((i * 37) % 200)).join(' ')}]`, Encoding: '/WinAnsiEncoding',
    FontDescriptor: b.add(dict({
      Type: '/FontDescriptor', FontName: `/${name}`, Flags: String(flags), FontBBox: '[-503 -250 1240 750]',
      ItalicAngle: '0', Ascent: '750', Descent: '-250', CapHeight: '632', StemV: '80',
    })),
  }))
  const calibriRegular = calibri('Calibri', 32)
  const calibriBold = calibri('Calibri-Bold', 32 + 262144)
  // A CID font with a vertical CMap, not embedded; its CIDs are Adobe-Japan1's. Under /Identity-V
  // pdf.js finds no vertical variant of any code; under /UniJIS-UTF16-V it swaps a glyph for its
  // vertical presentation form, or marks it to be drawn rotated.
  const kozMin = b.add(dict({
    Type: '/Font', Subtype: '/CIDFontType0', BaseFont: '/KozMinPr6N-Regular',
    CIDSystemInfo: '<< /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >>', DW: '1000',
    FontDescriptor: b.add(dict({
      Type: '/FontDescriptor', FontName: '/KozMinPr6N-Regular', Flags: '4', FontBBox: '[-437 -340 1147 1317]',
      ItalicAngle: '0', Ascent: '880', Descent: '-120', CapHeight: '742', StemV: '80',
    })),
  }))
  const vertical = (encoding: string) => b.add(dict({
    Type: '/Font', Subtype: '/Type0', BaseFont: '/KozMinPr6N-Regular', Encoding: encoding, DescendantFonts: `[${kozMin}]`,
  }))
  // Type 3: `a` is a path glyph (`d0`, its own colour), `b` an 8 x 8 image mask (`d1`, the fill colour).
  const type3 = b.add(dict({
    Type: '/Font', Subtype: '/Type3', FontBBox: '[0 0 1000 1000]', FontMatrix: '[0.001 0 0 0.001 0 0]',
    CharProcs: dict({
      a: b.add('<< >>', '1000 0 d0\n1 0 0 rg 100 100 800 800 re f'),
      b: b.add('<< >>', '1000 0 0 0 1000 1000 d1\nq 1000 0 0 1000 0 0 cm\nBI /W 8 /H 8 /IM true /BPC 1 /F /AHx ID 00FFC3DBDBC3FF00> EI\nQ'),
    }),
    Encoding: '<< /Type /Encoding /Differences [97 /a /b] >>', FirstChar: '97', LastChar: '98', Widths: '[1000 1000]',
    Resources: '<< >>',
  }))
  const fonts = {
    Font: dict({
      F1: helvetica, F2: times, F3: calibriRegular, F4: vertical('/Identity-V'), F5: type3, F6: calibriBold,
      F7: vertical('/UniJIS-UTF16-V'),
    }),
  }

  // Base-14 fonts, the missing Calibri, every text-state operator and TJ numbers.
  b.page([
    'BT /F1 14 Tf 20 215 Td (Helvetica text) Tj ET',
    'BT /F2 14 Tf 20 195 Td (Times text) Tj ET',
    'BT /F3 12 Tf 20 175 Td (Calibri missing) Tj /F6 12 Tf ( bold) Tj ET',
    'BT /F2 12 Tf 20 155 Td 2 Tc (Tc spaced) Tj 0 Tc 6 Tw ( Tw word space) Tj 0 Tw ET',
    'BT /F2 12 Tf 20 135 Td 150 Tz (Tz wide) Tj 100 Tz 14 TL T* 4 Ts (rise) Tj 0 Ts ET',
    'BT /F2 12 Tf 20 100 Td [(T) 120 (J) -500 (numbers)] TJ ET',
    'BT /F2 -12 Tf 220 80 Td (negative) Tj ET',
    'BT /F2 10 Tf 1 0 0 1 20 50 Tm (Tm set) Tj 0 -12 TD (TD move) Tj (quote) \' 1 2 (dquote) " ET',
  ], fonts)
  // Render modes 0 to 7 in an outline font. Modes 4 to 7 clip what is painted after `ET`.
  const modes: string[] = ['1 0 0 RG 0.5 w']
  for (let mode = 0; mode < 8; mode++) {
    const y = 215 - mode * 28
    modes.push(mode < 4
      ? `BT /F2 18 Tf ${mode} Tr 20 ${y} Td (Mode ${mode}) Tj ET`
      : `q BT /F2 18 Tf ${mode} Tr 20 ${y} Td (Mode ${mode}) Tj ET 0 0 1 rg 0 ${y - 6} 240 26 re f Q`)
  }
  b.page(modes, fonts)
  // Vertical text (three kana and three Latin letters by CID; then by UTF-16 a long vowel mark, which
  // is rotated, and an ideographic comma, which has a vertical form) and the two Type 3 glyphs.
  b.page([
    'BT /F4 16 Tf 200 220 Td <034B034D034F002200230024> Tj ET',
    'BT /F7 16 Tf 170 220 Td <30A230FC30013042> Tj ET',
    'BT /F5 30 Tf 20 150 Td (abab) Tj ET',
    'BT 0 0.5 0 rg /F5 20 Tf 20 100 Td (ba) Tj ET',
  ], fonts)
  return b.finish()
}

function images(): Uint8Array {
  const b = new DocumentBuilder()
  const rgb = Uint8Array.from({ length: 48 }, (_, i) => (i % 3 === 0 ? i * 5 : i % 3 === 1 ? 255 - i * 5 : 128))
  const image = (extra: Record<string, string>) => b.add(dict({
    Type: '/XObject', Subtype: '/Image', Width: '4', Height: '4', ColorSpace: '/DeviceRGB', BitsPerComponent: '8', ...extra,
  }), rgb)
  const plain = image({})
  const smooth = image({ Interpolate: 'true' })
  const alpha = image({
    SMask: b.add(dict({ Type: '/XObject', Subtype: '/Image', Width: '4', Height: '4', ColorSpace: '/DeviceGray', BitsPerComponent: '8' }),
      Uint8Array.from({ length: 16 }, (_, i) => i * 17)),
  })
  const mask = (size: number, data: Uint8Array) => b.add(dict({
    Type: '/XObject', Subtype: '/Image', Width: String(size), Height: String(size), ImageMask: 'true', BitsPerComponent: '1',
  }), data)
  const stencil = mask(8, Uint8Array.of(0x00, 0x7e, 0x42, 0x5a, 0x5a, 0x42, 0x7e, 0x00))
  // One opaque pixel: pdf.js paints it as a filled unit square.
  const pixel = mask(1, Uint8Array.of(0x00))

  // An RGB image, the same with /Interpolate, one with an /SMask, and an inline image.
  b.page([
    'q 60 0 0 60 20 160 cm /Im1 Do Q',
    'q 60 0 0 60 100 160 cm /Im2 Do Q',
    'q 60 0 0 60 20 80 cm /Im3 Do Q',
    'q 60 0 0 60 100 80 cm BI /W 2 /H 2 /CS /RGB /BPC 8 /F /AHx ID FF000000FF000000FFFFFF00> EI Q',
  ], { XObject: dict({ Im1: plain, Im2: smooth, Im3: alpha }) })
  // A stencil mask and a 1 x 1 stencil, each painted in the fill colour.
  b.page([
    '1 0 0 rg q 60 0 0 60 20 160 cm /Mk1 Do Q',
    '0 0 1 rg q 60 0 0 60 100 160 cm /Mk2 Do Q',
  ], { XObject: dict({ Mk1: stencil, Mk2: pixel }) })
  return b.finish()
}

function shadings(): Uint8Array {
  const b = new DocumentBuilder()
  const redToBlue = b.add(dict({ FunctionType: '2', Domain: '[0 1]', C0: '[1 0 0]', C1: '[0 0 1]', N: '1' }))
  const axial = b.add(dict({ ShadingType: '2', ColorSpace: '/DeviceRGB', Coords: '[20 0 120 0]', Function: redToBlue, Extend: '[true true]' }))
  const radial = b.add(dict({ ShadingType: '3', ColorSpace: '/DeviceRGB', Coords: '[175 180 0 175 180 40]', Function: redToBlue, Extend: '[false true]' }))
  const axialPattern = b.add(dict({
    Type: '/Pattern', PatternType: '2', Matrix: '[1 0 0 1 0 40]',
    Shading: b.add(dict({ ShadingType: '2', ColorSpace: '/DeviceRGB', Coords: '[0 0 200 0]', Function: redToBlue })),
  }))
  // pdf.js samples a function shading with one step per unit of its domain, so a small domain,
  // scaled up by `cm`, keeps its triangle list short.
  const functionBased = b.add(dict({
    ShadingType: '1', ColorSpace: '/DeviceRGB', Domain: '[0 4 0 4]',
    Function: b.add(dict({ FunctionType: '0', Domain: '[0 4 0 4]', Range: '[0 1 0 1 0 1]', Size: '[2 2]', BitsPerSample: '8' }),
      Uint8Array.of(255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0)),
  }))
  // Free-form mesh: one triangle, then a fourth vertex sharing the last edge (flag 1). Per vertex:
  // flag, x, y and r, g, b, one byte each, decoded to 0-240.
  const mesh = b.add(dict({
    ShadingType: '4', ColorSpace: '/DeviceRGB', BitsPerCoordinate: '8', BitsPerComponent: '8', BitsPerFlag: '8',
    Decode: '[0 240 0 240 0 1 0 1 0 1]',
  }), Uint8Array.of(0, 140, 130, 255, 0, 0, 0, 230, 130, 0, 255, 0, 0, 185, 230, 0, 0, 255, 1, 230, 230, 255, 255, 0))
  const tiling = (paintType: 1 | 2, cell: string) => b.add(dict({
    Type: '/Pattern', PatternType: '1', PaintType: String(paintType), TilingType: '1', BBox: '[0 0 20 20]',
    XStep: '20', YStep: '20', Resources: '<< >>',
  }), cell)
  const coloured = tiling(1, '1 0 0 rg 0 0 10 10 re f 0 0 1 rg 10 10 10 10 re f')
  const uncoloured = tiling(2, '0 0 m 10 20 l 20 0 l h f')
  const patterns = dict({ P1: axialPattern, P2: coloured, P3: uncoloured })

  // `sh` with an axial and a radial shading, each inside a clip.
  b.page([
    'q 20 140 100 80 re W n /Sh1 sh Q',
    'q 130 140 90 80 re W n /Sh2 sh Q',
  ], { Shading: dict({ Sh1: axial, Sh2: radial }) })
  // An axial shading as a fill pattern and as a stroke pattern.
  b.page([
    '/Pattern cs /P1 scn 20 20 200 100 re f',
    '/Pattern CS /P1 SCN 8 w 20 170 m 220 170 l S',
  ], { Pattern: patterns })
  // A function-based shading and a free-form mesh, both of which pdf.js turns into triangles.
  b.page([
    'q 20 0 0 20 20 130 cm /Sh3 sh Q',
    '/Sh4 sh',
  ], { Shading: dict({ Sh3: functionBased, Sh4: mesh }) })
  // A coloured and an uncoloured tiling pattern.
  b.page([
    '/Pattern cs /P2 scn 20 130 200 90 re f',
    '/Cs1 cs 0 0.6 0 /P3 scn 20 20 200 90 re f',
  ], { Pattern: patterns, ColorSpace: dict({ Cs1: '[/Pattern /DeviceRGB]' }) })
  // The same patterns set inside a form that is scaled and moved, so the pattern's base matrix differs
  // from the page's.
  const form = b.add(dict({
    Type: '/XObject', Subtype: '/Form', BBox: '[0 0 100 100]', Matrix: '[1 0 0 1 20 20]', Resources: dict({ Pattern: patterns }),
  }), '/Pattern cs /P1 scn 0 0 100 45 re f /P2 scn 0 55 100 45 re f')
  b.page(['q 1.5 0 0 1.5 0 0 cm /Fm1 Do Q'], { XObject: dict({ Fm1: form }) })
  return b.finish()
}

function groups(): Uint8Array {
  const b = new DocumentBuilder()
  const halfFill = b.add(dict({ Type: '/ExtGState', ca: '0.6' }))
  const form = (group: string, content: string, bbox = '[0 0 100 100]') => b.add(dict({
    Type: '/XObject', Subtype: '/Form', BBox: bbox, Group: group, Resources: dict({ ExtGState: dict({ Ga: halfFill }) }),
  }), content)
  const squares = '1 0 0 rg 0 0 60 60 re f 0 0 1 rg 30 30 60 60 re f'
  const isolated = form('<< /S /Transparency /I true /CS /DeviceRGB >>', squares)
  const knockout = form('<< /S /Transparency /K true >>', `/Ga gs ${squares}`)
  const luminosity = form('<< /S /Transparency /CS /DeviceGray >>', '1 g 10 140 90 80 re f 0.5 g 40 170 60 50 re f', '[0 0 240 240]')
  const alpha = form('<< /S /Transparency >>', '/Ga gs 0 g 130 140 90 80 re f', '[0 0 240 240]')
  const transferred = form('<< /S /Transparency /CS /DeviceGray >>', '0.25 g 10 20 220 40 re f 0.75 g 10 60 220 40 re f', '[0 0 240 240]')
  const invert = b.add(dict({ FunctionType: '2', Domain: '[0 1]', C0: '[1]', C1: '[0]', N: '1' }))
  const states = dict({
    Gs1: dict({ Type: '/ExtGState', ca: '0.5' }),
    Gs2: dict({ Type: '/ExtGState', ca: '0.4', CA: '0.7' }),
    Gs3: dict({ Type: '/ExtGState', BM: '/Multiply' }),
    Gs4: dict({ Type: '/ExtGState', SMask: dict({ Type: '/Mask', S: '/Luminosity', G: luminosity, BC: '[0.2]' }) }),
    Gs5: dict({ Type: '/ExtGState', SMask: dict({ Type: '/Mask', S: '/Alpha', G: alpha }) }),
    Gs6: dict({ Type: '/ExtGState', SMask: dict({ Type: '/Mask', S: '/Luminosity', G: transferred, TR: invert }) }),
  })

  // An isolated group drawn at half alpha, and a knockout group of two translucent squares.
  b.page([
    'q /Gs1 gs 1 0 0 1 20 120 cm /Fm1 Do Q',
    'q 1 0 0 1 120 120 cm /Fm2 Do Q',
  ], { ExtGState: states, XObject: dict({ Fm1: isolated, Fm2: knockout }) })
  // Fill and stroke alpha, and the Multiply blend mode.
  b.page([
    'q /Gs2 gs 1 0 0 rg 0 0 1 RG 6 w 20 130 100 80 re B Q',
    '0 1 0 rg 60 20 120 100 re f',
    'q /Gs3 gs 1 0 1 rg 120 50 100 100 re f Q',
  ], { ExtGState: states })
  // A luminosity soft mask with a backdrop, an alpha soft mask, and a luminosity mask with /TR.
  b.page([
    'q /Gs4 gs 1 0 0 rg 0 130 120 110 re f Q',
    'q /Gs5 gs 0 0 1 rg 120 130 120 110 re f Q',
    'q /Gs6 gs 0 0.5 0 rg 0 0 240 120 re f Q',
  ], { ExtGState: states })
  return b.finish()
}

function structure(): Uint8Array {
  const b = new DocumentBuilder()
  const times = b.add(dict({ Type: '/Font', Subtype: '/Type1', BaseFont: '/Times-Roman', Encoding: '/WinAnsiEncoding' }))
  // Three forms, the middle one with a transparency group; the innermost bbox clips its square.
  const inner = b.add(dict({ Type: '/XObject', Subtype: '/Form', BBox: '[10 10 60 60]' }), '0 1 0 rg 0 0 100 100 re f')
  const middle = b.add(dict({
    Type: '/XObject', Subtype: '/Form', BBox: '[0 0 150 150]', Group: '<< /S /Transparency >>',
    Resources: dict({ XObject: dict({ Fm3: inner }) }),
  }), '1 0 0 rg 0 0 150 150 re f /Fm3 Do')
  const outer = b.add(dict({
    Type: '/XObject', Subtype: '/Form', BBox: '[0 0 200 200]', Matrix: '[1 0 0 1 20 20]',
    Resources: dict({ XObject: dict({ Fm2: middle }) }),
  }), '0 0 1 RG 2 w 1 1 198 198 re S q 0.8 0 0 0.8 30 30 cm /Fm2 Do Q')
  const shown = b.add(dict({ Type: '/OCG', Name: '(Shown)' }))
  const hidden = b.add(dict({ Type: '/OCG', Name: '(Hidden)' }))
  const both = b.add(dict({ Type: '/OCMD', OCGs: `[${shown} ${hidden}]`, P: '/AllOn' }))
  const appearance = b.add(dict({ Type: '/XObject', Subtype: '/Form', BBox: '[0 0 160 160]' }), '1 0 0 RG 4 w 2 2 156 156 re S')
  const square = b.add(dict({
    Type: '/Annot', Subtype: '/Square', Rect: '[40 40 200 200]', C: '[1 0 0]', BS: '<< /W 4 >>', F: '4',
    AP: dict({ N: appearance }),
  }))

  b.page(['/Fm1 Do'], { XObject: dict({ Fm1: outer }) })
  // Optional content on, off and by a membership dictionary (off: one of its two groups is off),
  // then marked content with an MCID and plain marked content.
  b.page([
    '/OC /Oc1 BDC 1 0 0 rg 20 130 90 90 re f EMC',
    '/OC /Oc2 BDC 0 0 1 rg 130 130 90 90 re f EMC',
    '/OC /Oc3 BDC 0 1 0 rg 130 20 90 90 re f EMC',
    '/P << /MCID 0 >> BDC BT /F1 12 Tf 20 100 Td (Marked) Tj ET EMC',
    '/Span BMC 0 g 20 20 50 50 re f EMC',
  ], { Font: dict({ F1: times }), Properties: dict({ Oc1: shown, Oc2: hidden, Oc3: both }) })
  b.page(['0 0 0 rg 10 10 20 20 re f'], {}, { Annots: `[${square}]` })
  b.page(null, {})
  // Paths that end with `n` paint nothing.
  b.page(['10 10 m 100 100 l n', '20 20 50 50 re n', '30 200 m 60 230 90 170 120 200 c n'], {})
  return b.finish({
    OCProperties: dict({ OCGs: `[${shown} ${hidden}]`, D: dict({ ON: `[${shown}]`, OFF: `[${hidden}]`, Order: `[${shown} ${hidden}]` }) }),
  })
}

export const FIXTURE_DOCUMENTS: readonly { name: string; build(): Uint8Array }[] = [
  { name: 'paths', build: paths },
  { name: 'text', build: text },
  { name: 'images', build: images },
  { name: 'shadings', build: shadings },
  { name: 'groups', build: groups },
  { name: 'structure', build: structure },
]
