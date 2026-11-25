<script setup lang="ts">
/**
 * A text's live length against its cap, as every editor and dialog of the Agent page shows
 * it (docs/web.md § Pages): `n / max` in the page's locale, red past the cap — the one state
 * it exists to show — and the length alone while the cap is not known yet. One element, so
 * the caller's `id` (an `aria-describedby` target) and placement class land on it.
 */
import { formatCount } from '~/lib/format'

const props = defineProps<{
  /** The length, as `charLength` counts it. */
  count: number
  max: number | null
}>()

const { t, locale } = useI18n()

const over = computed(() => props.max !== null && props.count > props.max)
</script>

<template>
  <span class="count tabular" :class="{ over }">
    {{
      max === null
        ? formatCount(count, locale)
        : t('agents.promptCount', { count: formatCount(count, locale), max: formatCount(max, locale) })
    }}
  </span>
</template>

<style scoped>
.count {
  color: var(--faint);
  font-size: var(--text-xs);
}

.count.over {
  color: var(--danger);
}
</style>
