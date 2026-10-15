/**
 * Moving a long-lived tab onto a new build (docs/web.md § Rules).
 *
 * The authed pages are client-rendered and stay open for days, so a deploy reaches them
 * only when something reloads the page. This is the decision of *when* that is free: at
 * once when the page is idle, on the next navigation, or when the page lets go of its last
 * hold. It knows nothing of Nuxt or the DOM — the plugin supplies the three effects — so it
 * runs in the framework-free node suite (docs/testing.md).
 */

export interface BuildUpdateDeps {
  /** The build this tab is running. */
  currentId: string
  /** The newest build's id, or null when it could not be read — which is not an error. */
  fetchLatestId: () => Promise<string | null>
  /** A full page load, of `path` when given and of the current location otherwise. */
  reload: (path?: string) => void
}

export interface BuildUpdate {
  /** Asks the server which build is newest, and reloads if this is not it and nothing holds. */
  check: () => Promise<void>
  /** Something else (Nuxt's own periodic check) already knows a newer build exists. */
  markOutdated: () => void
  /** "Not now": no reload until the returned release is called. Releasing twice is one release. */
  hold: () => () => void
  /** A route navigation to `path`: true when it was turned into a full load of that path. */
  navigate: (path: string) => boolean
  isOutdated: () => boolean
}

export function createBuildUpdate(deps: BuildUpdateDeps): BuildUpdate {
  let outdated = false
  let holds = 0
  let reloading = false

  function reload(path?: string): void {
    // Once is enough: a second trigger while the first load is starting would only restart it.
    if (reloading) {
      return
    }
    reloading = true
    deps.reload(path)
  }

  function apply(): void {
    if (outdated && holds === 0) {
      reload()
    }
  }

  return {
    async check() {
      if (outdated) {
        apply()
        return
      }
      // Offline, mid-deploy, a proxy hiccup: the next check will do.
      const latest = await deps.fetchLatestId().catch(() => null)
      if (latest && latest !== deps.currentId) {
        outdated = true
        apply()
      }
    },
    markOutdated() {
      outdated = true
      apply()
    },
    hold() {
      let released = false
      holds += 1
      return () => {
        if (released) {
          return
        }
        released = true
        holds -= 1
        apply()
      }
    },
    navigate(path) {
      // A navigation abandons the page's state anyway, so holds do not apply to it.
      if (!outdated) {
        return false
      }
      reload(path)
      return true
    },
    isOutdated: () => outdated,
  }
}
