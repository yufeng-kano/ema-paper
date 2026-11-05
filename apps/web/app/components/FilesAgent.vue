<script setup lang="ts">
/**
 * The Files page's Agent card (docs/web.md § The Files page's Agent card): the agent panel in
 * library mode at the page's right, as the reader's Agent card sits at the document's — the
 * light-glass card inset from the window's edges, resized by the grip on its left edge as
 * the reader's cards are (§ Column widths) — or, below 900px, a full-screen sheet over the
 * page with a way back.
 *
 * Whether it is open and how wide it is are the page's own, remembered in the browser apart
 * from the reader's cards (`useViewerLayout`). It mounts the first time it opens and then
 * stays, hidden and inert while closed, so a close keeps the conversation, the draft and the
 * attachments, and a reader who never opens it costs no conversation read.
 */
import { COLUMN_WIDTHS, clampWidth, collapses, storedWidth } from '~/lib/columns'
import type { LibraryPlace } from '~/lib/library'
import type { LibraryStep } from '~/lib/steps'

/** Where the card's state is kept in the browser between visits. */
const STORAGE_KEY = 'sightread.files.agent'
/** The list never gets narrower than this to make room for the card, as the document pane
 *  does not beside the reader's. */
const LIST_MIN_WIDTH = 360
/** The card's inset from the window's edge, and the gap its grip sits in: `--space-4`. */
const CARD_GAP = 16
/** Matches the 900px breakpoint of this component's stylesheet and the layout's drawer. */
const SHEET_QUERY = '(max-width: 900px)'

defineProps<{
  library: LibraryPlace
  undoLibrary: (step: LibraryStep) => Promise<boolean>
  /** A document is open in a viewer over the page, whose own Agent card is the one in use. */
  covered: boolean
}>()

const emit = defineEmits<{
  /** The close button, a drag past half the minimum, or the sheet's way back. */
  close: []
  library: [records: LibraryStep[]]
  filed: []
  'open-files': [target: { kind: 'folder' | 'document'; id: number }]
  'rows-dropped': []
}>()

const open = defineModel<boolean>('open', { required: true })

const { t } = useI18n()

function readStored(): { open: boolean; width: number } {
  const initial = { open: false, width: COLUMN_WIDTHS.agent.initial }
  if (!import.meta.client) {
    return initial
  }
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null')
    if (!stored || typeof stored !== 'object') {
      return initial
    }
    const { open: wasOpen, width } = stored as { open?: unknown; width?: unknown }
    return { open: wasOpen === true, width: storedWidth('agent', width) }
  } catch {
    return initial
  }
}

const stored = readStored()
// The page renders on the client alone, so the card opens as it was left before the first paint.
open.value = stored.open
/** The width the reader chose — by a drag, a key or a double-click — which is what is kept: a
 *  width the window clamped is not a width the reader chose. */
const chosenWidth = ref(stored.width)

function save() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ open: open.value, width: chosenWidth.value }))
  } catch {
    // Storage full or blocked: the card still holds for as long as the page is loaded.
  }
}

watch(open, save)

const mounted = ref(stored.open)
watch(open, (value) => {
  if (value) {
    mounted.value = true
  }
})

// --- the phone's sheet -------------------------------------------------------------------

const sheet = ref(import.meta.client && window.matchMedia(SHEET_QUERY).matches)
let sheetQuery: MediaQueryList | null = null

function onSheetChange(event: MediaQueryListEvent) {
  sheet.value = event.matches
}

onMounted(() => {
  sheetQuery = window.matchMedia(SHEET_QUERY)
  sheet.value = sheetQuery.matches
  sheetQuery.addEventListener('change', onSheetChange)
})

const root = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
/** The sheet stands where the on-screen keyboard leaves room, as the viewer's phone panes do. */
const keyboard = useKeyboardFrame(root, panel, true)

/** A link followed from the sheet shows the list it leads to: the sheet steps aside. */
function openFiles(target: { kind: 'folder' | 'document'; id: number }) {
  emit('open-files', target)
  if (sheet.value) {
    emit('close')
  }
}

// --- the width ---------------------------------------------------------------------------
// The grip's behaviour below, and its `.split` and `.card` styles, are the reader's Agent card's
// (ResultViewer.vue's column resizing), kept as a copy for one card beside a list rather than
// two beside a document: the two must change together (docs/web.md § Column widths).

/** The page's width, which the list and the card share: what the card is clamped against. */
const pageWidth = ref(Number.POSITIVE_INFINITY)
/** The card's width as drawn: the width the reader chose, as far as the window allows. */
const width = ref(stored.width)
/** The grip being dragged: where the drag started, and the card's width then. */
const resizing = ref<{ x: number; width: number } | null>(null)
/** A drag below half the minimum hides the card while it lasts, and closes it if it ends there. */
const dragHidden = ref(false)
let pageObserver: ResizeObserver | null = null

