<script setup lang="ts">
/**
 * A segmented control: a few choices in one capsule track, the chosen one raised
 * (docs/web.md § Design system, *Primitives*).
 *
 * The segments are the caller's own `<button class="segment">`s, because what a choice
 * means differs from one use to the next — a theme is a radio, a screen's tab is a tab that
 * is also a drop target — and the roles, keys and events go with that meaning. The caller
 * puts `role` and `aria-label` on this element and marks the chosen segment `.active`;
 * this component draws the track and the segments, nothing else.
 *
 * `glass` is the toolbar's: the track floats on the window as a glass capsule the height of
 * the toolbar's other controls, and the chosen segment is a gray pill on it rather than a
 * raised one, as the system's own toolbar segments are.
 */
defineProps<{ glass?: boolean }>()
</script>

<template>
  <div class="segmented" :class="{ glass }">
    <slot />
  </div>
</template>

<style scoped>
.segmented {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
  padding: 2px;
  border-radius: var(--radius-full);
  background: var(--fill);
}

.segmented :deep(.segment) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: calc(var(--control-height-sm) - 4px);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: var(--text-sm);
  white-space: nowrap;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    box-shadow var(--duration-fast) var(--ease);
}

.segmented :deep(.segment:hover:not(.active)) {
  background: var(--fill-weak);
}

.segmented :deep(.segment.active) {
  background: var(--segment);
  font-weight: var(--weight-medium);
  box-shadow: var(--shadow-segment);
}

.segmented :deep(.segment svg) {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

/* --- On the toolbar ------------------------------------------------------------ */

.segmented.glass {
  height: var(--control-height-lg);
  padding: 0 var(--space-1);
  background: var(--glass);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  box-shadow: var(--shadow-glass);
}

@supports not (backdrop-filter: blur(1px)) {
  .segmented.glass {
    background: var(--paper);
  }
}

.segmented.glass :deep(.segment) {
  height: 36px;
  padding: 0 var(--space-4);
}

.segmented.glass :deep(.segment.active) {
  background: var(--fill);
  font-weight: var(--weight-semibold);
  box-shadow: none;
}
</style>
