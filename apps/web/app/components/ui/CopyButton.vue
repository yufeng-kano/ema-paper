<script setup lang="ts">
/**
 * Copy one value to the clipboard, confirming in place by swapping the glyph to a check.
 *
 * The confirmation is announced too, not just drawn: a screen-reader user pressing Copy
 * otherwise gets no feedback at all.
 *
 * `labelled` shows the word beside the glyph — for the two places the design puts Copy on
 * its own in a panel header or beside an endpoint, where an unlabelled square would be an
 * unexplained button rather than an obvious action on the value next to it. The word is
 * `label` when one is given — *Copy all* over the console's created codes — else *Copy*.
 */
const props = withDefaults(
  defineProps<{
    text: string
    variant?: 'secondary' | 'ghost'
    size?: 'xs' | 'sm' | 'md'
    labelled?: boolean
    /** The icon-only control's name, for a value its glyph alone does not say; with
     *  `labelled`, the word shown. */
    label?: string
  }>(),
  { variant: 'ghost', size: 'md', label: undefined },
)

const { t } = useI18n()
const copied = ref(false)
let resetTimer: ReturnType<typeof setTimeout> | undefined

async function copy() {
  try {
    await navigator.clipboard.writeText(props.text)
  } catch {
    // Clipboard access can be denied (insecure context, permissions); the value stays
    // selectable on the page, so there is nothing to report.
    return
  }
  copied.value = true
  clearTimeout(resetTimer)
  resetTimer = setTimeout(() => {
    copied.value = false
  }, 1600)
}

onUnmounted(() => clearTimeout(resetTimer))
</script>

<template>
  <span class="copy">
    <UiButton
      :variant="variant"
      :size="size"
      :icon-only="!labelled"
      :label="labelled ? undefined : copied ? t('common.copied') : (label ?? t('common.copy'))"
      @click="copy"
    >
      <template #icon><UiIcon :name="copied ? 'check' : 'copy'" /></template>
      <template v-if="labelled">{{ copied ? t('common.copied') : (label ?? t('common.copy')) }}</template>
    </UiButton>
    <span class="sr-only" role="status" aria-live="polite">{{ copied ? t('common.copied') : '' }}</span>
  </span>
</template>

<style scoped>
.copy {
  display: inline-flex;
  flex-shrink: 0;
}
</style>
