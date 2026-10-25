import { getSchema } from '@tiptap/core'
import { closeHistory, history, undo } from '@tiptap/pm/history'
import { EditorState, TextSelection } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'
import {
  UnitSelection,
  deleteUnit,
  moveUnit,
  movesNothing,
  noteUnits,
  slotAtY,
  slotY,
  stepSlot,
  unitAtY,
  unitBlock,
  unitIndexAt,
  unitMarkdown,
} from './note-blocks'
import { markExcerptLinks, richExtensions } from './rich-extensions'
import { docToMarkdown, markdownToDoc, sliceToMarkdown } from './richdoc'

const schema = getSchema(richExtensions({ links: { state: () => 'chip', locate: () => {} }, blocks: true }))

/** The notes as their editor holds them, with the editor's own history. */
function notes(markdown: string): EditorState {
  return EditorState.create({ schema, doc: schema.nodeFromJSON(markdownToDoc(markdown, { cites: true })), plugins: [history()] })
}

function written(state: EditorState): string {
  return docToMarkdown(state.doc.toJSON())
}

const EXCERPT = '> We suspect that the dot products grow large.\n\n[3.2.1 Scaled Dot-Product Attention](#321-scaled-dot-product-attention)'

describe('the units', () => {
  it('are the top-level blocks, a list whole', () => {
    const units = noteUnits(notes('## Ideas\n\nFirst thought.\n\n- one\n- two\n\n$$\nx = 1\n$$').doc)
    expect(units.map((unit) => unit.starts.length)).toEqual([1, 1, 1, 1])
    expect(units.every((unit) => !unit.excerpt && !unit.bare)).toBe(true)
  })

  it('take an excerpt — a quote, then a paragraph that is only a section link — as one', () => {
    const state = notes(`Intro\n\n${EXCERPT}\n\nAfter.`)
    const units = noteUnits(state.doc)
    expect(units).toHaveLength(3)
    expect(units[1]!.excerpt).toBe(true)
    expect(units[1]!.starts).toHaveLength(2)
    expect(unitMarkdown(state.doc, units[1]!)).toBe(EXCERPT)
    expect(unitBlock(state.doc, units[1]!)?.kind).toBe('quote')
  })

  it('keep a quote apart from a link line with words of its own, or from a paragraph after it', () => {
    const withWords = noteUnits(notes('> passage\n\nSee [3.2 Attention](#32-attention) too.').doc)
    expect(withWords.map((unit) => unit.excerpt)).toEqual([false, false])
    const plain = noteUnits(notes('> passage\n\nJust words.').doc)
    expect(plain.map((unit) => unit.excerpt)).toEqual([false, false])
  })

  it('take only the quote right before the link line into the excerpt', () => {
    const units = noteUnits(notes(`> first\n\n${EXCERPT}`).doc)
    expect(units.map((unit) => unit.excerpt)).toEqual([false, true])
  })

  it('give an empty line and a comment nothing to carry', () => {
    const state = notes('Text\n\n<!-- kept -->\n\nMore')
    expect(noteUnits(state.doc).map((unit) => unit.bare)).toEqual([false, true, false])
    expect(noteUnits(notes('').doc).map((unit) => unit.bare)).toEqual([true])
  })

  it('hold a position: between two units the later one, past the end the last', () => {
    const state = notes('One\n\nTwo')
    const units = noteUnits(state.doc)
    expect(unitIndexAt(units, units[0]!.from + 1)).toBe(0)
    expect(unitIndexAt(units, units[1]!.from)).toBe(1)
    expect(unitIndexAt(units, state.doc.content.size)).toBe(1)
    expect(unitIndexAt([], 0)).toBeNull()
  })
})

describe("an excerpt's link line", () => {
  it('is marked right after its quote, and nowhere else', () => {
    const state = notes(`> first\n\n${EXCERPT}\n\n> passage\n\nSee [3.2 Attention](#32-attention) too.\n\n[3.2 Attention](#32-attention)`)
    const excerpts = noteUnits(state.doc).filter((unit) => unit.excerpt)
    expect(excerpts).toHaveLength(1)
    const marked = markExcerptLinks(state.doc).find()
    expect(marked.map((mark) => mark.from)).toEqual([excerpts[0]!.starts[1]])
  })
})

