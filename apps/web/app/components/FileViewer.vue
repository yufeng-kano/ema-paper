<script setup lang="ts">
/**
 * The file viewer (docs/media-viewer.md § The shell): the result viewer's shell for every file
 * that is neither a PDF nor an image — those open in the result viewer on their original. The
 * way back at the left, the filename alone in the middle, and at the right one capsule of
 * Download and More, whose menu opens on the file's facts over Rename, Duplicate, Show in Files
 * and Move to Trash. The pane is what a browser shows safely on its own, from `…/file`
 * (docs/api.md): a video or an audio file in the player (`MediaPlayer`), plain text in a
 * monospace pane, and for anything else the state sheet with Download.
 */
import { documentFileUrl, downloadFile, readDocumentFileHead, type LibraryDocument } from '~/lib/api'
import { formatBytes, formatDuration } from '~/lib/format'
import { documentGlyph, filePreview } from '~/lib/library'
import type { MediaPlayerState } from '~/lib/media/player'
import type { MenuFact, MenuItem, Point } from '~/lib/menu'

/** How much of a text file the pane reads: the head of a log is what a reader opens it for. */
const TEXT_PREVIEW_MAX = 262_144

const props = defineProps<{
  document: LibraryDocument
  /** The file's facts as Files words them: Kind, Size, Date added, Location. */
  facts: MenuFact[]
}>()

const emit = defineEmits<{
  close: []
  rename: []
  duplicate: []
  show: []
  trash: []
}>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
/** The compact header on a phone, following a resize and a change of pointer. */
const layout = useWindowLayout()
const phone = computed(() => layout.value === 'phone')
/** The text pane's ring follows the keyboard, not `:focus-visible` (docs/web.md § Rules). */
const keyboardFocus = useKeyboardFocus()

const size = computed(() =>
  props.document.size_bytes === null ? null : formatBytes(props.document.size_bytes, locale.value),
)
const preview = computed(() => filePreview(props.document.media_type))
const url = computed(() => documentFileUrl(props.document.id))
const downloadUrl = computed(() => documentFileUrl(props.document.id, true))

/** What the player last reported: More's Dimensions and Duration once the browser knows them. */
const playerState = shallowRef<MediaPlayerState | null>(null)

// --- plain text, read once ------------------------------------------------------------

const textPane = ref<HTMLElement | null>(null)
const text = ref<string | null>(null)
const textCut = ref(false)
const textError = ref<string | null>(null)

async function readText() {
  try {
    const head = await readDocumentFileHead(props.document.id, TEXT_PREVIEW_MAX)
    textCut.value = head.partial && (props.document.size_bytes ?? 0) > TEXT_PREVIEW_MAX
    text.value = head.text
  } catch (error) {
    textError.value = await resolve(error)
  }
}

onMounted(() => {
  if (preview.value === 'text') {
    // The pane takes focus on open, so the keys scroll it; the player focuses its own pane.
    textPane.value?.focus({ preventScroll: true })
    void readText()
  }
})

// --- More -----------------------------------------------------------------------------

const moreMenu = ref<Point | null>(null)
const moreFromKey = ref(false)
/** The facts as they stood when the menu opened: it does not grow while it is open. */
const moreFacts = shallowRef<MenuFact[]>([])
/** When the menu last closed, and the press that closed it. A press on More while the menu is
 *  open closes it (a press outside the menu) and is followed by that button's click, which must
 *  not open it again; a close by any other cause leaves More free at once. */
let moreClosedAt = 0
let moreClosedBy: EventTarget | null = null

/** Files' facts with what the player has learnt since: Dimensions after Kind, then Duration,
 *  both before Size. */
const currentFacts = computed<MenuFact[]>(() => {
  const facts = [...props.facts]
  const state = playerState.value
  let at = 1
  if (state?.dimensions) {
    // Strings, so the numbers are never grouped (docs/media-viewer.md § The More menu).
    const value = t('viewer.file.dimensionsValue', {
      width: String(state.dimensions.width),
      height: String(state.dimensions.height),
    })
    facts.splice(at, 0, { label: t('viewer.file.dimensions'), value })
    at += 1
  }
  const duration = state && state.duration !== null ? formatDuration(state.duration) : null
  if (duration) {
    facts.splice(at, 0, { label: t('viewer.file.duration'), value: duration })
  }
  return facts
})

const moreItems = computed<MenuItem[]>(() => [
  { key: 'rename', label: t('files.rename'), icon: 'edit', separated: true },
  { key: 'duplicate', label: t('files.duplicate'), icon: 'copy-plus' },
  { key: 'show', label: t('viewer.file.showInFiles'), icon: 'folder-open' },
  { key: 'trash', label: t('files.moveToTrash'), icon: 'trash', danger: true, separated: true },
])

/** Anchored under the button's right edge: More sits at the dialog's right, so the menu
 *  opens leftwards under it (`placeMenu` flips it). */
