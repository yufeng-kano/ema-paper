<script setup lang="ts">
/**
 * One console page's frame (docs/admin.md § Pages): its screen, or — while the session is
 * past the console's window — the full-panel *Sign in again* in its place, or — while the
 * console's own read failed for a reason other than a sign-out or a console that is off —
 * *Couldn't open the admin console* with *Retry*. The screen is a child that mounts only
 * outside those states, so a page in one reads nothing more, and leaving it mounts the screen
 * afresh, which reads again.
 *
 * It also follows the account's permissions while the page is open: once a re-read (after a
 * 403 `permission_denied`) says the page's area is no longer readable, the page gives way to
 * the first one that is, or to the Not Found page when none is.
 */
import { firstReadablePath } from '~/lib/admin'

// The page's area is its `adminArea` meta, the one the `admin` middleware let it open on.
const area = useRoute().meta.adminArea
const localePath = useLocalePath()
const adminConsole = useAdminConsole()
const { reauthRequired, unavailable } = adminConsole

watch(adminConsole.permissions, (held) => {
  // No answer at all is a sign-out on its way, not a loss of permissions.
  if (!adminConsole.me.value || (area !== undefined && adminConsole.can(area, 'read'))) {
    return
  }
  const destination = firstReadablePath(held)
  if (destination === null) {
    showError(createError({ status: 404, statusText: 'Page Not Found', fatal: true }))
  } else {
    void navigateTo(localePath(destination), { replace: true })
  }
})
</script>

<template>
  <AdminReauth v-if="reauthRequired" />
  <AdminUnavailable v-else-if="unavailable" />
  <slot v-else />
</template>
