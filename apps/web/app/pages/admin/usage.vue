<script setup lang="ts">
/** The console's Usage page (docs/admin.md § Pages, *Usage*): `jobs.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'jobs' })
</script>

<template>
  <AdminPage area="jobs">
    <AdminUsage />
  </AdminPage>
</template>
