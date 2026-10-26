<script setup lang="ts">
/**
 * The file viewer (docs/web.md § Files): the result viewer's shell with the document's
 * panes left out. The way back and the name, with its kind and size, at the left; at the
 * right Download, Convert to document (a PDF or an image only) and More, whose menu opens
 * on the file's facts over Rename, Duplicate, Show in Files and Move to Trash. The body is
 * what a browser shows safely on its own, from `…/file` (docs/api.md): an image, a video or
 * audio file with the browser's controls, a PDF in the browser's own viewer, plain text in
 * a monospace pane; for anything else a sheet with the file's glyph, name, kind and size,
 * that there is no preview, that the agent can still read it, and Download.
 */
import type { ComponentPublicInstance } from 'vue'
import { documentFileUrl, type LibraryDocument } from '~/lib/api'
import { formatBytes, formatShortDateTime } from '~/lib/format'
import { documentGlyph, filePreview } from '~/lib/library'
import type { MenuFact, MenuItem, Point } from '~/lib/menu'

/** How much of a text file the pane reads: the head of a log is what a reader opens it for. */
const TEXT_PREVIEW_MAX = 262_144

const props = defineProps<{
  document: LibraryDocument
  /** Where the file is, Home first, as the facts say it. */
  location: string
  /** Whether Convert to document is offered: a PDF or an image intake would parse. */
  convertible: boolean
}>()

const emit = defineEmits<{
  close: []
  convert: []
  rename: []
  duplicate: []
  show: []
  trash: []
}>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const kindLabel = useKindLabel()

const kind = computed(() => kindLabel(props.document))
const size = computed(() =>
  props.document.size_bytes === null ? null : formatBytes(props.document.size_bytes, locale.value),
)
const facts = computed(() => (size.value ? `${kind.value} · ${size.value}` : kind.value))
const preview = computed(() => filePreview(props.document.media_type))
const url = computed(() => documentFileUrl(props.document.id))
const downloadUrl = computed(() => documentFileUrl(props.document.id, true))
const glyph = computed(() => documentGlyph(props.document))

// --- plain text, read once ------------------------------------------------------------

const text = ref<string | null>(null)
const textCut = ref(false)
const textError = ref<string | null>(null)

async function readText() {
  try {
    const response = await fetch(url.value, {
      headers: { Range: `bytes=0-${TEXT_PREVIEW_MAX - 1}` },
      credentials: 'same-origin',
    })
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }
    textCut.value = response.status === 206 && (props.document.size_bytes ?? 0) > TEXT_PREVIEW_MAX
    text.value = await response.text()
  } catch (error) {
    textError.value = await resolve(error)
  }
}

onMounted(() => {
  if (preview.value === 'text') {
    void readText()
  }
})

// --- More -----------------------------------------------------------------------------

const moreButton = ref<ComponentPublicInstance | null>(null)
const moreMenu = ref<Point | null>(null)

const moreFacts = computed<MenuFact[]>(() => [
  { label: t('viewer.file.kind'), value: kind.value },
  ...(size.value ? [{ label: t('viewer.file.size'), value: size.value }] : []),
  { label: t('viewer.file.added'), value: formatShortDateTime(props.document.created_at, locale.value) },
  { label: t('viewer.file.location'), value: props.location },
])

const moreItems = computed<MenuItem[]>(() => [
  { key: 'rename', label: t('files.rename'), icon: 'edit', separated: true },
  { key: 'duplicate', label: t('files.duplicate'), icon: 'copy-plus' },
  { key: 'show', label: t('viewer.file.showInFiles'), icon: 'folder-open' },
  { key: 'trash', label: t('files.moveToTrash'), icon: 'trash', danger: true, separated: true },
])

