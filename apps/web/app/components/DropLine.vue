<script setup lang="ts">
/**
 * Where a dragged block would land (docs/web.md § Editing, *Moving blocks*): a 2px accent
 * line across the column, with an 8px ring — a 2px accent border on paper — at its left end.
 * Placed in its parent's coordinates, which the parent positions.
 */
defineProps<{
  line: { top: number; left: number; width: number }
}>()
</script>

<template>
  <div
    class="drop-line"
    aria-hidden="true"
    :style="{ top: `${line.top}px`, left: `${line.left}px`, width: `${line.width}px` }"
  />
</template>

<style scoped>
.drop-line {
  position: absolute;
  z-index: 4;
  height: 2px;
  margin-top: -1px;
  background: var(--accent);
  pointer-events: none;
}

.drop-line::before {
  content: '';
  position: absolute;
  top: -3px;
  left: -8px;
  box-sizing: border-box;
  width: 8px;
  height: 8px;
  border: 2px solid var(--accent);
  border-radius: 50%;
  background: var(--paper);
}
</style>
