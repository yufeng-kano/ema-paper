/**
 * The page scene: one page's objects as a structure of typed arrays, built once per reading of its
 * operator list (docs/pdf-viewer.md § The WebGPU renderer, *The page scene*). Every kind, pool and
 * flag the later stages draw from is here from stage 3, so the layout never changes under them.
 *
 * Rows are in paint order and form a pre-order tree: a container's children follow it, and `end` is
 * one past its last descendant. Rows `[0, treeEnd)` are the page; the rows after are the out-of-tree
 * containers (`Type3Glyph`, `TilingCell`) a glyph or a pattern refers to by row. An absent reference
 * is -1, and every coordinate is in page space: `page.getViewport({ scale: 1 })`'s PDF points, y down,
 * `/Rotate` applied.
 *
 * A scene holds no pdf.js object, only numbers, strings and copies of pdf.js's arrays, so it can be
 * structured-cloned to a worker and kept after pdf.js has released the page.
 */

import { ClipInterner, grow, type Column } from './clips.ts'
import { EMPTY_BOX, union, type Box4, type Mat6 } from './matrix.ts'
import { OP_NAMES } from './ops.ts'

export const SCENE_VERSION = 1

export const Kind = {
  Path: 1,
  Text: 2,
  Image: 3,
  ImageMask: 4,
  Shading: 5,
  Form: 16,
  Group: 17,
  SoftMask: 18,
  Annotation: 19,
  OptionalContent: 20,
  Type3Glyph: 21,
  TilingCell: 22,
} as const
export type Kind = (typeof Kind)[keyof typeof Kind]

export function isContainer(kind: number): boolean {
  return kind >= 16
}

export const Flag = {
  Active: 1 << 0,
  Fill: 1 << 1,
  Stroke: 1 << 2,
  EvenOdd: 1 << 3,
  ClipOnly: 1 << 4,
  Invisible: 1 << 5,
  AddToClip: 1 << 6,
  OutOfTree: 1 << 7,
  NotDrawn: 1 << 8,
  Interpolate: 1 << 9,
  Isolated: 1 << 10,
  Knockout: 1 << 11,
  NeedsIsolation: 1 << 12,
  Gray: 1 << 13,
  Inline: 1 << 14,
} as const

export const GlyphFlag = { Accent: 1, Rotated: 2, Space: 4, NotInFont: 8 } as const
export const PaintKind = { Solid: 0, Shading: 1, Tiling: 2, Transparent: 3 } as const
export const ClipKind = { Path: 0, Text: 1 } as const
/**
 * `styles.inherit` bits: in a `Type3Glyph`, the stroke fields its procedure did not set, which come from
 * the style of the `Text` row that shows the glyph (pdf.js runs the procedure in that text's state).
 */
export const StyleInherit = { Width: 1, Cap: 2, Join: 4, Miter: 8, Dash: 16 } as const

/** pdf.js's normalised `BM` values, by `comps.blend` index. */
export const BLEND_MODES = ['source-over', 'multiply', 'screen', 'overlay', 'darken', 'lighten', 'color-dodge',
  'color-burn', 'hard-light', 'soft-light', 'difference', 'exclusion', 'hue', 'saturation', 'color',
  'luminosity'] as const

export const NoteReason = {
  FontFailed: 1,
  InvalidFont: 2,
  ImageMissing: 3,
  ShadingMissing: 4,
  UnexpectedOp: 5,
  ProcMissing: 6,
} as const
export type NoteReason = (typeof NoteReason)[keyof typeof NoteReason]

