<script setup lang="ts">
/**
 * The Original view of a PDF (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer): the scroll
 * pane on the desk, its column of white pages, the page indicator, the password prompt and the
 * error states. The result viewer owns the header around it and drives the zoom through the
 * exposed `zoomIn`, `zoomOut` and `zoomTo`; this reports `state` on every change.
 *
 * Every page's `<section>` stays mounted at the size the layout model gives it, which is the one
 * truth a scroll offset is computed from. Scroll, resize and size work is batched into one
 * animation frame; the pages' layers and draws are `usePdfPages`', the opening, the password
 * and the document's failure `usePdfDocument`'s, and nothing here calls pdf.js itself.
 */
import '~/assets/css/pdf-layers.css'
import { ApiRequestError } from '~/lib/api'
import type { LoadErrorKind } from '~/lib/pdf/errors'
import { attachPinch, type GesturePoint } from '~/lib/pdf/gestures'
import {
  CSS_UNITS,
  DESKTOP_SPACING,
  PHONE_SPACING,
  SETTLE_MS,
  createLayout,
  currentPage,
  destinationOffset,
  jumpOffset,
  pageRect,
  pageSize,
  scaleLayout,
  type Layout,
  type Size,
} from '~/lib/pdf/layout'
import type { PdfDestination } from '~/lib/pdf/pdfjs'
import {
  anchoredScroll,
  canStepZoom,
  doubleTapZoom,
  fitWidthScale,
  openingZoom,
  percentOf,
  refitZoom,
  resolveZoom,
  stepZoom,
  zoomBy,
  type PdfViewerState,
  type ZoomContext,
  type ZoomState,
  type ZoomTarget,
} from '~/lib/pdf/zoom'
import { zoomKey } from '~/lib/shortcuts'
import type { PdfPageFailureKind } from '~/composables/usePdfPages'

const props = defineProps<{
  /** The file's inline bytes, same origin. */
  url: string
  /** The filename: the pane's name and the opening announcement's. */
  name: string
  /** The attachment URL, for an unreadable file's *Download*. */
  download: string
  /** The file's size, formatted, for *Download ({size})*; null for plain *Download*. */
  downloadSize: string | null
  phone: boolean
  /** Where the reader was when this view last unmounted. */
  initial: { page: number; zoom: ZoomState } | null
}>()

const emit = defineEmits<{ state: [state: PdfViewerState]; close: [] }>()

const { t, locale } = useI18n()
const { apple } = useShortcuts()
const { liveText, announce } = useStatusNote()
const apiError = useApiError()
const { status, prompt, failure, handle, firstPage, open, submitPassword } = usePdfDocument()

const root = ref<HTMLElement | null>(null)
const pane = ref<HTMLElement | null>(null)
const indicator = ref<{ reveal(): void } | null>(null)
const errorState = ref<{ focusHeading(): void } | null>(null)

/** The pane's client box, which every zoom and every page position is measured in. */
const paneSize = shallowRef<Size>({ width: 0, height: 0 })
/** The column. Changed in place when page sizes arrive (`triggerRef`), replaced on a zoom. */
const layout = shallowRef<Layout | null>(null)
const zoom = shallowRef<ZoomState>(props.initial?.zoom ?? { mode: 'fit-width', scale: 1 })
/** The current page, from 0. */
const current = ref(0)
/** During a pinch, the transform the column shows until the zoom commits. */
const pinch = shallowRef<{ scale: number; originX: number; originY: number } | null>(null)
/** The pane's ring follows the keyboard, not `:focus-visible` (docs/web.md § Rules). */
const keyboardFocus = ref(false)

let frame = 0
let settled = true
let settleTimer: ReturnType<typeof setTimeout> | undefined
let touchDown = false
/** A touch has moved the pane since it last settled: a size correction above the viewport
 *  would stop the fling, so it waits. */
let touchScrolled = false
let starts = 0
let observer: ResizeObserver | null = null
let detachPinch: (() => void) | null = null

/** A failure's cause in the reader's words: under a state's title, and in its announcement. */
function causeText(kind: Exclude<LoadErrorKind, 'cancelled'> | PdfPageFailureKind): string {
  switch (kind) {
    case 'corrupt':
      return t('viewer.pdf.error.corrupt')
    case 'page':
      return t('viewer.pdf.pageError.body')
    case 'network':
      return t('errors.network')
    case 'signed-out':
      return t('errors.signedOut')
    case 'gone':
      return t('viewer.pdf.error.gone')
  }
}

