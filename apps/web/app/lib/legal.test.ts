import { describe, expect, it } from 'vitest'
import en from '../../i18n/locales/en'
import zhTW from '../../i18n/locales/zh-TW'
import { LEGAL_DOCS, LEGAL_UPDATED, legalSections } from './legal'

describe('the policy pages', () => {
  it('have the same numbered sections in both catalogs, each with a title and a body', () => {
    for (const doc of LEGAL_DOCS) {
      const ours = legalSections(en.legal[doc])
      expect(ours.length).toBeGreaterThan(0)
      expect(legalSections(zhTW.legal[doc])).toEqual(ours)
      // A section past the last contiguous number would never be shown.
      const highest = Math.max(...Object.keys(en.legal[doc]).map((key) => Number(/^s(\d+)Title$/.exec(key)?.[1] ?? 0)))
      expect(highest).toBe(ours.length)
    }
  })

  it('carry a date for every document', () => {
    for (const doc of LEGAL_DOCS) {
      expect(LEGAL_UPDATED[doc]).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    }
  })
})
