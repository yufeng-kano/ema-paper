import { describe, expect, it } from 'vitest'
import { PAGE_LOCALES, PUBLIC_PATHS, robotsTxt, sitemapXml } from './seo'

const ORIGIN = 'https://paper.example.com'

const entries = (xml: string) =>
  [...xml.matchAll(/<url>([\s\S]*?)<\/url>/g)].map(([, body]) => ({
    loc: /<loc>(.*?)<\/loc>/.exec(body!)![1]!,
    alternates: Object.fromEntries(
      [...body!.matchAll(/hreflang="([^"]+)" href="([^"]+)"/g)].map(([, hreflang, href]) => [hreflang, href]),
    ),
  }))

const disallowed = (text: string) =>
  text
    .split('\n')
    .filter((line) => line.startsWith('Disallow: '))
    .map((line) => line.slice('Disallow: '.length))

const underALocale = (path: string) =>
  PAGE_LOCALES.some(({ prefix }) => path === prefix || path.startsWith(`${prefix}/`))

describe('the public paths', () => {
  it('every one carries its locale: the bare root redirects by language and is no page', () => {
    for (const path of PUBLIC_PATHS) {
      expect(underALocale(path)).toBe(true)
    }
    expect(PUBLIC_PATHS).toContain('/en')
    expect(PUBLIC_PATHS).toContain('/zh-TW')
    expect(PUBLIC_PATHS).not.toContain('/')
    expect(PUBLIC_PATHS).not.toContain('/pricing')
  })
})

describe('the sitemap', () => {
  it('lists every public page in both locales once, under the origin', () => {
    const locs = entries(sitemapXml(ORIGIN)).map(({ loc }) => loc)
    expect(locs).toHaveLength(new Set(locs).size)
    expect([...locs].sort()).toEqual(PUBLIC_PATHS.map((path) => ORIGIN + path).sort())
    expect(locs).toContain(`${ORIGIN}/en`)
    expect(locs).toContain(`${ORIGIN}/en/pricing`)
    expect(locs).toContain(`${ORIGIN}/zh-TW`)
    expect(locs).toContain(`${ORIGIN}/zh-TW/pricing`)
  })

  it("names every locale of an entry's page, itself included, and the English page as x-default", () => {
    for (const { loc, alternates } of entries(sitemapXml(ORIGIN))) {
      expect(Object.keys(alternates).sort()).toEqual(['en', 'x-default', 'zh-TW'])
      expect(Object.values(alternates)).toContain(loc)
      expect(alternates['x-default']).toBe(alternates.en)
      expect(alternates.en).toMatch(new RegExp(`^${ORIGIN}/en(/|$)`))
      expect(alternates['zh-TW']).toMatch(new RegExp(`^${ORIGIN}/zh-TW(/|$)`))
    }
  })

  it('is empty without an origin, since a sitemap takes absolute URLs only', () => {
    expect(entries(sitemapXml(''))).toEqual([])
    expect(sitemapXml('')).toContain('<urlset')
  })
})

describe('robots.txt', () => {
  it('blocks no public page in any locale', () => {
    const rules = disallowed(robotsTxt(ORIGIN))
    for (const path of PUBLIC_PATHS) {
      expect(rules.some((rule) => path.startsWith(rule))).toBe(false)
    }
  })

  it('keeps crawlers off the authed pages, the operator console and the sign-in redirect, in both locales', () => {
    const rules = disallowed(robotsTxt(ORIGIN))
    for (const path of [
      '/en/files',
      '/en/settings/usage',
      '/zh-TW/files',
      '/zh-TW/settings/keys',
      '/en/admin',
      '/en/admin/groups',
      '/zh-TW/admin',
      '/zh-TW/admin/audit',
      '/api/auth/login',
    ]) {
      expect(rules.some((rule) => path.startsWith(rule))).toBe(true)
    }
  })

  it("leaves the API's public reads open, which the pricing and policy pages render from", () => {
    const rules = disallowed(robotsTxt(ORIGIN))
    for (const path of ['/api/billing/catalog', '/api/site']) {
      expect(rules.some((rule) => path.startsWith(rule))).toBe(false)
    }
  })

  it('names the sitemap only when the origin is known', () => {
    expect(robotsTxt(ORIGIN)).toContain(`\nSitemap: ${ORIGIN}/sitemap.xml\n`)
    expect(robotsTxt('')).not.toContain('Sitemap:')
  })
})
