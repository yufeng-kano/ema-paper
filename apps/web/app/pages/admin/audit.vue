<script setup lang="ts">
/** The console's Audit page (docs/admin.md § Pages, *Audit*): `audit.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'audit' })
</script>

<template>
  <AdminPage area="audit">
    <AdminAudit />
  </AdminPage>
</template>
