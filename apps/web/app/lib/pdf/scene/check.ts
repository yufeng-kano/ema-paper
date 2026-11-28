/**
 * The stage 3 scene check (docs/pdf-viewer.md § Stage 3: the scene check): a page's scene against pdf.js's
 * own Canvas 2D drawing of it. Two comparisons, then a report:
 *
 * - boxes: the cells of a 256 × 256 grid over the page that pdf.js recorded a box over
 *   (`recordOperations: true`), against the cells the scene's rows cover, each side widened by a cell;
 * - text: the scene's glyphs, character by character with whitespace removed, against
 *   `getTextContent({ disableNormalization: true })`, and each text item's origin against its glyph's.
 *
 * pdf.js's recorded boxes say where it may have drawn, not what. A state op holds the hull of the ops
 * that depend on it, so only the drawing ops' own boxes are read (`gridFromRecorded`). Of those, a
 * path's box is recorded before its visibility is checked, a run of outline glyphs records its whole
 * clip box and is read on neither side, and a soft mask's content records as any other; the scene's
 * side follows those rules (`gridFromScene`), so that a cell pdf.js has and the scene lacks means
 * content the scene lost. Cells only the scene has are reported, never a mismatch.
 *
 * Pure: it runs in the tests and in `pnpm scene:check`, on plain data.
 */

import { Feature, FEATURE_STAGE, featureCounts } from './coverage.ts'
import { apply, type Box4, type Mat6 } from './matrix.ts'
import { OPS } from './ops.ts'
import { ClipKind, Flag, GlyphFlag, isContainer, Kind, kindName, NoteReason, sceneBytes, type PageScene } from './scene.ts'
import type { SliceStats } from './slice.ts'

export const GRID = 256

export interface RecordedBoxes {
  /** A copy of `page.recordedBBoxes.buffer` (build/pdf.mjs 8107-8109): 4 bytes per op of the render list. */
  buffer: ArrayBuffer
  /**
   * The list the boxes were recorded for, pdf.js's render list. `getOperatorList()`'s list is that list
   * whenever it is as long: the render list's optimizer only merges ops (W 5429-5803).
   */
  fnArray: ArrayLike<number>
  argsArray: readonly unknown[]
}

/**
 * The ops that record a box of their own. Every other op, a state op, a `save`, a marked-content or
 * group op, gets the box of each op that depends on it added to its own (expandBBox per dependent,
 * build/pdf.mjs 8282-8290), so it holds their hull and would cover the gaps between them.
 */
const DRAWING_OPS: ReadonlySet<number> = new Set([OPS.constructPath, OPS.showText, OPS.shadingFill, OPS.paintImageXObject,
  OPS.paintInlineImageXObject, OPS.paintImageMaskXObject, OPS.paintSolidColorImageMask, OPS.paintImageMaskXObjectGroup,
  OPS.paintInlineImageXObjectGroup, OPS.paintImageXObjectRepeat, OPS.paintImageMaskXObjectRepeat])

/**
 * A `Text` row whose op pdf.js records as its whole clip box, a box that says nothing of where the run
 * drew: an outline font's glyphs are paths drawn under `disableFontFace`, which pdf.js does not
 * measure, and a glyph added to the clip paints nothing (`recordCharacterBBox` with no measure,
 * build/pdf.mjs 8496-8516, from `paintChar`, 11166 and 11219). On a page of text that box is the page,
 * and it would cover every loss elsewhere, so neither grid reads these runs.
 */
function recordsClipBox(scene: PageScene, row: number): boolean {
  if (scene.obj.flags[row]! & (Flag.AddToClip | Flag.ClipOnly)) return true
  return scene.obj.d1[row]! > 0 && scene.fonts[scene.glyphs.font[scene.obj.d0[row]!]!]?.kind === 'outline'
}

/**
 * The cells pdf.js recorded for its drawing ops, 1 = covered. Its `BBoxReader` reads each op's bytes as
 * minX, minY, maxX, maxY in 256ths of the canvas, and a max `c` as `(c + 1) / 256` (build/pdf.mjs
 * 8097-8128): cells min to max, both included. The bytes 255, 255, 0, 0 are its `EMPTY_BBOX` (8096), an op
 * that recorded nothing.
 *
 * Two `constructPath`s draw nothing of their own: one that takes a pending clip, whose box pdf.js moves
 * into the clip box (`consumePath`, 12222-12245), and an `endPath` (`n`), whose box pdf.js records all
 * the same (12242). Neither is read. Nor is a `showText` whose scene row `recordsClipBox`: the k-th
 * `showText` of the render list is the k-th `Text` row of the scene's page tree, since the render
 * list's optimizer merges text runs but keeps every `showText` (W 5690-5746).
 */