const pages = usePdfPages({
  layout,
  pane,
  paneSize,
  handle,
  settled: () => settled,
  pinching: () => pinch.value !== null,
  schedule,
  onNavigate,
  announce,
  failureText: causeText,
})
const { failures, slow } = pages

const ready = computed(() => status.value === 'open' && layout.value !== null)
/** The password prompt or a document error fills the pane in place of the pages. */
const showsState = computed(() => status.value === 'password' || status.value === 'failed')
/** Until a page is drawn the pane is busy and the progress bar runs. A page that failed ends
 *  the wait too: its message and its retry must not sit in a busy region for ever. */
const busy = computed(
  () => !pages.drawn.value && pages.failures.size === 0 && (status.value === 'opening' || status.value === 'open'),
)

/** What every zoom is measured from; null until page 1's size is known. */
function context(): ZoomContext | null {
  const column = ready.value ? layout.value : null
  return column ? { phone: props.phone, pane: paneSize.value, page: pageSize(column, current.value) } : null
}

const viewerState = computed<PdfViewerState>(() => {
  const measured = context()
  return {
    ready: measured !== null,
    zoom: zoom.value,
    fitWidth: measured ? fitWidthScale(measured) : null,
    canZoomIn: measured ? canStepZoom(zoom.value, 'in', measured) : false,
    canZoomOut: measured ? canStepZoom(zoom.value, 'out', measured) : false,
    page: measured ? current.value + 1 : 0,
    pages: measured ? layout.value!.count : 0,
  }
})

// A page size arriving recomputes the state without changing it; the result viewer hears only
// what changed.
let emitted = ''
watch(
  viewerState,
  (state) => {
    const key = JSON.stringify(state)
    if (key !== emitted) {
      emitted = key
      emit('state', state)
    }
  },
  { immediate: true },
)

const paneStyle = computed(() => ({
  '--scale-factor': String((layout.value?.scale ?? zoom.value.scale) * CSS_UNITS),
  '--pane-height': `${paneSize.value.height}px`,
}))

const columnStyle = computed(() => {
  const column = layout.value
  if (!column) {
    return {}
  }
  const shown = pinch.value
  return {
    width: `${Math.max(column.width, paneSize.value.width)}px`,
    height: `${column.height}px`,
    transform: shown ? `scale(${shown.scale})` : undefined,
    transformOrigin: shown ? `${shown.originX}px ${shown.originY}px` : undefined,
  }
})

function pageStyle(index: number) {
  const box = pageRect(layout.value!, index, paneSize.value.width)
  return {
    left: `${box.left}px`,
    top: `${box.top}px`,
    width: `${box.width}px`,
    height: `${box.height}px`,
    '--user-unit': String(pages.userUnit(index)),
  }
}

function pageLabel(index: number): string {
  const page = index + 1
  return failures.has(index)
    ? t('viewer.pdf.pageError.label', { page })
    : t('viewer.pdf.page', { page, total: layout.value?.count ?? 0 })
}

// --- opening ----------------------------------------------------------------------

async function start() {
  const mine = ++starts
  pages.reset(null)
  layout.value = null
  pinch.value = null
  current.value = 0
  announce(t('viewer.pdf.announce.opening', { name: props.name }))
  await open(props.url)
  if (mine !== starts) {
    return
  }
  if (status.value === 'open') {
    await opened()
  } else if (status.value === 'failed') {
    await failed()
  }
}

/** Page 1's size is known: the column is laid out at the opening zoom, on the page the reader
 *  left from if this view was open before. */
