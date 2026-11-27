<script setup lang="ts">
/**
 * The Original view of an image (docs/media-viewer.md § The image view): the scroll pane on the
 * desk, the stage it scrolls, and the image box on it. The result viewer owns the header around
 * it and drives the zoom through the exposed `zoomIn`, `zoomOut` and `zoomTo`, as it drives the
 * PDF viewer's; this reports `state` on every change.
 *
 * The `<img>` loads hidden at 0×0, so it takes no room until it shows, and stays the same element
 * from loading to shown: the file is served `no-store`, and a new element could fetch it again.
 * An `<img>` that fails says nothing of why, so the failure is classed by a one-byte read of the
 * same address (`probeFileStatus`, `mediaFailure`). The zoom and geometry are
 * `lib/media/image-zoom.ts`'; the gestures are the PDF viewer's `attachPinch`.
 */
import { ApiRequestError, downloadFile, probeFileStatus } from '~/lib/api'
import { mediaFailure, type MediaFailureKind, type ProbeOutcome } from '~/lib/media/failure'
import {
  canStepImageZoom,
  doubleClickZoom,
  doubleTapImageZoom,
  fitScale,
  imageBox,
  imageGutter,
  imagePlacement,
  naturalSize,
  openingImageZoom,
  pointAt,
  refitImageZoom,
  resolveImageZoom,
  scrollForPoint,
  stepImageZoom,
  zoomImageBy,
  type ImagePoint,
  type ImageReturn,
  type ImageViewerState,
  type ImageZoomContext,
  type ImageZoomState,
  type ImageZoomTarget,
  type Placement,
} from '~/lib/media/image-zoom'
import { attachPinch, type GesturePoint } from '~/lib/pdf/gestures'
import type { ScrollPoint, Size } from '~/lib/pdf/layout'
import { percentOf } from '~/lib/pdf/zoom'
import { zoomKey } from '~/lib/shortcuts'

const props = defineProps<{
  /** The file's inline bytes, same origin. */
  url: string
  /** The filename: the pane's name, the image's `alt` and the opening announcement's. */
  name: string
  /** The attachment URL, for an image that cannot be shown's *Download*. */
  download: string
  /** The file's size, formatted, for *Download ({size})*; null for plain *Download*. */
  downloadSize: string | null
  phone: boolean
  /** Where the reader was when this view last unmounted. */
  initial: ImageReturn | null
}>()

const emit = defineEmits<{ state: [state: ImageViewerState]; close: [] }>()

const { t, locale } = useI18n()
const { apple } = useShortcuts()
const { liveText, announce } = useStatusNote()
const apiError = useApiError()

const root = ref<HTMLElement | null>(null)
const pane = ref<HTMLElement | null>(null)
const sheet = ref<{ focusHeading(): void } | null>(null)

/** `probing` is a failed load whose cause is being read; the bar stays meanwhile. */
const status = ref<'loading' | 'probing' | 'shown' | 'failed'>('loading')
const failure = ref<MediaFailureKind | null>(null)
/** The `<img>`'s key: a retry or a new address mounts a new one. */
const attempt = ref(0)
/** The image's natural size, once it has loaded. */
const natural = shallowRef<Size | null>(null)
/** The pane's client box, which every zoom is measured in. */
const paneSize = shallowRef<Size>({ width: 0, height: 0 })
const zoom = shallowRef<ImageZoomState>({ mode: 'fit', scale: 1 })
/** The pane's scroll as last read, for the state's centre. */
const scroll = shallowRef<ScrollPoint>({ left: 0, top: 0 })
/** During a pinch, the transform the stage shows until the zoom commits. */
const pinch = shallowRef<{ scale: number; originX: number; originY: number } | null>(null)
const dragging = ref(false)
/** The pane's ring follows the keyboard, not `:focus-visible` (docs/web.md § Rules). */
const keyboardFocus = useKeyboardFocus()

/** The window listeners and the gestures, for the view's life. */
const lifetime = new AbortController()
/** The current load and its probe: a retry, a new address and unmounting abort it. */
let loading = new AbortController()
let observer: ResizeObserver | null = null
let frame = 0
let lastPointerType = ''
let drag: { pointer: number; x: number; y: number; left: number; top: number } | null = null

