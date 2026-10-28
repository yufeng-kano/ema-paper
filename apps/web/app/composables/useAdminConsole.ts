import { getAdminMe, type AdminMe } from '~/lib/api'
import { adminFailure, can as holds, conflictKey, type AdminArea, type AdminLevel } from '~/lib/admin'

/**
 * Only ever assigned on the client (the `admin` middleware and the pages are client-only), so
 * it cannot leak between server-rendered requests.
 */
let inFlight: Promise<void> | null = null

/**
 * The operator console's view of the signed-in account (docs/admin.md § Pages): what
 * `GET /api/admin/me` said — the permissions, and whether the session is past the console's
 * window — read once by the `admin` middleware and kept in memory only, never in browser
 * storage, since the console's pages name other people.
 *
 * An answer read for another account is no answer: `me` is null when the signed-in account
 * is not the one it was read for, so a sign-in as someone else in the same tab never shows
 * the last account's console; signing out clears it (`useAuth().clearSession`).
 *
 * `unavailable` is the middleware's note that the read failed for a reason other than a
 * sign-out or a console that is off — offline, the API restarting — so the page opens on
 * *Couldn't open the admin console* with *Retry* rather than an error page that leaves it.
 *
 * State only — no catalog, no navigation — so the route middleware can use it.
 */
export function useAdminConsole() {
  const auth = useAuth()
  const state = useState<AdminMe | null>('admin:me', () => null)
  const failed = useState<boolean>('admin:unavailable', () => false)

  const me = computed(() => {
    const answer = state.value
    return answer && answer.user.id === auth.me.value?.user.id ? answer : null
  })
  const permissions = computed(() => me.value?.permissions ?? [])
  const reauthRequired = computed(() => me.value?.reauth_required === true)
  const reauthHours = computed(() => me.value?.reauth_hours ?? 0)

  async function load(): Promise<void> {
    state.value = await getAdminMe()
    failed.value = false
  }

  /** Reads `GET /api/admin/me` unless the signed-in account's answer is here; concurrent
   *  callers share one request. Its failure — a 404 when the console is off — is the
   *  caller's to handle. */
  function ensureLoaded(): Promise<void> {
    if (import.meta.server || me.value) {
      return Promise.resolve()
    }
    inFlight ??= load().finally(() => {
      inFlight = null
    })
    return inFlight
  }

  /** Re-reads it: after a 403 `permission_denied`, so the nav and the buttons follow what
   *  the account holds now, and after signing in again. */
  function reload(): Promise<void> {
    return load()
  }

  /** A route answered 403 `reauth_required`: the page turns into *Sign in again*. */
  function markReauth(): void {
    if (state.value) {
      state.value = { ...state.value, reauth_required: true }
    }
  }

  /** The read failed, and not for a sign-out or a console that is off. */
  function markUnavailable(): void {
    failed.value = true
  }

  function clear(): void {
    state.value = null
    failed.value = false
  }

  function can(area: AdminArea, level: AdminLevel): boolean {
    return holds(permissions.value, area, level)
  }

  return {
    me,
    permissions,
    reauthRequired,
    reauthHours,
    unavailable: computed(() => failed.value && !me.value),
    ensureLoaded,
    reload,
    markReauth,
    markUnavailable,
    clear,
    can,
  }
}

/**
 * What a console page does with a failed request (docs/admin.md § Pages, *Errors*), answered
 * as the line to show where the action was taken — or null when the console took the failure
 * over itself: a 403 `reauth_required` turns the page into *Sign in again*, a 401 opens the
 * sign-in page (`useApiError`), and a 404 the caller has no words for shows the Not Found
 * page, which is what a console turned off answers.
 *
 * A 403 `permission_denied` re-reads the account's permissions, so the nav and the buttons
 * follow what it holds now; a 409 says its `reason` in the catalog's words.
 */
export function useAdminFailure() {
  const { t } = useI18n()
  const { resolve } = useApiError()
  const adminConsole = useAdminConsole()

  async function explain(error: unknown, notFound?: string): Promise<string | null> {
    const failure = adminFailure(error)
    switch (failure.kind) {
      case 'reauth':
        adminConsole.markReauth()
        return null
      case 'denied':
        try {
          await adminConsole.reload()
        } catch {
          // The permissions stay as they were read; the line below still says why.
        }
        return t('admin.errors.denied')
      case 'conflict': {
        const key = conflictKey(failure.reason)
        return key ? t(key) : await resolve(error)
      }
      case 'notFound':
        if (notFound) {
          return notFound
        }
        showError(createError({ status: 404, statusText: 'Page Not Found', fatal: true }))
        return null
      case 'signedOut':
        await resolve(error)
        return null
      default:
        return resolve(error)
    }
  }

  return { explain }
}
