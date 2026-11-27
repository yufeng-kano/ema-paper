<script setup lang="ts">
/**
 * The wait while a file opens (docs/media-viewer.md § The shell, *The loading bar*): a 2px
 * indeterminate accent bar under the header, static under reduced motion, and, with `spinner`,
 * a 24px spinner centred on the desk once the wait has taken 0.4 s. The PDF viewer asks for no
 * spinner, since each of its pages carries its own. The caller's box is the one both are placed
 * in, so it must be positioned; nothing is drawn while `busy` is false.
 */
const props = withDefaults(defineProps<{ busy: boolean; spinner?: boolean }>(), { spinner: false })

/** A wait shorter than this shows the bar alone, so a quick open does not flash a spinner. */
const SPINNER_DELAY_MS = 400

const { t } = useI18n()

const slow = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined

// Each new wait starts its own 0.4 s, so a retry does not inherit the last wait's spinner.
watch(
  () => props.busy,
  (busy) => {
    clearTimeout(timer)
    slow.value = false
    if (busy) {
      timer = setTimeout(() => (slow.value = true), SPINNER_DELAY_MS)
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <template v-if="busy">
    <div class="progress" role="progressbar" :aria-label="t('viewer.pdf.opening')">
      <span class="progress-bar" />
    </div>
    <UiSpinner v-if="spinner && slow" class="spinner" :size="24" />
  </template>
</template>

<style scoped>
/* An indeterminate bar under the header while the file opens; static under reduced motion. */
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

/* Centred on the desk, over a media element that loads hidden and takes no room. */
.spinner {
  position: absolute;
  top: 50%;
  left: 50%;
  z-index: 2;
  margin: -12px 0 0 -12px;
  color: var(--muted);
  pointer-events: none;
}

@media (prefers-reduced-motion: reduce) {
  .progress-bar {
    left: 34%;
    animation: none;
  }
}
</style>
