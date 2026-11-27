<script setup lang="ts">
/**
 * A PDF that cannot be opened (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Errors*):
 * the reason in the reader's language and the way out its cause has. An unreadable file offers
 * its download and the browser's own viewer in a new tab, a failed connection a retry, a file
 * no longer there the way back to Files; an ended session has no action of its own, since the
 * app's session handling takes the reader to sign in.
 *
 * The viewer words the reason, which it also announces once, and moves focus to the heading
 * (`focusHeading`) only when focus had been in what went away.
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

const heading = ref<HTMLElement | null>(null)

function focusHeading() {
  heading.value?.focus({ preventScroll: true })
}

defineExpose({ focusHeading })
</script>

<template>
  <div class="state">
    <UiIcon name="file-x" class="glyph" />
    <h3 ref="heading" class="title" tabindex="-1">{{ t('viewer.pdf.error.title') }}</h3>
    <p class="body">{{ reason }}</p>
    <div v-if="kind !== 'signed-out'" class="actions">
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
    </div>
  </div>
</template>

<style scoped>
.state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  text-align: center;
}

.glyph {
  width: 48px;
  height: 48px;
  margin-bottom: var(--space-2);
  color: var(--faint);
}

.title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.body {
  max-width: 44ch;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.55;
  text-wrap: balance;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--space-3);
  margin-top: var(--space-3);
}
</style>
