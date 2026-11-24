import { describe, expect, it } from 'vitest'
import en from '../../i18n/locales/en'
import zhTW from '../../i18n/locales/zh-TW'

/** Every leaf under a catalog, by its dotted path. */
function leaves(node: unknown, prefix = ''): [string, unknown][] {
  return node && typeof node === 'object'
    ? Object.entries(node).flatMap(([key, value]) => leaves(value, prefix ? `${prefix}.${key}` : key))
    : [[prefix, node]]
}

/** The named placeholders a message interpolates, each once, sorted — every plural form's. */
function placeholders(message: string): string[] {
  return [...new Set(message.match(/\{\w+\}/g))].sort()
}

const english = new Map(leaves(en))
const chinese = new Map(leaves(zhTW))

describe('the catalogs', () => {
  it('hold a string at every leaf', () => {
    for (const [path, value] of [...english, ...chinese]) {
      expect(typeof value, path).toBe('string')
    }
  })

  it('have the same keys in English and Traditional Chinese', () => {
    expect([...chinese.keys()].sort()).toEqual([...english.keys()].sort())
  })

  it('interpolate the same placeholders in both languages', () => {
    for (const [path, message] of english) {
      expect(placeholders(String(chinese.get(path))), path).toEqual(placeholders(String(message)))
    }
  })

  it('write no Simplified-only character in the Traditional Chinese copy', () => {
    for (const [path, message] of chinese) {
      // Simplified forms of characters the copy uses: 后台组权限录删设户证 …
      expect(String(message), path).not.toMatch(/[后组权录删设户证们这个时为对应开关]/)
    }
  })
})
