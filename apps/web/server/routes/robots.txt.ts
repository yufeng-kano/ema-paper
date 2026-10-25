import { robotsTxt } from '#shared/seo'

// docs/web.md § Search engines. Prerendered (nuxt.config.ts), so the origin is the build's.
export default defineEventHandler((event) => {
  setResponseHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return robotsTxt(useRuntimeConfig(event).public.siteUrl)
})