export function gridFromRecorded(boxes: RecordedBoxes, scene: PageScene): Uint8Array {
  const coords = new Uint8Array(boxes.buffer)
  const { fnArray } = boxes
  if (coords.length !== fnArray.length * 4) {
    throw new Error(`gridFromRecorded: ${coords.length / 4} boxes for ${fnArray.length} ops; the boxes need their own render list`)
  }
  const textRows: number[] = []
  for (let row = 0; row < scene.treeEnd; row++) if (scene.obj.kind[row] === Kind.Text) textRows.push(row)
  const grid = new Uint8Array(GRID * GRID)
  let clipPending = false
  let texts = 0
  for (let op = 0; op < fnArray.length; op++) {
    const fn = fnArray[op]!
    if (fn === OPS.clip || fn === OPS.eoClip) {
      clipPending = true
      continue
    }
    if (fn === OPS.constructPath) {
      const paintsNothing = clipPending || (boxes.argsArray[op] as readonly unknown[] | null)?.[0] === OPS.endPath
      clipPending = false
      if (paintsNothing) continue
    } else if (fn === OPS.showText) {
      const row = textRows[texts++]
      if (row === undefined) throw new Error(`gridFromRecorded: more showText ops than the scene's ${textRows.length} Text rows`)
      if (recordsClipBox(scene, row)) continue
    } else if (!DRAWING_OPS.has(fn)) {
      continue
    }
    const minX = coords[op * 4]!
    const minY = coords[op * 4 + 1]!
    const maxX = coords[op * 4 + 2]!
    const maxY = coords[op * 4 + 3]!
    if (minX === 255 && minY === 255 && maxX === 0 && maxY === 0) continue
    // A box min above max, which pdf.js leaves when a box lies outside its clip box, covers no cell.
    for (let y = minY; y <= maxY; y++) grid.fill(1, y * GRID + minX, y * GRID + maxX + 1)
  }
  if (texts !== textRows.length) throw new Error(`gridFromRecorded: ${texts} showText ops for ${textRows.length} Text rows`)
  return grid
}

/**
 * The cells the scene's page-tree leaves cover, by the rules pdf.js records its boxes by (build/pdf.mjs
 * 8133-8300 and its callers):
 *
 * - an inactive row, or a row inside an inactive container, covers nothing; containers cover nothing of
 *   their own, and out-of-tree rows are covered where the rows that use them are;
 * - inside a hidden `OptionalContent` only a `Path` covers its box: `constructPath` records the box
 *   before the paint checks visibility (10876, 10967), while text, images and shadings return first;
 * - a `Text` row that `recordsClipBox` is read on neither side; any other invisible run covers nothing,
 *   since pdf.js draws none of its glyphs, and any other run covers its box;
 * - pdf.js cuts its boxes by `W` clips, form bboxes and d1 boxes (12240, 11598, `setCharWidthAndBounds`)
 *   but not by a text clip, so a row under a text clip covers the box of the clip above the outermost
 *   text clip, the page when there is none. Nor does it cut them by a group's bbox or an annotation's
 *   rect; no page checked so far has content outside either, and the scene's box is cut by both;
 * - paint does not matter: pdf.js records a transparent paint like any other, and a soft mask's content
 *   is recorded as it is drawn into the mask.
 *
 * `ocVisible[k]` is the visibility of `ocProps[k]`; an index it lacks is visible.
 */
