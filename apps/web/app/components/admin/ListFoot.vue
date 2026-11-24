<script setup lang="ts">
/** A console list's foot: its hint at the left, and the pager at the right once it has rows. */
defineProps<{
  hint: string
  list: { page: number; page_size: number; total: number }
}>()

const emit = defineEmits<{ page: [page: number] }>()
</script>

<template>
  <footer class="foot">
    <span>{{ hint }}</span>
    <UiPager
      v-if="list.total"
      :page="list.page"
      :page-size="list.page_size"
      :total="list.total"
      @update:page="emit('page', $event)"
    />
  </footer>
</template>

<style scoped>
.foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2) var(--space-4);
  padding: var(--space-3) var(--cell-x) 0;
  color: var(--muted);
  font-size: var(--text-xs);
}
</style>
