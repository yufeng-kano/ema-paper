<script setup lang="ts">
/**
 * A PDF that cannot be opened (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Errors*):
 * the reason in the reader's language and the way out its cause has. An unreadable file offers
 * its download and the browser's own viewer in a new tab, a failed connection a retry, a file
 * no longer there the way back to Files; an ended session has no action of its own, since the
 * app's session handling takes the reader to sign in.
 *
 * Drawn as the viewers' state sheet (`ViewerStateSheet`). The viewer words the reason, which it
 * also announces once, and moves focus to the heading (`focusHeading`) only when focus had been
 * in what went away.
 */
import type { LoadErrorKind } from '~/lib/pdf/errors'

defineProps<{
  kind: Exclude<LoadErrorKind, 'cancelled'>
  /** The reason under the title, in the words the viewer also announces. */
  reason: string
  /** The file's inline URL, for *Open in new tab*. */
  url: string
  /** Its attachment URL, for *Download*. */
  download: string
  downloadSize: string | null
}>()

defineEmits<{ retry: []; close: [] }>()

const { t } = useI18n()

const sheet = ref<{ focusHeading(): void } | null>(null)

defineExpose({ focusHeading: () => sheet.value?.focusHeading() })
</script>

<template>
  <ViewerStateSheet ref="sheet" glyph="file-x" :title="t('viewer.pdf.error.title')" :body="reason">
    <template v-if="kind !== 'signed-out'" #default>
      <template v-if="kind === 'corrupt'">
        <UiButton variant="primary" :href="download">
          {{ downloadSize ? t('viewer.file.downloadSize', { size: downloadSize }) : t('viewer.file.download') }}
        </UiButton>
        <UiButton :href="url" target="_blank">{{ t('viewer.pdf.openInNewTab') }}</UiButton>
      </template>
      <UiButton v-else-if="kind === 'network'" variant="primary" @click="$emit('retry')">
        {{ t('common.retry') }}
      </UiButton>
      <UiButton v-else variant="primary" @click="$emit('close')">{{ t('viewer.pdf.error.backToFiles') }}</UiButton>
    </template>
  </ViewerStateSheet>
</template>
