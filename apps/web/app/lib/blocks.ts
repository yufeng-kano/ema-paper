/**
 * The result viewer's block rules that need no DOM (docs/web.md § Result viewer, § Editing):
 * what a block is called, what a collapsed section hides, which rows the keyboard and a move
 * step over, how the outline's ticks fit the pane, and the undo stack of block actions.
 *
 * The components only measure, draw and route events; every rule that can be pure is here,
 * unit-tested in node (docs/testing.md § Frontend).
 */
import {
  blockPlain,
  firstWords,
  inlinePlain,
  sectionExtent,
  type OutlineEntry,
  type PlacedBlock,
  type ResultBlock,
} from './markdown'

/** A block's place as the rendered document addresses it: `page-index`. */
export function blockKey(at: { page: number; index: number }): string {
  return `${at.page}-${at.index}`
}

// --- names ------------------------------------------------------------------------------------

/** The words a name is built from, in the reader's language. */
export interface NameText {
  formula: string
  /** A formula named by its `\tag`: `Formula (1)`. */
  formulaTag: (tag: string) => string
  /** A block's words in the locale's quotation marks, `“…”` or `「…」`. */
  quoted: (words: string) => string
  /** A figure named by its caption's first words: `Figure “…”`. */
  figure: (words: string) => string
  /** A figure with no caption. */
  figureBare: string
}

/** How long a name's words run before they are cut. A heading keeps more: it is its name. */
const NAME_CHARS = 24
const HEADING_NAME_CHARS = 60

/** Words cut to `chars` code points, at a space where there is one, the ellipsis inside the
 *  marks that will go round them. */
function cutWords(text: string, chars: number): string {
  const points = [...text]
  if (points.length <= chars) {
    return text
  }
  const head = points.slice(0, chars).join('')
  const space = head.lastIndexOf(' ')
  return `${(space > 0 ? head.slice(0, space) : head).trimEnd()}…`
}

/**
 * A block's name, as the handle's label and a drag's live region say it (docs/web.md § Editing,
 * *Moving blocks*): a formula by its kind — `Formula (1)` by its `\tag` — and anything else by
 * its words in the locale's quotation marks: a heading's text, other blocks' first words, a
 * figure's caption after *Figure*.
 */
export function blockName(block: ResultBlock, text: NameText): string {
  if (block.kind === 'math') {
    const tag = /\\tag\*?\{([^}]*)\}/.exec(block.text)?.[1]?.trim()
    return tag ? text.formulaTag(tag) : text.formula
  }
  if (block.kind === 'fig') {
    const caption = block.caption ? inlinePlain(block.caption) : ''
    return caption ? text.figure(cutWords(firstWords(caption), NAME_CHARS)) : text.figureBare
  }
  const words = blockPlain(block)
  if (block.kind === 'h2' || block.kind === 'h3') {
    return text.quoted(cutWords(words, HEADING_NAME_CHARS))
  }
  return text.quoted(cutWords(firstWords(words), NAME_CHARS))
}

/**
 * Whether `/translate` has something to translate in a block: prose — a paragraph, a heading,
 * a list, a quote, a figure's caption. A formula, code, a table, a contents run and a figure
 * without a caption are left as they are (docs/chat.md § Translation), so offering it there
 * would start a paid turn that writes nothing.
 */
export function translatable(block: ResultBlock): boolean {
  switch (block.kind) {
    case 'p':
    case 'h2':
    case 'h3':
    case 'list':
    case 'quote':
      return true
    case 'fig':
      return Boolean(block.caption)
    default:
      return false
  }
}

// --- collapsed sections -------------------------------------------------------------------------

export interface Folds {
  /** Blocks a collapsed section hides, by `blockKey`. */
  hidden: Set<string>
  /** Each drawn fold's hidden blocks, comments not counted, by its heading's `blockKey`. */
  counts: Map<string, number>
}

/**
 * What the collapsed headings hide (docs/web.md § Result viewer, *Outline, collapse and the
 * way back*): each one's section — the blocks after it up to the next heading of the same or a
 * higher level, across page markers. A collapsed heading inside a fold is hidden with it and
 * draws no fold of its own.
 */