describe('where the pointer is', () => {
  const boxes = [
    { top: 0, bottom: 20 },
    { top: 32, bottom: 72 },
    { top: 84, bottom: 100 },
  ]

  it('is level with the last unit whose top is above it, the gap under a unit keeping it', () => {
    expect(unitAtY(boxes, 10)).toBe(0)
    expect(unitAtY(boxes, 26)).toBe(0)
    expect(unitAtY(boxes, 32)).toBe(1)
    expect(unitAtY(boxes, 100)).toBe(2)
  })

  it('is level with none above the first unit or past the last', () => {
    expect(unitAtY(boxes, -1)).toBeNull()
    expect(unitAtY(boxes, 101)).toBeNull()
  })

  it('drops before or after the unit under it by its vertical middle', () => {
    expect(slotAtY(boxes, 5)).toBe(0)
    expect(slotAtY(boxes, 15)).toBe(1)
    expect(slotAtY(boxes, 51)).toBe(1)
    expect(slotAtY(boxes, 53)).toBe(2)
    expect(slotAtY(boxes, 500)).toBe(3)
  })

  it('draws a gap midway between two units, and past the ends by the edge', () => {
    expect(slotY(boxes, 1, 4)).toBe(26)
    expect(slotY(boxes, 0, 4)).toBe(-4)
    expect(slotY(boxes, 3, 4)).toBe(104)
  })
})

describe('a move', () => {
  it('steps one unit, and not past the first or the last', () => {
    expect(stepSlot(1, 3, 'up')).toBe(0)
    expect(stepSlot(1, 3, 'down')).toBe(3)
    expect(stepSlot(0, 3, 'up')).toBeNull()
    expect(stepSlot(2, 3, 'down')).toBeNull()
  })

  it('to either gap beside the unit moves nothing', () => {
    expect(movesNothing(1, 1)).toBe(true)
    expect(movesNothing(1, 2)).toBe(true)
    expect(movesNothing(1, 0)).toBe(false)
    expect(movesNothing(1, 3)).toBe(false)
  })

  it('carries an excerpt whole, every block keeping its bytes', () => {
    const state = notes(`Intro\n\n${EXCERPT}\n\n- *kept* as written`)
    const units = noteUnits(state.doc)
    const tr = state.tr
    const at = moveUnit(tr, units, 1, 3)
    const moved = state.apply(tr)
    expect(written(moved)).toBe(`Intro\n\n- *kept* as written\n\n${EXCERPT}`)
    expect(unitIndexAt(noteUnits(moved.doc), at)).toBe(2)
    expect(noteUnits(moved.doc)[2]!.excerpt).toBe(true)
  })

  it('goes up to the top', () => {
    const state = notes('One\n\nTwo\n\nThree')
    const tr = state.tr
    expect(moveUnit(tr, noteUnits(state.doc), 2, 0)).toBe(0)
    expect(written(state.apply(tr))).toBe('Three\n\nOne\n\nTwo')
  })

  it('is one step of the editor history: one undo puts it back', () => {
    const before = notes('One\n\nTwo\n\nThree')
    const tr = closeHistory(before.tr)
    moveUnit(tr, noteUnits(before.doc), 0, 2)
    let state = before.apply(tr)
    expect(written(state)).toBe('Two\n\nOne\n\nThree')
    undo(state, (next) => (state = state.apply(next)))
    expect(written(state)).toBe('One\n\nTwo\n\nThree')
  })
})

describe('a delete', () => {
  it('takes an excerpt whole', () => {
    const state = notes(`Intro\n\n${EXCERPT}\n\nAfter.`)
    const tr = state.tr
    deleteUnit(tr, noteUnits(state.doc), 1)
    expect(written(state.apply(tr))).toBe('Intro\n\nAfter.')
  })

  it('of the last unit leaves an empty line for the caret', () => {
    const state = notes(EXCERPT)
    const tr = state.tr
    deleteUnit(tr, noteUnits(state.doc), 0)
    const after = state.apply(tr)
    expect(written(after)).toBe('')
    expect(after.doc.childCount).toBe(1)
    expect(noteUnits(after.doc)[0]!.bare).toBe(true)
  })
})

describe('a unit selected whole', () => {
  it('covers its blocks and copies as their markdown, the browser drawing nothing', () => {
    const state = notes(`Intro\n\n${EXCERPT}`)
    const unit = noteUnits(state.doc)[1]!
    const selection = UnitSelection.create(state.doc, unit)
    expect([selection.from, selection.to]).toEqual([unit.from, unit.to])
    expect(selection.visible).toBe(false)
    expect(sliceToMarkdown(selection.content().content.toJSON())).toBe(EXCERPT)
  })

  it('follows its unit when text changes before it', () => {
    const state = notes('One\n\nTwo')
    const unit = noteUnits(state.doc)[1]!
    const selected = state.apply(state.tr.setSelection(UnitSelection.create(state.doc, unit)))
    const typed = selected.apply(selected.tr.insertText('Zero ', 1))
    expect(typed.selection).toBeInstanceOf(UnitSelection)
    expect(typed.doc.textBetween(typed.selection.from, typed.selection.to)).toBe('Two')
  })

  it('becomes a caret once its unit is gone', () => {
    const state = notes('One\n\nTwo')
    const unit = noteUnits(state.doc)[1]!
    const selected = state.apply(state.tr.setSelection(UnitSelection.create(state.doc, unit)))
    const removed = selected.apply(selected.tr.delete(unit.from, unit.to))
    expect(removed.selection).toBeInstanceOf(TextSelection)
  })
})
