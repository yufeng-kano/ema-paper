import { describe, expect, it } from 'vitest'
import { COLUMN_WIDTHS, clampWidth, collapses, shownCards, storedWidth, viewerLayout } from './columns'

describe('column widths', () => {
  it('keeps every default inside its card’s clamp', () => {
    for (const column of ['notes', 'agent'] as const) {
      const { initial, min, max } = COLUMN_WIDTHS[column]
      expect(clampWidth(column, initial)).toBe(initial)
      expect(initial).toBeGreaterThanOrEqual(min)
      expect(initial).toBeLessThanOrEqual(max)
    }
  })

  it('clamps to the card’s bounds in whole pixels', () => {
    expect(clampWidth('notes', 100)).toBe(240)
    expect(clampWidth('notes', 900)).toBe(560)
    expect(clampWidth('agent', 412.6)).toBe(413)
  })

  it('reads a stored width back clamped, and anything else as the default', () => {
    expect(storedWidth('notes', 600)).toBe(560)
    expect(storedWidth('agent', 420)).toBe(420)
    expect(storedWidth('notes', '400')).toBe(320)
    expect(storedWidth('agent', Number.NaN)).toBe(360)
    expect(storedWidth('agent', undefined)).toBe(360)
  })

  it('hides a card dragged below half its minimum, and not at the line itself', () => {
    expect(collapses('notes', 119)).toBe(true)
    expect(collapses('notes', 120)).toBe(false)
    expect(collapses('agent', 149)).toBe(true)
    expect(collapses('agent', 150)).toBe(false)
    expect(collapses('agent', -40)).toBe(true)
  })
})

describe('viewer layout', () => {
  it('is a phone below 700px, a tablet up to 900px, a desktop above', () => {
    expect(viewerLayout({ width: 402, height: 874 }, true)).toBe('phone')
    expect(viewerLayout({ width: 699, height: 900 }, false)).toBe('phone')
    expect(viewerLayout({ width: 700, height: 900 }, false)).toBe('tablet')
    expect(viewerLayout({ width: 820, height: 1180 }, true)).toBe('tablet')
    expect(viewerLayout({ width: 900, height: 1000 }, false)).toBe('tablet')
    expect(viewerLayout({ width: 901, height: 1000 }, false)).toBe('desktop')
    expect(viewerLayout({ width: 1180, height: 820 }, true)).toBe('desktop')
  })

  it('takes a short touch screen for a phone on its side, never a short window with a mouse', () => {
    expect(viewerLayout({ width: 874, height: 402 }, true)).toBe('phone')
    expect(viewerLayout({ width: 932, height: 430 }, true)).toBe('phone')
    expect(viewerLayout({ width: 1280, height: 480 }, false)).toBe('desktop')
    expect(viewerLayout({ width: 1024, height: 501 }, true)).toBe('desktop')
  })
})

describe('shown cards', () => {
  const both = { notes: true, agent: true }

  it('shows every open card on the desktop', () => {
    expect(shownCards('desktop', both, 'notes')).toEqual(both)
    expect(shownCards('desktop', { notes: false, agent: true }, 'notes')).toEqual({ notes: false, agent: true })
  })

  it('shows only the card in front on a tablet when both are open', () => {
    expect(shownCards('tablet', both, 'notes')).toEqual({ notes: true, agent: false })
    expect(shownCards('tablet', both, 'agent')).toEqual({ notes: false, agent: true })
  })

  it('shows a lone open card on a tablet whichever is in front', () => {
    expect(shownCards('tablet', { notes: true, agent: false }, 'agent')).toEqual({ notes: true, agent: false })
    expect(shownCards('tablet', { notes: false, agent: false }, 'agent')).toEqual({ notes: false, agent: false })
  })

  it('shows no card on a phone, whose cards are panes', () => {
    expect(shownCards('phone', both, 'agent')).toEqual({ notes: false, agent: false })
  })
})
