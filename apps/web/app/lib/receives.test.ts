import { describe, expect, it } from 'vitest'
import type { AgentContext, AgentContextFacts, AgentContextPart } from './api'
import { FACT_NAMES, escapeClosesDrawer, factValue, receivesRows } from './receives'

const PROMPT: AgentContextPart = { kind: 'prompt', text: 'You are the reader’s agent.', chars: 27, own: false }
const INSTRUCTIONS: AgentContextPart = { kind: 'instructions', text: '## How to answer', chars: 16 }
const CATALOGUE: AgentContextPart = { kind: 'catalogue', text: '# Skills\n- fix-math', chars: 19, skills: 1, subagents: 1 }
const MEMORY: AgentContextPart = { kind: 'memory', text: '# Memory', chars: 8, count: 2, paused: false }

function context(parts: AgentContextPart[], facts: AgentContextFacts | null = null): AgentContext {
  return { parts, turn: { text: '- reader locale: en', chars: 19 }, facts }
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

  it('gives every row a size, the facts included', () => {
    for (const row of receivesRows(context([PROMPT, MEMORY]))) {
      expect(row.chars).toBeGreaterThan(0)
    }
  })

  it('sizes the facts as the values it lists, in code points as the server counts', () => {
    const facts: AgentContextFacts = {
      document_path: '論文/𝑑-model.md',
      page_count: '15',
      bbox_format: 'yxyx_norm1000',
      parse_model: 'gemini',
      network: 'off',
    }
    const row = receivesRows(context([PROMPT], facts)).at(-1)

    expect(row?.kind === 'facts' && row.facts.map((fact) => fact.value)).toEqual(Object.values(facts))
    // '𝑑' is one code point but two UTF-16 units: 13 + 2 + 13 + 6 + 3.
    expect(row?.chars).toBe(37)
  })

  it('lists the tokens, and sizes them, where no document is open', () => {
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

describe('factValue', () => {
  it('is the document’s value when one is open, else the token the prompt keeps', () => {
    const facts: AgentContextFacts = {
      document_path: 'paper.md',
      page_count: '9',
      bbox_format: 'xyxy',
      parse_model: 'claude',
      network: 'on',
    }

    expect(factValue(facts, 'page_count')).toBe('9')
    expect(factValue(null, 'page_count')).toBe('{page_count}')
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