async function opened() {
  const file = handle.value
  const first = firstPage.value
  if (!file || !first) {
    return
  }
  pages.reset(first)
  readPane()
  const size = { width: first.width, height: first.height }
  const opening = openingZoom({ phone: props.phone, pane: paneSize.value, page: size }, props.initial?.zoom ?? null)
  const column = createLayout(file.numPages, size, opening.scale, props.phone ? PHONE_SPACING : DESKTOP_SPACING)
  zoom.value = opening
  layout.value = column
  const back = props.initial ? Math.min(column.count, Math.max(1, props.initial.page)) - 1 : 0
  current.value = back
  announce(t('viewer.pdf.announce.opened', { total: column.count }, column.count))
  await nextTick()
  if (back > 0 && pane.value) {
    pane.value.scrollTop = jumpOffset(column, back)
  }
  // The password prompt took focus with it when it went; the pane's scrolling keys need it back.
  if (!document.activeElement || document.activeElement === document.body) {
    pane.value?.focus({ preventScroll: true })
  }
  indicator.value?.reveal()
  schedule()
}

/** The document could not be opened: announced once, and focus carried to the heading only if
 *  it was in what went away (or had fallen to the body); on a header control it stays. */
async function failed() {
  const kind = failure.value
  if (!kind) {
    return
  }
  const active = document.activeElement
  const lost = !active || active === document.body || (root.value?.contains(active) ?? false)
  announce(t('viewer.pdf.announce.failed', { title: t('viewer.pdf.error.title'), reason: causeText(kind) }))
  if (kind === 'signed-out') {
    // An ended session is ended as everywhere else in the app (docs/web.md § Rules).
    void apiError.resolve(new ApiRequestError(401, 'auth', 'Not signed in'))
  }
  await nextTick()
  if (lost) {
    errorState.value?.focusHeading()
  }
}

async function retryDocument() {
  const restart = start()
  await nextTick()
  // The retry button went with the error state; the pane takes its place.
  if (document.activeElement === document.body) {
    pane.value?.focus({ preventScroll: true })
  }
  await restart
}

// --- the frame loop ---------------------------------------------------------------

function schedule() {
  frame ||= requestAnimationFrame(() => void update())
}

function readPane() {
  const el = pane.value
  if (el && (el.clientWidth !== paneSize.value.width || el.clientHeight !== paneSize.value.height)) {
    paneSize.value = { width: el.clientWidth, height: el.clientHeight }
  }
}

/** Starts the 150 ms quiet period after which a touch correction and a detail redraw run. */
function unsettle() {
  settled = false
  clearTimeout(settleTimer)
  settleTimer = setTimeout(() => {
    settled = true
    if (!touchDown) {
      touchScrolled = false
    }
    schedule()
  }, SETTLE_MS)
}

function onScroll() {
  indicator.value?.reveal()
  unsettle()
  schedule()
}

function onTouchStart() {
  touchDown = true
  touchScrolled = true
}

function onTouchEnd(event: TouchEvent) {
  touchDown = event.touches.length > 0
  if (!touchDown) {
    unsettle()
  }
}

/**
 * One frame's work: the page sizes that came in, with the correction for those above the
 * viewport set in this same frame so what the reader sees does not move, then the current page
 * and the pages' own work.
 */
async function update() {
  frame = 0
  const column = layout.value
  const el = pane.value
  if (!column || !el || pinch.value) {
    return
  }
  const before = el.scrollTop
  const correction = pages.applySizes(column, before, touchDown || touchScrolled)
  if (correction !== null) {
    triggerRef(layout)
    await nextTick()
    if (correction !== 0) {
      el.scrollTop = before + correction
    }
  }
  if (landing && column.measured[landing.index]) {
    el.scrollTop = destinationOffset(column, landing.index, landing.fraction)
    landing = null
  }
  const top = el.scrollTop
  const height = el.clientHeight
  current.value = currentPage(column, top, height)
  pages.sync(column, top, height)
}

// --- jumps and links --------------------------------------------------------------

/** An internal link's destination on a page whose size is not read yet. */
let landing: { index: number; fraction: number } | null = null

/** An internal link or a named action: its page and the destination's top, the zoom kept, and
 *  focus on the target page. */
function onNavigate(target: PdfDestination) {
  const column = layout.value
  const el = pane.value
  if (!column || !el) {
    return
  }
  const index = Math.min(column.count, Math.max(1, target.page)) - 1
  el.scrollTop = destinationOffset(column, index, target.fraction)
  // A page still at its guessed size is landed on again once its own size is read, or a
  // destination part-way down it would be off by the difference.
  landing = column.measured[index] || target.fraction === null ? null : { index, fraction: target.fraction }
  pages.focusSection(index)
}

