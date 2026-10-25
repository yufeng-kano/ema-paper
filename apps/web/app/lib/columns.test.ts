import { describe, expect, it } from 'vitest'
import { COLUMN_WIDTHS, WIDTH_STEPS, clampWidth, collapses, storedWidth, widthStep } from './columns'

describe('column widths', () => {
  it('keeps every step inside its card’s clamp, Standard being the default', () => {
    for (const column of ['notes', 'agent'] as const) {
      const { initial, min, max, steps } = COLUMN_WIDTHS[column]
      expect(steps.standard).toBe(initial)
      for (const step of WIDTH_STEPS) {
        expect(clampWidth(column, steps[step])).toBe(steps[step])
        expect(steps[step]).toBeGreaterThanOrEqual(min)
        expect(steps[step]).toBeLessThanOrEqual(max)
      }
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

  it('names the step a chosen width is, and none for a dragged one', () => {
    expect(widthStep('notes', 280)).toBe('narrow')
    expect(widthStep('notes', 440)).toBe('wide')
    expect(widthStep('agent', 300)).toBe('narrow')
    expect(widthStep('agent', 360)).toBe('standard')
    expect(widthStep('agent', 320)).toBeNull()
    expect(widthStep('notes', 336)).toBeNull()
  })
})