export function gridFromScene(scene: PageScene, ocVisible: Uint8Array): Uint8Array {
  const { kind, flags, end, bbox, clip, d0 } = scene.obj
  const { clips } = scene
  const { width, height } = scene.view
  const grid = new Uint8Array(GRID * GRID)
  // The end of the outermost inactive container and of the outermost hidden optional content the walk
  // is in. Subtrees nest, so the outermost one's end covers every inner one.
  let inactiveEnd = 0
  let hiddenEnd = 0
  for (let row = 0; row < scene.treeEnd; row++) {
    const k = kind[row]!
    const f = flags[row]!
    if (isContainer(k)) {
      if (row >= inactiveEnd && !(f & Flag.Active)) inactiveEnd = end[row]!
      if (k === Kind.OptionalContent && row >= hiddenEnd && ocVisible[d0[row]!] === 0) hiddenEnd = end[row]!
      continue
    }
    if (row < inactiveEnd || !(f & Flag.Active)) continue
    if (row < hiddenEnd && k !== Kind.Path) continue
    if (k === Kind.Text && (recordsClipBox(scene, row) || f & Flag.Invisible)) continue

    // The clip node above the outermost text clip, -1 for none above it; null when no text clip is in force.
    let aboveText: number | null = null
    for (let c = clip[row]!; c >= 0; c = clips.parent[c]!) if (clips.kind[c] === ClipKind.Text) aboveText = clips.parent[c]!
    const box: ArrayLike<number> = aboveText === null ? bbox.subarray(row * 4, row * 4 + 4)
      : aboveText < 0 ? [0, 0, width, height] : clips.box.subarray(aboveText * 4, aboveText * 4 + 4)

    // pdf.js's quantisation (8276-8279), and its clamp: the coordinates are stored in a Uint8ClampedArray.
    const minX = Math.min(255, Math.max(0, Math.floor(box[0]! * GRID / width)))
    const minY = Math.min(255, Math.max(0, Math.floor(box[1]! * GRID / height)))
    const maxX = Math.min(255, Math.max(0, Math.ceil(box[2]! * GRID / width)))
    const maxY = Math.min(255, Math.max(0, Math.ceil(box[3]! * GRID / height)))
    for (let y = minY; y <= maxY; y++) grid.fill(1, y * GRID + minX, y * GRID + maxX + 1)
  }
  return grid
}

/** Each covered cell also covers its 8 neighbours. */
export function dilate(grid: Uint8Array): Uint8Array {
  const out = new Uint8Array(GRID * GRID)
  for (let y = 0; y < GRID; y++) {
    for (let x = 0; x < GRID; x++) {
      if (!grid[y * GRID + x]) continue
      for (let ny = Math.max(0, y - 1); ny <= Math.min(GRID - 1, y + 1); ny++) {
        out.fill(1, ny * GRID + Math.max(0, x - 1), ny * GRID + Math.min(GRID - 1, x + 1) + 1)
      }
    }
  }
  return out
}

export interface GridDiff {
  canvasCells: number
  sceneCells: number
  /** Cells pdf.js covered with no scene cell on or next to them. */
  canvasOnly: number
  /** Cells the scene covered with no pdf.js cell on or next to them. */
  sceneOnly: number
  /** The canvas-only cells as page-space boxes: runs along a grid row, merged down while they line up; at most 16. */
  canvasOnlyBoxes: Box4[]
}

const MAX_BOXES = 16

/** The two grids, each side against the other widened by a cell, so a box a quantisation step off is no difference. */
export function diffGrids(canvas: Uint8Array, scene: Uint8Array, view: { width: number; height: number }): GridDiff {
  const canvasWide = dilate(canvas)
  const sceneWide = dilate(scene)
  const diff: GridDiff = { canvasCells: 0, sceneCells: 0, canvasOnly: 0, sceneOnly: 0, canvasOnlyBoxes: [] }
  // Cell-space boxes, inclusive, and those that reach the row above by their [x0, x1].
  const boxes: Box4[] = []
  let above = new Map<string, Box4>()
  for (let y = 0; y < GRID; y++) {
    const here = new Map<string, Box4>()
    let runStart = -1
    for (let x = 0; x <= GRID; x++) {
      const i = y * GRID + x
      const only = x < GRID && canvas[i] === 1 && sceneWide[i] === 0
      if (x < GRID) {
        if (canvas[i]) diff.canvasCells++
        if (scene[i]) diff.sceneCells++
        if (only) diff.canvasOnly++
        if (scene[i] && !canvasWide[i]) diff.sceneOnly++
      }
      if (only && runStart < 0) runStart = x
      if (!only && runStart >= 0) {
        const key = `${runStart}:${x - 1}`
        let box = above.get(key)
        if (box === undefined) {
          box = [runStart, y, x - 1, y]
          boxes.push(box)
        } else {
          box[3] = y
        }
        here.set(key, box)
        runStart = -1
      }
    }
    above = here
  }
  const sx = view.width / GRID
  const sy = view.height / GRID
  diff.canvasOnlyBoxes = boxes.slice(0, MAX_BOXES).map(([x0, y0, x1, y1]) => [x0 * sx, y0 * sy, (x1 + 1) * sx, (y1 + 1) * sy])
  return diff
}

/** A `getTextContent` item as `scene-input.ts` keeps it: its string and its text-space transform. */
export interface TextItem { str: string; transform: readonly number[] }

