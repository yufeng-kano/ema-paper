<script setup lang="ts">
/**
 * The outline's panel (docs/web.md § Result viewer, *Outline, collapse and the way back*): a
 * faint header, then a row per heading — a jump, and once the copy is editable its two
 * actions — the current row marked. One surface for both ways in: the ticks in the gutter
 * (`ViewerOutline`) and, where they do not fit, the header's button (`ViewerOutlineButton`).
 * Each places it; this draws it, opens scrolled to the current row, and hands Escape back.
 */
import type { OutlineEntry } from '~/lib/markdown'

const props = defineProps<{
  entries: readonly OutlineEntry[]
  /** The current section, by outline index; null above the first heading. */
  current: number | null
  /** How tall it may be: down to where the side cards end. */
  maxHeight: number
  /** The row actions — Files, once the document can be edited. */
  actions: boolean
  /** A user-invocable `translate` skill exists: the row offers it. */
  translate: boolean
}>()

const emit = defineEmits<{
  jump: [index: number]
  translate: [index: number]
  context: [index: number]
  escape: []
}>()

const { t } = useI18n()

const root = ref<HTMLElement | null>(null)

function currentRow(): HTMLElement | null {
  return root.value?.querySelector<HTMLElement>('[aria-current="location"]') ?? root.value?.querySelector<HTMLElement>('.row-jump') ?? null
}

/** Focus on the current row, the first when there is none — where a keyboard opening lands. */
function focusCurrent() {
  currentRow()?.focus()
}

function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The panel's Escape, not the dialog's: whoever opened it closes it.
    event.stopPropagation()
    event.preventDefault()
    emit('escape')
  }
}

// Opened scrolled to the current row, which is where a reader looks first.
onMounted(() => currentRow()?.scrollIntoView({ block: 'center' }))

defineExpose({ root, focusCurrent })
</script>

<template>
  <div ref="root" class="panel" :style="{ maxHeight: `${props.maxHeight}px` }" @keydown="onKey">
    <p class="panel-head">{{ t('viewer.outline.title') }}</p>
    <ul class="rows">
      <li
        v-for="(entry, index) in entries"
        :key="`${entry.page}-${entry.index}`"
        class="row"
        :class="{ deep: entry.level > 2, current: index === current }"
      >
        <button
          type="button"
          class="row-jump"
          :title="entry.plain"
          :aria-current="index === current ? 'location' : undefined"
          @click="emit('jump', index)"
        >
          {{ entry.plain }}
        </button>
        <span v-if="actions" class="row-actions">
          <button
            v-if="translate"
            type="button"
            class="row-act"
            :aria-label="t('viewer.outline.translate')"
            :title="t('viewer.outline.translate')"
            @click="emit('translate', index)"
          >
            <UiIcon name="languages" />
          </button>
          <button
            type="button"
            class="row-act"
            :aria-label="t('viewer.outline.addContext')"
            :title="t('viewer.outline.addContext')"
            @click="emit('context', index)"
          >
            <UiIcon name="message-circle-plus" />
          </button>
        </span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
/* The menu's floating surface; where it stands is its opener's to say. */
.panel {
  pointer-events: auto;
  width: 264px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  animation: panel-in var(--duration-fast) var(--ease-enter);
}

@keyframes panel-in {
  from {
    opacity: 0;
    transform: translateX(-4px);
  }
}

@media (prefers-reduced-motion: reduce) {
  .panel {
    animation: none;
  }
}

.panel-head {
  margin: 0;
  padding: var(--space-2) var(--space-3) var(--space-1);
  color: var(--faint);
  font-size: var(--text-xs);
}

.rows {
  margin: 0;
  padding: 0;
  list-style: none;
}

/* Filled with the rail, not the accent: the row holds two more buttons, and an accent fill
   would claim the whole row is one action. */
.row {
  position: relative;
  display: flex;
  align-items: center;
  height: 30px;
  border-radius: var(--radius-menu-item);
  transition: background var(--duration-fast) var(--ease);
}

.row:hover,
.row:focus-within {
  background: var(--fill-weak);
}

.row.current::before {
  content: '';
  position: absolute;
  top: 7px;
  bottom: 7px;
  left: 0;
  width: 2px;
  border-radius: 1px;
  background: var(--ink);
}

.row-jump {
  flex: 1 1 auto;
  min-width: 0;
  height: 100%;
  padding: 0 var(--space-3);
  overflow: hidden;
  border: 0;
  border-radius: var(--radius-menu-item);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.row.deep .row-jump {
  padding-left: calc(var(--space-3) + 12px);
  color: var(--muted);
}

.row.current .row-jump {
  color: var(--ink);
  font-weight: var(--weight-semibold);
}

.row-jump:focus-visible {
  outline-offset: -2px;
}

.row-actions {
  display: none;
  flex: none;
  gap: 2px;
  padding-right: var(--space-1);
}

.row:hover .row-actions,
.row:focus-within .row-actions {
  display: inline-flex;
}

.row-act {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
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

.row-act:hover {
  background: var(--fill);
  color: var(--ink);
}

.row-act > :deep(svg) {
  width: 15px;
  height: 15px;
}
</style>
