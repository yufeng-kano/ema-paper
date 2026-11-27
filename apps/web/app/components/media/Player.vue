<script setup lang="ts">
/**
 * The pane of a video or an audio file (docs/media-viewer.md § The player): the browser's own
 * controls on the desk, its keys, its failure sheet and its live region. The file viewer owns
 * the header around it; this reports `state` for More's facts.
 *
 * Nothing is shown until the metadata has come: the element loads hidden and takes no room, so
 * a portrait video never shows a landscape box first. A video then takes a pixel box of its
 * aspect ratio (`videoBox`), which the pane's grid centres and never sizes. A video that comes
 * with no picture is asked whether it has a video track, by `videoTracks` or by reading its own
 * track list (`scanIsoTracks`): with one it cannot be played here, without one it takes the
 * audio layout. The one exception to "nothing before the size" is a browser that reads nothing
 * until a play, which after `NO_PRELOAD_MS` gets the controls in a 16:9 box.
 *
 * One element reads the file at a time. The first is the one the pane mounts with; a Retry, a
 * new file and the audio layout's `<audio>` replace it, and the one replaced is released so it
 * stops reading. Vue leaves a removed element's listeners on it, so every handler acts only for
 * the current element.
 */
import { ApiRequestError, downloadFile, probeFileStatus, readFileBytes } from '~/lib/api'
import { mediaFailure, type MediaFailureKind, type ProbeOutcome } from '~/lib/media/failure'
import {
  FALLBACK_ASPECT,
  NO_PRELOAD_MS,
  seekTime,
  videoBox,
  type MediaPlayerState,
  type MediaStatus,
} from '~/lib/media/player'
import { scanIsoTracks, TRACK_HEAD_BYTES, TRACK_READS_MAX } from '~/lib/media/tracks'
import { DESKTOP_SPACING, PHONE_SPACING, type Size } from '~/lib/pdf/layout'
import { MEDIA_KEYSHORTCUTS, mediaKey } from '~/lib/shortcuts'

const props = defineProps<{
  /** The file's inline bytes, same origin. */
  url: string
  /** The filename: the pane's name and the opening announcement's. */
  name: string
  kind: 'video' | 'audio'
  /** The attachment URL, for an unplayable file's *Download*. */
  download: string
  /** The file's size, formatted, for *Download ({size})*; null for plain *Download*. */
  downloadSize: string | null
  phone: boolean
}>()

const emit = defineEmits<{ state: [state: MediaPlayerState]; close: [] }>()

const { t } = useI18n()
const { liveText, announce } = useStatusNote()
const apiError = useApiError()
/** The pane's ring follows the keyboard, not `:focus-visible` (docs/web.md § Rules). */
const keyboardFocus = useKeyboardFocus()

const root = ref<HTMLElement | null>(null)
const pane = ref<HTMLElement | null>(null)
const media = ref<HTMLMediaElement | null>(null)
const sheet = ref<{ focusHeading(): void } | null>(null)

/**
 * `loading` until the metadata (the no-preload wait included), `checking` while a video with
 * no picture is asked for its tracks, `unsized` once the no-preload wait ran out, `ready` once
 * shown, `probing` while an error's cause is read, then `failed`.
 */
const status = ref<'loading' | 'checking' | 'unsized' | 'ready' | 'probing' | 'failed'>('loading')
/** A video with no video track is drawn as an audio file. */
const layout = ref<'video' | 'audio'>(props.kind)
/** The element's key: a Retry or a new file mounts a new one, which opens the file again. */
const attempt = ref(0)
/** The element is shown; it stays shown while an error after it is probed. */
const revealed = ref(false)
const failure = ref<MediaFailureKind | null>(null)
const dimensions = shallowRef<Size | null>(null)
const duration = ref<number | null>(null)
/** The pane's client box, which the video box is measured in. */
const paneSize = shallowRef<Size>({ width: 0, height: 0 })

