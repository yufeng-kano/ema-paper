<script setup lang="ts">
/** The console's Settings page (docs/admin.md § Pages, *Settings*): `settings.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'settings' })
</script>

<template>
  <AdminPage area="settings">
    <AdminSettings />
  </AdminPage>
</template>
