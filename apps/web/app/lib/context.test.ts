import { describe, expect, it } from 'vitest'
import {
  IDLE_RELOAD_GRACE_MS,
  IDLE_RELOAD_POLL_MS,
  cacheState,
  contextFill,
  contextLine,
  formatTokens,
  idleReloadDelay,
  ringLevel,
} from './context'

const NOW = Date.parse('2026-10-26T12:00:00Z')

describe('contextFill and ringLevel', () => {
  it('is the context over the window, empty while none is known, never past full', () => {
    expect(contextFill(null)).toBe(0)
    expect(contextFill({ context_tokens: null, context_window: 200_000 })).toBe(0)
    expect(contextFill({ context_tokens: 62_000, context_window: 200_000 })).toBeCloseTo(0.31)
    expect(contextFill({ context_tokens: 250_000, context_window: 200_000 })).toBe(1)
    expect(contextFill({ context_tokens: 10, context_window: 0 })).toBe(0)
  })

  it('turns from the accent to the warning hue past 70 % and to the danger hue past 90 %', () => {
    expect(ringLevel(0)).toBe('accent')
    expect(ringLevel(0.7)).toBe('accent')
    expect(ringLevel(0.71)).toBe('warning')
    expect(ringLevel(0.9)).toBe('warning')
    expect(ringLevel(0.91)).toBe('danger')
  })
})

describe('formatTokens and contextLine', () => {
  it('reads as a person counts tokens', () => {
    expect(formatTokens(950)).toBe('950')
    expect(formatTokens(1500)).toBe('1.5K')
    expect(formatTokens(62_400)).toBe('62K')
    expect(formatTokens(200_000)).toBe('200K')
    expect(formatTokens(1_250_000)).toBe('1.3M')
    expect(formatTokens(12_000_000)).toBe('12M')
  })

  it('is the tooltip, or nothing while no context is known', () => {
    expect(contextLine({ context_tokens: 62_000, context_window: 200_000 })).toBe('62K / 200K · 31 %')
    expect(contextLine({ context_tokens: null, context_window: 200_000 })).toBeNull()
    expect(contextLine(null)).toBeNull()
  })
})

describe('cacheState', () => {
  it('counts the minutes down while the cache is warm, rounding up', () => {
    const warm = { context_tokens: 1000, cache_warm_until: '2026-10-26T12:42:30Z', recache_credits: 3 }
    expect(cacheState(warm, NOW)).toEqual({ kind: 'warm', minutesLeft: 43 })
    expect(cacheState(warm, NOW + 42 * 60_000 + 29_000)).toEqual({ kind: 'warm', minutesLeft: 1 })
  })

  it('says what a cold next message costs once the clock has passed', () => {
    const passed = { context_tokens: 1000, cache_warm_until: '2026-10-26T11:59:59Z', recache_credits: 3 }
    expect(cacheState(passed, NOW)).toEqual({ kind: 'expired', recacheCredits: 3 })
    expect(cacheState({ ...passed, recache_credits: null }, NOW)).toEqual({ kind: 'expired', recacheCredits: null })
  })

  it('knows a model that caches nothing from a conversation with nothing to say yet', () => {
    expect(cacheState({ context_tokens: 1000, cache_warm_until: null, recache_credits: null }, NOW)).toEqual({ kind: 'none' })
    // Compacted: the size and the clock are unknown until the next call.
    expect(cacheState({ context_tokens: null, cache_warm_until: null, recache_credits: null }, NOW)).toBeNull()
    expect(cacheState(null, NOW)).toBeNull()
    expect(cacheState({ context_tokens: 5, cache_warm_until: 'not a date', recache_credits: null }, NOW)).toEqual({ kind: 'none' })
  })
})

describe('idleReloadDelay', () => {
  const lead = 360
  const warm = { cache_warm_until: '2026-10-26T12:42:00Z' }
  const until = Date.parse(warm.cache_warm_until)

  it('waits for the window the worker takes the conversation in to open', () => {
    expect(idleReloadDelay(warm, lead, NOW)).toBe(until - lead * 1000 - NOW + IDLE_RELOAD_GRACE_MS)
    // Just before the window: the grace alone.
    expect(idleReloadDelay(warm, lead, until - lead * 1000)).toBe(IDLE_RELOAD_GRACE_MS)
  })

  it('reads again every minute inside the window, until the clock has passed by one', () => {
    expect(idleReloadDelay(warm, lead, until - lead * 1000 + IDLE_RELOAD_GRACE_MS)).toBe(IDLE_RELOAD_POLL_MS)
    expect(idleReloadDelay(warm, lead, until - 1000)).toBe(IDLE_RELOAD_POLL_MS)
    expect(idleReloadDelay(warm, lead, until + IDLE_RELOAD_POLL_MS - 1)).toBe(IDLE_RELOAD_POLL_MS)
    expect(idleReloadDelay(warm, lead, until + IDLE_RELOAD_POLL_MS)).toBeNull()
  })

  it('has nothing to wait for without a clock', () => {
    expect(idleReloadDelay({ cache_warm_until: null }, lead, NOW)).toBeNull()
    expect(idleReloadDelay({ cache_warm_until: 'not a date' }, lead, NOW)).toBeNull()
    expect(idleReloadDelay(null, lead, NOW)).toBeNull()
  })

  it('reads as the clock passes when the server names no lead', () => {
    expect(idleReloadDelay(warm, 0, NOW)).toBe(until - NOW + IDLE_RELOAD_GRACE_MS)
  })
})