export interface ObjectColumns {
  kind: Uint8Array
  flags: Uint16Array
  parent: Int32Array
  end: Int32Array
  bbox: Float32Array
  clip: Int32Array
  xform: Int32Array
  fill: Int32Array
  stroke: Int32Array
  style: Int32Array
  comp: Int32Array
  d0: Int32Array
  d1: Int32Array
  op: Int32Array
}
export interface ClipColumns {
  parent: Int32Array
  kind: Uint8Array
  evenOdd: Uint8Array
  /** A path clip's range in `pathData`; a text clip's in `clipRows`. */
  d0: Int32Array
  d1: Int32Array
  xform: Int32Array
  box: Float32Array
}
export interface PaintColumns { kind: Uint8Array; rgb: Uint32Array; ref: Int32Array; xform: Int32Array }
export interface StyleColumns {
  /**
   * As the state gives it, in the row's user space. A stroked `Text` row has it in em units under the
   * row's `xform`, as pdf.js strokes glyphs (the line width over the text matrix's scale and the font
   * size), with no 1 px floor, and 0 there is pdf.js's single device pixel. A Type 3 `Text` row has the
   * state's own value, which its glyphs' procedures take as it is, in glyph space.
   */
  width: Float32Array
  cap: Uint8Array
  join: Uint8Array
  miter: Float32Array
  dashStart: Int32Array
  dashCount: Int32Array
  dashPhase: Float32Array
  /** `StyleInherit` bits; 0 outside a `Type3Glyph`. */
  inherit: Uint8Array
}
export interface CompColumns {
  blend: Uint8Array
  fillAlpha: Float32Array
  strokeAlpha: Float32Array
  mask: Int32Array
  transfer: Int32Array
}
export interface GlyphColumns {
  font: Uint16Array
  code: Uint32Array
  x: Float32Array
  y: Float32Array
  /**
   * The width in glyph units pdf.js remeasures the glyph against when drawn (stage 5): its PDF width, or
   * for a vertical glyph that is not rotated its advance, `-vmetric[0]`.
   */
  width: Float32Array
  unicode: Int32Array
  flags: Uint8Array
}
export interface FontRecord {
  loadedName: string
  kind: 'outline' | 'missing' | 'type3' | 'failed' | 'invalid'
  css: string | null
  vertical: boolean
  remeasure: boolean
  fontMatrix: Mat6
  bbox: Box4 | null
  /** Type 3: operatorListId -> row of its `Type3Glyph`. */
  procs: Record<string, number>
}
export interface ImageRecord { id: string; width: number; height: number; pdfWidth: number; pdfHeight: number }
export interface TilingRecord {
  cell: number
  xform: number
  bbox: Box4
  xstep: number
  ystep: number
  paintType: 1 | 2
  tilingType: number
  /** -1 for a coloured pattern (paint type 1). */
  rgb: number
}

export interface PageScene {
  version: typeof SCENE_VERSION
  stamp: string
  /** 0-based. */
  page: number
  view: { width: number; height: number }
  /** Object rows. */
  count: number
  /** Rows `[0, treeEnd)` are the page tree. */
  treeEnd: number
  obj: ObjectColumns
  /** 6 per entry. */
  xforms: Float64Array
  /** pdf.js's DrawOPS encoding, copied. */
  pathData: Float32Array
  clips: ClipColumns
  /**
   * For each text clip, the `Text` rows whose glyphs make it, in order: the runs shown in modes 4 to 7
   * between one `beginText` and its `endText`, and no run in another mode.
   */
  clipRows: Int32Array
  paints: PaintColumns
  styles: StyleColumns
  dashes: Float32Array
  comps: CompColumns
  /** 1024 per entry: R, G, B, A maps of 256 (identity where pdf.js gives none). */
  transfers: Uint8Array
  glyphs: GlyphColumns
  fonts: FontRecord[]
  images: ImageRecord[]
  /** pdf.js shading IR, typed arrays copied. */
  shadings: unknown[]
  tilings: TilingRecord[]
  /** pdf.js OC props, as given. */
  ocProps: unknown[]
  strings: string[]
  notes: { row: Int32Array; reason: Uint8Array }
  /** Index = op code. */
  opCounts: Uint32Array
}