export function foldsOf(placed: readonly PlacedBlock[], outline: readonly OutlineEntry[], collapsed: ReadonlySet<string>): Folds {
  const folds: Folds = { hidden: new Set(), counts: new Map() }
  const collapsedAt = new Set(outline.filter((entry) => collapsed.has(entry.slug)).map(blockKey))
  let index = 0
  while (index < placed.length) {
    const entry = placed[index]!
    const key = blockKey(entry)
    if (!collapsedAt.has(key)) {
      index += 1
      continue
    }
    const { start, end } = sectionExtent(placed, index)
    let count = 0
    for (let at = start; at < end; at += 1) {
      folds.hidden.add(blockKey(placed[at]!))
      if (placed[at]!.block.kind !== 'comment') {
        count += 1
      }
    }
    folds.counts.set(key, count)
    index = end
  }
  return folds
}

function isHeading(entry: PlacedBlock): boolean {
  return entry.block.kind === 'h2' || entry.block.kind === 'h3'
}

/** How many shared blocks a move's rotation may take back from either side. */
const TWIN_REACH = 4

/** The `turn` for which blocks `start`… of `to`, `width` of them, are those of `from` rotated
 *  left by it, or null. */
function blockRotation(from: readonly string[], to: readonly string[], start: number, width: number): number | null {
  for (let turn = 1; turn < width; turn += 1) {
    let rotated = true
    for (let at = 0; at < width && rotated; at += 1) {
      rotated = to[start + at] === from[start + ((at + turn) % width)]
    }
    if (rotated) {
      return turn
    }
  }
  return null
}

/**
 * Where each block of `before` stands in `after`, by positions in `placedBlocks`, null for one
 * that is gone (docs/web.md § Result viewer, *A fold follows its heading through an edit*). The
 * blocks both versions open and close with keep their order. Between them, a run moved past
 * another is the same blocks rotated, each carried to its new place; anything else was
 * replaced, and only its headings are followed — by their source, in order, and a renamed one
 * paired with the new heading between the same two kept ones, where as many came as went.
 */
export function followBlocks(before: readonly PlacedBlock[], after: readonly PlacedBlock[]): (number | null)[] {
  const from = before.map((entry) => entry.block.src)
  const to = after.map((entry) => entry.block.src)
  let head = 0
  while (head < from.length && head < to.length && from[head] === to[head]) {
    head += 1
  }
  let tail = 0
  while (tail < from.length - head && tail < to.length - head && from[from.length - 1 - tail] === to[to.length - 1 - tail]) {
    tail += 1
  }
  const fromEnd = from.length - tail
  const toEnd = to.length - tail
  const map = from.map((_, index) => (index < head ? index : index >= fromEnd ? index - fromEnd + toEnd : null))
  if (from.length === to.length && head < fromEnd) {
    // A run moved next to its twin starts with blocks the shared head or tail already took,
    // so they are given back a block at a time until the rotation shows.
    for (let back = 0; back <= Math.min(TWIN_REACH, head); back += 1) {
      for (let forward = 0; forward <= Math.min(TWIN_REACH, tail); forward += 1) {
        const start = head - back
        const width = fromEnd + forward - start
        const turn = blockRotation(from, to, start, width)
        if (turn === null) {
          continue
        }
        for (let at = 0; at < width; at += 1) {
          map[start + ((at + turn) % width)] = start + at
        }
        return map
      }
    }
  }
  const went: number[] = []
  for (let index = head; index < fromEnd; index += 1) {
    if (isHeading(before[index]!)) {
      went.push(index)
    }
  }
  const came: number[] = []
  for (let index = head; index < toEnd; index += 1) {
    if (isHeading(after[index]!)) {
      came.push(index)
    }
  }
  // Kept by source, in order: each heading takes the first new one after the last taken.
  const kept: [number, number][] = [[head - 1, head - 1]]
  let next = 0
  for (const index of went) {
    const found = came.findIndex((other, at) => at >= next && to[other] === from[index])
    if (found !== -1) {
      map[index] = came[found]!
      kept.push([index, came[found]!])
      next = found + 1
    }
  }
  kept.push([fromEnd, toEnd])
  // Renamed: between two kept headings, as many new ones as old ones are the same ones.
  for (let gap = 1; gap < kept.length; gap += 1) {
    const [fromStart, toStart] = kept[gap - 1]!
    const [fromStop, toStop] = kept[gap]!
    const old = went.filter((index) => index > fromStart && index < fromStop)
    const renamed = came.filter((index) => index > toStart && index < toStop)
    if (old.length === renamed.length) {
      old.forEach((index, at) => {
        map[index] = renamed[at]!
      })
    }
  }
  return map
}

