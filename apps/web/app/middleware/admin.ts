import { ApiRequestError } from '~/lib/api'
import { areaOf, consoleDestination } from '~/lib/admin'

/**
 * Every console page's second middleware, after `authed` (docs/admin.md § Pages): reads
 * `GET /api/admin/me` once, then lets the page open, sends it to the first console page the
 * account can read, or shows the Not Found page — for a console that is off (the API's 404)
 * and for an account that can read no console page. The `/admin` HTML is a client-rendered
 * shell the server answers 200 for; the API's 404 is what enforces it. Any other failure —
 * offline, the API restarting — lets the page open on *Couldn't open the admin console*,
 * whose *Retry* runs this again at the same path.
 */
export default defineNuxtRouteMiddleware(async (to) => {
  if (import.meta.server) {
    return
  }
  const notFound = () => abortNavigation(createError({ status: 404, statusText: 'Page Not Found', fatal: true }))
  const localePath = useLocalePath()
  const adminConsole = useAdminConsole()
  try {
    await adminConsole.ensureLoaded()
  } catch (error) {
    if (error instanceof ApiRequestError && error.isUnauthorized) {
      useAuth().clearSession()
      return navigateTo(localePath('/'))
    }
    if (error instanceof ApiRequestError && error.status === 404) {
      return notFound()
    }
    adminConsole.markUnavailable()
    return
  }

  const area = areaOf(to.meta.adminArea)
  const destination = consoleDestination(adminConsole.permissions.value, area)
  if (destination === null) {
    return notFound()
  }
  if (area === null || !adminConsole.can(area, 'read')) {
    return navigateTo(localePath(destination), { replace: true })
  }
})
