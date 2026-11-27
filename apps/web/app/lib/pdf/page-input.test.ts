import { describe, expect, it } from 'vitest'
import { pageInputTarget, parsePageNumber } from './page-input'

describe('reading the page field', () => {
  it('reads digits, around spaces and in full width', () => {
    expect(parsePageNumber('12')).toBe(12)
    expect(parsePageNumber(' 7 ')).toBe(7)
    expect(parsePageNumber('００９')).toBe(9)
  })

  it('reads nothing from a blank or anything but digits', () => {
    for (const text of ['', '   ', 'abc', '3.5', '-2', '+4', '1e3', '2 3']) {
      expect(parsePageNumber(text)).toBeNull()
    }
  })
})

describe('committing the page field, by Enter or by leaving it', () => {
  it('jumps to a number', () => {
    expect(pageInputTarget('9', 3, 14)).toBe(9)
  })

  it('clamps a number out of range to the first or the last page', () => {
    expect(pageInputTarget('0', 3, 14)).toBe(1)
    expect(pageInputTarget('15', 3, 14)).toBe(14)
    expect(pageInputTarget('99999999999999999999', 3, 14)).toBe(14)
  })

  it('cancels on a blank or a non-number', () => {
    expect(pageInputTarget('', 3, 14)).toBeNull()
    expect(pageInputTarget('page', 3, 14)).toBeNull()
  })

  it('changes nothing for the current page, typed or clamped to', () => {
    expect(pageInputTarget('3', 3, 14)).toBeNull()
    expect(pageInputTarget('40', 14, 14)).toBeNull()
    expect(pageInputTarget('1', 1, 1)).toBeNull()
  })
})
