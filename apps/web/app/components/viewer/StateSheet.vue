<script setup lang="ts">
/**
 * The state sheet every original's viewer draws on its desk (docs/media-viewer.md § The shell):
 * a PDF that cannot be opened, an image that cannot be shown, a video or an audio file that
 * cannot be played, a file with no preview. The glyph, the title, the reason under it, and the
 * actions the caller puts in the default slot; with no slot there is no actions row.
 *
 * The caller words the reason, announces it, and moves focus to the heading (`focusHeading`)
 * only when focus had been in what went away.
 */
import type { IconName } from '~/lib/icons'

defineProps<{
  glyph: IconName
  title: string
  body?: string
}>()

const heading = ref<HTMLElement | null>(null)

function focusHeading() {
  heading.value?.focus({ preventScroll: true })
}

defineExpose({ focusHeading })
</script>

<template>
  <div class="state">
    <UiIcon :name="glyph" class="glyph" />
    <h3 ref="heading" class="title" tabindex="-1">{{ title }}</h3>
    <p v-if="body" class="body">{{ body }}</p>
    <div v-if="$slots.default" class="actions">
      <slot />
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
  width: var(--space-12);
  height: var(--space-12);
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
