/**
 * The notes' blocks as a handle moves them (docs/web.md § Notes, *The notes' blocks have
 * handles, and move by them*).
 *
 * A unit is one of the editor's top-level blocks, except that an excerpt — a quote right before
 * a paragraph that is only a section link, the pairing `matchExcerpts` reads — is one unit of
 * two, so a passage never parts from its link. Everything here is pure over the editor's
 * document, or over the units' measured boxes, so the rules are unit-tested in node; the editor
 * only measures, draws and routes events.
 */
import type { Node as ProseNode } from '@tiptap/pm/model'
import { Selection, type Transaction } from '@tiptap/pm/state'
import type { Mappable } from '@tiptap/pm/transform'
import { parseResultMarkdown, type ResultBlock } from './markdown'
import { docToMarkdown } from './richdoc'

export interface NoteUnit {
  /** Where its first top-level block starts and its last one ends. */
  from: number
  to: number
  /** Where each of its top-level blocks starts: one, or an excerpt's quote and link line. */
  starts: number[]
  excerpt: boolean
  /** Nothing to carry — an empty line, a comment: no handle, and Escape does not select it. */
  bare: boolean
}

/** A paragraph that is only one section link, spacing aside: an excerpt's link line, as the
 *  link's own node view reads it. */
export function isLinkLine(node: ProseNode): boolean {
  if (node.type.name !== 'paragraph') {
    return false
  }
  let links = 0
  let other = false
  node.forEach((child) => {
    if (child.type.name === 'sectionLink') {
      links += 1
    } else if (!child.isText || (child.text ?? '').trim()) {
      other = true
    }
  })
  return links === 1 && !other
}

function isBare(node: ProseNode): boolean {
  if (node.type.name === 'paragraph') {
    return node.content.size === 0
  }
  if (node.type.name !== 'rawBlock') {
    return false
  }
  const blocks = parseResultMarkdown(String(node.attrs.source)).flatMap((page) => page.blocks)
  return !blocks.length || blocks[0]!.kind === 'comment'
}

/** The editor's units, in order. */
export function noteUnits(doc: ProseNode): NoteUnit[] {
  const nodes: { node: ProseNode; pos: number }[] = []
  doc.forEach((node, pos) => nodes.push({ node, pos }))
  const units: NoteUnit[] = []
  for (let index = 0; index < nodes.length; index += 1) {
    const { node, pos } = nodes[index]!
    const next = nodes[index + 1]
    if (node.type.name === 'blockquote' && next && isLinkLine(next.node)) {
      units.push({ from: pos, to: next.pos + next.node.nodeSize, starts: [pos, next.pos], excerpt: true, bare: false })
      index += 1
    } else {
      units.push({ from: pos, to: pos + node.nodeSize, starts: [pos], excerpt: false, bare: isBare(node) })
    }
  }
  return units
}

/** The unit holding a position — a position between two units is the later one's, the end of
 *  the document the last one's — or null with no units. */
export function unitIndexAt(units: readonly NoteUnit[], pos: number): number | null {
  if (!units.length) {
    return null
  }
  const index = units.findIndex((unit) => pos < unit.to)
  return index === -1 ? units.length - 1 : index
}

/** A unit's markdown as the file holds it: its blocks' own bytes, as `docToMarkdown` writes them. */
export function unitMarkdown(doc: ProseNode, unit: NoteUnit): string {
  return docToMarkdown({ type: 'doc', content: unit.starts.map((start) => doc.nodeAt(start)!.toJSON()) })
}

/** The unit as the viewer's parser reads it, for its kind and its name: an excerpt's quote, any
 *  other unit's one block; null for an empty line. */
export function unitBlock(doc: ProseNode, unit: NoteUnit): ResultBlock | null {
  return parseResultMarkdown(unitMarkdown(doc, unit)).flatMap((page) => page.blocks)[0] ?? null
}

// --- where the pointer is ---------------------------------------------------------------------

/** A unit's measured extent: its first block's top to its last block's bottom. */
export interface UnitBox {
  top: number
  bottom: number
}

