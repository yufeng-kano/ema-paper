/**
 * What search engines read beside the pages (docs/web.md § Search engines): which pages are
 * public, where each locale's pages live, `robots.txt` and the sitemap, the last two
 * prerendered from the public origin the build was given (`WEB_URL`, empty on a local build).
 * Framework-free: nuxt.config.ts, the server routes and a test all read it.
 */

/**
 * The public pages, as paths under a locale's prefix: the sign-in page (the SEO surface),
 * pricing, the rates, and the policy pages Dodo's site review reads (docs/billing.md
 * § Transition, step 6).
 */
export const PUBLIC_PAGES = ['/', '/pricing', '/rates', '/terms', '/privacy', '/refund'] as const

/**
 * The locales as `hreflang` names them, each with the prefix its pages live under. Every
 * locale has one (`strategy: 'prefix'`, docs/web.md § Search engines, *The locale in the
 * URL*): the bare root and any path without a locale redirect to the visitor's language
 * (nuxt.config.ts, `detectBrowserLanguage`). The default locale comes first — it is the
 * sitemap's `x-default`, as it is `useLocaleHead`'s on the pages themselves.
 */
export const PAGE_LOCALES = [
  { hreflang: 'en', prefix: '/en' },
  { hreflang: 'zh-TW', prefix: '/zh-TW' },
] as const

/** A public page's path in one locale; a locale's home page is its bare prefix (`/en`). */
function localized(page: string, prefix: string): string {
  return page === '/' ? prefix : prefix + page
}

/** Every public page in every locale: what the build prerenders, and what the sitemap lists. */
export const PUBLIC_PATHS = PUBLIC_PAGES.flatMap((page) => PAGE_LOCALES.map(({ prefix }) => localized(page, prefix)))

/**
 * The authed pages, client-rendered shells that send a signed-out visitor to sign in — the
 * operator console's among them. Under every locale: nothing is served without one.
 */
const AUTHED_PAGES = ['/files', '/settings', '/admin'] as const

/**
 * Kept off every crawler: the authed pages in each locale, the sign-in redirect, the data
 * plane, the authorization server and the MCP endpoint. The rest of `/api/` stays open: the
 * pricing and policy pages render from `/api/billing/catalog` and `/api/site`.
 */
export const DISALLOWED_PATHS = [
  ...PAGE_LOCALES.flatMap(({ prefix }) => AUTHED_PAGES.map((page) => prefix + page)),
  '/api/auth/',
  '/v1/',
  '/oauth/',
  '/mcp',
]

export function robotsTxt(origin: string): string {
  const lines = ['User-agent: *', ...DISALLOWED_PATHS.map((path) => `Disallow: ${path}`)]
  if (origin) {
    lines.push('', `Sitemap: ${origin}/sitemap.xml`)
  }
  return `${lines.join('\n')}\n`
}

/** Every public page in every locale, each listing all its locales and `x-default`; empty without an origin. */
export function sitemapXml(origin: string): string {
  const entries = origin
    ? PUBLIC_PAGES.flatMap((page) => {
        const alternates = PAGE_LOCALES.map(({ hreflang, prefix }) => ({ hreflang, href: origin + localized(page, prefix) }))
        const fallback = { hreflang: 'x-default', href: origin + localized(page, PAGE_LOCALES[0].prefix) }
        const links = [...alternates, fallback]
          .map(({ hreflang, href }) => `    <xhtml:link rel="alternate" hreflang="${hreflang}" href="${href}"/>`)
          .join('\n')
        return alternates.map(({ href }) => `  <url>\n    <loc>${href}</loc>\n${links}\n  </url>`)
      })
    : []
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">',
    ...entries,
    '</urlset>',
    '',
  ].join('\n')
}
