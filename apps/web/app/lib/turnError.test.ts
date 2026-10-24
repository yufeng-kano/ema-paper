import { describe, expect, it } from 'vitest'
import { turnErrorKey, turnErrorNeedsPlan } from './turnError'

describe('turnErrorKey', () => {
  it('words the errors the worker writes', () => {
    expect(turnErrorKey('storage_full')).toBe('chat.errors.storageFull')
    expect(turnErrorKey('the turn timed out')).toBe('chat.errors.timedOut')
    expect(turnErrorKey('The turn made more than 40 tool calls')).toBe('chat.errors.tooManySteps')
    expect(
      turnErrorKey(
        "This conversation's history is too large to continue; start a new conversation about this document",
      ),
    ).toBe('chat.errors.historyFull')
  })

  it('never shows an unknown error bare', () => {
    expect(turnErrorKey('upstream 500: Internal Server Error')).toBe('chat.errors.generic')
    expect(turnErrorKey('')).toBe('chat.errors.generic')
  })
})

describe('turnErrorNeedsPlan', () => {
  it('offers the plan only where the plan lifts the limit', () => {
    expect(turnErrorNeedsPlan('out_of_credits')).toBe(true)
    expect(turnErrorNeedsPlan('storage_full')).toBe(true)
    expect(turnErrorNeedsPlan('the turn timed out')).toBe(false)
  })
})
