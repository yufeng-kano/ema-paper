<script setup lang="ts">
/**
 * What a surface shows when it has nothing — which is the *first* thing a new user sees, so
 * it says what would be here rather than just "No data". Empty is a real state
 * (docs/web.md § Rules): nothing here is ever filled with sample data.
 *
 * `compact` is for an empty state nested in a short section, where the full vertical
 * treatment would push everything else off-screen.
 */
defineProps<{
  title: string
  body?: string
  compact?: boolean
}>()
</script>

<template>
  <div class="empty" :class="{ compact }">
    <p class="empty-title">{{ title }}</p>
    <p v-if="body" class="empty-body">{{ body }}</p>
    <div v-if="$slots.action" class="empty-action">
      <slot name="action" />
    </div>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-14) var(--space-5);
  text-align: center;
}

.empty.compact {
  padding: var(--space-6) var(--space-4);
  gap: var(--space-1);
}

.empty-title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--ink);
}

/* Balanced, so a line never ends on one stray character — which a CJK line otherwise
   does at this width. */
.empty-body {
  max-width: 44ch;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.55;
  text-wrap: balance;
}

.empty-action {
  margin-top: var(--space-3);
}
</style>