/**
 * The collapsed headings after an edit (docs/web.md § Result viewer, *A fold follows its heading
 * through an edit*): each fold moves to its own heading's new slug — which repeated headings,
 * numbered in document order, change whenever one of them is added, deleted, moved or renamed —
 * and a fold whose heading is gone is dropped. A fold whose heading `before` did not hold — not
 * loaded yet, or on a page still to come — is kept as it is.
 */
export function followFolds(
  before: readonly PlacedBlock[],
  beforeOutline: readonly OutlineEntry[],
  after: readonly PlacedBlock[],
  afterOutline: readonly OutlineEntry[],
  collapsed: ReadonlySet<string>,
): Set<string> {
  const map = followBlocks(before, after)
  const beforeAt = new Map(before.map((entry, index) => [blockKey(entry), index]))
  const afterAt = new Map(after.map((entry, index) => [blockKey(entry), index]))
  const slugAt = new Map(afterOutline.map((entry) => [afterAt.get(blockKey(entry)), entry.slug]))
  const known = new Set(beforeOutline.map((entry) => entry.slug))
  const followed = new Set([...collapsed].filter((slug) => !known.has(slug)))
  for (const entry of beforeOutline) {
    const index = beforeAt.get(blockKey(entry))
    const moved = index === undefined ? null : (map[index] ?? null)
    const slug = moved === null ? undefined : slugAt.get(moved)
    if (collapsed.has(entry.slug) && slug !== undefined) {
      followed.add(slug)
    }
  }
  return followed
}

/**
 * The collapsed headings whose sections hold any of the blocks at `indices` of `placedBlocks` —
 * all of them, so a fold inside a fold opens with it (docs/web.md § Result viewer, *What leads
 * into a fold opens it*). A heading's own fold does not hold it: the heading is what the reader
 * is taken to.
 */
export function foldsOver(
  placed: readonly PlacedBlock[],
  outline: readonly OutlineEntry[],
  collapsed: ReadonlySet<string>,
  indices: readonly number[],
): string[] {
  const at = new Map(placed.map((entry, position) => [blockKey(entry), position]))
  const targets = new Set(indices)
  return outline
    .filter((entry) => {
      const heading = at.get(blockKey(entry))
      if (!collapsed.has(entry.slug) || heading === undefined) {
        return false
      }
      const { start, end } = sectionExtent(placed, heading)
      for (let index = start; index < end; index += 1) {
        if (targets.has(index)) {
          return true
        }
      }
      return false
    })
    .map((entry) => entry.slug)
}

/**
 * The blocks of `after` whose source `before` did not have, by their positions in `placedBlocks`
 * — where an edit the viewer did not make landed (docs/web.md § Result viewer, *What leads into a
 * fold opens it*). Counted, so a paragraph written once more is new once. Comments draw nothing,
 * so none is new.
 */
export function changedBlocks(before: readonly PlacedBlock[], after: readonly PlacedBlock[]): number[] {
  const left = new Map<string, number>()
  for (const entry of before) {
    left.set(entry.block.src, (left.get(entry.block.src) ?? 0) + 1)
  }
  const changed: number[] = []
  after.forEach((entry, index) => {
    const count = left.get(entry.block.src) ?? 0
    if (count > 0) {
      left.set(entry.block.src, count - 1)
    } else if (entry.block.kind !== 'comment') {
      changed.push(index)
    }
  })
  return changed
}

/** The most documents whose folds the browser keeps: `localStorage` is small, and shared. */
export const FOLD_MEMORY_LIMIT = 100

/** The folds remembered per document — `doc:<id>` on Files, `job:<id>` on History — with when
 *  each was last set, so the oldest are the ones let go. */
export type FoldMemory = Record<string, { slugs: string[]; at: number }>

/** The remembered folds as stored: anything unreadable is nothing remembered. */
export function readFoldMemory(raw: string | null): FoldMemory {
  try {
    const parsed: unknown = JSON.parse(raw ?? '{}')
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }
    const memory: FoldMemory = {}
    for (const [id, entry] of Object.entries(parsed as Record<string, unknown>)) {
      const { slugs, at } = (entry ?? {}) as { slugs?: unknown; at?: unknown }
      if (Array.isArray(slugs) && typeof at === 'number') {
        memory[id] = { slugs: slugs.filter((slug): slug is string => typeof slug === 'string'), at }
      }
    }
    return memory
  } catch {
    return {}
  }
}

