<script setup lang="ts">
/**
 * First paint, before the first response has arrived.
 *
 * Shaped like the rows it stands in for rather than a bare "Loading…": the layout does not
 * jump when the data lands, and the wait reads as this page filling in rather than as
 * nothing having happened. The word itself is still there for screen readers, which have no
 * shape to read.
 */
withDefaults(defineProps<{ rows?: number }>(), { rows: 3 })

const { t } = useI18n()
</script>

<template>
  <div class="skeleton-rows">
    <span class="sr-only" role="status">{{ t('common.loading') }}</span>
    <div v-for="row in rows" :key="row" class="skeleton-row" aria-hidden="true">
      <span class="bar name" />
      <span class="bar meta" />
    </div>
  </div>
</template>

<style scoped>
.skeleton-rows {
  display: grid;
  gap: var(--space-4);
  padding: var(--space-5) 0;
}

.skeleton-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr);
  gap: var(--space-4);
}

.bar {
  display: block;
  height: var(--text-sm);
  border-radius: var(--radius-full);
  background: var(--fill);
  animation: breathe 1.6s var(--ease) infinite;
}

.name {
  width: 60%;
}

.meta {
  width: 85%;
}

@keyframes breathe {
  50% {
    opacity: 0.55;
  }
}
</style>
