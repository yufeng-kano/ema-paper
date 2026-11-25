/**
 * Fetches lazy chunks once the component has mounted and the page is idle, so the part that
 * needs them opens at once later (docs/web.md § Rules): `requestIdleCallback` with a 3 s
 * timeout, or a 1.5 s timer where the browser has none (Safari). Each loader is a
 * `() => import('literal')` at the call site, so the bundler still splits those chunks.
 *
 * Nuxt reloads only for a chunk that fails during a route navigation, and these parts open
 * without one, so a chunk a deploy has since replaced would render nothing: a warm-up that
 * fails runs the build check instead, which reloads onto the new build under the page's
 * holds.
 */
export function useIdleWarmUp(...loaders: Array<() => Promise<unknown>>): void {
  const { $buildUpdate } = useNuxtApp()
  onMounted(() => {
    const warm = () => {
      void Promise.all(loaders.map((load) => load())).catch(() => $buildUpdate.check())
    }
    if ('requestIdleCallback' in window) {
      requestIdleCallback(warm, { timeout: 3000 })
    } else {
      setTimeout(warm, 1500)
    }
  })
}
