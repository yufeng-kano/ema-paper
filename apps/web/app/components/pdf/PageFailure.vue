<script setup lang="ts">
/**
 * One page that failed to draw (docs/pdf-viewer.md § Stage 1: the Canvas 2D viewer, *Errors*),
 * centred on the page's own paper while the other pages draw as usual. A failure whose cause
 * lies elsewhere — the connection, the session, the file gone — says that cause in the words of
 * the matching document state, never that the page is broken.
 *
 * While a retry runs its button stays put, `aria-disabled` and busy rather than disabled, so it
 * keeps focus; the viewer moves focus to the page once the retry works.
 */
const props = defineProps<{
  /** From 1. */
  page: number
  /** The cause, in the viewer's words (which it also announces). */
  body: string
  /** Only a page's own failure and a failed connection can come right by trying again. */
  retryable: boolean
  retrying: boolean
}>()

const emit = defineEmits<{ retry: [] }>()

const { t } = useI18n()

function onRetry() {
  if (!props.retrying) {
    emit('retry')
  }
}
</script>

<template>
  <div class="failure">
    <UiIcon name="file-exclamation-point" class="glyph" />
    <h3 class="title">{{ t('viewer.pdf.pageError.title', { page }) }}</h3>
    <p class="body">{{ body }}</p>
    <UiButton
      v-if="retryable"
      class="retry"
      :label="t('viewer.pdf.pageError.retry', { page })"
      :aria-disabled="retrying ? 'true' : undefined"
      :aria-busy="retrying ? 'true' : undefined"
      @click="onRetry"
    >
      {{ t('common.retry') }}
    </UiButton>
  </div>
</template>

<style scoped>
/* Drawn on white paper inside the page's light content layer, at the reading size whatever
   the zoom. */
.failure {
  position: absolute;
  inset: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-4);
  overflow: hidden;
  font-family: var(--font);
  text-align: center;
}

.glyph {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  margin-bottom: var(--space-1);
  color: var(--faint);
}

.title {
  color: var(--ink);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  line-height: 1.4;
}

.body {
  max-width: 40ch;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.55;
}

.retry {
  margin-top: var(--space-3);
}
</style>
