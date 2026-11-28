/**
 * The adapter: a resumable walk of a page's operator list that writes its page scene
 * (docs/pdf-viewer.md § The WebGPU renderer, *The page scene*).
 *
 * Its input is plain data read from pdf.js beforehand (`scene-input.ts`), so this module never
 * imports pdf.js and every `dependency` is resolved before the walk starts.
 *
 * Ported from pdf.js 6.5.438, src/display/canvas.js (`CanvasGraphics`: `executeOperatorList`, the path,
 * clip, text, image, shading, pattern, form, group, annotation and marked-content operators;
 * build/pdf.mjs 9979-12245), Apache-2.0, Copyright 2024 Mozilla Foundation (THIRD_PARTY_NOTICES § 1).
 * Changed: writes a page scene instead of drawing on a canvas. Each paint becomes a row with its
 * page-space box; containers (forms, groups, soft masks, annotations, optional content) become rows
 * around their contents; Type 3 glyphs and tiling cells are adapted once each, after the page, as
 * out-of-tree rows; optional content is recorded, not evaluated. docs/pdf-viewer.md § What is taken
 * from where.
 */

import { ClipKind, Flag, Kind, NoteReason, SceneBuilder, StyleInherit, type FontRecord, type PageScene,
  type RowInit, type SceneSide, type TilingRecord } from './scene.ts'
import { pathKey } from './clips.ts'
import { apply, boxThrough, EMPTY_BOX, IDENTITY, intersect, maxScale, multiply, union, type Box4, type Mat6 } from './matrix.ts'
import { OPS, TEXT_MODE } from './ops.ts'
import { applyGState, initialState, setFont, StateStack, type GState, type GStateContext, type PaintRef } from './state.ts'
import { missingFontCss, placeGlyphs, type GlyphBounds } from './text.ts'

/** What the display side knows of one font, read from `commonObjs`. */
export interface FontInput {
  loadedName: string
  missingFile: boolean
  isType3Font: boolean
  isInvalidPDFjsFont: boolean
  vertical: boolean
  remeasure: boolean
  bold: boolean
  black: boolean
  italic: boolean
  fallbackName: string
  fontMatrix?: readonly number[]
  bbox?: readonly number[]
  type3Procs?: ReadonlyMap<string, { fnArray: ArrayLike<number>; argsArray: readonly unknown[] }>
}

export interface PageInput {
  page: number
  stamp: string
  /** `page.getViewport({ scale: 1 })`. */
  view: { width: number; height: number; transform: Mat6 }
  /** Read only. */
  fnArray: ArrayLike<number>
  /** Read only, never retained, but for an inline image's data, which `SceneSide.inline` holds. */
  argsArray: readonly unknown[]
  /** A string is pdf.js's load error for that font. */
  font(loadedName: string): FontInput | string | null
  glyphBounds: GlyphBounds
  /** `interpolate` is the image's `/Interpolate`, when the reader has it. */
  image(objId: string): { width: number; height: number; interpolate?: boolean } | null
  shading(objId: string): readonly unknown[] | null
}

export interface Stepper {
  /** Runs ops until `expired()` is true or the list ends; true when finished. */
  step(expired: () => boolean): boolean
  finish(): { scene: PageScene; side: SceneSide }
}

export function adaptPage(input: PageInput): Stepper {
  return new Walk(input)
}

/** `beginGroup`'s argument (W 34515-34524); `hasSoftMask` and `isGray` are new in 6.x. */
interface GroupInput {
  matrix: readonly number[] | null
  bbox: ArrayLike<number> | null
  smask: { subtype: string; backdrop?: string | null; transferMap?: Uint8Array | null } | null
  isolated: boolean
  knockout: boolean
  needsIsolation: boolean
  hasSoftMask?: boolean
  isGray?: boolean
}

interface Font { index: number; record: FontRecord; input: FontInput }

/** An operator list the walk adapts: the page, or one out-of-tree procedure. */
interface Frame {
  kind: typeof Kind.Type3Glyph | typeof Kind.TilingCell | null
  fnArray: ArrayLike<number>
  argsArray: readonly unknown[]
  /** Type 3: the font's index; tiling: the first record's index. */
  d0: number
  /** Type 3: the string id of the glyph's `operatorListId`. */
  d1: number
  /** Type 3: the font, and the glyph's `operatorListId`, whose row the font record learns. */
  font: Font | null
  procId: string
  /** Tiling: the cell's bbox, its clip. */
  bbox: Box4 | null
  /** Tiling: every record that draws this cell, given its row once it is opened. */
  records: TilingRecord[]
  row: number
}

const UNIT_SQUARE: Readonly<Box4> = [0, 0, 1, 1]
const UNIT_SQUARE_PATH = [0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 4]

/** What each `constructPath` paint op paints (build/pdf.mjs 10896-11008). `endPath` paints nothing. */
const PAINT_FLAGS: Readonly<Record<number, number>> = {
  [OPS.stroke]: Flag.Stroke,
  [OPS.closeStroke]: Flag.Stroke,
  [OPS.fill]: Flag.Fill,
  [OPS.eoFill]: Flag.Fill | Flag.EvenOdd,
  [OPS.fillStroke]: Flag.Fill | Flag.Stroke,
  [OPS.eoFillStroke]: Flag.Fill | Flag.Stroke | Flag.EvenOdd,
  [OPS.closeFillStroke]: Flag.Fill | Flag.Stroke,
  [OPS.closeEOFillStroke]: Flag.Fill | Flag.Stroke | Flag.EvenOdd,
}

