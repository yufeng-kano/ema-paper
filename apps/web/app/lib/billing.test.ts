import { describe, expect, it } from 'vitest'
import en from '../../i18n/locales/en'
import zhTW from '../../i18n/locales/zh-TW'
import { CYCLE_INTERVALS, PAYMENT_REFUSALS } from './billing'

describe('the payment refusals', () => {
  const lookup = (catalog: unknown, path: string): unknown =>
    path.split('.').reduce<unknown>((node, key) => (node as Record<string, unknown> | undefined)?.[key], catalog)

  it('are worded in both catalogs', () => {
    for (const catalog of [en, zhTW]) {
      for (const key of Object.values(PAYMENT_REFUSALS)) {
        expect(typeof lookup(catalog, key ?? '')).toBe('string')
      }
    }
  })

  it('never word a refused portal as a closed checkout', () => {
    expect(PAYMENT_REFUSALS.portal_unavailable).not.toBe(PAYMENT_REFUSALS.checkout_unavailable)
  })
})

describe('the billing cycle', () => {
  const cycles = (catalog: { pricing: { cycle: Record<string, string> } }) => catalog.pricing.cycle

  it('is worded in both catalogs for every interval, one cycle and several', () => {
    for (const catalog of [en, zhTW]) {
      for (const interval of CYCLE_INTERVALS) {
        const forms = cycles(catalog)[interval]?.split(' | ') ?? []
        expect(forms).toHaveLength(2)
        expect(forms[0]).not.toContain('{n}')
        expect(forms[1]).toContain('{n}')
      }
    }
  })

  it('reads in Chinese in zh-TW, never with an English interval', () => {
    for (const interval of CYCLE_INTERVALS) {
      expect(cycles(zhTW)[interval]?.replaceAll('{n}', '')).not.toMatch(/[A-Za-z]/)
    }
  })
})