const busy = computed(() => status.value === 'loading' || status.value === 'probing')

/** What every zoom is measured from; null until the image shows. */
function context(): ImageZoomContext | null {
  return status.value === 'shown' && natural.value
    ? { phone: props.phone, pane: paneSize.value, image: natural.value }
    : null
}

const placement = computed<Placement | null>(() => {
  const measured = context()
  return measured
    ? imagePlacement(imageBox(measured.image, zoom.value.scale), measured.pane, imageGutter(measured.phone))
    : null
})

/** A drag pans only a box larger than the pane on either axis. */
const pannable = computed(() => {
  const placed = placement.value
  return placed !== null && (placed.stage.width > paneSize.value.width || placed.stage.height > paneSize.value.height)
})

const viewerState = computed<ImageViewerState>(() => {
  const measured = context()
  const placed = placement.value
  return {
    ready: measured !== null,
    failed: status.value === 'failed',
    zoom: zoom.value,
    fit: measured ? fitScale(measured) : null,
    canZoomIn: measured ? canStepImageZoom(zoom.value, 'in', measured) : false,
    canZoomOut: measured ? canStepImageZoom(zoom.value, 'out', measured) : false,
    size: measured ? measured.image : null,
    centre: placed ? pointAt(placed, scroll.value, paneCentre()) : null,
  }
})

// The result viewer hears only what changed.
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

/** Until the image shows the stage is 0×0, so nothing gives the pane scrollbars. */
const stageStyle = computed(() => {
  const placed = placement.value
  if (!placed) {
    return { width: '0px', height: '0px' }
  }
  const shown = pinch.value
  return {
    width: `${placed.stage.width}px`,
    height: `${placed.stage.height}px`,
    transform: shown ? `scale(${shown.scale})` : undefined,
    transformOrigin: shown ? `${shown.originX}px ${shown.originY}px` : undefined,
  }
})

const imageStyle = computed(() => {
  const placed = placement.value
  return placed
    ? {
        left: `${placed.left}px`,
        top: `${placed.top}px`,
        width: `${placed.box.width}px`,
        height: `${placed.box.height}px`,
      }
    : undefined
})

/** A failure's cause in the reader's words: under the sheet's title, and in its announcement. */
function causeText(kind: MediaFailureKind): string {
  switch (kind) {
    case 'corrupt':
      return t('viewer.image.error.unsupported')
    case 'network':
      return t('errors.network')
    case 'signed-out':
      return t('errors.signedOut')
    case 'gone':
      return t('viewer.pdf.error.gone')
  }
}

// --- loading ----------------------------------------------------------------------

/** A retry or a new address: the pending probe is dropped and a new `<img>` loads. */
function restart() {
  loading.abort()
  loading = new AbortController()
  status.value = 'loading'
  failure.value = null
  natural.value = null
  pinch.value = null
  zoom.value = { mode: 'fit', scale: 1 }
  attempt.value++
  announce(t('viewer.pdf.announce.opening', { name: props.name }))
}

/** The image has loaded: shown whole at the opening zoom once its pixels are decoded, so neither
 *  a large JPEG nor the checkerboard flashes first, and where the reader was if this is the way
 *  back from Markdown. */
async function onLoad(event: Event) {
  const img = event.currentTarget as HTMLImageElement
  const { signal } = loading
  // An `<img>` a new address replaced still finishes loading, detached.
  if (!img.isConnected) {
    return
  }
  const size = naturalSize(img.naturalWidth, img.naturalHeight)
  // Safari may refuse to decode a very large image ahead; it shows anyway.
  await img.decode().catch(() => {})
  if (signal.aborted) {
    return
  }
  zoom.value = openingImageZoom({ phone: props.phone, pane: paneSize.value, image: size }, props.initial?.zoom ?? null)
  natural.value = size
  status.value = 'shown'
  await nextTick()
  const el = pane.value
  const placed = placement.value
  if (props.initial && el && placed) {
    el.scrollTo(scrollForPoint(props.initial.centre, paneCentre(), placed, paneSize.value))
  }
  readScroll()
  announce(t('viewer.media.opened'))
}

