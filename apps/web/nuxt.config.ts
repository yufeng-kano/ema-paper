// Nuxt control plane (docs/web.md). The app only ever uses relative URLs: in production
// Caddy routes /api/*, /v1/* to FastAPI and everything else here; in development the
// nitro dev proxy below reproduces that same-origin split so session cookies work.
export default defineNuxtConfig({
  modules: ['@nuxtjs/i18n', '@nuxt/eslint'],
  compatibilityDate: '2025-08-20',
  devtools: { enabled: false },
  // KaTeX's stylesheet ships with the package (fonts bundled by the build) — the result
  // viewer's math depends on nothing remote (docs/web.md § Result viewer).
  css: ['~/assets/css/main.css', '~/assets/css/markdown.css', 'katex/dist/katex.min.css'],

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
    strategy: 'prefix_except_default',
    // Locale lives in the URL, so no cookie or Accept-Language redirect guessing.
    detectBrowserLanguage: false,
    locales: [
      { code: 'en', language: 'en', name: 'English', file: 'en.ts' },
      { code: 'zh-TW', language: 'zh-TW', name: '繁體中文', file: 'zh-TW.ts' },
    ],
  },

  routeRules: {
    // The landing page is the SEO surface: prerendered HTML in both locales.
    '/': { prerender: true },
    '/zh-TW': { prerender: true },
    '/pricing': { prerender: true },
    '/zh-TW/pricing': { prerender: true },
    // The policy pages Dodo's site review reads (docs/billing.md § Transition, step 6).
    '/terms': { prerender: true },
    '/zh-TW/terms': { prerender: true },
    '/privacy': { prerender: true },
    '/zh-TW/privacy': { prerender: true },
    '/refund': { prerender: true },
    '/zh-TW/refund': { prerender: true },
    // Control-plane pages are per-user and session-authenticated: client-rendered only.
    '/files': { ssr: false },
    '/history': { ssr: false },
    '/settings/**': { ssr: false },
    '/zh-TW/files': { ssr: false },
    '/zh-TW/history': { ssr: false },
    '/zh-TW/settings/**': { ssr: false },
    // The four screens that became sections of the settings dialog (docs/web.md § Pages):
    // a bookmark of the old path lands on its section, through the `/settings/<section>`
    // deep-link page.
    '/agents': { redirect: '/settings/agent' },
    '/keys': { redirect: '/settings/keys' },
    '/connect': { redirect: '/settings/mcp' },
    '/dashboard': { redirect: '/settings/usage' },
    '/zh-TW/agents': { redirect: '/zh-TW/settings/agent' },
    '/zh-TW/keys': { redirect: '/zh-TW/settings/keys' },
    '/zh-TW/connect': { redirect: '/zh-TW/settings/mcp' },
    '/zh-TW/dashboard': { redirect: '/zh-TW/settings/usage' },
  },

  nitro: {
    devProxy: {
      '/api': { target: 'http://localhost:8000/api', changeOrigin: false },
      '/v1': { target: 'http://localhost:8000/v1', changeOrigin: false },
    },
  },
})
