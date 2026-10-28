import { describe, expect, it } from 'vitest'
import en from '../../i18n/locales/en'
import zhTW from '../../i18n/locales/zh-TW'
import { PAYMENT_REFUSALS } from './billing'

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