/** The `<img>` failed: one byte of the same address says why. */
async function onError(event: Event) {
  const { signal } = loading
  if (!(event.currentTarget as HTMLImageElement).isConnected) {
    return
  }
  status.value = 'probing'
  let outcome: ProbeOutcome
  try {
    outcome = { status: await probeFileStatus(props.url, signal) }
  } catch (error) {
    outcome = { error }
  }
  const kind = mediaFailure(outcome, !navigator.onLine, { code: null, afterMetadata: false })
  if (kind === null || signal.aborted) {
    return
  }
  await failed(kind)
}

/** Announced once, and focus carried to the heading only if it was in what went away (or had
 *  fallen to the body); on a header control it stays. */
async function failed(kind: MediaFailureKind) {
  const active = document.activeElement
  const lost = !active || active === document.body || (root.value?.contains(active) ?? false)
  failure.value = kind
  status.value = 'failed'
  announce(t('viewer.pdf.announce.failed', { title: t('viewer.image.error.title'), reason: causeText(kind) }))
  if (kind === 'signed-out') {
    // An ended session is ended as everywhere else in the app (docs/web.md § Rules).
    void apiError.resolve(new ApiRequestError(401, 'auth', 'Not signed in'))
  }
  await nextTick()
  if (lost) {
    sheet.value?.focusHeading()
  }
}

async function retry() {
  restart()
  await nextTick()
  // The retry button went with the sheet; the pane takes its place.
  if (document.activeElement === document.body) {
    pane.value?.focus({ preventScroll: true })
  }
}

// --- the pane ---------------------------------------------------------------------

function readScroll() {
  const el = pane.value
  if (el) {
    scroll.value = { left: el.scrollLeft, top: el.scrollTop }
  }
}

function onScroll() {
  frame ||= requestAnimationFrame(() => {
    frame = 0
    readScroll()
  })
}

function paneCentre() {
  return { x: paneSize.value.width / 2, y: paneSize.value.height / 2 }
}

function paneAnchor(point: GesturePoint) {
  const box = pane.value!.getBoundingClientRect()
  return { x: point.clientX - box.left, y: point.clientY - box.top }
}

/** The image point under `anchor` (pane client px) as the box is drawn now. */
function pointUnder(anchor: { x: number; y: number }): ImagePoint {
  const el = pane.value!
  return pointAt(placement.value!, { left: el.scrollLeft, top: el.scrollTop }, anchor)
}

/**
 * Commits a zoom: the box takes its new size and the pane scrolls so `point` of the image is
 * under `anchor` (pane client px) again. Only call it while the image shows.
 */
function applyZoom(next: ImageZoomState, anchor: { x: number; y: number }, point = pointUnder(anchor)) {
  zoom.value = next
  void nextTick(() => {
    const el = pane.value
    const placed = placement.value
    if (el && placed) {
      el.scrollTo(scrollForPoint(point, anchor, placed, paneSize.value))
      readScroll()
    }
  })
}

/** A key's or a pinch's zoom, said once: the new level, or *Fit to window*. */
function announceZoom(next: ImageZoomState) {
  const percent = new Intl.NumberFormat(locale.value, { style: 'percent' }).format(percentOf(next.scale) / 100)
  announce(next.mode === 'fit' ? t('viewer.image.fit') : t('viewer.pdf.zoomLevel', { percent }))
}

function zoomTo(target: ImageZoomTarget) {
  const measured = context()
  if (measured) {
    applyZoom(resolveImageZoom(target, measured), paneCentre())
  }
}

function zoomStep(direction: 'in' | 'out') {
  const measured = context()
  if (measured) {
    applyZoom(stepImageZoom(zoom.value, direction, measured), paneCentre())
  }
}

/**
 * ⌘+, ⌘− and ⌘0 (Ctrl elsewhere), taken from the browser in every state, loading and failed
 * included, so the web page itself never zooms while an image is open; but only while this
 * view's dialog is the topmost one, so a dialog opened over it keeps its keys.
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
  const next =
    key === 'reset' ? resolveImageZoom({ mode: 'custom', scale: 1 }, measured) : stepImageZoom(zoom.value, key, measured)
  applyZoom(next, paneCentre())
  announceZoom(next)
}

/** The pinch in progress: the zoom it started from, its anchor in the pane and the stage point
 *  under it, and its ratio so far. */
