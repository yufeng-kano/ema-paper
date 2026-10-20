import { describe, expect, it } from 'vitest'
import type { Memory } from './api'
import {
  MEMORY_FILTERS,
  countMemories,
  filterMemories,
  formatMemoryDate,
  memoryExcerpt,
  memoryMatches,
  sortMemories,
} from './memory'

function memory(id: number, fields: Partial<Memory> = {}): Memory {
  return {
    id,
    kind: 'term',
    text: `memory ${id}`,
    document_id: null,
    document_name: null,
    pinned: false,
    conversation_id: null,
    conversation_title: null,
    source_ref: null,
    created_at: '2026-10-01T09:00:00Z',
    updated_at: '2026-10-01T09:00:00Z',
    ...fields,
  }
}

describe('sortMemories', () => {
  it('orders by kind — terms, preferences, about, documents — whatever the input order', () => {
    const list = [
      memory(1, { kind: 'document' }),
      memory(2, { kind: 'about' }),
      memory(3, { kind: 'preference' }),
      memory(4, { kind: 'term' }),
    ]

    expect(sortMemories(list).map((row) => row.kind)).toEqual(['term', 'preference', 'about', 'document'])
  })

  it('puts pinned before unpinned within a kind, and the newest change first after that', () => {
    const list = [
      memory(1, { updated_at: '2026-10-10T00:00:00Z' }),
      memory(2, { updated_at: '2026-10-01T00:00:00Z', pinned: true }),
      memory(3, { updated_at: '2026-10-12T00:00:00Z' }),
      memory(4, { updated_at: '2026-10-05T00:00:00Z', pinned: true }),
    ]

    expect(sortMemories(list).map((row) => row.id)).toEqual([4, 2, 3, 1])
  })

  it('never lets a pinned memory jump ahead of an earlier kind', () => {
    const list = [memory(1, { kind: 'preference', pinned: true }), memory(2, { kind: 'term' })]

    expect(sortMemories(list).map((row) => row.id)).toEqual([2, 1])
  })

  it('breaks a tie on the same stamp by the higher id, and leaves its input alone', () => {
    const list = [memory(1), memory(2)]

    expect(sortMemories(list).map((row) => row.id)).toEqual([2, 1])
    expect(list.map((row) => row.id)).toEqual([1, 2])
  })
})

describe('filterMemories', () => {
  const list = [
    memory(1, { kind: 'term', text: 'Query → 查詢' }),
    memory(2, { kind: 'preference', text: 'Conclusion first' }),
    memory(3, { kind: 'term', text: 'softmax stays in English' }),
  ]

  it('keeps one kind under its chip and everything under All', () => {
    expect(filterMemories(list, 'term', '').map((row) => row.id)).toEqual([1, 3])
    expect(filterMemories(list, 'about', '')).toEqual([])
    expect(filterMemories(list, 'all', '').map((row) => row.id)).toEqual([1, 2, 3])
  })

  it('searches the text case-insensitively, inside the chosen kind', () => {
    expect(filterMemories(list, 'all', 'QUERY').map((row) => row.id)).toEqual([1])
    expect(filterMemories(list, 'preference', 'query')).toEqual([])
    expect(filterMemories(list, 'all', '查詢').map((row) => row.id)).toEqual([1])
  })

  it('treats a blank search as no search', () => {
    expect(memoryMatches(list[0]!, '   ')).toBe(true)
    expect(filterMemories(list, 'term', ' ').map((row) => row.id)).toEqual([1, 3])
  })
})

describe('countMemories', () => {
  it('counts every chip, All included, with a zero for a kind that has none', () => {
    const counts = countMemories([
      memory(1, { kind: 'term' }),
      memory(2, { kind: 'term' }),
      memory(3, { kind: 'document' }),
    ])

    expect(counts).toEqual({ all: 3, term: 2, preference: 0, about: 0, document: 1 })
    expect(Object.keys(counts).sort()).toEqual([...MEMORY_FILTERS].sort())
  })
})

describe('memoryExcerpt', () => {
  it('keeps a short text whole and cuts a long one at a code point', () => {
    expect(memoryExcerpt('  query → 查詢 ')).toBe('query → 查詢')
    expect(memoryExcerpt('abcdef', 3)).toBe('abc…')
    expect(memoryExcerpt('😀😀😀', 2)).toBe('😀😀…')
  })
})

describe('formatMemoryDate', () => {
  const now = new Date(2026, 9, 20, 15, 0)

  it('says nothing of its own for today, which the page words', () => {
    expect(formatMemoryDate(new Date(2026, 9, 20, 0, 5).toISOString(), 'en', now)).toBeNull()
  })

  it('gives month and day this year, and adds the year for an older one', () => {
    expect(formatMemoryDate(new Date(2026, 9, 12, 8).toISOString(), 'en', now)).toBe('Oct 12')
    expect(formatMemoryDate(new Date(2025, 9, 12, 8).toISOString(), 'en', now)).toBe('Oct 12, 2025')
  })

  it('answers an empty string for a stamp that does not parse', () => {
    expect(formatMemoryDate('not a date', 'en', now)).toBe('')
  })
})
