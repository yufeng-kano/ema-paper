import { describe, expect, it } from 'vitest'
import type { AgentContext, AgentContextPart } from './api'
import { FACT_NAMES, escapeClosesDrawer, receivesRows } from './receives'

const PROMPT: AgentContextPart = { kind: 'prompt', text: 'You are the reader’s agent.', chars: 27, own: false }
const INSTRUCTIONS: AgentContextPart = { kind: 'instructions', text: '## How to answer', chars: 16 }
const CATALOGUE: AgentContextPart = { kind: 'catalogue', text: '# Skills\n- fix-math', chars: 19, skills: 1 }
const MEMORY: AgentContextPart = { kind: 'memory', text: '# Memory', chars: 8, count: 2, paused: false }

function context(parts: AgentContextPart[]): AgentContext {
  return { parts, turn: { text: '- reader locale: en', chars: 19 } }
}

describe('receivesRows', () => {
  it('lists the parts as sent, then the turn block, then the facts', () => {
    const rows = receivesRows(context([PROMPT, INSTRUCTIONS, CATALOGUE, MEMORY]))

    expect(rows.map((row) => (row.kind === 'part' ? row.part.kind : row.kind))).toEqual([
      'prompt',
      'instructions',
      'catalogue',
      'memory',
      'turn',
      'facts',
    ])
  })

  it('lists the tokens, and sizes them, as no document is open', () => {
    const row = receivesRows(context([PROMPT])).at(-1)

    expect(row?.kind === 'facts' && row.facts.map((fact) => fact.value)).toEqual(
      FACT_NAMES.map((name) => `{${name}}`),
    )
    expect(row?.chars).toBe(FACT_NAMES.reduce((sum, name) => sum + name.length + 2, 0))
  })

  it('keeps a row’s key when a part is added before it, so an opened row stays open', () => {
    const before = receivesRows(context([PROMPT, CATALOGUE, MEMORY]))
    const after = receivesRows(context([PROMPT, INSTRUCTIONS, CATALOGUE, MEMORY]))
    const keyOf = (rows: typeof before, kind: string) =>
      rows.find((row) => row.kind === 'part' && row.part.kind === kind)?.key

    expect(keyOf(after, 'catalogue')).toBe(keyOf(before, 'catalogue'))
    expect(keyOf(after, 'memory')).toBe(keyOf(before, 'memory'))
  })

  it('never gives two rows the same key, even for two parts of one kind', () => {
    const keys = receivesRows(context([PROMPT, { ...PROMPT, own: true }, MEMORY])).map((row) => row.key)

    expect(new Set(keys).size).toBe(keys.length)
  })
})

describe('escapeClosesDrawer', () => {
  const escape = { key: 'Escape', defaultPrevented: false, isComposing: false }

  it('closes on an Escape pressed anywhere on the page', () => {
    expect(escapeClosesDrawer(escape, false)).toBe(true)
  })

  it('leaves an Escape a control already took, or one that ends a composition', () => {
    expect(escapeClosesDrawer({ ...escape, defaultPrevented: true }, false)).toBe(false)
    expect(escapeClosesDrawer({ ...escape, isComposing: true }, false)).toBe(false)
  })

  it('leaves the Escape to an open dialog or menu', () => {
    expect(escapeClosesDrawer(escape, true)).toBe(false)
  })

  it('ignores every other key', () => {
    expect(escapeClosesDrawer({ ...escape, key: 'Enter' }, false)).toBe(false)
  })
})