class Walk implements Stepper {
  #input: PageInput
  #b: SceneBuilder
  #side: SceneSide = { inline: [] }
  #context: GStateContext

  #frames: Frame[]
  #frame = 0
  #i = 0
  #done = false

  #stack: StateStack
  /** pdf.js's `baseTransform`: the CTM a form or an annotation started under, which patterns are set against. */
  #base: Mat6
  #baseStack: Mat6[] = []
  /** Open containers, innermost last. */
  #open: { row: number; kind: number }[] = []
  /** Per marked-content level: its `OptionalContent` row, or -1. */
  #marked: number[] = []
  /** The `Text` rows shown in a clip mode since `beginText`, which `endText` makes a clip. */
  #textClip: { rows: number[]; box: Box4 } | null = null
  /** pdf.js's `tempSMask`: the `SoftMask` row the last `endGroup` closed. */
  #softMask = -1
  #knockout = 0
  #groups: { row: number; knockout: number; mask: boolean }[] = []

  #fonts = new Map<string, Font>()
  #fontList: Font[] = []
  #images = new Map<string, number>()
  #shadings = new Map<string, number>()
  #shadingPaints = new Map<string, number>()
  #type3Frames = new Map<string, Frame>()
  #cellFrames = new Map<unknown, Frame>()
  #procBoxes = new Map<string, Box4 | null>()

