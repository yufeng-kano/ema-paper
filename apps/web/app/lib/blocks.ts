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

/** The stretches of the resulting text an undo (or redo) put back: where to select. */
export function restoredRanges(spans: readonly Span[], direction: 'undo' | 'redo'): { start: number; end: number }[] {
  if (direction === 'redo') {
    // Applied forward, each span's insertion stands at its own place once the later spans
    // have gone in after it.
    return spans.filter((span) => span.inserted).map((span) => ({ start: span.at, end: span.at + span.inserted.length }))
  }
  // Reverted last first, the first span's removal is back where it was; with two spans (a
  // move) the second was taken out after it, beyond it, so it holds.
  return spans.filter((span) => span.removed).map((span) => ({ start: span.at, end: span.at + span.removed.length }))
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
