<script setup lang="ts">
/**
 * A share as a bar: how much of a limit is used — the Usage section's credits and storage
 * (docs/web.md § Pages, `/settings/usage`) — or how far a parse has got.
 *
 * The fill never escalates through amber to red: the figure is always printed beside it
 * (*N% used*, *9 of 32*), so the bar is emphasis and never the only signal — which is also why it
 * carries an accessible name rather than a visible label. A meter is data, so it is one of
 * the few things allowed to be the accent colour.
 */
const props = defineProps<{
  /** 0..1, the used share of the limit, or the parse's pages done. */
  share: number
  /** Accessible name — the row this bar belongs to. */
  label: string
}>()

const percent = computed(() => Math.round(Math.max(0, Math.min(1, props.share)) * 100))
</script>

<template>
  <div
    class="track"
    role="meter"
    :aria-valuenow="percent"
    aria-valuemin="0"
    aria-valuemax="100"
    :aria-label="label"
  >
    <div class="fill" :style="{ width: `${percent}%` }" />
  </div>
</template>

<style scoped>
/* A rounded bar on the system's gray fill, filled in the accent: tall enough for its round
   ends not to eat a small value. */
.track {
  height: 6px;
  border-radius: var(--radius-full);
  background: var(--fill);
  overflow: hidden;
}

.fill {
  height: 100%;
  min-width: 2px;
  border-radius: inherit;
  background: var(--accent);
  transition: width var(--duration-slow) var(--ease);
}
</style>
