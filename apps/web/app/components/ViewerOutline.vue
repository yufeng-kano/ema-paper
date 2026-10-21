<script setup lang="ts">
/**
 * The document's outline, floating over the pane's left gutter (docs/web.md § Result viewer,
 * *Outline, collapse and the way back*): a column of short ticks, one per heading, that shows
 * the document's shape and the reader's place in it, and — the pointer resting on it, or the
 * keyboard opening it — a panel listing every heading. It never takes width.
 *
 * For the keyboard the ticks are one button. Focus on it opens nothing; Enter, Space or ↓
 * opens the panel with focus on the current row, Escape closes it back to the button, and
 * focus leaving the panel closes it.
 */
import { currentTick, fitTicks } from '~/lib/blocks'
import type { OutlineEntry } from '~/lib/markdown'

const props = defineProps<{
  entries: readonly OutlineEntry[]
  /** The current section, by outline index; null above the first heading. */
  current: number | null
  /** How tall the ticks may run, in pixels: down to where the pane's foot begins. */
  room: number
  /** How tall the panel may be: the pane's height below the outline's top. */
  panelHeight: number
  /** The row actions — Files, once the document can be edited. */
  actions: boolean
  /** A user-invocable `translate` skill exists: the row offers it. */
  translate: boolean
}>()

const emit = defineEmits<{
  jump: [index: number]
  translate: [index: number]
  context: [index: number]
}>()

const { t } = useI18n()

/** How long the pointer rests on the ticks before the panel opens: a pointer only crossing
 *  the gutter on its way to a handle throws no panel over the text. */
const REST_MS = 150

const fit = computed(() => fitTicks(props.entries.map((entry) => entry.level), props.room))
const currentAt = computed(() => (fit.value ? currentTick(fit.value, props.current) : null))

const open = ref(false)
const nav = ref<HTMLElement | null>(null)
const button = ref<HTMLButtonElement | null>(null)
const panel = ref<HTMLElement | null>(null)
let restTimer: ReturnType<typeof setTimeout> | undefined

async function show(focusRow: boolean) {
  open.value = true
  await nextTick()
  const row = panel.value?.querySelector<HTMLElement>('[aria-current="location"]') ?? panel.value?.querySelector<HTMLElement>('.row-jump')
  // Opened scrolled to the current row, which is where a reader looks first.
  row?.scrollIntoView({ block: 'center' })
  if (focusRow) {
    row?.focus()
  }
}

function hide(refocus = false) {
  clearTimeout(restTimer)
  open.value = false
  if (refocus) {
    button.value?.focus()
  }
}

function onRest() {
  clearTimeout(restTimer)
  restTimer = setTimeout(() => void show(false), REST_MS)
}

function onLeaveTicks() {
  clearTimeout(restTimer)
}

function onButtonKey(event: KeyboardEvent) {
  if (event.key === 'Enter' || event.key === ' ' || event.key === 'ArrowDown') {
    event.preventDefault()
    void show(true)
  }
}

/** A pointer click on the ticks opens the panel too, for a pointer that does not rest. */
function onButtonClick(event: MouseEvent) {
  if (event.detail !== 0 && !open.value) {
    void show(false)
  }
}

function onPanelKey(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The panel's Escape, not the dialog's: it closes back to the button.
    event.stopPropagation()
    event.preventDefault()
    hide(true)
  }
}

function onFocusOut(event: FocusEvent) {
  if (!nav.value?.contains(event.relatedTarget as Node | null)) {
    hide()
  }
}

function onPointerDownOutside(event: PointerEvent) {
  if (open.value && !nav.value?.contains(event.target as Node | null)) {
    hide()
  }
}

onMounted(() => window.addEventListener('pointerdown', onPointerDownOutside, true))
onBeforeUnmount(() => {
  clearTimeout(restTimer)
  window.removeEventListener('pointerdown', onPointerDownOutside, true)
})
</script>

<template>
  <nav ref="nav" class="outline" :aria-label="t('viewer.outline.nav')" @focusout="onFocusOut">
    <button
      ref="button"
      type="button"
      class="ticks"
      :class="{ glyph: !fit }"
      :style="fit ? { gap: `${fit.gap}px` } : undefined"
      :aria-label="t('viewer.outline.button')"
      :title="t('viewer.outline.title')"
      :aria-expanded="open ? 'true' : 'false'"
      @pointerenter="onRest"
      @pointerleave="onLeaveTicks"
      @keydown="onButtonKey"
      @click="onButtonClick"
    >
      <template v-if="fit">
        <span
          v-for="tick in fit.ticks"
          :key="tick.index"
          class="tick"
          :class="{ long: tick.long, current: tick.index === currentAt }"
        />
      </template>
      <UiIcon v-else name="list-tree" />
    </button>
    <div
      v-if="open"
      ref="panel"
      class="panel"
      :style="{ maxHeight: `${panelHeight}px` }"
      @pointerleave="hide()"
      @keydown="onPanelKey"
    >
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
  </nav>
</template>

<style scoped>
.outline {
  position: absolute;
  top: 64px;
  left: var(--space-2);
  z-index: 3;
}

/* The ticks' own box is what the pointer rests on — never the whole gutter. */
.ticks {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 24px;
  padding: var(--space-1) 0;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  cursor: pointer;
}

.ticks.glyph {
  align-items: center;
  height: 24px;
  padding: 0;
  color: var(--faint);
}

.ticks.glyph:hover {
  color: var(--ink);
}

.ticks.glyph > :deep(svg) {
  width: 16px;
  height: 16px;
}

.tick {
  flex: none;
  width: 10px;
  height: 2px;
  border-radius: 1px;
  background: var(--edge);
}

.tick.long {
  width: 16px;
}

.tick.current {
  background: var(--ink);
}

/* The menu's floating surface, over the ticks. */
.panel {
  position: absolute;
  top: 0;
  left: 0;
  width: 264px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  animation: panel-in var(--duration-fast) var(--ease-enter);
}

@supports not (backdrop-filter: blur(1px)) {
  .panel {
    background: var(--paper);
  }
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
  background: var(--rail);
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
  background: var(--rail-active);
  color: var(--ink);
}

.row-act > :deep(svg) {
  width: 15px;
  height: 15px;
}
</style>
