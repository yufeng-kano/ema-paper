// Nuxt control plane (docs/web.md). The app only ever uses relative URLs: in production
// Caddy routes /api/*, /v1/* to FastAPI and everything else here; in development the
// nitro dev proxy below reproduces that same-origin split so session cookies work.
import { PAGE_LOCALES, PUBLIC_PATHS } from './shared/seo'

// The public origin (docs/web.md § Search engines). The public pages are prerendered by this
// build, so their absolute links are fixed here, not when they are served: the production
// compose file passes WEB_URL to the image's build. Empty (a local build) keeps them relative.
const siteUrl = (process.env.WEB_URL ?? '').replace(/\/+$/, '')

export default defineNuxtConfig({
  modules: ['@nuxtjs/i18n', '@nuxt/eslint'],
  compatibilityDate: '2025-08-20',
  devtools: { enabled: false },
  // KaTeX's stylesheet ships with the package (fonts bundled by the build) — the result
  // viewer's math depends on nothing remote (docs/web.md § Result viewer).
  css: ['~/assets/css/main.css', '~/assets/css/markdown.css', 'katex/dist/katex.min.css'],
  // This file reads WEB_URL from the build's environment, so its own type check needs Node's types.
  typescript: {
    nodeTsConfig: { compilerOptions: { types: ['node'] } },
  },

  runtimeConfig: {
    public: { siteUrl },
  },

  app: {
    head: {
      htmlAttrs: { lang: 'en' },
      meta: [{ name: 'viewport', content: 'width=device-width, initial-scale=1' }],
      // The pinned theme, applied before first paint so a reader who chose dark never sees
      // the light tokens flash. The same key `useTheme` writes (docs/web.md § Design system).
      script: [
        {
          innerHTML:
            "try{var t=localStorage.getItem('sightread.theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t)}}catch(e){}",
          tagPosition: 'head',
        },
      ],
      // The .ico is not decoration: clients that do not take SVG (and connector webviews
      // that just fetch /favicon.ico) fall back to whatever icon they already hold for the
      // parent domain when this 404s or answers a blank placeholder.
      // `?v=` is the mark's version. Browsers keep a site's icon in a store of their own,
      // apart from the HTTP cache, and fetch it again only when its URL changes; without it
      // a tab went on showing the old mark beside the new one on the page. Raise it whenever
      // the icons change, with the consent page's links (apps/api routes/oauth.py).
      link: [
        { rel: 'icon', href: '/favicon.svg?v=2', type: 'image/svg+xml' },
        { rel: 'icon', href: '/favicon.ico?v=2', sizes: '48x48' },
        { rel: 'apple-touch-icon', href: '/apple-touch-icon.png?v=2' },
      ],
    },
  },

  i18n: {
    defaultLocale: 'en',
    // Every locale has a prefix (docs/web.md § Search engines, *The locale in the URL*): a URL
    // names its language, and the bare root belongs to none.
    strategy: 'prefix',
    // Makes every canonical, hreflang and og:url link absolute; Google ignores a relative hreflang.
    baseUrl: siteUrl,
    // The root and any path without a locale go to the same path under the visitor's language,
    // read from `Accept-Language` on the server and `navigator.languages` in the browser,
    // English when no locale matches. No cookie: the root follows the browser every time, and
    // a chosen language is a chosen URL. A prefixed path is never redirected, so a shared
    // link, a bookmark or a search result opens in the language it names.
    detectBrowserLanguage: { useCookie: false, redirectOn: 'no prefix', fallbackLocale: 'en' },
    locales: [
      { code: 'en', language: 'en', name: 'English', file: 'en.ts' },
      { code: 'zh-TW', language: 'zh-TW', name: '繁體中文', file: 'zh-TW.ts' },
    ],
  },

  routeRules: {
    // The root answers by the visitor's language (the i18n redirect above): say so to any
    // cache between. The redirect itself is a 302, which nothing keeps.
    '/': { headers: { vary: 'Accept-Language' } },
    // The public pages — the sign-in page, pricing, the rates and the policy pages — are
    // prerendered HTML under each locale, from the same list the sitemap gives (shared/seo.ts).
    ...Object.fromEntries(PUBLIC_PATHS.map((path) => [path, { prerender: true }])),
    // What crawlers read beside the pages, built from the same origin.
    '/robots.txt': { prerender: true },
    '/sitemap.xml': { prerender: true },
    // Control-plane pages are per-user and session-authenticated: client-rendered only, under
    // each locale. The operator console (docs/admin.md § Pages) is client-rendered like the rest
    // of the control plane; its bare path is listed beside its subtree, which `/**` alone may
    // not match.
    ...Object.fromEntries(
      PAGE_LOCALES.flatMap(({ prefix }) => [
        [`${prefix}/files`, { ssr: false }],
        [`${prefix}/settings/**`, { ssr: false }],
        [`${prefix}/admin`, { ssr: false }],
        [`${prefix}/admin/**`, { ssr: false }],
      ]),
    ),
    // The four screens that became sections of the settings dialog (docs/web.md § Pages):
    // a bookmark of the old path lands on its section, through the `/settings/<section>`
    // deep-link page. The unprefixed paths are English's from before every locale had a
    // prefix; their targets are localized by the browser's language, like any bare path.
    '/agents': { redirect: '/settings/agent' },
    '/keys': { redirect: '/settings/keys' },
    '/connect': { redirect: '/settings/mcp' },
    '/dashboard': { redirect: '/settings/usage' },
    '/zh-TW/agents': { redirect: '/zh-TW/settings/agent' },
    '/zh-TW/keys': { redirect: '/zh-TW/settings/keys' },
    '/zh-TW/connect': { redirect: '/zh-TW/settings/mcp' },
    '/zh-TW/dashboard': { redirect: '/zh-TW/settings/usage' },
    // History, the list of every parse, was removed (docs/web.md § Pages): a bookmark of it
    // lands on Files.
    '/history': { redirect: '/files' },
    '/zh-TW/history': { redirect: '/zh-TW/files' },
  },

  nitro: {
    devProxy: {
      '/api': { target: 'http://localhost:8000/api', changeOrigin: false },
      '/v1': { target: 'http://localhost:8000/v1', changeOrigin: false },
    },
  },
})
