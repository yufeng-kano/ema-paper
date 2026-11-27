import { describe, expect, it } from 'vitest'
import { supportsModernPdfjs } from './support'

const fn = () => undefined

/** A global with every built-in the modern build needs, as plain stand-ins. */
function modern(): Record<string, unknown> {
  return {
    Map: { prototype: { getOrInsertComputed: fn } },
    WeakMap: { prototype: { getOrInsertComputed: fn } },
    Math: { sumPrecise: fn },
    Promise: { try: fn },
    Uint8Array: { fromBase64: fn, prototype: { toHex: fn } },
    Iterator: { prototype: { toArray: fn } },
    Set: { prototype: { intersection: fn } },
    URL: { parse: fn },
    Response: { prototype: { bytes: fn } },
  }
}

describe('the modern build probe', () => {
  it('takes a browser with every built-in', () => {
    expect(supportsModernPdfjs(modern())).toBe(true)
  })

  it('keeps a browser that lacks any one of them to the legacy build', () => {
    const missing: [string, string][] = [
      ['Map', 'prototype'],
      ['Math', 'sumPrecise'],
      ['Promise', 'try'],
      ['Uint8Array', 'fromBase64'],
      ['Set', 'prototype'],
      ['URL', 'parse'],
      ['Response', 'prototype'],
    ]
    for (const [owner, name] of missing) {
      const scope = modern()
      ;(scope[owner] as Record<string, unknown>)[name] = undefined
      expect(supportsModernPdfjs(scope)).toBe(false)
    }
    const noIterator = modern()
    delete noIterator.Iterator
    expect(supportsModernPdfjs(noIterator)).toBe(false)
  })

  it('takes a built-in that is there but not a function as missing', () => {
    const scope = modern()
    ;(scope.Math as Record<string, unknown>).sumPrecise = 1
    expect(supportsModernPdfjs(scope)).toBe(false)
  })

  it('keeps a global whose getters throw to the legacy build', () => {
    const scope = Object.defineProperty(modern(), 'Map', {
      get() {
        throw new Error('locked')
      },
    })
    expect(supportsModernPdfjs(scope)).toBe(false)
    expect(supportsModernPdfjs(null)).toBe(false)
  })
})