/** The memory with one document's folds set — none forgets it — keeping the `limit` most
 *  recently set. */
export function rememberFolds(memory: FoldMemory, id: string, slugs: Iterable<string>, now: number, limit = FOLD_MEMORY_LIMIT): FoldMemory {
  const list = [...new Set(slugs)]
  const entries = Object.entries(memory).filter(([key]) => key !== id)
  if (list.length) {
    entries.push([id, { slugs: list, at: now }])
  }
  return Object.fromEntries(entries.sort(([, a], [, b]) => b.at - a.at).slice(0, limit))
}

// --- rows: what the keyboard and a move step over ---------------------------------------------

/**
 * One visible item of the document: a block the reader can see, comments aside. A collapsed
 * heading is one row reaching to its section's last hidden block — what a move carries.
 * `first` and `last` index `placedBlocks`.
 */
export interface Row {
  key: string
  first: number
  last: number
}

export function rowsOf(placed: readonly PlacedBlock[], folds: Folds): Row[] {
  const rows: Row[] = []
  placed.forEach((entry, index) => {
    const key = blockKey(entry)
    if (entry.block.kind === 'comment' || folds.hidden.has(key)) {
      return
    }
    const last = folds.counts.has(key) ? sectionExtent(placed, index).end - 1 : index
    rows.push({ key, first: index, last })
  })
  return rows
}

/**
 * Where a keyboard move of rows `from`–`to` goes (docs/web.md § Editing, *Moving blocks*): past
 * one visible item — before the row above, or after the row below, a collapsed section being
 * one row, so *Move down* past a fold lands inside its section. Null at the document's edge.
 */
export function moveTarget(
  rows: readonly Row[],
  from: number,
  to: number,
  direction: 'up' | 'down',
): { target: number; place: 'before' | 'after' } | null {
  if (direction === 'up') {
    const above = rows[from - 1]
    return above ? { target: above.first, place: 'before' } : null
  }
  const below = rows[to + 1]
  return below ? { target: below.last, place: 'after' } : null
}

// --- the outline's ticks ----------------------------------------------------------------------

/** A tick is 2px high, and the gap between two closes from 8px to 2px before any is dropped. */
const TICK = 2
const GAP_MAX = 8
const GAP_MIN = 2

export interface TickFit {
  gap: number
  /** The headings drawn, by outline index, and whether each is a long (`#`/`##`) tick. */
  ticks: { index: number; long: boolean }[]
}

function fitGap(count: number, room: number): number | null {
  if (count <= 1) {
    return count * TICK <= room ? GAP_MAX : null
  }
  const gap = Math.min(GAP_MAX, Math.floor((room - count * TICK) / (count - 1)))
  return gap >= GAP_MIN ? gap : null
}

/**
 * How the outline's ticks fit `room` pixels (docs/web.md § Result viewer, *The ticks fit the
 * pane*): every heading's tick while they fit, the gap closing from 8px to 2px; past that only
 * the `#` and `##` ticks; past that none — null, and the `list-tree` glyph stands in.
 */
export function fitTicks(levels: readonly number[], room: number): TickFit | null {
  const all = levels.map((level, index) => ({ index, long: level <= 2 }))
  const gap = fitGap(all.length, room)
  if (gap !== null) {
    return { gap, ticks: all }
  }
  const top = all.filter((tick) => tick.long)
  const topGap = top.length ? fitGap(top.length, room) : null
  return topGap === null ? null : { gap: topGap, ticks: top }
}

/** The tick that marks the current section: its own, or — where only `#`/`##` are drawn — the
 *  tick of the heading it falls under. */
export function currentTick(fit: TickFit, current: number | null): number | null {
  if (current === null) {
    return null
  }
  let found: number | null = null
  for (const tick of fit.ticks) {
    if (tick.index > current) {
      break
    }
    found = tick.index
  }
  return found
}

// --- undo ---------------------------------------------------------------------------------------

/** One change to a file: at `at`, `removed` was replaced by `inserted`. */
export interface Span {
  at: number
  removed: string
  inserted: string
}

export type UndoFile = 'document' | 'notes'

/** A block action as the stack keeps it: the spans it changed, and hashes of the text before
 *  and after — which is what *exactly what the action left* is checked against. */
