<script setup lang="ts">
/**
 * `/admin` (docs/admin.md § Pages): never shown itself — the `admin` middleware replaces it
 * with the first console page the account can read, or the Not Found page when it can read
 * none. Until the console has an overview of its own, this is only that hop — or, when the
 * console's own read failed, *Couldn't open the admin console*, whose *Retry* makes it again.
 */
definePageMeta({ middleware: ['authed', 'admin'] })

const { unavailable } = useAdminConsole()
</script>

<template>
  <AdminUnavailable v-if="unavailable" />
  <div v-else />
</template>