/** The page field's jump: after Enter focus goes to the page; after leaving the field it stays
 *  where the reader put it. */
function onJump(page: number, by: 'enter' | 'leave') {
  const column = layout.value
  const el = pane.value
  if (!column || !el) {
    return
  }
  landing = null
  el.scrollTop = jumpOffset(column, page - 1)
  if (by === 'enter') {
    pages.focusSection(page - 1)
  }
}

// --- zoom -------------------------------------------------------------------------

function paneCentre() {
  return { x: paneSize.value.width / 2, y: paneSize.value.height / 2 }
}

function paneAnchor(point: GesturePoint) {
  const box = pane.value!.getBoundingClientRect()
  return { x: point.clientX - box.left, y: point.clientY - box.top }
}

/**
 * Commits a zoom: the column is laid out again at the new scale (and the platform's spacing),
 * and the pane scrolls so the point under `anchor` (pane client px) stays put. Canvases are
 * drawn again from the next frame; until then the old ones stretch with their boxes.
 */
function applyZoom(next: ZoomState, anchor: { x: number; y: number }) {
  const before = layout.value
  const el = pane.value
  zoom.value = next
  if (!before || !el) {
    return
  }
  const spacing = props.phone ? PHONE_SPACING : DESKTOP_SPACING
  if (next.scale === before.scale && spacing === before.spacing) {
    // A pinch that ends at a limit changes nothing, but the frames it held back are still owed.
    schedule()
    return
  }
  const after = scaleLayout(before, next.scale, spacing)
  const scroll = anchoredScroll(before, after, { left: el.scrollLeft, top: el.scrollTop }, anchor, paneSize.value)
  layout.value = after
  void nextTick(() => {
    el.scrollLeft = scroll.left
    el.scrollTop = scroll.top
    schedule()
  })
}

/** A key's or a pinch's zoom, said once: the new level, or *Fit width*. */
function announceZoom(next: ZoomState) {
  const percent = new Intl.NumberFormat(locale.value, { style: 'percent' }).format(percentOf(next.scale) / 100)
  announce(next.mode === 'fit-width' ? t('viewer.pdf.fitWidth') : t('viewer.pdf.zoomLevel', { percent }))
}

function zoomTo(target: ZoomTarget) {
  const measured = context()
  if (measured) {
    applyZoom(resolveZoom(target, measured), paneCentre())
  }
}

function zoomStep(direction: 'in' | 'out') {
  const measured = context()
  if (measured) {
    applyZoom(stepZoom(zoom.value, direction, measured), paneCentre())
  }
}

/**
 * ⌘+, ⌘− and ⌘0 (Ctrl elsewhere), taken from the browser so the web page itself never zooms,
 * but only while this viewer's dialog is the topmost one, so a dialog opened over it keeps its
 * keys. In the capture phase, as the result viewer's own keys, ahead of the dialog's handler.
 */
function onDocumentKey(event: KeyboardEvent) {
  const own = root.value?.closest('[aria-modal="true"]')
  const dialogs = document.querySelectorAll('[aria-modal="true"]')
  const key = own && dialogs[dialogs.length - 1] === own ? zoomKey(event, apple) : null
  if (!key) {
    return
  }
  event.preventDefault()
  const measured = context()
  if (!measured) {
    return
  }
  const next = key === 'reset' ? resolveZoom({ mode: 'custom', scale: 1 }, measured) : stepZoom(zoom.value, key, measured)
  applyZoom(next, paneCentre())
  announceZoom(next)
}

/** The pinch in progress: the zoom it started from, its anchor in the pane and the column point
 *  under it, and its ratio so far. */
let pinchFrom: { zoom: ZoomState; anchor: { x: number; y: number }; origin: { x: number; y: number }; ratio: number } | null =
  null