// The page is the card's parent: the Files page's row of the list and the card.
watch(root, (element) => {
  pageObserver?.disconnect()
  pageObserver = null
  const page = element?.parentElement
  if (page) {
    pageObserver = new ResizeObserver(() => {
      pageWidth.value = page.clientWidth
    })
    pageObserver.observe(page)
  }
})

/** The widest the card may be now: its own cap, and whatever leaves the list its minimum
 *  beside the card's grip and its inset from the window's edge. */
function widthMax(): number {
  const { min, max } = COLUMN_WIDTHS.agent
  return Math.max(min, Math.min(max, pageWidth.value - LIST_MIN_WIDTH - 2 * CARD_GAP))
}

function setWidth(value: number) {
  width.value = Math.round(Math.min(widthMax(), Math.max(COLUMN_WIDTHS.agent.min, value)))
}

/** Clamped from the chosen width, never from the width the window last allowed, so a card
 *  the window narrowed grows back as it widens; the card being dragged follows the pointer. */
watch([pageWidth, chosenWidth], () => {
  if (!resizing.value) {
    setWidth(chosenWidth.value)
  }
})

function chooseWidth(value: number) {
  chosenWidth.value = clampWidth('agent', value)
  setWidth(chosenWidth.value)
  save()
}

function startResize(event: PointerEvent) {
  if (event.button !== 0 || !(event.currentTarget instanceof HTMLElement)) {
    return
  }
  event.preventDefault()
  event.currentTarget.setPointerCapture(event.pointerId)
  resizing.value = { x: event.clientX, width: width.value }
  // The column-resize cursor holds over the whole page while the drag lasts.
  document.documentElement.classList.add('col-resizing')
}

function onResize(event: PointerEvent) {
  const drag = resizing.value
  if (!drag) {
    return
  }
  // The card is right of its grip: a drag to the left widens it.
  const next = drag.width - (event.clientX - drag.x)
  // Below half its minimum the card goes, live, and the list takes its room; dragged back
  // past that line it returns at its minimum.
  const hidden = collapses('agent', next)
  if (hidden !== dragHidden.value) {
    if (hidden) {
      holdFocus()
    }
    dragHidden.value = hidden
    if (!hidden) {
      void giveFocusBack()
    }
  }
  if (!hidden) {
    setWidth(next)
  }
}

/** What held focus in the card a drag hid: hiding it drops the focus to the page, so it is
 *  given back as the card returns. */
let heldFocus: HTMLElement | null = null

function holdFocus() {
  const active = document.activeElement
  heldFocus = active instanceof HTMLElement && panel.value?.contains(active) ? active : null
}

/** Called once the card is shown again in state: the wait is for the render that shows it. */
async function giveFocusBack() {
  const held = heldFocus
  heldFocus = null
  await nextTick()
  if (held?.isConnected && (!document.activeElement || document.activeElement === document.body)) {
    held.focus({ preventScroll: true })
  }
}

function stopResize() {
  resizing.value = null
  dragHidden.value = false
  document.documentElement.classList.remove('col-resizing')
}

/** The drag's release: a card released hidden closes, its chosen width the one it had before
 *  the drag; otherwise the width as it stands is the reader's choice — unless the press moved
 *  nothing, which chooses nothing. */
function endResize() {
  const drag = resizing.value
  if (!drag) {
    return
  }
  const hidden = dragHidden.value
  stopResize()
  if (hidden) {
    heldFocus = null
    width.value = drag.width
    emit('close')
  } else if (width.value !== drag.width) {
    chooseWidth(width.value)
  }
}

/** A cancelled drag puts the width back and never closes the card. */
function cancelResize() {
  const drag = resizing.value
  if (drag) {
    const hidden = dragHidden.value
    stopResize()
    width.value = drag.width
    if (hidden) {
      void giveFocusBack()
    }
  }
}

function onResizeKey(event: KeyboardEvent) {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
    return
  }
  event.preventDefault()
  // The arrow moves the edge: to the left widens the card. It stops at the card's minimum:
  // only a drag puts the card away.
  const before = width.value
  const step = event.shiftKey ? 64 : 16
  setWidth(before + (event.key === 'ArrowLeft' ? step : -step))
  // A key that moves nothing — the edge already at a bound — chooses nothing.
  if (width.value !== before) {
    chooseWidth(width.value)
  }
}

onBeforeUnmount(() => {
  pageObserver?.disconnect()
  sheetQuery?.removeEventListener('change', onSheetChange)
  if (resizing.value) {
    document.documentElement.classList.remove('col-resizing')
  }
})

const chat = ref<{ holds: boolean } | null>(null)

defineExpose({
  /** The Files page keeps its keys off the card: a key from inside it, or one after a press in
   *  it that left the focus on the page (docs/web.md § The Files page's Agent card). */
  contains: (node: Node | null) => !!node && !!root.value?.contains(node),
  /** What the card holds that a reload would lose — closed too, since it keeps it. */
  holds: computed(() => chat.value?.holds ?? false),
  /** Open as the sheet over the whole page: the list under it is out of reach. */
  sheet: computed(() => open.value && sheet.value),
})
</script>

