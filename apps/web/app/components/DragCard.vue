<script setup lang="ts">
/**
 * A dragged block's picture (docs/web.md § Editing, *Moving blocks*): the grip and the
 * block's first line on paper, one line with an ellipsis, never wider than 280px — the
 * document's, the notes' and an agent answer's drags alike. Never seen in place: `paint`
 * writes the line as the drag starts and hands the card to the browser, which takes its
 * picture before the next render.
 */
export interface DragCardControls {
  paint: (transfer: DataTransfer, text: string) => void
}

const root = ref<HTMLElement | null>(null)
const label = ref<HTMLElement | null>(null)

function paint(transfer: DataTransfer, text: string) {
  if (root.value && label.value) {
    label.value.textContent = text
    transfer.setDragImage(root.value, 12, 14)
  }
}

defineExpose<DragCardControls>({ paint })
</script>

<template>
  <div ref="root" class="drag-card" aria-hidden="true">
    <UiIcon name="grip-vertical" />
    <span ref="label" class="drag-card-text" />
  </div>
</template>

<style scoped>
/* Off screen: the browser takes its picture of it as the drag starts. */
.drag-card {
  position: fixed;
  top: -1000px;
  left: -1000px;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  max-width: 280px;
  padding: var(--space-1) var(--space-3) var(--space-1) var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  box-shadow: var(--shadow-menu);
  color: var(--ink);
  font-size: var(--text-sm);
  white-space: nowrap;
  pointer-events: none;
}

.drag-card > :deep(svg) {
  flex: none;
  width: 16px;
  height: 16px;
  color: var(--faint);
}

.drag-card-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
