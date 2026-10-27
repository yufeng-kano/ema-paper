<script setup lang="ts">
/** The console's Groups page (docs/admin.md § Pages, *Groups*): `groups.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'groups' })
</script>

<template>
  <AdminPage area="groups">
    <AdminGroups />
  </AdminPage>
</template>