/** The unit the pointer is level with: the last one whose top is above it, so in the gap under
 *  a unit that unit stays; null above the first or past the last one's bottom. */
export function unitAtY(boxes: readonly UnitBox[], y: number): number | null {
  let found: number | null = null
  boxes.forEach((box, index) => {
    if (box.top <= y) {
      found = index
    }
  })
  return found !== null && found === boxes.length - 1 && y > boxes[found]!.bottom ? null : found
}

/** The gap a drop lands in — 0 before the first unit, `boxes.length` after the last: before or
 *  after the unit under the pointer by its vertical middle. */
export function slotAtY(boxes: readonly UnitBox[], y: number): number {
  const index = boxes.findIndex((box) => y < (box.top + box.bottom) / 2)
  return index === -1 ? boxes.length : index
}

/** Where a gap's drop line is drawn: midway between the units either side of it, or `edge`
 *  beyond the first or the last unit. */
export function slotY(boxes: readonly UnitBox[], slot: number, edge: number): number {
  const before = boxes[slot - 1]
  const after = boxes[slot]
  if (before && after) {
    return (before.bottom + after.top) / 2
  }
  return before ? before.bottom + edge : after ? after.top - edge : 0
}

// --- moves ------------------------------------------------------------------------------------

/** The gap a unit lands in moved one unit up or down, or null at the first or the last. */
export function stepSlot(index: number, count: number, direction: 'up' | 'down'): number | null {
  if (direction === 'up') {
    return index > 0 ? index - 1 : null
  }
  return index + 1 < count ? index + 2 : null
}

/** Whether moving a unit to a gap changes nothing: the gaps either side of it are its place. */
export function movesNothing(index: number, slot: number): boolean {
  return slot === index || slot === index + 1
}

/**
 * Moves a unit to a gap within one transaction — deleted, then inserted where the gap now is —
 * and answers where it now starts. The caller checks `movesNothing` first.
 */
export function moveUnit(tr: Transaction, units: readonly NoteUnit[], index: number, slot: number): number {
  const unit = units[index]!
  const gap = slot < units.length ? units[slot]!.from : units[units.length - 1]!.to
  const content = tr.doc.slice(unit.from, unit.to).content
  tr.delete(unit.from, unit.to)
  const at = gap > unit.from ? gap - (unit.to - unit.from) : gap
  tr.insert(at, content)
  return at
}

/** Deletes a unit; the last one leaves an empty line, since an editor needs a textblock. */
export function deleteUnit(tr: Transaction, units: readonly NoteUnit[], index: number): void {
  const unit = units[index]!
  if (units.length === 1) {
    tr.replaceWith(unit.from, unit.to, tr.doc.type.schema.nodes.paragraph!.create())
  } else {
    tr.delete(unit.from, unit.to)
  }
}

/**
 * A unit selected whole, from its first block's start to its last block's end (docs/web.md
 * § Notes, *The keyboard*). The browser draws nothing for it — the editor draws the selection's
 * look — and a change that cuts into it, or a reload that replaces it, maps it to a caret. Not
 * registered for JSON: nothing here serialises a selection.
 */
export class UnitSelection extends Selection {
  static create(doc: ProseNode, unit: { from: number; to: number }): UnitSelection {
    return new UnitSelection(doc.resolve(unit.from), doc.resolve(unit.to))
  }

  map(doc: ProseNode, mapping: Mappable): Selection {
    const from = mapping.mapResult(this.from, 1)
    const to = mapping.mapResult(this.to, -1)
    const $from = doc.resolve(Math.min(from.pos, doc.content.size))
    const $to = doc.resolve(Math.min(to.pos, doc.content.size))
    if (!from.deleted && !to.deleted && $from.pos < $to.pos && $from.depth === 0 && $to.depth === 0) {
      return new UnitSelection($from, $to)
    }
    return Selection.near($from)
  }

  eq(other: Selection): boolean {
    return other instanceof UnitSelection && other.from === this.from && other.to === this.to
  }

  toJSON() {
    return { type: 'unit', anchor: this.from, head: this.to }
  }
}

// As a node selection or a table's cell selection: the browser's highlight is hidden.
UnitSelection.prototype.visible = false