export interface UndoStep {
  file: UndoFile
  spans: Span[]
  before: string
  after: string
}

/** A 53-bit hash of a text (cyrb53), with its length: the stack keeps this, not copies of a
 *  document that may be megabytes long. */
export function hashText(text: string): string {
  let h1 = 0xdeadbeef
  let h2 = 0x41c6ce57
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index)
    h1 = Math.imul(h1 ^ code, 2654435761)
    h2 = Math.imul(h2 ^ code, 1597334677)
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909)
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909)
  return `${text.length}:${(4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(36)}`
}

/** How far a move's separator may reach into the text both versions share. */
const SEPARATOR_REACH = 4

/**
 * The spans that turn `before` into `after`: one span between the common head and tail, or —
 * when that span is a run of text moved past another — two, its removal and its insertion, so
 * a move across a book is kept as the run it moved rather than everything it passed.
 */
export function diffSpans(before: string, after: string): Span[] {
  if (before === after) {
    return []
  }
  let head = 0
  const shortest = Math.min(before.length, after.length)
  while (head < shortest && before[head] === after[head]) {
    head += 1
  }
  let tail = 0
  while (tail < shortest - head && before[before.length - 1 - tail] === after[after.length - 1 - tail]) {
    tail += 1
  }
  const removed = before.slice(head, before.length - tail)
  const inserted = after.slice(head, after.length - tail)
  if (removed.length === inserted.length && removed.length > 0) {
    // A move rotates the changed stretch: the run and what it passed swap places. The
    // separators between blocks may have been taken into the shared head or tail, so they
    // are given back a few characters at a time until the rotation shows.
    for (let back = 0; back <= Math.min(SEPARATOR_REACH, head); back += 1) {
      for (let forward = 0; forward <= Math.min(SEPARATOR_REACH, tail); forward += 1) {
        const start = head - back
        const from = before.slice(start, before.length - tail + forward)
        const to = after.slice(start, after.length - tail + forward)
        const turn = rotation(from, to)
        if (turn === null) {
          continue
        }
        // `from` is A + B and `to` is B + A: either A moved past B or B past A. The shorter
        // is the run that moved; the other is what it passed.
        if (turn <= from.length - turn) {
          const run = from.slice(0, turn)
          return [
            { at: start, removed: run, inserted: '' },
            { at: start + from.length - turn, removed: '', inserted: run },
          ]
        }
        const run = from.slice(turn)
        return [
          { at: start + turn, removed: run, inserted: '' },
          { at: start, removed: '', inserted: run },
        ]
      }
    }
  }
  return [{ at: head, removed, inserted }]
}

/** The `k` for which `to` is `from` rotated left by `k` (0 < k < length), or null. */
function rotation(from: string, to: string): number | null {
  if (from.length !== to.length || from.length < 2) {
    return null
  }
  const at = `${from}${from}`.indexOf(to, 1)
  return at > 0 && at < from.length ? at : null
}

/** The spans applied in order: `before` becomes `after`. */
export function applySpans(text: string, spans: readonly Span[]): string {
  return spans.reduce(
    (out, span) => `${out.slice(0, span.at)}${span.inserted}${out.slice(span.at + span.removed.length)}`,
    text,
  )
}

/** The spans taken back, last first: `after` becomes `before`. */
export function revertSpans(text: string, spans: readonly Span[]): string {
  return [...spans]
    .reverse()
    .reduce((out, span) => `${out.slice(0, span.at)}${span.removed}${out.slice(span.at + span.inserted.length)}`, text)
}

/** The stretches of the resulting text an undo (or redo) put back: where to select. A step
 *  that puts nothing back — a delete, redone — gives the point where the text went, so the
 *  block now standing there can be selected. */
export function restoredRanges(spans: readonly Span[], direction: 'undo' | 'redo'): { start: number; end: number }[] {
  // Applied forward, each span's insertion stands at its own place once the later spans have
  // gone in after it. Reverted last first, the first span's removal is back where it was; with
  // two spans (a move) the second was taken out after it, beyond it, so it holds.
  const back = (span: Span) => (direction === 'redo' ? span.inserted : span.removed)
  const ranges = spans.filter((span) => back(span)).map((span) => ({ start: span.at, end: span.at + back(span).length }))
  return ranges.length ? ranges : spans.slice(0, 1).map((span) => ({ start: span.at, end: span.at }))
}

