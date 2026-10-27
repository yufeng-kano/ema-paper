<script setup lang="ts">
/** The console's Users page (docs/admin.md § Pages, *Users*): `users.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'users' })
</script>

<template>
  <AdminPage area="users">
    <AdminUsers />
  </AdminPage>
</template>