/** Bumped by every replacement and by unmounting: async work of an earlier one is dropped. */
let starts = 0
/** This attempt's reads (the track scan, the probe): a replacement and unmounting abort them. */
let controller = new AbortController()
let waitTimer: ReturnType<typeof setTimeout> | undefined
/** This attempt's element had reached `loadedmetadata`. */
let metadata = false
/** Where an error after the metadata stopped the file, for its Retry to seek back to. */
let resumeAt: number | null = null
/** The pane took this Space's keydown, so it takes its keyup too. */
let spaceTaken = false
let observer: ResizeObserver | null = null

const busy = computed(() => status.value === 'loading' || status.value === 'checking' || status.value === 'probing')
const shown = computed(() => revealed.value && status.value !== 'failed')
const failed = computed(() => status.value === 'failed' && failure.value !== null)

const box = computed(() => {
  const size = dimensions.value ?? (status.value === 'unsized' ? FALLBACK_ASPECT : null)
  const gutter = props.phone ? PHONE_SPACING.gutter : DESKTOP_SPACING.gutter
  return size ? videoBox(size, paneSize.value, gutter) : null
})

const videoStyle = computed(() =>
  shown.value && box.value ? { width: `${box.value.width}px`, height: `${box.value.height}px` } : undefined,
)

const errorTitle = computed(() =>
  layout.value === 'video' ? t('viewer.video.error.title') : t('viewer.audio.error.title'),
)

/** A failure's cause in the reader's words: under the sheet's title, and in its announcement. */
function causeText(kind: MediaFailureKind): string {
  switch (kind) {
    case 'corrupt':
      return layout.value === 'video' ? t('viewer.video.error.unsupported') : t('viewer.audio.error.unsupported')
    case 'network':
      return t('errors.network')
    case 'signed-out':
      return t('errors.signedOut')
    case 'gone':
      return t('viewer.pdf.error.gone')
  }
}

const playerState = computed<MediaPlayerState>(() => {
  const reported: MediaStatus =
    status.value === 'failed'
      ? 'failed'
      : status.value === 'ready' || status.value === 'unsized'
        ? 'ready'
        : 'loading'
  return {
    status: reported,
    dimensions: layout.value === 'video' ? dimensions.value : null,
    duration: duration.value,
  }
})

// The file viewer hears only what changed.
let emitted = ''
watch(
  playerState,
  (state) => {
    const key = JSON.stringify(state)
    if (key !== emitted) {
      emitted = key
      emit('state', state)
    }
  },
  { immediate: true },
)

// --- opening ----------------------------------------------------------------------

/** A Retry or a new file: a new element opens it, and whatever the last one was doing is
 *  dropped. The first element is the pane's own, from its mount. */
function start() {
  starts++
  controller.abort()
  controller = new AbortController()
  clearTimeout(waitTimer)
  status.value = 'loading'
  revealed.value = false
  failure.value = null
  dimensions.value = null
  duration.value = null
  metadata = false
  spaceTaken = false
  attempt.value++
  announce(t('viewer.pdf.announce.opening', { name: props.name }))
}

/**
 * Chrome fires `suspend` at `readyState` 0 before every file's metadata, so a `suspend` only
 * arms the wait; any read, the metadata or an error ends it, and until it runs out nothing on
 * screen changes.
 */
function onSuspend(event: Event) {
  if (event.currentTarget !== media.value) {
    return
  }
  if (status.value === 'loading' && media.value?.readyState === 0) {
    clearTimeout(waitTimer)
    waitTimer = setTimeout(showUnsized, NO_PRELOAD_MS)
  }
}

function onProgress(event: Event) {
  if (event.currentTarget === media.value) {
    clearTimeout(waitTimer)
  }
}

/** The browser waits for a play before it reads anything: the controls show, so it can have one. */
function showUnsized() {
  if (status.value !== 'loading') {
    return
  }
  status.value = 'unsized'
  revealed.value = true
  announce(t('viewer.media.opened'))
}

