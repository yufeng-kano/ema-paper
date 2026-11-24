import { describe, expect, it } from 'vitest'
import { formatBytes, formatDate, formatMinorAmount, formatTime, packValidity } from './format'

describe('formatBytes', () => {
  it('steps by 1000, as the unit names Intl prints do', () => {
    expect(formatBytes(512_000_000, 'en')).toBe('512 MB')
    expect(formatBytes(1e9, 'en')).toBe('1 GB')
    expect(formatBytes(20 * 1024 * 1024, 'en')).toBe('21 MB')
    expect(formatBytes(2_400_000, 'en')).toBe('2.4 MB')
    expect(formatBytes(10, 'en')).toBe('10 byte')
  })
})

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

describe('formatTime', () => {
  it('is the time of day alone, in the locale, from a date or a timestamp', () => {
    const at = new Date(2026, 10, 1, 14, 5)
    expect(formatTime(at, 'en')).toMatch(/^2:05\sPM$/)
    expect(formatTime(at.getTime(), 'en')).toBe(formatTime(at, 'en'))
  })
})

describe('packValidity', () => {
  it('reads a whole number of months as months, and anything else as days', () => {
    expect(packValidity(365)).toEqual({ unit: 'months', n: 12 })
    expect(packValidity(30)).toEqual({ unit: 'months', n: 1 })
    expect(packValidity(90)).toEqual({ unit: 'months', n: 3 })
    expect(packValidity(45)).toEqual({ unit: 'days', n: 45 })
    expect(packValidity(7)).toEqual({ unit: 'days', n: 7 })
  })
})
