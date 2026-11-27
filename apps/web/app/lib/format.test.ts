import { describe, expect, it } from 'vitest'
import { formatBytes, formatDate, formatDuration, formatMinorAmount, formatTime, packValidity } from './format'

describe('formatBytes', () => {
  it('steps by 1000, as the unit names Intl prints do', () => {
    expect(formatBytes(512_000_000, 'en')).toBe('512 MB')
    expect(formatBytes(1e9, 'en')).toBe('1 GB')
    expect(formatBytes(20 * 1024 * 1024, 'en')).toBe('21 MB')
    expect(formatBytes(2_400_000, 'en')).toBe('2.4 MB')
    expect(formatBytes(10, 'en')).toBe('10 byte')
  })

  it('rounds a limit down at the precision shown, so no file within the label is over it', () => {
    expect(formatBytes(20_971_520, 'en', { limit: true })).toBe('20.9 MB')
    expect(formatBytes(20_971_520, 'zh-TW', { limit: true })).toBe('20.9 MB')
    expect(formatBytes(8_388_608, 'en', { limit: true })).toBe('8.3 MB')
    // A round limit reads as it is.
    expect(formatBytes(128_000_000, 'en', { limit: true })).toBe('128 MB')
    expect(formatBytes(2_000_000_000, 'en', { limit: true })).toBe('2 GB')
  })

  it('rounds a size to the nearest', () => {
    expect(formatBytes(20_971_520, 'zh-TW')).toBe('21 MB')
    expect(formatBytes(8_388_608, 'en')).toBe('8.4 MB')
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

describe('formatDuration', () => {
  it('reads minutes and seconds under an hour, cut to the second', () => {
    expect(formatDuration(0)).toBe('0:00')
    expect(formatDuration(59.9)).toBe('0:59')
    expect(formatDuration(768)).toBe('12:48')
    expect(formatDuration(3599.99)).toBe('59:59')
  })

  it('reads hours from one, with the minutes padded', () => {
    expect(formatDuration(3600)).toBe('1:00:00')
    expect(formatDuration(3723)).toBe('1:02:03')
    expect(formatDuration(36_000)).toBe('10:00:00')
  })

  it('has nothing for a length that is infinite, unknown or negative', () => {
    expect(formatDuration(Infinity)).toBeNull()
    expect(formatDuration(NaN)).toBeNull()
    expect(formatDuration(-1)).toBeNull()
  })
})
