/**
 * Moving a long-lived tab onto a new build (docs/web.md § Rules).
 *
 * The authed pages are client-rendered and stay open for days, so a deploy reaches them
 * only when something reloads the page. This is the decision of *when* that is free: at
 * once when the page is idle, or when the page lets go of its last hold — a navigation
 * under a hold included, since the page may refuse it. It knows nothing of Nuxt or the
 * DOM — the plugin supplies the three effects — so it runs in the framework-free node
 * suite (docs/testing.md).
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
  /** A route navigation to `path`: true when it was turned into a full load of that path —
   *  never while something holds. */
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
      // A hold applies here too: the page may refuse the navigation and keep what it holds
      // (the console's created codes refuse Back), so this does not turn it into a load, and
      // the reload follows the last release — a page that is left releases its holds as it
      // unmounts. Only this decision is ours: once Nuxt's own periodic check has found the
      // new build, Nuxt's chunk-reload plugin loads every navigation that goes on in full,
      // hold or not, so only a navigation the page refuses keeps what it holds. With nothing
      // held an outdated page is already reloading, and the navigation gives way to that load.
      if (!outdated || holds > 0) {
        return false
      }
      reload(path)
      return true
    },
    isOutdated: () => outdated,
  }
}
