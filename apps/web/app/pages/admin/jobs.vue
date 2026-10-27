<script setup lang="ts">
/** The console's Jobs page (docs/admin.md § Pages, *Jobs*): `jobs.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'jobs' })
</script>

<template>
  <AdminPage area="jobs">
    <AdminJobs />
  </AdminPage>
</template>