function onLoadedMetadata(event: Event) {
  const el = media.value
  if (!el || event.currentTarget !== el || (status.value !== 'loading' && status.value !== 'unsized')) {
    return
  }
  clearTimeout(waitTimer)
  metadata = true
  if (layout.value === 'video') {
    const video = el as HTMLVideoElement
    if (video.videoWidth === 0 || video.videoHeight === 0) {
      void checkTracks(video)
      return
    }
    dimensions.value = { width: video.videoWidth, height: video.videoHeight }
  }
  readDuration(el)
  // The fallback already said it opened.
  if (status.value !== 'unsized') {
    announce(t('viewer.media.opened'))
  }
  status.value = 'ready'
  revealed.value = true
  // A Retry after a failure past the metadata opens where it failed, paused.
  if (resumeAt !== null) {
    el.currentTime = resumeAt
    resumeAt = null
  }
}

/** A length the browser learns after the metadata counts too; an infinite one never does. */
function onDurationChange(event: Event) {
  const el = media.value
  if (el && event.currentTarget === el && metadata) {
    readDuration(el)
  }
}

function readDuration(el: HTMLMediaElement) {
  duration.value = Number.isFinite(el.duration) ? el.duration : null
}

/**
 * A video whose metadata came with no picture: sound alone, or a picture this browser cannot
 * decode. WebKit counts its video tracks; elsewhere the file's own track list is read, at most
 * `TRACK_READS_MAX` reads. With a video track it cannot be played here; with none, or when the
 * read cannot tell, it is drawn as the audio file it sounds like.
 */
async function checkTracks(video: HTMLVideoElement) {
  const mine = starts
  status.value = 'checking'
  revealed.value = false
  const tracks = (video as HTMLVideoElement & { videoTracks?: { length: number } }).videoTracks
  const found = tracks ? (tracks.length > 0 ? 'video' : 'no-video') : await scanTracks(controller.signal)
  if (mine !== starts || status.value !== 'checking') {
    return
  }
  if (found === 'video') {
    await fail('corrupt')
    return
  }
  // The `<audio>` that replaces the video opens the same file and reports its own metadata.
  layout.value = 'audio'
  status.value = 'loading'
  metadata = false
}

async function scanTracks(signal: AbortSignal): Promise<'video' | 'no-video' | 'unknown'> {
  let start = 0
  let length = TRACK_HEAD_BYTES
  try {
    for (let reads = 0; reads < TRACK_READS_MAX; reads++) {
      const { bytes, total } = await readFileBytes(props.url, start, length, signal)
      const scan = scanIsoTracks(bytes, start, total)
      if ('done' in scan) {
        return scan.done
      }
      ;({ start, length } = scan.read)
    }
  } catch {
    // A read past the end (416), a failed read or an abort: the file cannot tell.
  }
  return 'unknown'
}

// --- failure ----------------------------------------------------------------------

/** The element failed: its cause is read from one byte of the same address, then narrowed by
 *  the media error's code and whether the metadata had come. */
async function onError(event: Event) {
  const el = media.value
  if (!el || event.currentTarget !== el) {
    return
  }
  const code = el.error?.code ?? null
  // MEDIA_ERR_ABORTED comes only from an abort the page asked for.
  if (code === 1 || status.value === 'probing' || status.value === 'failed') {
    return
  }
  clearTimeout(waitTimer)
  const mine = starts
  const afterMetadata = metadata
  if (afterMetadata) {
    resumeAt = el.currentTime
  }
  status.value = 'probing'
  let outcome: ProbeOutcome
  try {
    outcome = { status: await probeFileStatus(props.url, controller.signal) }
  } catch (error) {
    outcome = { error }
  }
  if (mine !== starts) {
    return
  }
  const kind = mediaFailure(outcome, !navigator.onLine, { code, afterMetadata })
  if (kind) {
    await fail(kind)
  }
}

/** Announced once, and focus carried to the heading only if it was in what went away (or had
 *  fallen to the body); on a header control it stays. */
