<script setup lang="ts">
/**
 * A screen's large title, and what is beside it.
 *
 * The title is the screen's name in the system's large-title style — or, through the
 * `title` slot, something made of more than a string, as the file library's breadcrumb is;
 * the default slot is the quiet line beside it on the same baseline — a count, a tally, a
 * fact about the list — never a sentence explaining what the screen is for (docs/web.md
 * § Design system, *The frame*).
 */
defineProps<{ title?: string }>()

defineSlots<{
  /** In place of the title's text: a breadcrumb. */
  title?: () => unknown
  /** Beside the title: a count, a tally or a fact about the list, in the muted color. */
  default?: () => unknown
}>()
</script>

<template>
  <header class="page-head">
    <h1 class="title">
      <slot name="title">{{ title }}</slot>
    </h1>
    <div v-if="$slots.default" class="meta">
      <slot />
    </div>
  </header>
</template>

<style scoped>
.page-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-3);
  min-width: 0;
}

.title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-lg);
  line-height: 1.15;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-1) var(--space-3);
  min-width: 0;
  color: var(--muted);
  font-size: var(--text-sm);
}
</style>