/** Main-thread only: data pdf.js put inline in the op list (inline images). Never posted. */
export interface SceneSide { inline: unknown[] }

export interface RowInit {
  flags: number
  clip: number
  xform: number
  fill?: number
  stroke?: number
  style?: number
  comp?: number
  d0?: number
  d1?: number
}
export interface StrokeStyle {
  width: number
  cap: number
  join: number
  miter: number
  dash: readonly number[]
  phase: number
  /** `StyleInherit` bits, 0 when absent. */
  inherit?: number
}
export interface Composite { blend: number; fillAlpha: number; strokeAlpha: number; mask: number; transfer: number }
export interface GlyphInit { font: number; code: number; x: number; y: number; width: number; unicode: number; flags: number }

/**
 * Parallel columns that grow together, one entry per `add()`. A column may hold several values per
 * entry (a box holds 4); its width is its length over the pool's capacity.
 */
class Pool<T extends { [K in keyof T]: Column }> {
  columns: T
  count = 0
  #capacity: number
  #make: (capacity: number) => T

  constructor(capacity: number, make: (capacity: number) => T) {
    this.#capacity = capacity
    this.#make = make
    this.columns = make(capacity)
  }

  add(): number {
    if (this.count === this.#capacity) {
      this.#capacity *= 2
      const next = this.#make(this.#capacity)
      for (const key in next) next[key].set(this.columns[key])
      this.columns = next
    }
    return this.count++
  }