const gestures = {
  start(point: GesturePoint) {
    const el = pane.value
    if (!context() || !el) {
      return
    }
    const anchor = paneAnchor(point)
    pinchFrom = { zoom: zoom.value, anchor, origin: { x: el.scrollLeft + anchor.x, y: el.scrollTop + anchor.y }, ratio: 1 }
  },
  // Only a CSS transform moves during the pinch; the zoom commits when it ends.
  change({ ratio }: { ratio: number }) {
    const measured = context()
    if (!pinchFrom || !measured) {
      return
    }
    pinchFrom.ratio = ratio
    pinch.value = {
      scale: zoomBy(pinchFrom.zoom, ratio, measured).scale / pinchFrom.zoom.scale,
      originX: pinchFrom.origin.x,
      originY: pinchFrom.origin.y,
    }
  },
  end() {
    const from = pinchFrom
    const measured = context()
    pinchFrom = null
    pinch.value = null
    if (!from || !measured || from.ratio === 1) {
      schedule()
      return
    }
    const next = zoomBy(from.zoom, from.ratio, measured)
    applyZoom(next, from.anchor)
    announceZoom(next)
  },
  doubleTap(point: GesturePoint) {
    const measured = context()
    if (props.phone && measured) {
      applyZoom(doubleTapZoom(zoom.value, measured), paneAnchor(point))
    }
  },
}

/** A resize refits a fit mode, and keeps a custom scale inside a phone's moving limits. */
function onResize() {
  const before = paneSize.value
  readPane()
  if (paneSize.value === before) {
    return
  }
  const measured = context()
  if (measured) {
    applyZoom(refitZoom(zoom.value, measured), { x: 0, y: 0 })
  }
  schedule()
}

// A phone and a desktop space the column differently, and a phone's limits are its own.
watch(
  () => props.phone,
  () => {
    const measured = context()
    if (measured) {
      applyZoom(refitZoom(zoom.value, measured), paneCentre())
    }
  },
)

watch(
  () => props.url,
  () => void start(),
)

// --- focus ------------------------------------------------------------------------

/** Tab, ⇧Tab and Safari's ⌥Tab move focus; with ⌘ or Ctrl held, Tab switches apps or tabs. */
function onTabPress(event: KeyboardEvent) {
  if (event.key === 'Tab' && !event.metaKey && !event.ctrlKey) {
    keyboardFocus.value = true
  }
}

function onPointerPress() {
  keyboardFocus.value = false
}

onMounted(() => {
  const el = pane.value!
  // The pane is the dialog's first field, so the dialog focuses it on open; on a switch back
  // from Markdown it takes focus itself, unless the reader has put it somewhere already.
  const active = document.activeElement
  if (!active || active === document.body || active === root.value?.closest('[aria-modal="true"]')) {
    el.focus({ preventScroll: true })
  }
  readPane()
  observer = new ResizeObserver(onResize)
  observer.observe(el)
  detachPinch = attachPinch(el, gestures)
  document.addEventListener('keydown', onDocumentKey, true)
  window.addEventListener('keydown', onTabPress, true)
  window.addEventListener('keyup', onTabPress, true)
  window.addEventListener('pointerdown', onPointerPress, true)
  void start()
})

onBeforeUnmount(() => {
  starts++
  cancelAnimationFrame(frame)
  clearTimeout(settleTimer)
  observer?.disconnect()
  detachPinch?.()
  document.removeEventListener('keydown', onDocumentKey, true)
  window.removeEventListener('keydown', onTabPress, true)
  window.removeEventListener('keyup', onTabPress, true)
  window.removeEventListener('pointerdown', onPointerPress, true)
})

defineExpose({
  zoomIn: () => zoomStep('in'),
  zoomOut: () => zoomStep('out'),
  zoomTo,
})
</script>

