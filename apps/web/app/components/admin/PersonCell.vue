<script setup lang="ts">
/** An account in a console table's cell: its name over its address, or the address alone. */
import { nameOrEmail } from '~/lib/admin'

const props = defineProps<{ name: string | null; email: string }>()

const named = computed(() => !!props.name?.trim())
</script>

<template>
  <span class="person">
    <span class="person-name cell-block">{{ nameOrEmail(props) }}</span>
    <span v-if="named" class="person-email cell-block">{{ email }}</span>
  </span>
</template>

<style scoped>
.person {
  display: grid;
  min-width: 0;
  padding: 6px 0;
  line-height: 1.35;
}

.person-name {
  font-weight: var(--weight-medium);
}

.person-email {
  color: var(--muted);
  font-size: var(--text-xs);
}
</style>