export interface TextDiff {
  sceneChars: number
  contentChars: number
  /** The first character index where the two differ, -1 when they are equal. */
  firstMismatch: number
  /** 20 characters on each side of the first difference. */
  context: { scene: string; content: string } | null
  originsCompared: number
  originsOff: number
  /** In page units (pt). */
  maxOriginError: number
}

/** Page units (pt). */
export const ORIGIN_TOLERANCE = 1

const CONTEXT = 20

/**
 * The scene's text against the page's text content. The scene side is the glyphs of active `Text` rows
 * in the page tree, outside annotations (which `getTextContent` does not read), in row order, accents
 * skipped (they are their base glyph's mark, not a character); text in hidden optional content counts,
 * as it does in `getTextContent`. Each side is its characters with whitespace removed.
 *
 * Origins are compared for each item whose string starts with a character: its origin, through
 * `view`, against the origin of the glyph that carries that character, up to the first difference.
 * An item that starts with whitespace is not compared: whitespace has no place in the sequences. Nor is
 * an item of a vertical font: `getTextContent` gives its pen position, while pdf.js draws each vertical
 * glyph at its vertical origin, offset from the pen by the glyph's `vmetric` (build/pdf.mjs 11348-11353),
 * and the scene keeps the origin drawn. Text with rise compares as any other.
 */
export function diffText(scene: PageScene, items: readonly TextItem[], view: Readonly<Mat6>): TextDiff {
  const { kind, flags, end, d0, d1 } = scene.obj
  const { glyphs, strings } = scene
  const sceneText: string[] = []
  const glyphOf: number[] = []
  let inactiveEnd = 0
  let annotationEnd = 0
  for (let row = 0; row < scene.treeEnd; row++) {
    const k = kind[row]!
    if (isContainer(k)) {
      if (row >= inactiveEnd && !(flags[row]! & Flag.Active)) inactiveEnd = end[row]!
      if (k === Kind.Annotation && row >= annotationEnd) annotationEnd = end[row]!
      continue
    }
    if (k !== Kind.Text || row < inactiveEnd || row < annotationEnd || !(flags[row]! & Flag.Active)) continue
    for (let g = d0[row]!; g < d0[row]! + d1[row]!; g++) {
      if (glyphs.flags[g]! & GlyphFlag.Accent || glyphs.unicode[g]! < 0) continue
      for (const ch of strings[glyphs.unicode[g]!]!) {
        if (/\s/u.test(ch)) continue
        sceneText.push(ch)
        glyphOf.push(g)
      }
    }
  }

  const contentText: string[] = []
  const starts: { item: TextItem; at: number }[] = []
  for (const item of items) {
    const chars = Array.from(item.str)
    if (chars.length > 0 && !/\s/u.test(chars[0]!)) starts.push({ item, at: contentText.length })
    for (const ch of chars) if (!/\s/u.test(ch)) contentText.push(ch)
  }

  let firstMismatch = 0
  while (firstMismatch < sceneText.length && firstMismatch < contentText.length
    && sceneText[firstMismatch] === contentText[firstMismatch]) firstMismatch++
  if (firstMismatch === sceneText.length && firstMismatch === contentText.length) firstMismatch = -1
  const around = (chars: string[]): string => chars.slice(Math.max(0, firstMismatch - CONTEXT), firstMismatch + CONTEXT).join('')

  const diff: TextDiff = {
    sceneChars: sceneText.length,
    contentChars: contentText.length,
    firstMismatch,
    context: firstMismatch < 0 ? null : { scene: around(sceneText), content: around(contentText) },
    originsCompared: 0,
    originsOff: 0,
    maxOriginError: 0,
  }
  for (const { item, at } of starts) {
    if (firstMismatch >= 0 && at >= firstMismatch) break
    const g = glyphOf[at]!
    if (scene.fonts[glyphs.font[g]!]?.vertical) continue
    const [x, y] = apply(view, item.transform[4]!, item.transform[5]!)
    const error = Math.hypot(x - glyphs.x[g]!, y - glyphs.y[g]!)
    diff.originsCompared++
    if (error > ORIGIN_TOLERANCE) diff.originsOff++
    diff.maxOriginError = Math.max(diff.maxOriginError, error)
  }
  return diff
}

export interface SceneReport {
  /** 0-based. */
  page: number
  stamp: string
  /** The ops of the page's list. */
  ops: number
  rows: number
  glyphs: number
  bytes: number
  /** Rows by kind name, the kinds the scene has. */
  byKind: Record<string, number>
  /** Features a later stage than the renderer's draws, the ones the scene uses. */
  pending: Partial<Record<Feature, number>>
  /** Rows not drawn, by reason. */
  notDrawn: Partial<Record<keyof typeof NoteReason, number>>
  adapter: SliceStats
  renderMs: number | null
  grid: GridDiff
  text: TextDiff
  /**
   * `mismatch` when pdf.js drew where the scene has nothing, the text or an origin differs, or the list
   * held an op `getOperatorList()` never emits.
   */
  verdict: 'match' | 'mismatch'
}

