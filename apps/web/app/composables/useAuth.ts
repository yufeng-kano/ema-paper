import { ApiRequestError, getMe, logout, type MeResponse } from '~/lib/api'

/**
 * Only ever assigned on the client (`ensureLoaded` runs from a `.client` plugin and from
 * route middleware, both of which return early during SSR/prerender), so it cannot leak
 * state between server-rendered requests.
 */
let inFlight: Promise<void> | null = null

/** Session state from `GET /api/me` — the app's single source of "is anyone signed in". */
export function useAuth() {
  const me = useState<MeResponse | null>('auth:me', () => null)
  const loaded = useState<boolean>('auth:loaded', () => false)

  async function load(): Promise<void> {
    try {
      me.value = await getMe()
    } catch (error) {
      // 401 is the normal signed-out answer. Anything else (offline, backend down) also
      // leaves the app in its signed-out state; pages surface their own errors on demand.
      if (!(error instanceof ApiRequestError)) {
        throw error
      }
      me.value = null
    } finally {
      loaded.value = true
    }
  }

  /** Resolves once `GET /api/me` has answered; concurrent callers share one request. */
  function ensureLoaded(): Promise<void> {
    if (import.meta.server || loaded.value) {
      return Promise.resolve()
    }
    inFlight ??= load().finally(() => {
      inFlight = null
    })
    return inFlight
  }

  /** Re-reads the session after a mutation that changes what `/api/me` reports. */
  function refresh(): Promise<void> {
    return load()
  }

  /** Drops local session state without calling the backend (used when a request 401s),
   *  the operator console's with it (docs/admin.md § Pages). */
  function clearSession(): void {
    me.value = null
    loaded.value = true
    useAdminConsole().clear()
  }

  async function signOut(): Promise<void> {
    try {
      await logout()
    } finally {
      clearSession()
    }
  }

  return {
    me,
    loaded,
    signedIn: computed(() => me.value !== null),
    ensureLoaded,
    refresh,
    clearSession,
    signOut,
  }
}
