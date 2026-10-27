<script setup lang="ts">
/** The console's Redeem codes page (docs/admin.md § Pages, *Redeem codes*): `codes.read`. */
definePageMeta({ middleware: ['authed', 'admin'], adminArea: 'codes' })
</script>

<template>
  <AdminPage area="codes">
    <AdminCodes />
  </AdminPage>
</template>