async function fail(kind: MediaFailureKind) {
  const active = document.activeElement
  const lost = !active || active === document.body || (root.value?.contains(active) ?? false)
  failure.value = kind
  status.value = 'failed'
  announce(t('viewer.pdf.announce.failed', { title: errorTitle.value, reason: causeText(kind) }))
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
  start()
  await nextTick()
  // The retry button went with the sheet; the pane takes its place.
  if (document.activeElement === document.body) {
    pane.value?.focus({ preventScroll: true })
  }
}

// --- keys -------------------------------------------------------------------------

function isSpace(event: KeyboardEvent) {
  return event.key === ' ' || event.code === 'Space'
}

/**
 * The player's keys, in the capture phase so they run before the element and its native
 * controls. Space, F and M are taken wherever focus is inside the pane, and cancelled and
 * stopped there, so each acts exactly once in every browser. The arrows are taken only from
 * the pane itself: once the element holds focus its scrubber or its volume slider may be what
 * they move.
 */
function onKeyDown(event: KeyboardEvent) {
  const el = media.value
  // An audio file has no F: it is left to the page, held or not.
  const action = mediaKey(event, layout.value)
  if (!action || !el || (status.value !== 'ready' && status.value !== 'unsized')) {
    return
  }
  if (action === 'back' || action === 'forward') {
    if (event.target === pane.value) {
      event.preventDefault()
      el.currentTime = seekTime(el.currentTime, action === 'back' ? -5 : 5, el.duration)
    }
    return
  }
  event.preventDefault()
  event.stopPropagation()
  if (isSpace(event)) {
    spaceTaken = true
  }
  if (action === 'toggle') {
    if (el.paused) {
      el.play().catch(() => {})
    } else {
      el.pause()
    }
  } else if (action === 'mute') {
    el.muted = !el.muted
  } else if (action === 'fullscreen') {
    // An iPhone has only the video's own full screen, which its controls leave.
    const video = el as { requestFullscreen?: () => Promise<void>; webkitEnterFullscreen?: () => void }
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {})
    } else if (video.requestFullscreen) {
      video.requestFullscreen().catch(() => {})
    } else {
      video.webkitEnterFullscreen?.()
    }
  }
}

/** The release of a Space the pane took, so a focused control button cannot press itself. */
function onKeyUp(event: KeyboardEvent) {
  if (spaceTaken && isSpace(event)) {
    spaceTaken = false
    event.preventDefault()
    event.stopPropagation()
  }
}

// --- the pane ---------------------------------------------------------------------

/**
 * The pane's size, which the video box is measured in: the observer's content box rounded
 * down, since `clientWidth` rounds to the nearest pixel and under a browser zoom a phone's
 * full-width box would overflow the pane by a fraction of one. Read from `clientWidth` only
 * until the observer first reports.
 */
function onResize(entries: ResizeObserverEntry[]) {
  const box = entries[0]!.contentBoxSize[0]!
  const size = { width: Math.floor(box.inlineSize), height: Math.floor(box.blockSize) }
  if (size.width !== paneSize.value.width || size.height !== paneSize.value.height) {
    paneSize.value = size
  }
}

// Whatever replaced it — a Retry, a new file, the audio layout, the failure sheet — the element
// that leaves is released, so it stops reading the file and holds no connection; a removed
// element otherwise keeps loading until it is collected.
watch(media, (_el, old) => {
  if (old) {
    old.removeAttribute('src')
    old.load()
  }
})

watch(
  [() => props.url, () => props.kind],
  () => {
    layout.value = props.kind
    resumeAt = null
    start()
  },
)

onMounted(() => {
  const el = pane.value!
  // The pane is the dialog's first field, so the dialog focuses it on open; it takes focus
  // itself unless the reader has put it somewhere already.
  const active = document.activeElement
  if (!active || active === document.body || active === root.value?.closest('[aria-modal="true"]')) {
    el.focus({ preventScroll: true })
  }
  paneSize.value = { width: el.clientWidth, height: el.clientHeight }
  observer = new ResizeObserver(onResize)
  observer.observe(el)
  el.addEventListener('keydown', onKeyDown, true)
  el.addEventListener('keyup', onKeyUp, true)
  // The element mounted with the pane and is already opening the file.
  announce(t('viewer.pdf.announce.opening', { name: props.name }))
})

