<script setup lang="ts">
/**
 * Where a paged list is (docs/web.md § Design system, *Primitives*): one muted line of the
 * range and the total — *1–50 of 1,284* — then the previous and next pages as two ghost
 * round buttons, each disabled at its end.
 *
 * The page number is the caller's, which keeps it in the route query; this only says which
 * page the reader asked for next.
 */
import { formatCount } from '~/lib/format'

const props = defineProps<{
  /** From 1. */
  page: number
  pageSize: number
  total: number
}>()

const emit = defineEmits<{ 'update:page': [page: number] }>()

const { t, locale } = useI18n()

const last = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))
const range = computed(() => {
  const from = Math.min((props.page - 1) * props.pageSize + 1, props.total)
  const to = Math.min(props.page * props.pageSize, props.total)
  return t('common.pageRange', {
    from: formatCount(from, locale.value),
    to: formatCount(to, locale.value),
    total: formatCount(props.total, locale.value),
  })
})
</script>

<template>
  <nav class="pager" :aria-label="t('common.pages')">
    <span class="range tabular">{{ range }}</span>
    <UiButton
      variant="ghost"
      size="xs"
      icon-only
      :label="t('common.previousPage')"
      :disabled="page <= 1"
      @click="emit('update:page', Math.min(page - 1, last))"
    >
      <template #icon><UiIcon name="chevron-left" /></template>
    </UiButton>
    <UiButton
      variant="ghost"
      size="xs"
      icon-only
      :label="t('common.nextPage')"
      :disabled="page >= last"
      @click="emit('update:page', page + 1)"
    >
      <template #icon><UiIcon name="chevron-right" /></template>
    </UiButton>
  </nav>
</template>

<style scoped>
.pager {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--muted);
  font-size: var(--text-xs);
}

.range {
  margin-right: var(--space-1);
  white-space: nowrap;
}
</style>
