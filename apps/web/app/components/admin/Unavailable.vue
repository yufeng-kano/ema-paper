<script setup lang="ts">
/**
 * *Couldn't open the admin console* (docs/admin.md § Pages, *Errors*): `GET /api/admin/me`
 * failed for a reason other than a sign-out or a console that is off — offline, the API
 * restarting — so the page opens on this in place of its content, at its own path, with one
 * button. *Retry* navigates to the same path again, which runs the `admin` middleware again:
 * it opens the page, sends it on, or shows Not Found, as it would have the first time.
 */
const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const pending = ref(false)

async function retry() {
  if (pending.value) {
    return
  }
  pending.value = true
  try {
    // `force`: the same location, navigated to again rather than ignored as a duplicate.
    await router.replace({ path: route.path, query: route.query, hash: route.hash, force: true })
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <UiScreen>
    <UiPanel>
      <UiEmptyState :title="t('admin.unavailable.title')" :body="t('admin.unavailable.body')">
        <template #action>
          <UiButton variant="primary" :loading="pending" @click="retry">
            {{ t('common.retry') }}
          </UiButton>
        </template>
      </UiEmptyState>
    </UiPanel>
  </UiScreen>
</template>