/** The menu under the button's right edge; flipped by the menu itself where it does not fit. */
function toggleMore() {
  if (moreMenu.value) {
    moreMenu.value = null
    return
  }
  const element = moreButton.value?.$el as HTMLElement | undefined
  const rect = element?.getBoundingClientRect()
  moreMenu.value = rect ? { x: rect.right, y: rect.bottom + 4 } : { x: 0, y: 0 }
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
  <UiModal :title="document.name" size="full" tall flush back @close="emit('close')">
    <template #title>
      <div class="doc-head">
        <h2 class="doc-title" :title="document.name">{{ document.name }}</h2>
        <span class="doc-facts">{{ facts }}</span>
      </div>
    </template>

    <template #actions>
      <UiCapsule :label="t('viewer.file.tools')">
        <UiButton variant="ghost" icon-only :label="t('viewer.file.download')" :href="downloadUrl">
          <template #icon><UiIcon name="download" /></template>
        </UiButton>
        <UiButton
          v-if="convertible"
          variant="ghost"
          icon-only
          :label="t('viewer.file.convert')"
          @click="emit('convert')"
        >
          <template #icon><UiIcon name="scan-text" /></template>
        </UiButton>
        <UiButton
          ref="moreButton"
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
        @select="onMoreSelect"
        @close="moreMenu = null"
      />
    </template>

    <div class="file" :class="preview">
      <img v-if="preview === 'image'" class="media" :src="url" :alt="document.name">
      <video v-else-if="preview === 'video'" class="media" :src="url" controls preload="metadata" />
      <div v-else-if="preview === 'audio'" class="sheet">
        <UiIcon :name="glyph" class="sheet-glyph" />
        <p class="sheet-name">{{ document.name }}</p>
        <p class="sheet-facts">{{ facts }}</p>
        <audio class="audio" :src="url" controls preload="metadata" />
      </div>
      <iframe v-else-if="preview === 'pdf'" class="frame" :src="url" :title="document.name" />
      <template v-else-if="preview === 'text'">
        <UiBanner v-if="textError" tone="error">{{ textError }}</UiBanner>
        <UiSkeleton v-else-if="text === null" class="text-wait" :rows="8" />
        <div v-else class="text-pane">
          <pre class="text mono">{{ text }}</pre>
          <p v-if="textCut" class="text-cut">
            {{ t('viewer.file.textCut', { size: formatBytes(TEXT_PREVIEW_MAX, locale) }) }}
          </p>
        </div>
      </template>
      <div v-else class="sheet">
        <UiIcon :name="glyph" class="sheet-glyph" />
        <p class="sheet-name">{{ document.name }}</p>
        <p class="sheet-facts">{{ facts }}</p>
        <p class="sheet-note">{{ t('viewer.file.noPreview') }}</p>
        <p class="sheet-note">{{ t('viewer.file.agentReads') }}</p>
        <UiButton variant="primary" :href="downloadUrl">
          {{ size ? t('viewer.file.downloadSize', { size }) : t('viewer.file.download') }}
        </UiButton>
      </div>
    </div>
  </UiModal>
</template>

<style scoped>
/* The header as the result viewer draws it: the name, and the file's facts after it. */
.doc-head {
  display: flex;
  align-items: baseline;
  justify-content: center;
  gap: var(--space-3);
  min-width: 0;
}

.doc-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.01em;
}

.doc-facts {
  flex-shrink: 0;
  color: var(--faint);
  font-size: var(--text-xs);
  white-space: nowrap;
}

@media (max-width: 720px) {
  .doc-facts {
    display: none;
  }
}

/* The body: the preview centred in the window, scrolling where it is taller. */
.file {
  display: grid;
  place-items: center;
  height: 100%;
  min-height: 0;
  padding: var(--space-4);
  overflow: auto;
}

.file.pdf {
  display: block;
  padding: 0;
  overflow: hidden;
}

.file.text {
  place-items: start center;
}

.media {
  max-width: 100%;
  max-height: 100%;
  border-radius: var(--radius-card);
  object-fit: contain;
}

.frame {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
}

.text-pane,
.text-wait {
  width: min(100%, 84ch);
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

/* No preview: the file's glyph, its name and facts, and the way to its bytes. */
.sheet {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  max-width: 440px;
  text-align: center;
}

.sheet-glyph {
  width: 48px;
  height: 48px;
  margin-bottom: var(--space-2);
  color: var(--faint);
}

.sheet-name {
  max-width: 100%;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-md);
  font-weight: var(--weight-semibold);
}

.sheet-facts {
  margin: 0;
  color: var(--faint);
  font-size: var(--text-xs);
}

.sheet-note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-sm);
}

.sheet-note + .sheet-note {
  margin-bottom: var(--space-3);
}

.audio {
  width: min(100%, 360px);
  margin-top: var(--space-3);
}
</style>