export function buildReport(input: { scene: PageScene; stats: SliceStats; renderMs: number | null;
  grid: GridDiff; text: TextDiff; rendererStage: number }): SceneReport {
  const { scene, grid, text } = input
  const byKind: Record<string, number> = {}
  for (const code of Object.values(Kind)) {
    const n = scene.obj.kind.filter((k) => k === code).length
    if (n > 0) byKind[kindName(code)] = n
  }
  const pending: Partial<Record<Feature, number>> = {}
  const counts = featureCounts(scene)
  for (const feature of Object.values(Feature)) {
    if (FEATURE_STAGE[feature] > input.rendererStage && counts[feature] > 0) pending[feature] = counts[feature]
  }
  const notDrawn: Partial<Record<keyof typeof NoteReason, number>> = {}
  for (const [name, reason] of Object.entries(NoteReason) as [keyof typeof NoteReason, NoteReason][]) {
    const n = scene.notes.reason.filter((r) => r === reason).length
    if (n > 0) notDrawn[name] = n
  }
  const mismatch = grid.canvasOnly > 0 || text.firstMismatch >= 0 || text.originsOff > 0 || (notDrawn.UnexpectedOp ?? 0) > 0
  return {
    page: scene.page,
    stamp: scene.stamp,
    ops: scene.opCounts.reduce((sum, n) => sum + n, 0),
    rows: scene.count,
    glyphs: scene.glyphs.font.length,
    bytes: sceneBytes(scene),
    byKind,
    pending,
    notDrawn,
    adapter: input.stats,
    renderMs: input.renderMs,
    grid,
    text,
    verdict: mismatch ? 'mismatch' : 'match',
  }
}

/** The report as console lines, in English (developer output, not UI copy). Pages are numbered from 1. */
export function reportLines(report: SceneReport): string[] {
  const { adapter, grid, text } = report
  const size = report.bytes >= 1024 * 1024
    ? `${(report.bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${(report.bytes / 1024).toFixed(1)} KB`
  const list = (entries: string[]): string => (entries.length > 0 ? entries.join(', ') : 'none')
  const lines = [
    [
      `page ${report.page + 1}`,
      `${report.ops} ops`,
      `${report.rows} rows`,
      `${report.glyphs} glyphs`,
      size,
      `adapter ${adapter.totalMs.toFixed(1)} ms in ${adapter.slices} slice${adapter.slices === 1 ? '' : 's'}`
        + ` (longest ${adapter.longestMs.toFixed(1)} ms)`,
      ...(report.renderMs === null ? [] : [`render ${Math.round(report.renderMs)} ms`]),
    ].join(' · '),
    `kinds: ${list(Object.entries(report.byKind).map(([name, n]) => `${name} ${n}`))}`,
    `pending (drawn from a later stage): ${list(Object.entries(report.pending)
      .map(([feature, n]) => `${feature} ${n} (${FEATURE_STAGE[feature as Feature]})`))}`,
    `not drawn: ${list(Object.entries(report.notDrawn).map(([reason, n]) => `${reason} ${n}`))}`,
  ]
  lines.push(`grid: canvas ${grid.canvasCells} cells, scene ${grid.sceneCells}, canvas-only ${grid.canvasOnly}, scene-only ${grid.sceneOnly}`)
  if (grid.canvasOnlyBoxes.length > 0) {
    const boxes = grid.canvasOnlyBoxes.map((b) => b.map((v) => v.toFixed(1)).join(' '))
    lines.push(`  canvas-only boxes (pt): ${boxes.join('; ')}`)
  }
  const origins = `origins ${text.originsCompared} compared, ${text.originsOff} off (max ${text.maxOriginError.toFixed(2)} pt)`
  if (text.context === null) {
    lines.push(`text: ${text.sceneChars} chars, equal; ${origins}`)
  } else {
    lines.push(`text: scene ${text.sceneChars} chars, content ${text.contentChars}, first difference at ${text.firstMismatch}; ${origins}`)
    lines.push(`  scene:   ${JSON.stringify(text.context.scene)}`)
    lines.push(`  content: ${JSON.stringify(text.context.content)}`)
  }
  lines.push(`verdict: ${report.verdict}`)
  return lines
}