<template>
  <div
    v-if="mounted"
    v-show="open"
    ref="root"
    class="files-agent"
    :class="{ sheet, keyboard: sheet && keyboard }"
    :inert="!open || covered"
  >
    <div
      v-if="!sheet"
      class="split"
      :class="{ dragging: resizing }"
      role="separator"
      tabindex="0"
      aria-orientation="vertical"
      :aria-label="t('viewer.resizeAgent')"
      :aria-valuenow="width"
      :aria-valuemin="COLUMN_WIDTHS.agent.min"
      :aria-valuemax="widthMax()"
      @pointerdown="startResize"
      @pointermove="onResize"
      @pointerup="endResize"
      @pointercancel="cancelResize"
      @dblclick="chooseWidth(COLUMN_WIDTHS.agent.initial)"
      @keydown="onResizeKey"
    />
    <aside
      v-show="!dragHidden"
      ref="panel"
      class="card"
      :style="
        sheet
          ? keyboard
            ? { marginTop: `${keyboard.top}px`, height: `${keyboard.height}px` }
            : undefined
          : { '--column-width': `${width}px` }
      "
      :aria-label="t('chat.panel')"
    >
      <ChatPanel
        ref="chat"
        :document-id="null"
        :document-selection="() => null"
        :document-pages="() => []"
        :current-section="() => null"
        :closable="!sheet"
        :library="library"
        :undo-library="undoLibrary"
        @close="emit('close')"
        @filed="emit('filed')"
        @library="emit('library', $event)"
        @open-files="openFiles"
        @rows-dropped="emit('rows-dropped')"
      >
        <template v-if="sheet" #head-start>
          <button type="button" class="back" @click="emit('close')">
            <UiIcon name="chevron-left" />
            <span>{{ t('files.agentBack') }}</span>
          </button>
        </template>
      </ChatPanel>
    </aside>
  </div>
</template>

<style scoped>
/* Beside the list, as tall as it: the grip's gap, then the card inset from the window's top,
   right and bottom edges, the counterpart of the sidebar's card at the left. */
.files-agent {
  display: flex;
  flex: none;
  min-height: 0;
  padding: var(--space-4) var(--space-4) var(--space-4) 0;
}

/* The grip is the 16px gap between the list and the card, the whole gap the hit area; its
   pill is drawn while it is dragged or holds keyboard focus, and always under a finger. */
.split {
  position: relative;
  z-index: 1;
  flex: none;
  width: var(--space-4);
  border-radius: var(--radius-full);
  cursor: col-resize;
  touch-action: none;
}

.split::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 5px;
  height: 40px;
  border-radius: var(--radius-full);
  background: var(--grip);
  opacity: 0;
  transform: translate(-50%, -50%);
  transition:
    background var(--duration-fast) var(--ease),
    height var(--duration-fast) var(--ease);
}

.split.dragging::after,
.split:focus-visible::after {
  height: 56px;
  background: var(--grip-active);
  opacity: 1;
}

@media (hover: none) {
  .split::after {
    opacity: 1;
  }
}

/* Widths, not flex-bases, here and on the grip: this box sizes itself from what it holds, and
   Safari leaves a flex-basis out of that size, so a basis alone let the card run past the
   window's edge. */
.card {
  display: flex;
  flex: none;
  width: var(--column-width);
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
  border-radius: var(--radius-card);
  background: var(--glass-pane);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  box-shadow: var(--shadow-pane);
}

@supports not (backdrop-filter: blur(1px)) {
  .card {
    background: var(--rail);
  }
}

/* The sheet's way back: a glass capsule at the head's left, as tall as a finger needs. */
.back {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 2px;
  height: 44px;
  margin-right: var(--space-1);
  padding: 0 14px 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-full);
  background: var(--glass);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  box-shadow: var(--shadow-glass);
  color: var(--ink);
  font: inherit;
  font-size: var(--text-base);
  cursor: pointer;
}

.back :deep(svg) {
  width: 20px;
  height: 20px;
}

/* Below 900px the card is a sheet over the whole page, the paper rather than glass. Its box
   is the window's, `inset: 0`, which the keyboard never moves. */
.files-agent.sheet {
  position: fixed;
  inset: 0;
  z-index: 30;
  padding: 0;
  background: var(--paper);
}

.files-agent.sheet .card {
  flex: 1 1 auto;
  width: auto;
  border-radius: 0;
  background: var(--paper);
  -webkit-backdrop-filter: none;
  backdrop-filter: none;
  box-shadow: none;
}

/* While the on-screen keyboard is up: at the visual viewport's top, at its height. */
.files-agent.keyboard {
  align-items: flex-start;
}
</style>