let pinchFrom: { zoom: ImageZoomState; anchor: { x: number; y: number }; origin: { x: number; y: number }; ratio: number } | null =
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
  // Only a CSS transform moves during the pinch; the zoom commits when it ends, so the box
  // takes its real size and WebKit draws it sharp again.
  change({ ratio }: { ratio: number }) {
    const measured = context()
    if (!pinchFrom || !measured) {
      return
    }
    pinchFrom.ratio = ratio
    pinch.value = {
      scale: zoomImageBy(pinchFrom.zoom, ratio, measured).scale / pinchFrom.zoom.scale,
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
      return
    }
    const next = zoomImageBy(from.zoom, from.ratio, measured)
    applyZoom(next, from.anchor)
    announceZoom(next)
  },
  // A phone's double tap goes to 2 × fit; on a tablet or a touch laptop it is a double click.
  doubleTap(point: GesturePoint) {
    const measured = context()
    if (measured) {
      const next = props.phone ? doubleTapImageZoom(zoom.value, measured) : doubleClickZoom(zoom.value, measured)
      applyZoom(next, paneAnchor(point))
    }
  },
}

/** A touch double tap fires `dblclick` too, and `doubleTap` has taken it already. */
function onDoubleClick(event: MouseEvent) {
  const measured = context()
  if (lastPointerType !== 'touch' && measured) {
    applyZoom(doubleClickZoom(zoom.value, measured), paneAnchor(event))
  }
}

/** A mouse or a pen drags a box larger than the pane; a finger scrolls the pane itself. */
function onPointerDown(event: PointerEvent) {
  lastPointerType = event.pointerType
  const el = pane.value
  if (!el || event.pointerType === 'touch' || event.button !== 0 || !pannable.value) {
    return
  }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  drag = { pointer: event.pointerId, x: event.clientX, y: event.clientY, left: el.scrollLeft, top: el.scrollTop }
  dragging.value = true
}

function onPointerMove(event: PointerEvent) {
  const el = pane.value
  if (!drag || !el || event.pointerId !== drag.pointer) {
    return
  }
  el.scrollLeft = drag.left - (event.clientX - drag.x)
  el.scrollTop = drag.top - (event.clientY - drag.y)
}

/** The capture goes by itself on `pointerup` and `pointercancel`. */
function onPointerUp(event: PointerEvent) {
  if (drag?.pointer === event.pointerId) {
    drag = null
    dragging.value = false
  }
}

/**
 * A resize refits a fit, and keeps a custom scale inside the new range, the point at the
 * pane's centre kept there. The size is the observer's content box (less any scrollbar),
 * rounded down: `clientWidth` rounds to the nearest pixel, so under a browser zoom a pane
 * 744.6px tall reads 745 and a fit to it would scroll by a fraction of a pixel.
 */
function onResize(entries: ResizeObserverEntry[]) {
  const box = entries[0]!.contentBoxSize[0]!
  const size = { width: Math.floor(box.inlineSize), height: Math.floor(box.blockSize) }
  if (size.width === paneSize.value.width && size.height === paneSize.value.height) {
    return
  }
  // Read on the box as drawn, before the new pane size moves it.
  const point = context() ? pointUnder(paneCentre()) : null
  paneSize.value = size
  const measured = context()
  if (measured && point) {
    applyZoom(refitImageZoom(zoom.value, measured), paneCentre(), point)
  }
}

// A phone keeps no gutter, and its range is its own.
watch(
  () => props.phone,
  (phone) => {
    const measured = context()
    if (!measured) {
      return
    }
    // The box as drawn had the other layout's gutter.
    const drawn = imagePlacement(imageBox(measured.image, zoom.value.scale), measured.pane, imageGutter(!phone))
    const el = pane.value!
    const point = pointAt(drawn, { left: el.scrollLeft, top: el.scrollTop }, paneCentre())
    applyZoom(refitImageZoom(zoom.value, measured), paneCentre(), point)
  },
)