  constructor(input: PageInput) {
    this.#input = input
    this.#b = new SceneBuilder(input.page, input.stamp, { width: input.view.width, height: input.view.height })
    this.#context = {
      font: (name) => this.#font(name).index,
      takeSoftMask: () => {
        const row = this.#softMask
        this.#softMask = -1
        return row
      },
    }
    this.#frames = [{ kind: null, fnArray: input.fnArray, argsArray: input.argsArray, d0: -1, d1: -1, font: null,
      procId: '', bbox: null, records: [], row: -1 }]
    this.#stack = new StateStack(initialState(input.view.transform))
    this.#base = [...input.view.transform]
  }

  step(expired: () => boolean): boolean {
    while (!this.#done) {
      const frame = this.#frames[this.#frame]!
      if (this.#i >= frame.fnArray.length) {
        this.#endFrame()
        continue
      }
      const i = this.#i++
      this.#op(i, frame.fnArray[i]!, frame.argsArray[i] ?? null, frame.kind === null)
      if (expired()) return this.#done
    }
    return true
  }

  finish(): { scene: PageScene; side: SceneSide } {
    while (!this.step(() => false));
    return { scene: this.#b.finish(), side: this.#side }
  }

  /** Closes what the frame left open, then starts the next queued procedure, breadth first. */
  #endFrame(): void {
    const frame = this.#frames[this.#frame]!
    while (this.#open.length > 0) this.#b.close(this.#open.pop()!.row)
    if (frame.kind === null) this.#b.markTreeEnd()
    const next = this.#frames[++this.#frame]
    if (next === undefined) {
      this.#done = true
      return
    }
    this.#i = 0
    // A procedure starts in glyph or cell space. A tiling cell starts from pdf.js's initial state, since
    // pdf.js draws it with a CanvasGraphics of its own (build/pdf.mjs 9370-9385); an uncoloured one paints
    // in its pattern's colour. A Type 3 glyph's procedure runs in the state of the text that shows it
    // (showType3Text, 11458-11466), which differs from one use to the next, so what it does not set is
    // left to the using `Text` row: its paints, and the stroke fields in `styleInherit`. The base
    // transform is the identity, so a pattern it sets is against that row's base: the xform of the row's
    // nearest `Form` or `Annotation`, else the view's.
    const fresh = initialState(IDENTITY)
    if (next.kind === Kind.Type3Glyph || next.records[0]?.paintType === 2) fresh.fillPaint = fresh.strokePaint = { type: 'inherit' }
    if (next.kind === Kind.Type3Glyph) {
      fresh.styleInherit = StyleInherit.Width | StyleInherit.Cap | StyleInherit.Join | StyleInherit.Miter | StyleInherit.Dash
    }
    this.#stack = new StateStack(fresh)
    this.#base = [...IDENTITY]
    this.#baseStack = []
    this.#marked = []
    this.#textClip = null
    this.#softMask = -1
    this.#knockout = 0
    this.#groups = []
    const row = this.#b.open(next.kind!, -1, { flags: 0, clip: -1, xform: this.#ctmId(fresh), d0: next.d0, d1: next.d1 })
    this.#open.push({ row, kind: next.kind! })
    next.row = row
    if (next.font !== null) next.font.record.procs[next.procId] = row
    for (const record of next.records) record.cell = row
    // TilingPattern#clipBbox: the cell is cut to its bbox.
    if (next.bbox !== null) this.#rectClip(fresh, next.bbox, null)
  }

  #op(i: number, fn: number, args: unknown, inTree: boolean): void {
    const b = this.#b
    if (inTree) b.opCounts[fn]!++
    const s = this.#stack.current
    const a = args as unknown[]
    switch (fn) {
      case OPS.dependency:
      case OPS.setRenderingIntent:
      case OPS.setFlatness:
      case OPS.setCharWidth:
        break
      case OPS.setLineWidth:
        s.lineWidth = a[0] as number
        s.styleInherit &= ~StyleInherit.Width
        break
      case OPS.setLineCap:
        s.cap = a[0] as number
        s.styleInherit &= ~StyleInherit.Cap
        break
      case OPS.setLineJoin:
        s.join = a[0] as number
        s.styleInherit &= ~StyleInherit.Join
        break
      case OPS.setMiterLimit:
        s.miter = a[0] as number
        s.styleInherit &= ~StyleInherit.Miter
        break
      case OPS.setDash:
        s.dash = Array.from(a[0] as ArrayLike<number>)
        s.phase = a[1] as number
        s.styleInherit &= ~StyleInherit.Dash
        break
      case OPS.setGState:
        applyGState(s, a[0] as [string, unknown][], b, this.#context)
        break
      case OPS.save:
        this.#stack.save()
        break
      case OPS.restore:
        this.#stack.restore()
        break
      case OPS.transform:
        s.ctm = multiply(s.ctm, a as Mat6)
        s.ctmId = -1
        break

      case OPS.constructPath:
        this.#constructPath(i, a[0] as number, (a[1] as (ArrayLike<number> | null)[] | null)?.[0] ?? null,
          a[2] as ArrayLike<number> | null)
        break
      case OPS.clip:
        s.pendingClip = 1
        break
      case OPS.eoClip:
        s.pendingClip = 2
        break

      case OPS.beginText:
        s.text.matrix = [...IDENTITY]
        s.text.matrixScale = 1
        s.text.x = s.text.lineX = 0
        s.text.y = s.text.lineY = 0
        break
      case OPS.endText:
        // The clip of every glyph shown in modes 4 to 7 (build/pdf.mjs 11030-11060), and of no other: the
        // node lists those runs' rows. Each row has its own matrix, so the node has none.
        if (this.#textClip !== null) {
          const [d0, d1] = b.clipRows(this.#textClip.rows)
          s.clip = b.clips.intern(s.clip, ClipKind.Text, false, d0, d1, -1, `text:${d0}:${d1}`, this.#textClip.box)
          this.#textClip = null
        }
        break
      case OPS.setCharSpacing:
        s.text.charSpacing = a[0] as number
        break
      case OPS.setWordSpacing:
        s.text.wordSpacing = a[0] as number
        break
      case OPS.setHScale:
        s.text.hScale = (a[0] as number) / 100
        break
      case OPS.setLeading:
        s.text.leading = -(a[0] as number)
        break
      case OPS.setFont:
        setFont(s.text, this.#font(a[0] as string).index, a[1] as number)
        break
      case OPS.setTextRenderingMode:
        s.text.mode = a[0] as number
        break
      case OPS.setTextRise:
        s.text.rise = a[0] as number
        break
      case OPS.moveText:
      case OPS.setLeadingMoveText:
      case OPS.nextLine: {
        const [tx, ty] = fn === OPS.nextLine ? [0, s.text.leading] : (a as [number, number])
        if (fn === OPS.setLeadingMoveText) s.text.leading = ty
        s.text.x = s.text.lineX += tx
        s.text.y = s.text.lineY += ty
        break
      }
      case OPS.setTextMatrix: {
        const m = Array.from((a[0] as ArrayLike<number>)) as Mat6
        s.text.matrix = m
        s.text.matrixScale = Math.hypot(m[0], m[1])
        s.text.x = s.text.lineX = 0
        s.text.y = s.text.lineY = 0
        break
      }
      case OPS.showText:
        this.#showText(i, a[0] as readonly unknown[])
        break
      case OPS.setCharWidthAndBounds: {
        // The glyph is cut to its d1 bounds (build/pdf.mjs 11480-11486).
        const [, , llx, lly, urx, ury] = a as number[]
        this.#rectClip(s, [llx!, lly!, urx!, ury!], null)
        break
      }

      case OPS.setStrokeRGBColor:
        s.strokePaint = { type: 'solid', rgb: parseInt((a[0] as string).slice(1), 16) }
        break
      case OPS.setFillRGBColor:
        s.fillPaint = { type: 'solid', rgb: parseInt((a[0] as string).slice(1), 16) }
        break
      case OPS.setStrokeTransparent:
        s.strokePaint = { type: 'transparent' }
        break
      case OPS.setFillTransparent:
        s.fillPaint = { type: 'transparent' }
        break
      case OPS.setStrokeColorN:
        s.strokePaint = this.#pattern(a)
        break
      case OPS.setFillColorN:
        s.fillPaint = this.#pattern(a)
        break

      case OPS.shadingFill: {
        // It fills the whole canvas through the inverse CTM (build/pdf.mjs 11549-11575): the clip, or the page.
        const objId = a[0] as string
        const shading = this.#shading(objId)
        const row = this.#leaf(Kind.Shading, i, s, { flags: 0, clip: s.clip, xform: this.#ctmId(s),
          comp: this.#comp(s), d0: shading, d1: 0 }, [0, 0, this.#input.view.width, this.#input.view.height])
        if (shading < 0) b.note(row, NoteReason.ShadingMissing)
        break
      }

      case OPS.paintImageXObject: {
        const [objId, pdfWidth, pdfHeight] = a as [string, number, number]
        const image = this.#input.image(objId)
        let index = this.#images.get(objId) ?? -1
        if (image !== null && index < 0) {
          index = b.image({ id: objId, width: image.width, height: image.height, pdfWidth, pdfHeight })
          this.#images.set(objId, index)
        }
        const row = this.#leaf(Kind.Image, i, s, { flags: image?.interpolate ? Flag.Interpolate : 0, clip: s.clip,
          xform: this.#ctmId(s), comp: this.#comp(s), d0: index, d1: 0 }, boxThrough(s.ctm, UNIT_SQUARE))
        if (image === null) b.note(row, NoteReason.ImageMissing)
        break
      }
      case OPS.paintInlineImageXObject: {
        const data = a[0] as { interpolate?: boolean }
        this.#leaf(Kind.Image, i, s, { flags: Flag.Inline | (data.interpolate ? Flag.Interpolate : 0), clip: s.clip,
          xform: this.#ctmId(s), comp: this.#comp(s), d0: this.#side.inline.push(data) - 1, d1: 0 },
        boxThrough(s.ctm, UNIT_SQUARE))
        break
      }
      case OPS.paintImageMaskXObject: {
        const mask = a[0] as { data: unknown; width: number; height: number; interpolate?: boolean }
        let flags = Flag.Fill | (mask.interpolate ? Flag.Interpolate : 0)
        let d0: number
        let missing = false
        if (typeof mask.data === 'string') {
          const image = this.#input.image(mask.data)
          d0 = this.#images.get(mask.data) ?? -1
          if (image !== null && d0 < 0) {
            d0 = b.image({ id: mask.data, width: image.width, height: image.height, pdfWidth: mask.width, pdfHeight: mask.height })
            this.#images.set(mask.data, d0)
          }
          missing = image === null
        } else {
          // A mask inside a Type 3 glyph that pdf.js could not trace carries its bits in the op.
          flags |= Flag.Inline
          d0 = this.#side.inline.push(mask) - 1
        }
        const fill = this.#paint(s.fillPaint)
        const row = this.#leaf(Kind.ImageMask, i, s, { flags, clip: s.clip, xform: this.#ctmId(s), fill: fill.id,
          comp: this.#comp(s), d0, d1: 0 }, boxThrough(s.ctm, UNIT_SQUARE))
        if (missing) b.note(row, NoteReason.ImageMissing)
        else if (fill.missing) b.note(row, NoteReason.ShadingMissing)
        break
      }
      case OPS.paintSolidColorImageMask: {
        // A 1 × 1 opaque mask: the unit square filled (build/pdf.mjs 12184-12193).
        const [d0, d1] = b.path(UNIT_SQUARE_PATH)
        const fill = this.#paint(s.fillPaint)
        const row = this.#leaf(Kind.Path, i, s, { flags: Flag.Fill, clip: s.clip, xform: this.#ctmId(s), fill: fill.id,
          comp: this.#comp(s), d0, d1 }, boxThrough(s.ctm, UNIT_SQUARE))
        if (fill.missing) b.note(row, NoteReason.ShadingMissing)
        break
      }

      case OPS.paintFormXObjectBegin: {
        const [matrix, bbox] = a as [ArrayLike<number> | null, ArrayLike<number> | null]
        this.#stack.save()
        const inner = this.#stack.current
        this.#baseStack.push(this.#base)
        if (matrix) {
          inner.ctm = multiply(inner.ctm, Array.from(matrix) as Mat6)
          inner.ctmId = -1
        }
        this.#base = inner.ctm
        this.#openRow(Kind.Form, i, { flags: 0, clip: inner.clip, xform: this.#ctmId(inner) })
        if (bbox) this.#rectClip(inner, bbox, null)
        break
      }
      case OPS.paintFormXObjectEnd:
        this.#stack.restore()
        this.#base = this.#baseStack.pop() ?? [...this.#input.view.transform]
        this.#closeTo((entry) => entry.kind === Kind.Form)
        break

      case OPS.beginGroup:
        this.#beginGroup(i, a[0] as GroupInput)
        break
      case OPS.endGroup: {
        const group = this.#groups.pop()
        if (group === undefined) break
        this.#knockout = group.knockout
        this.#stack.restore()
        this.#closeTo((entry) => entry.row === group.row)
        if (group.mask) this.#softMask = group.row
        break
      }

      case OPS.beginAnnotation: {
        // pdf.js drops every saved state, then starts afresh under the base transform, clipped to the rect,
        // with the annotation's transform and matrix (build/pdf.mjs 11877-11933). `hasOwnCanvas` draws
        // inline, as pdf.js does without an `annotationCanvasMap`.
        const [id, rect, transform, matrix] = a as [unknown, ArrayLike<number> | null, Mat6, Mat6]
        const fresh = initialState(this.#base)
        this.#stack = new StateStack(fresh)
        if (rect) this.#rectClip(fresh, rect, null)
        this.#baseStack.push(this.#base)
        fresh.ctm = multiply(multiply(fresh.ctm, transform), matrix)
        fresh.ctmId = -1
        this.#base = fresh.ctm
        // The annotation itself is outside its rect, which only its contents are cut to.
        this.#openRow(Kind.Annotation, i, { flags: 0, clip: -1, xform: this.#ctmId(fresh), d0: b.string(String(id)), d1: 0 })
        break
      }
      case OPS.endAnnotation:
        this.#base = this.#baseStack.pop() ?? [...this.#input.view.transform]
        this.#closeTo((entry) => entry.kind === Kind.Annotation)
        break

      case OPS.beginMarkedContent:
        this.#marked.push(-1)
        break
      case OPS.beginMarkedContentProps:
        if (a[0] === 'OC') {
          const row = this.#openRow(Kind.OptionalContent, i, { flags: 0, clip: s.clip, xform: this.#ctmId(s),
            d0: b.ocProps(a[1] ?? null), d1: 0 })
          this.#marked.push(row)
        } else {
          this.#marked.push(-1)
        }
        break
      case OPS.endMarkedContent: {
        const row = this.#marked.pop() ?? -1
        if (row >= 0) this.#closeTo((entry) => entry.row === row)
        break
      }

      default: {
        // An op getOperatorList() never emits (docs/pdf-viewer.md § The op coverage table).
        const row = b.leaf(Kind.Path, i, { flags: 0, clip: s.clip, xform: this.#ctmId(s), d0: -1, d1: 0 })
        b.note(row, NoteReason.UnexpectedOp)
      }
    }
  }

  /**
   * consumePath's order (build/pdf.mjs 12222-12245): the path is painted under the clip in force, and
   * only then does a pending `W` or `W*` clip to it. A path pdf.js got no points for paints nothing and
   * leaves the clip alone, since pdf.js skips `ctx.clip` for an empty path and its clip box stays whole.
   */
  #constructPath(i: number, paintOp: number, buffer: ArrayLike<number> | null, minMax: ArrayLike<number> | null): void {
    const b = this.#b
    const s = this.#stack.current
    if (buffer === null || buffer.length === 0) {
      s.pendingClip = 0
      return
    }
    // rawFillPath's min-max is in the traced mask's pixels while its points are in the unit square
    // (W 16340, 16377), so its box comes from the points.
    const local: Box4 = minMax && paintOp !== OPS.rawFillPath
      ? [minMax[0]!, minMax[1]!, minMax[2]!, minMax[3]!]
      : pathBounds(buffer)
    const flags = paintOp === OPS.rawFillPath ? Flag.Fill : PAINT_FLAGS[paintOp] ?? 0
    const pending = paintOp === OPS.rawFillPath ? 0 : s.pendingClip
    if (flags === 0 && pending === 0) return

    const [d0, d1] = b.path(buffer)
    const xform = this.#ctmId(s)
    if (flags !== 0) {
      const fill = flags & Flag.Fill ? this.#paint(s.fillPaint) : null
      const stroke = flags & Flag.Stroke ? this.#paint(s.strokePaint) : null
      const box = boxThrough(s.ctm, local)
      if (stroke !== null) this.#growForStroke(box, s, s.lineWidth, s.ctm)
      const row = this.#leaf(Kind.Path, i, s, { flags, clip: s.clip, xform, fill: fill?.id ?? -1,
        stroke: stroke?.id ?? -1, style: stroke ? this.#style(s, s.lineWidth) : -1, comp: this.#comp(s), d0, d1 }, box)
      if (fill?.missing || stroke?.missing) b.note(row, NoteReason.ShadingMissing)
    }
    if (pending !== 0) {
      s.clip = b.clips.intern(s.clip, ClipKind.Path, pending === 2, d0, d1, xform, pathKey(buffer, 0, buffer.length),
        boxThrough(s.ctm, local))
      s.pendingClip = 0
    }
  }

  #showText(i: number, items: readonly unknown[]): void {
    const b = this.#b
    const s = this.#stack.current
    const text = s.text
    // Text shown before any `Tf` has a font pdf.js cannot draw: one that failed, named ''.
    const font = text.font >= 0 ? this.#fontList[text.font]! : this.#font('')
    const kind = font.record.kind
    const bounds: GlyphBounds = kind === 'type3'
      ? (_name, id) => this.#procBox(font, id, s)
      : this.#input.glyphBounds
    const run = placeGlyphs(items, s, font.record, font.input, b, bounds)

    let flags: number
    if (kind === 'type3') {
      // showType3Text ignores every mode but invisible, clip modes included (build/pdf.mjs 11434).
      flags = text.mode === TEXT_MODE.INVISIBLE || text.fontSize === 0 ? Flag.Invisible : Flag.Fill
    } else if (kind === 'invalid') {
      flags = Flag.Fill
    } else if (text.fontSize === 0) {
      flags = Flag.Invisible
    } else {
      const fillStroke = text.mode & TEXT_MODE.FILL_STROKE_MASK
      flags = fillStroke === TEXT_MODE.FILL ? Flag.Fill : fillStroke === TEXT_MODE.STROKE ? Flag.Stroke
        : fillStroke === TEXT_MODE.FILL_STROKE ? Flag.Fill | Flag.Stroke : 0
      if (text.mode === TEXT_MODE.INVISIBLE) flags |= Flag.Invisible
      else if (text.mode === TEXT_MODE.ADD_TO_PATH) flags |= Flag.ClipOnly
      else if (text.mode & TEXT_MODE.ADD_TO_PATH_FLAG) flags |= Flag.AddToClip
    }
    const fill = flags & Flag.Fill ? this.#paint(s.fillPaint) : null
    let stroke: { id: number; missing: boolean } | null = null
    let style = -1
    const box: Box4 = run.box === null ? [...EMPTY_BOX] : [...run.box]
    if (kind === 'type3') {
      // The glyphs' procedures run in this state: a stroke in one takes this row's stroke paint and, for
      // the fields it does not set, this row's style (`StyleInherit`).
      stroke = this.#paint(s.strokePaint)
      style = this.#style(s, s.lineWidth)
    } else if (flags & Flag.Stroke) {
      stroke = this.#paint(s.strokePaint)
      // pdf.js strokes a glyph in em space with the line width over the text matrix's scale and the size,
      // and with one device pixel when either is 0 (build/pdf.mjs 11295-11301, 11194).
      const width = text.matrixScale === 0 || s.lineWidth === 0 ? 0 : s.lineWidth / text.matrixScale / text.fontSize
      style = this.#style(s, width)
      if (run.box !== null) this.#growForStroke(box, s, width, run.xform)
    }
    const row = this.#leaf(Kind.Text, i, s, { flags, clip: s.clip, xform: b.xform(run.xform), fill: fill?.id ?? -1,
      stroke: stroke?.id ?? -1, style, comp: this.#comp(s), d0: run.glyphStart, d1: run.glyphCount }, box)

    if (kind === 'failed') b.note(row, NoteReason.FontFailed)
    else if (kind === 'invalid') b.note(row, NoteReason.InvalidFont)
    if (kind === 'type3') {
      let missing = false
      for (const id of run.procs) {
        const proc = font.input.type3Procs?.get(id)
        const key = `${font.index}|${id}`
        if (proc === undefined) {
          missing = true
        } else if (!this.#type3Frames.has(key)) {
          const frame: Frame = { kind: Kind.Type3Glyph, fnArray: proc.fnArray, argsArray: proc.argsArray, d0: font.index,
            d1: b.string(id), font, procId: id, bbox: null, records: [], row: -1 }
          this.#type3Frames.set(key, frame)
          this.#frames.push(frame)
        }
      }
      if (missing) b.note(row, NoteReason.ProcMissing)
    } else if (fill?.missing || stroke?.missing) {
      b.note(row, NoteReason.ShadingMissing)
    }

    // Modes 4 to 7 add the glyphs to the clip endText applies; a glyph with no outline adds no path.
    if (kind !== 'type3' && kind !== 'invalid' && text.mode & TEXT_MODE.ADD_TO_PATH_FLAG && text.fontSize !== 0
      && run.glyphCount > 0) {
      const clipBox: Box4 = kind === 'outline' && run.box !== null ? [...run.box] : [...EMPTY_BOX]
      if (this.#textClip === null) {
        this.#textClip = { rows: [row], box: clipBox }
      } else {
        this.#textClip.rows.push(row)
        union(this.#textClip.box, clipBox)
      }
    }
  }

  #beginGroup(i: number, group: GroupInput): void {
    const b = this.#b
    const outer = this.#stack.current
    // pdf.js draws a group in place, clipped to its bbox, when nothing about it needs a layer
    // (build/pdf.mjs 11622-11638); otherwise into a layer whose content starts opaque and source-over.
    const inPlace = (!group.needsIsolation || !group.isolated && !group.hasSoftMask) && !group.knockout
      && !group.isGray && this.#knockout === 0 && outer.fillAlpha === 1 && outer.blend === 0 && outer.mask === -1
    const flags = (group.isolated ? Flag.Isolated : 0) | (group.knockout ? Flag.Knockout : 0)
      | (group.needsIsolation ? Flag.NeedsIsolation : 0) | (group.isGray ? Flag.Gray : 0)
    const init: RowInit = { flags, clip: outer.clip, xform: this.#ctmId(outer), comp: this.#comp(outer) }
    let row: number
    if (group.smask) {
      const { subtype, backdrop, transferMap } = group.smask
      row = this.#openRow(Kind.SoftMask, i, { ...init,
        comp: b.comp({ blend: 0, fillAlpha: 1, strokeAlpha: 1, mask: -1, transfer: transferMap ? b.transfer([transferMap]) : -1 }),
        d0: subtype === 'Luminosity' ? 1 : 0, d1: backdrop ? parseInt(backdrop.slice(1), 16) : 0 })
    } else {
      row = this.#openRow(Kind.Group, i, init)
    }
    this.#groups.push({ row, knockout: this.#knockout, mask: group.smask !== null })

    this.#stack.save()
    const inner = this.#stack.current
    // A soft mask in force applies to the group as a whole, not to its contents.
    inner.mask = -1
    if (!inPlace) {
      this.#knockout = group.knockout ? this.#knockout + 1 : 0
      inner.blend = 0
      inner.fillAlpha = 1
      inner.strokeAlpha = 1
      inner.transfer = -1
    }
    if (group.bbox) this.#rectClip(inner, group.bbox, group.matrix ? Array.from(group.matrix) as Mat6 : null)
  }

  /**
   * A tiling pattern's paint is made here, against the base transform of now (pdf.js's `TilingPattern`
   * constructor); a shading pattern's when it paints.
   */
  #pattern(args: unknown[]): PaintRef {
    const b = this.#b
    if (args[0] === 'TilingPattern') {
      const [, color, list, matrix, bbox, xstep, ystep, paintType, tilingType] = args as [string, string | null,
        { fnArray: ArrayLike<number>; argsArray: readonly unknown[] }, ArrayLike<number> | null, ArrayLike<number>,
        number, number, 1 | 2, number]
      const record: TilingRecord = {
        cell: -1,
        xform: b.xform(matrix ? multiply(this.#base, Array.from(matrix) as Mat6) : this.#base),
        bbox: [bbox[0]!, bbox[1]!, bbox[2]!, bbox[3]!],
        xstep,
        ystep,
        paintType,
        tilingType,
        rgb: paintType === 2 && color ? parseInt(color.slice(1), 16) : -1,
      }
      const index = b.tiling(record)
      let frame = this.#cellFrames.get(list)
      if (frame === undefined) {
        frame = { kind: Kind.TilingCell, fnArray: list.fnArray, argsArray: list.argsArray, d0: index, d1: 0, font: null,
          procId: '', bbox: record.bbox, records: [], row: -1 }
        this.#cellFrames.set(list, frame)
        this.#frames.push(frame)
      }
      frame.records.push(record)
      if (frame.row >= 0) record.cell = frame.row
      return { type: 'paint', id: b.pattern(2, index, record.xform) }
    }
    const matrix = args[2] as ArrayLike<number> | null
    return { type: 'shading', shading: this.#shading(args[1] as string), matrix: matrix ? Array.from(matrix) as Mat6 : null }
  }

  /** A paint ref as a scene paint id; `missing` when it is a shading pdf.js delivered no IR for. */
  #paint(ref: PaintRef): { id: number; missing: boolean } {
    const b = this.#b
    switch (ref.type) {
      case 'solid':
        return { id: b.solid(ref.rgb), missing: false }
      case 'transparent':
        return { id: b.transparent(), missing: false }
      case 'paint':
        return { id: ref.id, missing: false }
      case 'inherit':
        return { id: -1, missing: false }
      case 'shading': {
        if (ref.shading < 0) return { id: b.transparent(), missing: true }
        const m = ref.matrix ? multiply(this.#base, ref.matrix) : this.#base
        const key = `${ref.shading}|${m.join(',')}`
        let id = this.#shadingPaints.get(key)
        if (id === undefined) {
          id = b.pattern(1, ref.shading, b.xform(m))
          this.#shadingPaints.set(key, id)
        }
        return { id, missing: false }
      }
    }
  }

  #shading(objId: string): number {
    let index = this.#shadings.get(objId)
    if (index === undefined) {
      const ir = this.#input.shading(objId)
      index = ir === null ? -1 : this.#b.shading(ir)
      this.#shadings.set(objId, index)
    }
    return index
  }

  #font(loadedName: string): Font {
    let font = this.#fonts.get(loadedName)
    if (font !== undefined) return font
    const value = this.#input.font(loadedName)
    // pdf.js resolves a font that failed as its error message (build/pdf.mjs 14682-14686).
    const input: FontInput = typeof value === 'object' && value !== null ? value : { loadedName, missingFile: false,
      isType3Font: false, isInvalidPDFjsFont: false, vertical: false, remeasure: false, bold: false, black: false,
      italic: false, fallbackName: '' }
    const kind: FontRecord['kind'] = input !== value ? 'failed' : input.isType3Font ? 'type3'
      : input.isInvalidPDFjsFont ? 'invalid' : input.missingFile ? 'missing' : 'outline'
    const bbox = input.bbox
    const record: FontRecord = {
      loadedName,
      kind,
      css: kind === 'missing' || kind === 'invalid' ? missingFontCss(input) : null,
      vertical: input.vertical,
      remeasure: input.remeasure,
      // pdf.js's FONT_IDENTITY_MATRIX when the font gives none (build/pdf.mjs 11084).
      fontMatrix: input.fontMatrix ? Array.from(input.fontMatrix) as Mat6 : [0.001, 0, 0, 0.001, 0, 0],
      bbox: bbox && bbox.length === 4 ? [bbox[0]!, bbox[1]!, bbox[2]!, bbox[3]!] : null,
      procs: {},
    }
    font = { index: this.#b.font(record), record, input }
    this.#fonts.set(loadedName, font)
    this.#fontList[font.index] = font
    return font
  }

  /**
   * A Type 3 glyph's box in glyph space, from what its procedure paints in the using state `s`, cut to its
   * d1 bounds. Text inside a glyph is not looked into, and the procedure's own miter limit is not
   * followed: the larger of the PDF default and the using state's bounds a stroke's join.
   */
  #procBox(font: Font, id: string, s: GState): Box4 | null {
    const key = `${font.index}|${id}|${s.lineWidth}|${s.miter}`
    if (this.#procBoxes.has(key)) return this.#procBoxes.get(key)!
    const proc = font.input.type3Procs?.get(id)
    let box: Box4 | null = null
    if (proc !== undefined) {
      const content: Box4 = [...EMPTY_BOX]
      let bounds: Box4 | null = null
      let ctm: Mat6 = [...IDENTITY]
      // The procedure takes the line width of the text that shows it until it sets its own.
      let lineWidth = s.lineWidth
      const miter = Math.max(10, s.miter)
      const saved: [Mat6, number][] = []
      for (let i = 0; i < proc.fnArray.length; i++) {
        const a = (proc.argsArray[i] ?? []) as unknown[]
        switch (proc.fnArray[i]) {
          case OPS.save:
            saved.push([ctm, lineWidth])
            break
          case OPS.restore:
            [ctm, lineWidth] = saved.pop() ?? [ctm, lineWidth]
            break
          case OPS.transform:
            ctm = multiply(ctm, a as Mat6)
            break
          case OPS.setLineWidth:
            lineWidth = a[0] as number
            break
          case OPS.constructPath: {
            const buffer = (a[1] as (ArrayLike<number> | null)[] | null)?.[0] ?? null
            const minMax = a[2] as ArrayLike<number> | null
            if (buffer === null || buffer.length === 0 || a[0] === OPS.endPath) break
            const path = boxThrough(ctm, minMax && a[0] !== OPS.rawFillPath
              ? [minMax[0]!, minMax[1]!, minMax[2]!, minMax[3]!]
              : pathBounds(buffer))
            if ((PAINT_FLAGS[a[0] as number] ?? 0) & Flag.Stroke) {
              const pad = lineWidth / 2 * miter * maxScale(ctm)
              path[0] -= pad
              path[1] -= pad
              path[2] += pad
              path[3] += pad
            }
            union(content, path)
            break
          }
          case OPS.paintImageXObject:
          case OPS.paintImageMaskXObject:
          case OPS.paintInlineImageXObject:
          case OPS.paintSolidColorImageMask:
            union(content, boxThrough(ctm, UNIT_SQUARE))
            break
          case OPS.setCharWidthAndBounds: {
            const [, , llx, lly, urx, ury] = a as number[]
            const cut = boxThrough(ctm, [Math.min(llx!, urx!), Math.min(lly!, ury!), Math.max(llx!, urx!), Math.max(lly!, ury!)])
            bounds = bounds === null ? cut : intersect(bounds, cut) ?? [...EMPTY_BOX]
            break
          }
        }
      }
      const cut = bounds === null ? content : intersect(content, bounds)
      box = cut !== null && cut[0] <= cut[2] ? cut : null
    }
    this.#procBoxes.set(key, box)
    return box
  }

  /** Clips the state to a rectangle (a form's bbox, a group's, an annotation's rect, a d1 box, a cell). */
  #rectClip(s: GState, rect: ArrayLike<number>, matrix: Mat6 | null): void {
    const [x0, y0, x1, y1] = [rect[0]!, rect[1]!, rect[2]!, rect[3]!]
    // A group's bbox is in its form's space: pdf.js maps the rectangle through the group matrix.
    const m = matrix ?? IDENTITY
    const buffer = [0, ...apply(m, x0, y0), 1, ...apply(m, x1, y0), 1, ...apply(m, x1, y1), 1, ...apply(m, x0, y1), 4]
    const [d0, d1] = this.#b.path(buffer)
    const box = boxThrough(multiply(s.ctm, m), [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)])
    s.clip = this.#b.clips.intern(s.clip, ClipKind.Path, false, d0, d1, this.#ctmId(s), pathKey(buffer, 0, buffer.length), box)
    // Each of these ends in pdf.js's endPath, which drops a pending clip.
    s.pendingClip = 0
  }

  #openRow(kind: Kind, i: number, init: RowInit): number {
    const row = this.#b.open(kind, i, init)
    this.#open.push({ row, kind })
    return row
  }

  /**
   * Closes open containers down to and including the one `isTarget` picks, innermost first; nothing when
   * none is open. Containers left open inside it (marked content a form did not end) close with it.
   */
  #closeTo(isTarget: (entry: { row: number; kind: number }) => boolean): void {
    const at = this.#open.findLastIndex(isTarget)
    if (at < 0) return
    while (this.#open.length > at) this.#b.close(this.#open.pop()!.row)
  }

  /** A leaf whose box is cut to the clip in force. */
  #leaf(kind: Kind, i: number, s: GState, init: RowInit, box: Readonly<Box4>): number {
    const row = this.#b.leaf(kind, i, init)
    const clip = this.#b.clips.box(s.clip)
    this.#b.setBox(row, clip === null ? box : intersect(box, clip) ?? EMPTY_BOX)
    return row
  }

  /**
   * Half the line width `width` under `m`'s largest stretch, times what a miter join or a square cap can
   * reach beyond it. pdf.js's 1 px floor depends on the zoom and is applied when drawn. Inside a Type 3
   * glyph whose procedure has not set its width, miter limit or cap, those are the using text's, so the
   * stroke may reach anywhere within its clip.
   */
  #growForStroke(box: Box4, s: GState, width: number, m: Readonly<Mat6>): void {
    if (s.styleInherit & (StyleInherit.Width | StyleInherit.Miter | StyleInherit.Cap)) {
      box[0] = box[1] = -Infinity
      box[2] = box[3] = Infinity
      return
    }
    const pad = width / 2 * Math.max(1, s.miter, s.cap === 2 ? Math.SQRT2 : 1) * maxScale(m)
    box[0] -= pad
    box[1] -= pad
    box[2] += pad
    box[3] += pad
  }

  #ctmId(s: GState): number {
    if (s.ctmId < 0) s.ctmId = this.#b.xform(s.ctm)
    return s.ctmId
  }

  /** Under a soft mask, pdf.js composites with the blend in force at the `SMask` key (build/pdf.mjs 10335, 10767). */
  #comp(s: GState): number {
    return this.#b.comp({ blend: s.mask >= 0 ? s.maskBlend : s.blend, fillAlpha: s.fillAlpha, strokeAlpha: s.strokeAlpha,
      mask: s.mask, transfer: s.transfer })
  }

  #style(s: GState, width: number): number {
    return this.#b.style({ width, cap: s.cap, join: s.join, miter: s.miter, dash: s.dash, phase: s.phase,
      inherit: s.styleInherit })
  }
}

/** The hull of a DrawOPS buffer's points, control points included. */
function pathBounds(data: ArrayLike<number>): Box4 {
  const box: Box4 = [...EMPTY_BOX]
  for (let i = 0; i < data.length;) {
    const verb = data[i++]
    const coords = verb === 0 || verb === 1 ? 2 : verb === 2 ? 6 : verb === 3 ? 4 : 0
    for (let k = 0; k < coords; k += 2) {
      box[0] = Math.min(box[0], data[i + k]!)
      box[1] = Math.min(box[1], data[i + k + 1]!)
      box[2] = Math.max(box[2], data[i + k]!)
      box[3] = Math.max(box[3], data[i + k + 1]!)
    }
    i += coords
  }
  return box
}
