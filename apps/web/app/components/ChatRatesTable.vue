<script setup lang="ts">
/**
 * The chat models' credits per 1K tokens (docs/web.md § Pages, `/pricing` and `/rates`): one
 * row a model of the catalog's `rates.chat`, none typed here. A skeleton on the same sheet
 * while `loading`; once the catalog answers with no model, the page's `#empty`, if it gives
 * one. `tableMin` narrows the table's floor (`--table-min`) for a column narrower than the
 * page tables'.
 */
import type { BillingRates, ChatRate } from '~/lib/api'
import type { TableColumn } from '~/lib/table'

const props = defineProps<{ rates: BillingRates | null; loading?: boolean; tableMin?: string }>()

const { t, locale } = useI18n()

interface RateRow extends ChatRate {
  model: string
}

const rows = computed<RateRow[]>(() =>
  Object.entries(props.rates?.chat ?? {}).map(([model, rate]) => ({ model, ...rate })),
)

const columns = computed<TableColumn<RateRow>[]>(() => [
  { key: 'model', header: t('pricing.columnModel') },
  { key: 'input', header: t('pricing.columnInput'), numeric: true },
  { key: 'cache_read', header: t('pricing.columnCacheRead'), numeric: true },
  { key: 'cache_write', header: t('pricing.columnCacheWrite'), numeric: true },
  { key: 'output', header: t('pricing.columnOutput'), numeric: true },
])

/** A rate to at most three decimals: 0.005 credits per 1K cache reads stays legible. */
function rate(value: number): string {
  return new Intl.NumberFormat(locale.value, { maximumFractionDigits: 3 }).format(value)
}
</script>

<template>
  <UiSkeleton v-if="loading" class="sheet" :rows="4" />
  <!-- Wider than a phone: the sheet scrolls sideways, the page does not. -->
  <div v-else-if="rows.length" class="sheet rates-sheet" :style="tableMin ? { '--table-min': tableMin } : undefined">
    <UiDataTable
      :columns="columns"
      :rows="rows"
      :row-key="(row) => row.model"
      :caption="t('pricing.ratesTitle')"
      density="tight"
    >
      <template #cell-model="{ row }"><span class="mono">{{ row.model }}</span></template>
      <template #cell-input="{ row }">{{ rate(row.input) }}</template>
      <template #cell-cache_read="{ row }">{{ rate(row.cache_read) }}</template>
      <template #cell-cache_write="{ row }">{{ rate(row.cache_write) }}</template>
      <template #cell-output="{ row }">{{ rate(row.output) }}</template>
    </UiDataTable>
  </div>
  <slot v-else-if="rates" name="empty" />
</template>

<style scoped>
/* A table on the desk sits on a paper sheet with the plans' edge, at the grouped list's
   radius. */
.sheet {
  overflow: hidden;
  border-radius: var(--radius-lg);
  background: var(--paper);
  box-shadow: 0 0 0 0.5px var(--pane-edge);
}

.rates-sheet {
  overflow-x: auto;
  overscroll-behavior-x: contain;
  padding: var(--space-2) var(--space-3);
}
</style>