watch(
  () => props.url,
  () => restart(),
)

onMounted(() => {
  const el = pane.value!
  // The pane is the dialog's first field, so the dialog focuses it on open; on a switch back
  // from Markdown it takes focus itself, unless the reader has put it somewhere already.
  const active = document.activeElement
  if (!active || active === document.body || active === root.value?.closest('[aria-modal="true"]')) {
    el.focus({ preventScroll: true })
  }
  // Until the observer first reports it (`onResize`).
  paneSize.value = { width: el.clientWidth, height: el.clientHeight }
  observer = new ResizeObserver(onResize)
  observer.observe(el)
  attachPinch(el, gestures, lifetime.signal)
  document.addEventListener('keydown', onDocumentKey, { capture: true, signal: lifetime.signal })
  announce(t('viewer.pdf.announce.opening', { name: props.name }))
})

onBeforeUnmount(() => {
  lifetime.abort()
  loading.abort()
  cancelAnimationFrame(frame)
  observer?.disconnect()
})

defineExpose({
  zoomIn: () => zoomStep('in'),
  zoomOut: () => zoomStep('out'),
  zoomTo,
})
</script>

<template>
  <div ref="root" class="viewer keyboard-frame">
    <ViewerLoading :busy="busy" spinner />
    <main
      ref="pane"
      class="pane keyboard-pane"
      :class="{ 'keyboard-focus': keyboardFocus, centred: status === 'failed', pannable, dragging }"
      :tabindex="status === 'failed' ? undefined : 0"
      :aria-label="name"
      :aria-busy="busy ? 'true' : undefined"
      @scroll.passive="onScroll"
    >
      <ViewerStateSheet
        v-if="status === 'failed' && failure"
        ref="sheet"
        glyph="file-x"
        :title="t('viewer.image.error.title')"
        :body="causeText(failure)"
      >
        <template v-if="failure !== 'signed-out'" #default>
          <UiButton v-if="failure === 'corrupt'" variant="primary" @click="downloadFile(download)">
            {{ downloadSize ? t('viewer.file.downloadSize', { size: downloadSize }) : t('viewer.file.download') }}
          </UiButton>
          <UiButton v-else-if="failure === 'network'" variant="primary" @click="retry">{{ t('common.retry') }}</UiButton>
          <UiButton v-else variant="primary" @click="emit('close')">{{ t('viewer.pdf.error.backToFiles') }}</UiButton>
        </template>
      </ViewerStateSheet>
      <div
        v-else
        class="stage"
        :style="stageStyle"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointercancel="onPointerUp"
        @dblclick="onDoubleClick"
      >
        <img
          :key="attempt"
          class="image"
          :class="status === 'shown' ? 'shown' : 'loading'"
          :style="imageStyle"
          :src="url"
          :alt="name"
          draggable="false"
          @load="onLoad"
          @error="onError"
        >
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

/* The desk behind the image. Its own scrolling only: a pan that reaches an edge never chains
   into Safari's back or forward swipe, the browser's scroll anchoring never moves it (a zoom
   sets the scroll itself), and a pinch or double tap is the view's, not the page's. */
.pane {
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  overflow-anchor: none;
  touch-action: pan-x pan-y;
  background: var(--desk);
}

/* The failure sheet, centred on the desk. */
.pane.centred {
  display: grid;
  place-items: center;
  padding: var(--space-4);
}

.stage {
  position: relative;
}

.pannable .stage {
  cursor: grab;
}

.dragging .stage {
  cursor: grabbing;
  user-select: none;
}

/* No radius, no border and no shadow: the file's own pixels. */
.image {
  position: absolute;
  display: block;
}

/* Loading: it loads at once and takes no room. */
.image.loading {
  width: 0;
  height: 0;
  visibility: hidden;
}

/* A light checkerboard behind transparent pixels, inside the image box only, the same in both
   themes. */
.image.shown {
  background-color: var(--checker-base);
  background-image: conic-gradient(
    var(--checker-square) 25%,
    transparent 0 50%,
    var(--checker-square) 0 75%,
    transparent 0
  );
  background-size: calc(var(--checker-size) * 2) calc(var(--checker-size) * 2);
}
</style>