<template>
  <div ref="root" class="viewer">
    <div v-if="busy" class="progress" role="progressbar" :aria-label="t('viewer.pdf.opening')">
      <span class="progress-bar" />
    </div>
    <!-- After the header and before the pages, so it is the next Tab stop after More. -->
    <PdfPageIndicator
      v-if="ready && layout"
      ref="indicator"
      :page="current + 1"
      :total="layout.count"
      :phone="phone"
      @jump="onJump"
    />
    <main
      ref="pane"
      class="pane"
      :class="{ 'keyboard-focus': keyboardFocus, centred: showsState }"
      :tabindex="showsState ? undefined : 0"
      :aria-label="name"
      :aria-busy="busy ? 'true' : undefined"
      :style="paneStyle"
      @scroll.passive="onScroll"
      @touchstart.passive="onTouchStart"
      @touchend.passive="onTouchEnd"
      @touchcancel.passive="onTouchEnd"
      @focusout="schedule"
    >
      <PdfPasswordPrompt v-if="status === 'password' && prompt" :prompt="prompt" @submit="submitPassword" />
      <PdfDocumentError
        v-else-if="status === 'failed' && failure"
        ref="errorState"
        :kind="failure"
        :reason="causeText(failure)"
        :url="url"
        :download="download"
        :download-size="downloadSize"
        @retry="retryDocument"
        @close="emit('close')"
      />
      <div v-else-if="layout" class="column" :style="columnStyle">
        <section
          v-for="page in layout.count"
          :key="page"
          :ref="(el) => pages.setSection(page - 1, el)"
          class="page"
          tabindex="-1"
          :data-page="page - 1"
          :aria-label="pageLabel(page - 1)"
          :style="pageStyle(page - 1)"
        >
          <!-- The content layer: light whatever the theme, since it is white paper; the engine
               draws its canvases, text and links in here. -->
          <div :ref="(el) => pages.setHost(page - 1, el)" class="content pdf-page-content">
            <UiSpinner v-if="slow.has(page - 1)" class="spinner" :size="24" />
            <PdfPageFailure
              v-if="failures.has(page - 1)"
              :page="page"
              :body="causeText(failures.get(page - 1)!.kind)"
              :retryable="failures.get(page - 1)!.kind === 'page' || failures.get(page - 1)!.kind === 'network'"
              :retrying="failures.get(page - 1)!.retrying"
              @retry="pages.retry(page - 1)"
            />
          </div>
        </section>
      </div>
    </main>
    <p class="sr-only" role="status" aria-live="polite">{{ liveText }}</p>
  </div>
</template>

<style scoped>
.viewer {
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

/* The desk behind the pages. Its own scrolling only: a pan that reaches an edge never chains
   into Safari's back or forward swipe, the browser's scroll anchoring never moves it (the size
   correction does that, once), and a pinch or double tap is the viewer's, not the page's. */
.pane {
  height: 100%;
  overflow: auto;
  overscroll-behavior: contain;
  overflow-anchor: none;
  touch-action: pan-x pan-y;
  background: var(--desk);
}

/* Its ring is the keyboard's, set by `keyboardFocus` rather than `:focus-visible`, which a key
   pressed after a click turns on (docs/web.md § Files), and drawn inside its edge, which the
   result viewer clips. */
.pane:focus {
  outline: none;
}

.pane.keyboard-focus:focus {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

/* The password prompt and a document error, centred on the desk. */
.pane.centred {
  display: grid;
  place-items: center;
  padding: var(--space-4);
}

.column {
  position: relative;
}

/* White paper with a hairline and no shadow; its border follows the theme, its content layer
   does not. The layers are laid out with these (`--scale-factor` comes from the pane). */
.page {
  position: absolute;
  border: 1px solid var(--line-strong);
  --total-scale-factor: calc(var(--scale-factor) * var(--user-unit));
  --scale-round-x: 1px;
  --scale-round-y: 1px;
}

.page:focus {
  outline: none;
}

/* Sized by the page box, never by what it holds: pdf-layers.css makes it a size container. */
.content {
  position: absolute;
  inset: 0;
  overflow: hidden;
  color-scheme: light;
  background: var(--paper);
  color: var(--ink);
}

/* Centred across the part of the page a pane can show from its top, so a tall page's spinner
   is in view. */
.spinner {
  position: absolute;
  top: min(50%, calc(var(--pane-height) / 2 - var(--space-4)));
  left: 50%;
  z-index: 2;
  margin: -12px 0 0 -12px;
  color: var(--muted);
}

/* An indeterminate bar under the header while the document opens; static under reduced
   motion. */
.progress {
  position: absolute;
  top: 0;
  right: 0;
  left: 0;
  z-index: 3;
  height: 2px;
  overflow: hidden;
  pointer-events: none;
}

.progress-bar {
  position: absolute;
  top: 0;
  left: 0;
  width: 28%;
  height: 2px;
  border-radius: 2px;
  background: var(--accent);
  animation: sweep 1.2s var(--ease) infinite alternate;
}

@keyframes sweep {
  to {
    transform: translateX(257%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .progress-bar {
    left: 34%;
    animation: none;
  }
}
</style>
