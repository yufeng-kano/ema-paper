<script setup lang="ts">
/**
 * What the result viewer says at the foot of the document pane (docs/web.md § Result viewer,
 * *The way back after a jump*; § Editing, *Keyboard block selection*, *Undo and the status
 * note*): the hint bar while a keyboard selection exists, the pill back to where the last jump
 * started, and the status note. They stack upward — the hint bar lowest, then the pill, then
 * the note — and never overlap. Each is built from the menu's floating surface, since each
 * floats over the document rather than being part of it.
 *
 * Nothing here is announced: the viewer keeps one live region for what these say, so a note
 * replaced before it was read is not read twice.
 */
const props = defineProps<{
  /** A selection the keyboard made exists: the legend of its keys. */
  hint: boolean
  /** The pill's text — *Back to “1 Introduction”* — or null while there is no way back. */
  back: string | null
  /** The status note, and whether it carries *Undo*. */
  note: { text: string; undo: boolean } | null
}>()

const emit = defineEmits<{
  back: []
  undo: []
  /** The pointer rests on the note, which holds it past its time; false as it leaves. */
  hold: [held: boolean]
}>()

const { t } = useI18n()
const { label, aria } = useShortcuts()

const root = ref<HTMLElement | null>(null)

defineExpose({ root })
</script>

<template>
  <div ref="root" class="foot" :class="{ hinted: props.hint }">
    <!-- A legend, not a control: a press on it takes no focus from the selected block. -->
    <p v-if="props.hint" class="surface hint" :aria-label="t('viewer.select.legend')" @mousedown.prevent>
      <span class="hint-item"><UiKbd keys="updown" />{{ t('viewer.select.select') }}</span>
      <span class="hint-rule" aria-hidden="true">·</span>
      <span class="hint-item"><UiKbd keys="shift+updown" />{{ t('viewer.select.extend') }}</span>
      <span class="hint-rule" aria-hidden="true">·</span>
      <span class="hint-item"><UiKbd keys="mod+shift+updown" />{{ t('viewer.select.move') }}</span>
      <span class="hint-rule" aria-hidden="true">·</span>
      <span class="hint-item"><UiKbd keys="mod+/" />{{ t('viewer.select.menu') }}</span>
      <span class="hint-rule" aria-hidden="true">·</span>
      <span class="hint-item"><UiKbd keys="esc" />{{ t('viewer.select.cancel') }}</span>
    </p>
    <button
      v-if="props.back"
      type="button"
      class="surface pill"
      :title="props.back"
      :aria-keyshortcuts="aria('mod+[')"
      @click="emit('back')"
    >
      <UiIcon name="corner-up-left" />
      <span class="pill-text">{{ props.back }}</span>
      <UiKbd class="pill-key" keys="mod+[" />
    </button>
    <p
      v-if="props.note"
      class="surface note"
      @pointerenter="emit('hold', true)"
      @pointerleave="emit('hold', false)"
    >
      <span>{{ props.note.text }}</span>
      <template v-if="props.note.undo">
        <span class="hint-rule" aria-hidden="true">·</span>
        <button type="button" class="note-undo" @click="emit('undo')">
          {{ t('viewer.note.undo', { keys: label('mod+z') }) }}
        </button>
      </template>
    </p>
  </div>
</template>

<style scoped>
/* Centered at the pane's foot, held inside it; the pieces stack upward from the lowest. Alone,
   the hint bar stands `--space-4` above the foot and anything else `--space-6`. */
.foot {
  position: absolute;
  bottom: var(--space-6);
  left: 50%;
  z-index: 3;
  display: flex;
  flex-direction: column-reverse;
  align-items: center;
  gap: var(--space-2);
  width: max-content;
  max-width: calc(100% - var(--space-8));
  transform: translateX(-50%);
  pointer-events: none;
}

.foot.hinted {
  bottom: var(--space-4);
}

.foot > * {
  pointer-events: auto;
}

/* The menu's floating surface (docs/web.md § Design system). */
.surface {
  margin: 0;
  max-width: 100%;
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  animation: foot-in var(--duration-fast) var(--ease-enter);
}

@keyframes foot-in {
  from {
    opacity: 0;
    transform: translateY(4px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .surface {
    animation: none;
  }
}

/* Wraps on a narrow screen rather than running off it. */
.hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: var(--space-1) var(--space-2);
  padding: var(--space-1) var(--space-3);
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.hint-item {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  white-space: nowrap;
}

.hint-rule {
  color: var(--faint);
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: 32px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-full);
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.pill:hover {
  background: var(--rail-active);
}

.pill > :deep(svg) {
  flex: none;
  width: 15px;
  height: 15px;
  color: var(--muted);
}

.pill-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* A coarse pointer has no keyboard to name a key of. */
@media (pointer: coarse) {
  .pill-key {
    display: none;
  }
}

.note {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.6;
}

/* A text button, as the agent panel's memory lines draw theirs: a faint underline, inked on
   hover. */
.note-undo {
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ink);
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--edge);
  text-underline-offset: 3px;
  cursor: pointer;
  transition: text-decoration-color var(--duration-fast) var(--ease);
}

.note-undo:hover {
  text-decoration-color: currentcolor;
}
</style>