/**
 * The blocks a stretch of the file holds, by their positions in `placedBlocks`: each block at
 * least half inside one of `ranges`, or holding all of it. A diff puts a change as late as the
 * text allows, so a restored block's stretch can start a few characters into it and run as far
 * into the next one — `Two.` deleted before `Three.` comes back as `wo.\n\nT` — and touching a
 * block is not enough. An empty range, or one that holds no block, gives the block at that point
 * or the first after it. Comments are never among them; nothing is selected that draws nothing.
 */
export function blocksInRanges(
  markdown: string,
  placed: readonly PlacedBlock[],
  ranges: readonly { start: number; end: number }[],
): number[] {
  const found = new Set<number>()
  // A block's source is whole lines, found only where whole lines are: a paragraph that is only
  // `1` is not the `1` in `<!-- page: 1 -->`.
  const wholeLines = (start: number, length: number) =>
    (start === 0 || markdown[start - 1] === '\n') && (start + length === markdown.length || markdown[start + length] === '\n')
  let cursor = 0
  const spans = placed.map((entry) => {
    const { src } = entry.block
    let start = markdown.indexOf(src, cursor)
    while (start !== -1 && !wholeLines(start, src.length)) {
      start = markdown.indexOf(src, start + 1)
    }
    if (start === -1) {
      return null
    }
    // A comment is stepped over, so no later block is found inside it, but never selected.
    cursor = start + src.length
    return entry.block.kind === 'comment' ? null : { start, end: cursor }
  })
  for (const range of ranges) {
    const held = spans.flatMap((span, index) => {
      if (!span) {
        return []
      }
      const overlap = Math.min(span.end, range.end) - Math.max(span.start, range.start)
      const within = span.start <= range.start && range.end <= span.end
      return overlap > 0 && (overlap * 2 >= span.end - span.start || within) ? [index] : []
    })
    const point = spans.findIndex((span) => span !== null && span.end > range.start)
    for (const index of held.length ? held : point === -1 ? [] : [point]) {
      found.add(index)
    }
  }
  return [...found].sort((a, b) => a - b)
}

/** What undo or redo hands back: the file, its text as it now is, and what was put back. */
export interface UndoResult {
  file: UndoFile
  text: string
  ranges: { start: number; end: number }[]
}

/**
 * The block actions' own undo (docs/web.md § Editing, *Undo and the status note*): at most
 * `limit` steps across the document and the notes, in order. A step undoes only while its file
 * is exactly what the action left; one that is not is removed — with every other step on that
 * file, since they are a chain — never skipped over.
 */
export class UndoHistory {
  private done: UndoStep[] = []
  private undone: UndoStep[] = []

  constructor(private readonly limit = 50) {}

  get canUndo(): boolean {
    return this.done.length > 0
  }

  get canRedo(): boolean {
    return this.undone.length > 0
  }

  /** A block action: `before` became `after`. A new action ends every redo. */
  record(file: UndoFile, before: string, after: string): void {
    const spans = diffSpans(before, after)
    if (!spans.length) {
      return
    }
    this.done.push({ file, spans, before: hashText(before), after: hashText(after) })
    if (this.done.length > this.limit) {
      this.done.shift()
    }
    this.undone = []
  }

  /** A change to `file` that was not a block action: every step on it is stale. */
  clear(file: UndoFile): void {
    this.done = this.done.filter((step) => step.file !== file)
    this.undone = this.undone.filter((step) => step.file !== file)
  }

  undo(current: (file: UndoFile) => string): UndoResult | null {
    while (this.done.length) {
      const step = this.done[this.done.length - 1]!
      const text = current(step.file)
      if (hashText(text) !== step.after) {
        this.clear(step.file)
        continue
      }
      this.done.pop()
      this.undone.push(step)
      return { file: step.file, text: revertSpans(text, step.spans), ranges: restoredRanges(step.spans, 'undo') }
    }
    return null
  }

  redo(current: (file: UndoFile) => string): UndoResult | null {
    while (this.undone.length) {
      const step = this.undone[this.undone.length - 1]!
      const text = current(step.file)
      if (hashText(text) !== step.before) {
        this.clear(step.file)
        continue
      }
      this.undone.pop()
      this.done.push(step)
      return { file: step.file, text: applySpans(text, step.spans), ranges: restoredRanges(step.spans, 'redo') }
    }
    return null
  }
}
