/**
 * Wires `createBuildUpdate` to the browser: a tab left open across a deploy reloads onto
 * the new build at the first moment that costs nothing (docs/web.md § Rules).
 *
 * The newest build id comes from Nuxt's own app manifest — `builds/latest.json`, which the
 * server already publishes — read when the tab becomes visible, which is when a stale tab
 * is about to be used. Nuxt's periodic check reports through `app:manifest:update`.
 */
import { createBuildUpdate } from '~/lib/build-update'

export default defineNuxtPlugin((nuxtApp) => {
  const config = useRuntimeConfig()
  // Where Nuxt publishes its app manifest: `<baseURL>/<buildAssetsDir>/builds/latest.json`.
  const { baseURL, buildAssetsDir } = config.app
  const manifest = `${baseURL.replace(/\/$/, '')}/${buildAssetsDir.replace(/^\/|\/$/g, '')}/builds/latest.json`

  const update = createBuildUpdate({
    currentId: config.app.buildId,
    async fetchLatestId() {
      // The query defeats any cache between here and the file; the file itself is tiny.
      const latest = await $fetch<{ id?: string }>(`${manifest}?${Date.now()}`)
      return latest.id ?? null
    },
    reload(path) {
      reloadNuxtApp({ path, persistState: false })
    },
  })

  nuxtApp.hook('app:manifest:update', () => update.markOutdated())

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      void update.check()
    }
  })

  // `false` cancels the client-side navigation; the full load of the same target replaces it.
  useRouter().beforeEach((to) => (update.navigate(to.fullPath) ? false : undefined))

  return { provide: { buildUpdate: update } }
})