onBeforeUnmount(() => {
  starts++
  controller.abort()
  if (media.value) {
    media.value.removeAttribute('src')
    media.value.load()
  }
  clearTimeout(waitTimer)
  observer?.disconnect()
  pane.value?.removeEventListener('keydown', onKeyDown, true)
  pane.value?.removeEventListener('keyup', onKeyUp, true)
})
</script>

<template>
  <div ref="root" class="player keyboard-frame">
    <ViewerLoading :busy="busy" spinner />
    <main
      ref="pane"
      class="pane keyboard-pane"
      :class="{ 'keyboard-focus': keyboardFocus, padded: failed || layout === 'audio' }"
      :tabindex="failed ? undefined : 0"
      :aria-label="name"
      :aria-busy="busy ? 'true' : undefined"
      :aria-keyshortcuts="failed ? undefined : MEDIA_KEYSHORTCUTS[layout]"
    >
      <ViewerStateSheet v-if="failed && failure" ref="sheet" glyph="file-x" :title="errorTitle" :body="causeText(failure)">
        <template v-if="failure !== 'signed-out'" #default>
          <UiButton v-if="failure === 'corrupt'" variant="primary" @click="downloadFile(download)">
            {{ downloadSize ? t('viewer.file.downloadSize', { size: downloadSize }) : t('viewer.file.download') }}
          </UiButton>
          <UiButton v-else-if="failure === 'network'" variant="primary" @click="retry">{{ t('common.retry') }}</UiButton>
          <UiButton v-else variant="primary" @click="emit('close')">{{ t('viewer.pdf.error.backToFiles') }}</UiButton>
        </template>
      </ViewerStateSheet>
      <!-- `#t=0.001` makes an iPhone seek to the first frame and draw it before play; it is never
           sent to the server, and the controls still start at 0:00. -->
      <video
        v-else-if="layout === 'video'"
        :key="attempt"
        ref="media"
        class="video"
        :class="{ loading: !shown }"
        :inert="!shown"
        :style="videoStyle"
        :src="`${url}#t=0.001`"
        controls
        playsinline
        preload="metadata"
        @loadedmetadata="onLoadedMetadata"
        @durationchange="onDurationChange"
        @suspend="onSuspend"
        @progress="onProgress"
        @error="onError"
      />
      <div v-else class="player-audio">
        <UiIcon v-if="shown" name="music" class="glyph" />
        <audio
          :key="attempt"
          ref="media"
          :class="{ loading: !shown }"
          :inert="!shown"
          :src="url"
          controls
          preload="metadata"
          @loadedmetadata="onLoadedMetadata"
          @durationchange="onDurationChange"
          @suspend="onSuspend"
          @progress="onProgress"
          @error="onError"
        />
      </div>
    </main>
    <p class="sr-only" role="status" aria-live="polite">{{ liveText }}</p>
  </div>
</template>

<style scoped>
.player {
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

/* The desk. The video's box is in pixels from `videoBox`, so the grid only centres it and its
   auto rows never size it. Its own scrolling only, so a pan that reaches an edge never chains
   into Safari's back or forward swipe. */
.pane {
  position: relative;
  display: grid;
  place-items: center;
  height: 100%;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--desk);
}

/* The state sheet and the audio layout keep off the pane's edges; the video's gutter is in its
   box. */
.pane.padded {
  padding: var(--space-4);
}

/* Loading: the element reads its metadata but takes no room and is not seen. Above the audio
   player's width rule. */
.pane .loading {
  position: absolute;
  width: 0;
  height: 0;
  visibility: hidden;
}

.video {
  display: block;
  background: var(--media-matte);
}

/* The glyph and the player, 16px apart: the gap and the glyph's margin, as on the state sheet. */
.player-audio {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
}

.glyph {
  width: var(--space-12);
  height: var(--space-12);
  margin-bottom: var(--space-2);
  color: var(--faint);
}

.player-audio audio {
  width: min(100%, var(--audio-width));
}
</style>
