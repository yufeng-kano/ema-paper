<script setup lang="ts">
/**
 * `/admin` (docs/admin.md § Pages): the console's *Overview*, `jobs.read` — the first entry of
 * the nav. The `admin` middleware sends an account without `jobs.read` on to the first console
 * page it can read, or to the Not Found page when it can read none.
 */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'jobs' })
</script>

<template>
  <AdminPage area="jobs">
    <AdminOverview />
  </AdminPage>
</template>
