import { describe, expect, it } from 'vitest'
import { formatDate, formatMinorAmount } from './format'

describe('formatMinorAmount', () => {
  it('reads Pay’s amount in the currency’s minor unit', () => {
    expect(formatMinorAmount('1500', 'USD', 'en')).toBe('$15.00')
    expect(formatMinorAmount('800', 'EUR', 'en')).toBe('€8.00')
    // A currency with no minor unit is not divided.
    expect(formatMinorAmount('1500', 'JPY', 'en')).toBe('¥1,500')
  })
})

describe('formatDate', () => {
  it('is the day alone, in the locale', () => {
    expect(formatDate('2026-11-01T12:00:00Z', 'en')).toMatch(/Nov 1, 2026/)
  })
})
