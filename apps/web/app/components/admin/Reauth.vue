<script setup lang="ts">
/**
 * *Sign in again to use the console* (docs/admin.md § Signing in, § Pages): a session older
 * than the console's window is refused by every console route, so the page says so in place
 * of its content, with one button.
 *
 * The button keeps the console path it is on for after the sign-in (the return path the
 * sign-in page takes once, in `sessionStorage` until then) — without its search, which may be
 * someone's address and is console data — signs out — so the old session row goes instead of lingering for
 * its 30 days — and opens Google's sign-in in the reader's language. Where the local dev
 * sign-in exists (the probe the sign-in page makes) it signs in through that instead and
 * stays on the page, which then reads again.
 */
import { devLogin, isDevLoginAvailable, logout } from '~/lib/api'
import { formatCount } from '~/lib/format'
import { keepAfterSignIn, takeAfterSignIn } from '~/lib/settings'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const auth = useAuth()
const adminConsole = useAdminConsole()
const { resolve } = useApiError()

const pending = ref(false)
const error = ref<string | null>(null)

const body = computed(() => {
  const hours = adminConsole.reauthHours.value
  return t('admin.reauth.body', { n: formatCount(hours, locale.value) }, hours)
})

async function signInAgain() {
  if (pending.value) {
    return
  }
  pending.value = true
  error.value = null
  const query = { ...route.query }
  delete query.q
  const back = router.resolve({ path: route.path, query, hash: route.hash }).fullPath
  keepAfterSignIn(back)
  const dev = await isDevLoginAvailable()
  try {
    await logout()
  } catch {
    // The new sign-in replaces the cookie either way; the old row expires on its own.
  }
  if (!dev) {
    // A full-page navigation: the backend owns the redirect flow, and it returns to the
    // sign-in page, which goes on to the kept path. Still pending until the page goes.
    window.location.assign(`/api/auth/login?locale=${encodeURIComponent(locale.value)}`)
    return
  }
  try {
    await devLogin()
    await auth.refresh()
    await adminConsole.reload()
    // Taken either way, so a later sign-in does not land here; the page itself stays as
    // it is, search included.
    const kept = takeAfterSignIn()
    if (kept && kept !== back) {
      await navigateTo(kept)
    }
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <UiScreen>
    <UiPanel>
      <UiEmptyState :title="t('admin.reauth.title')" :body="body">
        <template #action>
          <UiButton variant="primary" :loading="pending" @click="signInAgain">
            {{ t('admin.reauth.action') }}
          </UiButton>
        </template>
      </UiEmptyState>
      <UiBanner v-if="error" class="failure" tone="error">{{ error }}</UiBanner>
    </UiPanel>
  </UiScreen>
</template>

<style scoped>
.failure {
  max-width: 44ch;
  margin: 0 auto;
}
</style>
