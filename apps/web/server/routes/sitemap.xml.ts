import { sitemapXml } from '#shared/seo'

// docs/web.md § Search engines. Prerendered (nuxt.config.ts), so the origin is the build's.
export default defineEventHandler((event) => {
  setResponseHeader(event, 'content-type', 'application/xml; charset=utf-8')
  return sitemapXml(useRuntimeConfig(event).public.siteUrl)
})
