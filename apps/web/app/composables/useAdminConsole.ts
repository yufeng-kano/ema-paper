import type { Ref } from 'vue'
import { getAdminMe, type AdminMe, type AdminPerson, type AuditEntry, type PeriodSource } from '~/lib/api'
import {
  adminFailure,
  can as holds,
  conflictKey,
  nameOrEmail,
  pastLastPage,
  planLabel,
  type AdminArea,
  type AdminLevel,
} from '~/lib/admin'
import { formatCount } from '~/lib/format'

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

  /** Reads `GET /api/admin/me` unless the signed-in account's answer is here; concurrent
   *  callers share one request. Its failure — a 404 when the console is off — is the
   *  caller's to handle. */
  function ensureLoaded(): Promise<void> {
    if (me.value) {
      return Promise.resolve()
    }
    inFlight ??= reload().finally(() => {
      inFlight = null
    })
    return inFlight
  }

  /** Re-reads it: after a 403 `permission_denied`, so the nav and the buttons follow what
   *  the account holds now, and after signing in again. */
  async function reload(): Promise<void> {
    state.value = await getAdminMe()
    failed.value = false
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

/** What a redeem code, or a batch of them, grants: credits and how long they last, or days of
 *  a plan. */
interface CodeGrant {
  kind: 'credits' | 'plan'
  credits: number | null
  valid_days: number | null
  plan_id: string | null
  days: number | null
}

/**
 * The console's words for accounts, audit records, grants and plans, the same on every page
 * that shows them. Called in setup. A caller whose account can be missing says what that
 * shows itself.
 */
export function useAdminLabels() {
  const { t, te, locale } = useI18n()

  /** An account the console names without its address — who added, changed or disabled
   *  something: its name, else its account number. */
  function personName(person: AdminPerson): string {
    return person.name?.trim() || t('admin.accountId', { id: person.id })
  }

  /** Who made an audit record; null once that account is deleted. */
  function actorName(row: AuditEntry): string | null {
    return row.actor ? nameOrEmail(row.actor) : null
  }

  /** The catalog's words for an action; one it does not know prints as its key. */
  function actionLabel(action: string): string {
    return te(`admin.audit.actions.${action}`) ? t(`admin.audit.actions.${action}`) : action
  }

  /** What an audit record's target is called now; a job, or one gone, by its type and id. */
  function targetLabel(row: AuditEntry): string {
    if (row.target_label) {
      return row.target_label
    }
    const type = te(`admin.audit.targets.${row.target_type}`) ? t(`admin.audit.targets.${row.target_type}`) : row.target_type
    return `${type} ${row.target_id}`
  }

  function grantLabel(grant: CodeGrant): string {
    if (grant.kind === 'credits') {
      const days = grant.valid_days ?? 0
      return t(
        'admin.codes.grantCredits',
        { credits: formatCount(grant.credits ?? 0, locale.value), n: formatCount(days, locale.value) },
        days,
      )
    }
    const days = grant.days ?? 0
    return t('admin.codes.grantPlan', { n: formatCount(days, locale.value), plan: planLabel(grant.plan_id ?? '') }, days)
  }

  /** An account's plan, and what pays for its period: *Free*, or *Pro · Subscription*. */
  function planLine(plan: { id: string; source: PeriodSource }): string {
    return plan.source === 'free' ? t('admin.users.free') : `${planLabel(plan.id)} · ${t(`admin.users.paidBy.${plan.source}`)}`
  }

  return { personName, actorName, actionLabel, targetLabel, grantLabel, planLine }
}

/**
 * A console list's place in the route query (docs/admin.md § Pages): every page keeps its
 * filters there, so Back and a reload return to them. Each page reads its own filters.
 */
export function useAdminQuery() {
  const route = useRoute()
  const router = useRouter()

  /** Moves the query, a `null` dropping its key; a filter or a search passes `page: null`,
   *  so it starts again at the first page. */
  function setQuery(patch: Record<string, string | null>, replace = false): void {
    const query = Object.fromEntries(Object.entries({ ...route.query, ...patch }).filter(([, value]) => value !== null))
    void (replace ? router.replace({ query }) : router.push({ query }))
  }

  function goToPage(next: number): void {
    setQuery({ page: next > 1 ? String(next) : null })
  }

  /** Whether a list read for `page` is one to show. A page past the end — a link from a
   *  longer list — is not: the query moves to its last page instead, which the query's
   *  change reads. */
  function withinPages(list: Parameters<typeof pastLastPage>[0], page: number): boolean {
    const last = pastLastPage(list, page)
    if (last === null) {
      return true
    }
    setQuery({ page: last > 1 ? String(last) : null }, true)
    return false
  }

  return { setQuery, goToPage, withinPages }
}

/** How long typing rests before a list's search runs. */
const SEARCH_DELAY_MS = 300

/**
 * A list's search field, typed ahead of the query: what is typed reaches `?q=` once typing
 * rests, replacing the entry and starting again at the first page, and Back and Forward,
 * which move the query under the field, move the field with it.
 */
export function useQuerySearch(
  q: Readonly<Ref<string>>,
  setQuery: ReturnType<typeof useAdminQuery>['setQuery'],
): Ref<string> {
  const search = ref(q.value)
  let timer: ReturnType<typeof setTimeout> | undefined

  watch(search, (value) => {
    clearTimeout(timer)
    timer = setTimeout(() => {
      const next = value.trim()
      if (next !== q.value) {
        setQuery({ q: next || null, page: null }, true)
      }
    }, SEARCH_DELAY_MS)
  })

  watch(q, (value) => {
    if (value !== search.value.trim()) {
      search.value = value
    }
  })

  onBeforeUnmount(() => clearTimeout(timer))

  return search
}

interface AdminReadOptions<T> {
  /** False keeps an answer off the page: the read leads elsewhere (`withinPages`). */
  accept?: (loaded: T) => boolean
  /** Takes the newest read's failure over: true when it did, and the page says nothing. */
  onError?: (failure: unknown) => boolean
  /** A failure also clears what the last read showed. */
  clearOnError?: boolean
  onLoaded?: (loaded: T) => void
}

/**
 * One console read and its state. Reads can overlap — a filter changed while the last one
 * runs, *Refresh* — and only the newest may write. A failure is said through
 * `useAdminFailure`, so a reauth, a denial and a console turned off take the page over as
 * everywhere in the console; the last answer stays on screen under it unless
 * `clearOnError`.
 */
export function useAdminRead<T>(fetch: () => Promise<T>, options: AdminReadOptions<T> = {}) {
  const { explain } = useAdminFailure()
  const data = shallowRef<T | null>(null)
  const pending = ref(false)
  const error = ref<string | null>(null)
  let seq = 0

  async function load(): Promise<void> {
    const mine = ++seq
    pending.value = true
    try {
      const loaded = await fetch()
      if (mine !== seq || options.accept?.(loaded) === false) {
        return
      }
      data.value = loaded
      error.value = null
      options.onLoaded?.(loaded)
    } catch (failure) {
      if (mine === seq && options.onError?.(failure)) {
        return
      }
      const message = await explain(failure)
      if (mine === seq) {
        if (options.clearOnError) {
          data.value = null
        }
        error.value = message
      }
    } finally {
      if (mine === seq) {
        pending.value = false
      }
    }
  }

  /** Shows an answer newer than any read still running — a change's own — which then
   *  writes nothing. */
  function replace(value: T): void {
    seq += 1
    pending.value = false
    data.value = value
    error.value = null
  }

  return { data, pending, error, load, replace }
}

type HeldResult<T> = { value: T } | { failure: unknown }

/**
 * A dialog's request with Back held while it runs: a navigation would close the dialog, and
 * the answer would land unseen (docs/admin.md § Pages, *Errors*). The hold is released
 * before the result is returned, so a 401's way back to the landing page — taken while the
 * failure is explained — is never the navigation it refuses. Called in setup, where the
 * router is in reach.
 */
export function useHeldRequest() {
  const router = useRouter()
  return async function held<T>(call: () => Promise<T>): Promise<HeldResult<T>> {
    const release = router.beforeEach(() => false)
    try {
      return { value: await call() }
    } catch (failure) {
      return { failure }
    } finally {
      release()
    }
  }
}
