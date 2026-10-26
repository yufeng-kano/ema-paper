/**
 * What search engines read beside the pages (docs/web.md § Search engines): which pages are
 * public, `robots.txt` and the sitemap, the last two prerendered from the public origin the
 * build was given (`WEB_URL`, empty on a local build). Framework-free: nuxt.config.ts, the
 * server routes and a test all read it.
 */

/**
 * The public pages, as the default locale has them: the landing page (the SEO surface),
 * pricing, the rates, and the policy pages Dodo's site review reads (docs/billing.md
 * § Transition, step 6).
 */
export const PUBLIC_PAGES = ['/', '/pricing', '/rates', '/terms', '/privacy', '/refund'] as const

/**
 * The locales as `hreflang` names them, with the prefix their pages live under — none for the
 * default locale (`prefix_except_default`), which comes first and is the sitemap's `x-default`.
 */
export const PAGE_LOCALES = [
  { hreflang: 'en', prefix: '' },
  { hreflang: 'zh-TW', prefix: '/zh-TW' },
] as const

/** A public page's path in one locale; a locale's home page is its bare prefix (`/zh-TW`). */
function localized(page: string, prefix: string): string {
  return prefix && page === '/' ? prefix : prefix + page
}

/** Every public page in every locale: what the build prerenders, and what the sitemap lists. */
export const PUBLIC_PATHS = PUBLIC_PAGES.flatMap((page) => PAGE_LOCALES.map(({ prefix }) => localized(page, prefix)))

/**
 * Kept off every crawler: the authed pages in each locale (client-rendered shells that send a
 * signed-out visitor to sign in), the sign-in redirect, the data plane, the authorization
 * server and the MCP endpoint. The rest of `/api/` stays open: the pricing and policy pages
 * render from `/api/billing/catalog` and `/api/site`.
 */
export const DISALLOWED_PATHS = [
  '/files',
  '/settings',
  '/zh-TW/files',
  '/zh-TW/settings',
  '/api/auth/',
  '/v1/',
  '/oauth/',
  '/mcp',
] as const

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
