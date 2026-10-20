<script setup lang="ts">
/**
 * One chip of what goes with a message besides its text — the document, the selection, a
 * whole section (docs/web.md § The agent panel, *Context chips*). The composer's carry a
 * remove control; a sent message's, under its bubble in the thread, do not. The text is cut
 * to one line: a passage reads by its first words.
 */
import type { IconName } from '~/lib/icons'

defineProps<{
  icon: IconName
  text: string
  /** The fixed **This document** chip: drawn quieter, since it carries nothing of its own. */
  faint?: boolean
  /** The remove control's name; none, no control. */
  removeLabel?: string
}>()

const emit = defineEmits<{ remove: [] }>()
</script>

<template>
  <span class="chip" :class="{ faint }">
    <UiIcon :name="icon" />
    <span class="chip-text">{{ text }}</span>
    <button
      v-if="removeLabel"
      type="button"
      class="chip-drop"
      :aria-label="removeLabel"
      :title="removeLabel"
      @click="emit('remove')"
    >
      <UiIcon name="x" />
    </button>
  </span>
</template>

<style scoped>
.chip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  max-width: 100%;
  height: 24px;
  padding: 0 6px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1;
  white-space: nowrap;
}

.chip > :deep(svg) {
  flex: none;
  width: 13px;
  height: 13px;
  color: var(--faint);
}

.chip.faint {
  color: var(--faint);
}

.chip-text {
  min-width: 0;
  max-width: 212px;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* Pulled into the chip's padding, so the glyph sits as close to the edge as the icon on the
   other side. */
.chip-drop {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  margin: 0 -4px 0 -2px;
  padding: 0;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--faint);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.chip-drop:hover {
  background: var(--rail-active);
  color: var(--ink);
}

.chip-drop :deep(svg) {
  width: 12px;
  height: 12px;
}

.chip-drop:focus-visible {
  outline-offset: 1px;
}
</style>
