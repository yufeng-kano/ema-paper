<script setup lang="ts">
/**
 * The document's outline, floating over the pane's left gutter (docs/web.md § Result viewer,
 * *Outline, collapse and the way back*): a column of short ticks, one per heading, that shows
 * the document's shape and the reader's place in it, and — the pointer resting on it, or the
 * keyboard opening it — a panel listing every heading. It never takes width.
 *
 * The ticks are centred in the room the pane gives them; the panel opens at their left with
 * its top at the pane's top, where the side cards start, and ends at most where they end. The
 * ticks and the panel are one hover region, the column between them included while the panel
 * is open, and the pointer leaving it closes the panel after a short delay.
 *
 * For the keyboard the ticks are one button. Focus on it opens nothing; Enter, Space or ↓
 * opens the panel with focus on the current row, Escape closes it back to the button, and
 * focus leaving the panel closes it.
 *
 * Where the gutter has no room for the ticks, the header's `ViewerOutlineButton` opens the same
 * panel (`ViewerOutlinePanel`) instead; the viewer draws one or the other, never both.
 */
import { currentTick, fitTicks } from '~/lib/blocks'
import type { OutlineEntry } from '~/lib/markdown'

const props = defineProps<{
  entries: readonly OutlineEntry[]
  /** The current section, by outline index; null above the first heading. */
  current: number | null
  /** Where the ticks may run, in pixels from the pane's top: from `top`, `height` down to
   *  where the pieces at the pane's foot begin. The ticks are centred in it. */
  room: { top: number; height: number }
  /** How tall the panel may be, from the pane's top: down to where the side cards end. */
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
/** How long the pointer may be off the ticks and the panel before the panel closes: the way
 *  from one to the other, or a pointer overshooting the panel's edge, keeps it open. */
const CLOSE_MS = 300

const fit = computed(() => fitTicks(props.entries.map((entry) => entry.level), props.room.height))
const currentAt = computed(() => (fit.value ? currentTick(fit.value, props.current) : null))

const open = ref(false)
const nav = ref<HTMLElement | null>(null)
const button = ref<HTMLButtonElement | null>(null)
const panel = ref<{ root: HTMLElement | null; focusCurrent: () => void } | null>(null)
let restTimer: ReturnType<typeof setTimeout> | undefined
let closeTimer: ReturnType<typeof setTimeout> | undefined

async function show(focusRow: boolean) {
  open.value = true
  await nextTick()
  if (focusRow) {
    panel.value?.focusCurrent()
  }
}

function hide(refocus = false) {
  clearTimeout(restTimer)
  clearTimeout(closeTimer)
  open.value = false
  if (refocus) {
    button.value?.focus()
  }
}

function onRest() {
  clearTimeout(restTimer)
  if (!open.value) {
    restTimer = setTimeout(() => void show(false), REST_MS)
  }
}

function onLeaveTicks() {
  clearTimeout(restTimer)
}

/** Back in the region before the delay ran out: the panel stays. */
function onEnterRegion() {
  clearTimeout(closeTimer)
}

/** The pointer left the ticks, the panel and the column between them. Focus the panel held
 *  goes back to the button rather than to the page as the panel goes. A finger leaves each
 *  time it lifts or starts scrolling the panel, so on a touch screen the panel closes as a
 *  menu does instead: a press elsewhere, Escape or focus leaving. */
function onLeaveRegion(event: PointerEvent) {
  clearTimeout(restTimer)
  clearTimeout(closeTimer)
  if (open.value && event.pointerType !== 'touch') {
    closeTimer = setTimeout(() => hide(panel.value?.root?.contains(document.activeElement) ?? false), CLOSE_MS)
  }
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
  clearTimeout(closeTimer)
  window.removeEventListener('pointerdown', onPointerDownOutside, true)
})
</script>

<template>
  <nav
    ref="nav"
    class="outline"
    :class="{ open }"
    :style="{ height: `${room.top + room.height}px`, paddingTop: `${room.top}px` }"
    :aria-label="t('viewer.outline.nav')"
    @focusout="onFocusOut"
    @pointerenter="onEnterRegion"
    @pointerleave="onLeaveRegion"
  >
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
    <ViewerOutlinePanel
      v-if="open"
      ref="panel"
      class="rail-panel"
      :entries="entries"
      :current="current"
      :max-height="panelHeight"
      :actions="actions"
      :translate="translate"
      @jump="emit('jump', $event)"
      @translate="emit('translate', $event)"
      @context="emit('context', $event)"
      @escape="hide(true)"
    />
  </nav>
</template>

<style scoped>
/* From the pane's top down to the end of the ticks' room, the ticks' width, the ticks centred
   in the room under its top padding: no target of its own at rest, and the bridge between
   the ticks and the panel while the panel is open. */
.outline {
  position: absolute;
  top: 0;
  left: var(--space-2);
  z-index: 3;
  display: flex;
  flex-direction: column;
  justify-content: center;
  width: 24px;
  pointer-events: none;
}

.outline.open {
  pointer-events: auto;
}

/* The ticks' own box is what the pointer rests on — never the whole gutter. */
.ticks {
  display: flex;
  flex: none;
  flex-direction: column;
  align-items: center;
  width: 24px;
  padding: var(--space-1) 0;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  cursor: pointer;
  pointer-events: auto;
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

/* The panel over the ticks at their left, its top the pane's top (`ViewerOutlinePanel`). */
.rail-panel {
  position: absolute;
  top: 0;
  left: 0;
}
</style>