function toggleMore(event: MouseEvent) {
  const button = event.currentTarget as HTMLElement
  const closedByThis = moreClosedBy instanceof Node && button.contains(moreClosedBy)
  if (moreMenu.value || (closedByThis && performance.now() - moreClosedAt < 200)) {
    moreMenu.value = null
    return
  }
  const box = button.getBoundingClientRect()
  // A click the keyboard made carries no pointer detail.
  moreFromKey.value = event.detail === 0
  moreFacts.value = currentFacts.value
  moreMenu.value = { x: box.right, y: box.bottom + 4 }
}

function closeMore(cause?: Event) {
  moreMenu.value = null
  moreClosedAt = performance.now()
  moreClosedBy = cause?.target ?? null
}

function onMoreSelect(key: string) {
  moreMenu.value = null
  switch (key) {
    case 'rename':
      return emit('rename')
    case 'duplicate':
      return emit('duplicate')
    case 'show':
      return emit('show')
    case 'trash':
      return emit('trash')
  }
}
</script>

<template>
  <UiModal :title="document.name" size="full" tall flush back :compact="phone" @close="emit('close')">
    <template #title>
      <h2 class="doc-title" :class="{ 'phone-title': phone }" :title="document.name">{{ document.name }}</h2>
    </template>

    <template #actions>
      <UiCapsule :label="t('viewer.file.tools')">
        <!-- A button, never a link: the dialog focuses its first field that is not a button on
             open, and Enter on a link there would start a download. -->
        <UiButton variant="ghost" icon-only :label="t('viewer.file.download')" @click="downloadFile(downloadUrl)">
          <template #icon><UiIcon name="download" /></template>
        </UiButton>
        <UiButton
          variant="ghost"
          icon-only
          :label="t('viewer.more')"
          aria-haspopup="menu"
          :aria-expanded="moreMenu ? 'true' : 'false'"
          @click="toggleMore"
        >
          <template #icon><UiIcon name="ellipsis" /></template>
        </UiButton>
      </UiCapsule>
      <UiContextMenu
        v-if="moreMenu"
        :at="moreMenu"
        :items="moreItems"
        :label="t('viewer.more')"
        :header="t('viewer.file.details')"
        :facts="moreFacts"
        :focus-first="moreFromKey"
        @select="onMoreSelect"
        @close="closeMore"
      />
    </template>

    <MediaPlayer
      v-if="preview === 'video' || preview === 'audio'"
      :url="url"
      :name="document.name"
      :kind="preview"
      :download="downloadUrl"
      :download-size="size"
      :phone="phone"
      @state="playerState = $event"
      @close="emit('close')"
    />
    <!-- The head of a text file, in a column centred on the desk; its skeleton is the wait, so
         there is no loading bar. -->
    <div v-else-if="preview === 'text'" class="text-frame keyboard-frame">
      <main
        ref="textPane"
        class="pane text-pane keyboard-pane"
        :class="{ 'keyboard-focus': keyboardFocus }"
        tabindex="0"
        :aria-label="document.name"
      >
        <div class="text-column">
          <UiBanner v-if="textError" tone="error">{{ textError }}</UiBanner>
          <UiSkeleton v-else-if="text === null" :rows="8" />
          <template v-else>
            <pre class="text mono">{{ text }}</pre>
            <p v-if="textCut" class="text-cut">
              {{ t('viewer.file.textCut', { size: formatBytes(TEXT_PREVIEW_MAX, locale, { limit: true }) }) }}
            </p>
          </template>
        </div>
      </main>
    </div>
    <!-- Nothing to scroll or play, so not a Tab stop. -->
    <main v-else class="pane centred" :aria-label="document.name">
      <ViewerStateSheet :glyph="documentGlyph(document)" :title="t('viewer.file.noPreview')" :body="t('viewer.file.agentReads')">
        <UiButton variant="primary" @click="downloadFile(downloadUrl)">
          {{ size ? t('viewer.file.downloadSize', { size }) : t('viewer.file.download') }}
        </UiButton>
      </ViewerStateSheet>
    </main>
  </UiModal>
</template>

<style scoped>
/* The filename alone, centred between the back button and the capsule, as the result viewer
   draws it; a phone's compact header sets it smaller, with no tracking. */
.doc-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.01em;
}

.doc-title.phone-title {
  font-size: var(--text-sm);
  letter-spacing: normal;
}

/* The desk under the header. Its own scrolling only, so a scroll that reaches an edge never
   chains into Safari's back or forward swipe. */
.pane {
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  background: var(--desk);
}

.pane.centred {
  display: grid;
  place-items: center;
  padding: var(--space-4);
}

/* The text pane's positioned box, which draws its keyboard ring (main.css `.keyboard-frame`). */
.text-frame {
  position: relative;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.text-pane {
  padding: var(--space-4);
}

.text-column {
  width: min(100%, 84ch);
  margin: 0 auto;
}

.text {
  margin: 0;
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.text-cut {
  margin: var(--space-4) 0 0;
  color: var(--faint);
  font-size: var(--text-xs);
}
</style>