  trimmed(): T {
    const out = {} as T
    for (const key in this.columns) {
      const column = this.columns[key]
      out[key] = column.slice(0, this.count * (column.length / this.#capacity)) as T[typeof key]
    }
    return out
  }
}

export class SceneBuilder {
  readonly clips = new ClipInterner()
  /** How many of each op the list had, by op code. The adapter counts as it walks. */
  readonly opCounts = new Uint32Array(OP_NAMES.length)

  #page: number
  #stamp: string
  #view: { width: number; height: number }
  #treeEnd = -1
  /** The containers opened and not yet closed, innermost last. */
  #open: number[] = []

  #rows = new Pool(256, (n): ObjectColumns => ({
    kind: new Uint8Array(n),
    flags: new Uint16Array(n),
    parent: new Int32Array(n),
    end: new Int32Array(n),
    bbox: new Float32Array(n * 4),
    clip: new Int32Array(n),
    xform: new Int32Array(n),
    fill: new Int32Array(n),
    stroke: new Int32Array(n),
    style: new Int32Array(n),
    comp: new Int32Array(n),
    d0: new Int32Array(n),
    d1: new Int32Array(n),
    op: new Int32Array(n),
  }))

  #xforms = new Pool(64, (n) => ({ values: new Float64Array(n * 6) }))
  #pathData = new Float32Array(4096)
  #pathLength = 0
  #clipRows = new Int32Array(16)
  #clipRowsLength = 0
  #paints = new Pool(16, (n): PaintColumns => ({
    kind: new Uint8Array(n),
    rgb: new Uint32Array(n),
    ref: new Int32Array(n),
    xform: new Int32Array(n),
  }))
  #solids = new Map<number, number>()
  #transparent = -1
  #styles = new Pool(16, (n): StyleColumns => ({
    width: new Float32Array(n),
    cap: new Uint8Array(n),
    join: new Uint8Array(n),
    miter: new Float32Array(n),
    dashStart: new Int32Array(n),
    dashCount: new Int32Array(n),
    dashPhase: new Float32Array(n),
    inherit: new Uint8Array(n),
  }))
  #styleIds = new Map<string, number>()
  #dashes = new Float32Array(16)
  #dashLength = 0
  #comps = new Pool(16, (n): CompColumns => ({
    blend: new Uint8Array(n),
    fillAlpha: new Float32Array(n),
    strokeAlpha: new Float32Array(n),
    mask: new Int32Array(n),
    transfer: new Int32Array(n),
  }))
  #compIds = new Map<string, number>()
  #transfers = new Pool(1, (n) => ({ maps: new Uint8Array(n * 1024) }))
  #glyphs = new Pool(1024, (n): GlyphColumns => ({
    font: new Uint16Array(n),
    code: new Uint32Array(n),
    x: new Float32Array(n),
    y: new Float32Array(n),
    width: new Float32Array(n),
    unicode: new Int32Array(n),
    flags: new Uint8Array(n),
  }))
  #strings: string[] = []
  #stringIds = new Map<string, number>()
  #fonts: FontRecord[] = []
  #images: ImageRecord[] = []
  #shadings: unknown[] = []
  #tilings: TilingRecord[] = []
  #ocProps: unknown[] = []
  #notes = new Pool(4, (n) => ({ row: new Int32Array(n), reason: new Uint8Array(n) }))

  constructor(page: number, stamp: string, view: { width: number; height: number }) {
    this.#page = page
    this.#stamp = stamp
    this.#view = { width: view.width, height: view.height }
    // Composite 0 is the default every row starts with: source-over, opaque, no mask, no transfer.
    this.comp({ blend: 0, fillAlpha: 1, strokeAlpha: 1, mask: -1, transfer: -1 })
  }

  /** Opens a container; its children are the rows added until `close(row)`. */
  open(kind: Kind, op: number, init: RowInit): number {
    if (!isContainer(kind)) throw new Error(`open: ${kindName(kind)} is not a container`)
    const row = this.#row(kind, op, init)
    this.#open.push(row)
    return row
  }

  /** Closes the innermost open container: sets its `end`, and grows its box to hold its children's. */
  close(row: number): void {
    if (this.#open.at(-1) !== row) throw new Error(`close: row ${row} is not the innermost open container`)
    this.#open.pop()
    const { end, bbox } = this.#rows.columns
    end[row] = this.#rows.count
    const box = this.#box(row)
    for (let child = row + 1; child < end[row]!; child = end[child]!) union(box, this.#box(child))
    bbox.set(box, row * 4)
  }

  leaf(kind: Kind, op: number, init: RowInit): number {
    if (isContainer(kind)) throw new Error(`leaf: ${kindName(kind)} is a container`)
    return this.#row(kind, op, init)
  }

  setBox(row: number, box: Readonly<Box4>): void {
    this.#rows.columns.bbox.set(box, row * 4)
  }

  /** Records why a row is not drawn, and flags it `NotDrawn`. */
  note(row: number, reason: NoteReason): void {
    const i = this.#notes.add()
    this.#notes.columns.row[i] = row
    this.#notes.columns.reason[i] = reason
    this.#rows.columns.flags[row]! |= Flag.NotDrawn
  }

  /** Ends the page tree. Every row added after it is flagged `OutOfTree`. */
  markTreeEnd(): void {
    if (this.#open.length > 0) throw new Error(`markTreeEnd: ${this.#open.length} containers are still open`)
    this.#treeEnd = this.#rows.count
  }

  /** Dedupes against the last one added: consecutive rows under one matrix share an entry. */
  xform(m: Readonly<Mat6>): number {
    const { values } = this.#xforms.columns
    const last = this.#xforms.count - 1
    if (last >= 0 && m.every((v, k) => values[last * 6 + k] === v)) return last
    const i = this.#xforms.add()
    this.#xforms.columns.values.set(m, i * 6)
    return i
  }

  /** Copies a path buffer into the pool; pdf.js caches its buffers per op, so none is held. */
  path(buffer: ArrayLike<number>): [offset: number, length: number] {
    const offset = this.#pathLength
    while (offset + buffer.length > this.#pathData.length) this.#pathData = grow(this.#pathData)
    this.#pathData.set(buffer, offset)
    this.#pathLength += buffer.length
    return [offset, buffer.length]
  }

  /** A text clip's rows, copied into `clipRows`. */
  clipRows(rows: readonly number[]): [offset: number, count: number] {
    const offset = this.#clipRowsLength
    while (offset + rows.length > this.#clipRows.length) this.#clipRows = grow(this.#clipRows)
    this.#clipRows.set(rows, offset)
    this.#clipRowsLength += rows.length
    return [offset, rows.length]
  }

  /** Interned. */
  solid(rgb: number): number {
    let id = this.#solids.get(rgb)
    if (id === undefined) {
      id = this.#paint(PaintKind.Solid, rgb, -1, -1)
      this.#solids.set(rgb, id)
    }
    return id
  }

  /** Interned: one id for every transparent paint. */
  transparent(): number {
    if (this.#transparent < 0) this.#transparent = this.#paint(PaintKind.Transparent, 0, -1, -1)
    return this.#transparent
  }

  pattern(kind: 1 | 2, ref: number, xform: number): number {
    return this.#paint(kind, 0, ref, xform)
  }

  /** Interned by value. The width is stored as given; a path's 1 px floor is applied when drawn. */
  style(s: StrokeStyle): number {
    const inherit = s.inherit ?? 0
    const key = `${s.width}|${s.cap}|${s.join}|${s.miter}|${s.phase}|${s.dash.join(',')}|${inherit}`
    let id = this.#styleIds.get(key)
    if (id !== undefined) return id
    const dashStart = this.#dashLength
    while (dashStart + s.dash.length > this.#dashes.length) this.#dashes = grow(this.#dashes)
    this.#dashes.set(s.dash, dashStart)
    this.#dashLength += s.dash.length
    id = this.#styles.add()
    const c = this.#styles.columns
    c.width[id] = s.width
    c.cap[id] = s.cap
    c.join[id] = s.join
    c.miter[id] = s.miter
    c.dashStart[id] = dashStart
    c.dashCount[id] = s.dash.length
    c.dashPhase[id] = s.phase
    c.inherit[id] = inherit
    this.#styleIds.set(key, id)
    return id
  }

  /** Interned; id 0 is the default. */
  comp(c: Composite): number {
    const key = `${c.blend}|${c.fillAlpha}|${c.strokeAlpha}|${c.mask}|${c.transfer}`
    let id = this.#compIds.get(key)
    if (id !== undefined) return id
    id = this.#comps.add()
    const columns = this.#comps.columns
    columns.blend[id] = c.blend
    columns.fillAlpha[id] = c.fillAlpha
    columns.strokeAlpha[id] = c.strokeAlpha
    columns.mask[id] = c.mask
    columns.transfer[id] = c.transfer
    this.#compIds.set(key, id)
    return id
  }

  /**
   * pdf.js's `TR` maps as 1024 bytes, R, G, B and A; -1 for none. As pdf.js's Canvas 2D applies them
   * (`TransferMapsFallback`): a single map serves R, G and B, a fourth map is not used, a null map is
   * the identity, and alpha is never mapped.
   */
  transfer(maps: readonly (Uint8Array | null)[] | null): number {
    if (maps === null || maps.length === 0) return -1
    const [r, g = r, b = r] = maps
    const id = this.#transfers.add()
    const out = this.#transfers.columns.maps.subarray(id * 1024, id * 1024 + 1024)
    for (let i = 0; i < 256; i++) {
      out[i] = r ? r[i]! : i
      out[256 + i] = g ? g[i]! : i
      out[512 + i] = b ? b[i]! : i
      out[768 + i] = i
    }
    return id
  }

  glyph(g: GlyphInit): number {
    const id = this.#glyphs.add()
    const c = this.#glyphs.columns
    c.font[id] = g.font
    c.code[id] = g.code
    c.x[id] = g.x
    c.y[id] = g.y
    c.width[id] = g.width
    c.unicode[id] = g.unicode
    c.flags[id] = g.flags
    return id
  }

  /** Interned. */
  string(s: string): number {
    let id = this.#stringIds.get(s)
    if (id === undefined) {
      id = this.#strings.push(s) - 1
      this.#stringIds.set(s, id)
    }
    return id
  }

  font(f: FontRecord): number {
    return this.#fonts.push(f) - 1
  }

  image(r: ImageRecord): number {
    return this.#images.push(r) - 1
  }

  /** Copies the IR, typed arrays included, so the scene never shares a buffer with pdf.js. */
  shading(ir: unknown): number {
    return this.#shadings.push(structuredClone(ir)) - 1
  }

  tiling(t: TilingRecord): number {
    return this.#tilings.push(t) - 1
  }

  ocProps(p: unknown): number {
    return this.#ocProps.push(structuredClone(p)) - 1
  }

  /** The scene, every array trimmed to its length. */
  finish(): PageScene {
    const count = this.#rows.count
    return {
      version: SCENE_VERSION,
      stamp: this.#stamp,
      page: this.#page,
      view: { ...this.#view },
      count,
      treeEnd: this.#treeEnd < 0 ? count : this.#treeEnd,
      obj: this.#rows.trimmed(),
      xforms: this.#xforms.trimmed().values,
      pathData: this.#pathData.slice(0, this.#pathLength),
      clips: this.clips.columns(),
      clipRows: this.#clipRows.slice(0, this.#clipRowsLength),
      paints: this.#paints.trimmed(),
      styles: this.#styles.trimmed(),
      dashes: this.#dashes.slice(0, this.#dashLength),
      comps: this.#comps.trimmed(),
      transfers: this.#transfers.trimmed().maps,
      glyphs: this.#glyphs.trimmed(),
      fonts: this.#fonts,
      images: this.#images,
      shadings: this.#shadings,
      tilings: this.#tilings,
      ocProps: this.#ocProps,
      strings: this.#strings,
      notes: this.#notes.trimmed(),
      opCounts: this.opCounts.slice(),
    }
  }

  #row(kind: Kind, op: number, init: RowInit): number {
    const i = this.#rows.add()
    const c = this.#rows.columns
    c.kind[i] = kind
    c.flags[i] = init.flags | Flag.Active | (this.#treeEnd >= 0 ? Flag.OutOfTree : 0)
    c.parent[i] = this.#open.at(-1) ?? -1
    c.end[i] = i + 1
    c.bbox.set(EMPTY_BOX, i * 4)
    c.clip[i] = init.clip
    c.xform[i] = init.xform
    c.fill[i] = init.fill ?? -1
    c.stroke[i] = init.stroke ?? -1
    c.style[i] = init.style ?? -1
    c.comp[i] = init.comp ?? 0
    c.d0[i] = init.d0 ?? -1
    c.d1[i] = init.d1 ?? -1
    c.op[i] = op
    return i
  }

  #box(row: number): Box4 {
    const b = this.#rows.columns.bbox
    return [b[row * 4]!, b[row * 4 + 1]!, b[row * 4 + 2]!, b[row * 4 + 3]!]
  }

  #paint(kind: number, rgb: number, ref: number, xform: number): number {
    const id = this.#paints.add()
    const c = this.#paints.columns
    c.kind[id] = kind
    c.rgb[id] = rgb
    c.ref[id] = ref
    c.xform[id] = xform
    return id
  }
}

/** An estimate of the memory a scene holds: its typed arrays, its strings, and 8 bytes per number. */
export function sceneBytes(scene: PageScene): number {
  let bytes = 0
  const visit = (value: unknown): void => {
    if (ArrayBuffer.isView(value)) bytes += value.byteLength
    else if (typeof value === 'string') bytes += value.length * 2
    else if (typeof value === 'number') bytes += 8
    else if (value !== null && typeof value === 'object') for (const v of Object.values(value)) visit(v)
  }
  visit(scene)
  return bytes
}

export function kindName(kind: number): string {
  return Object.entries(Kind).find(([, value]) => value === kind)?.[0] ?? `kind ${kind}`
}
