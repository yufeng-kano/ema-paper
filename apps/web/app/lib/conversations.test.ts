import { describe, expect, it } from 'vitest'
import type { Conversation } from './api'
import { byRecency, renamedTitle } from './conversations'

function row(id: number, updated: string): Conversation {
  return { id, title: `#${id}`, created_at: '2026-10-01T00:00:00Z', updated_at: updated }
}

describe('byRecency', () => {
  it('lists the thread continued last first, without touching the input', () => {
    const rows = [row(1, '2026-10-20T08:00:00Z'), row(2, '2026-10-25T09:30:00Z'), row(3, '2026-10-22T12:00:00Z')]

    expect(byRecency(rows).map((entry) => entry.id)).toEqual([2, 3, 1])
    expect(rows.map((entry) => entry.id)).toEqual([1, 2, 3])
  })
})

describe('renamedTitle', () => {
  it('makes one line of what was typed', () => {
    expect(renamedTitle('  Table 2,\n\tthe   baseline ', 'Old')).toBe('Table 2, the baseline')
  })

  it('cuts at the cap without leaving a trailing space', () => {
    expect(renamedTitle('abcd efgh', null, 5)).toBe('abcd')
    expect(renamedTitle('x'.repeat(100), null, 80)).toBe('x'.repeat(80))
  })

  it('changes nothing for a blank field or the same title', () => {
    expect(renamedTitle('', 'Old')).toBeNull()
    expect(renamedTitle(' \n\t ', 'Old')).toBeNull()
    expect(renamedTitle(' Old ', 'Old')).toBeNull()
  })

  it('names an untitled conversation', () => {
    expect(renamedTitle('Notes', null)).toBe('Notes')
    expect(renamedTitle('Notes', '')).toBe('Notes')
  })
})
