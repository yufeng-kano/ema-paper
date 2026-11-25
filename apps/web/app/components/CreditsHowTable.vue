<script setup lang="ts">
/**
 * How credits are spent, one action a row (docs/web.md § Pages, `/pricing` and `/rates`): a
 * parse, a retry and a reconvert by the page, a chat turn, and a web search where the
 * deployment offers one — every number from the catalog's `rates`, none typed here. The chat
 * row's cell is the page's (`#chat`), its tokens being priced per model. A skeleton on the
 * same sheet while the catalog loads; nothing once it failed.
 */
import type { BillingRates } from '~/lib/api'
import { formatCount } from '~/lib/format'

defineProps<{ rates: BillingRates | null; loading: boolean }>()

const { t, locale } = useI18n()
</script>

<template>
  <UiSkeleton v-if="loading" class="sheet" :rows="4" />
  <table v-else-if="rates" class="how-table sheet">
    <thead>
      <tr>
        <th scope="col">{{ t('pricing.columnAction') }}</th>
        <th scope="col" class="num">{{ t('pricing.columnCredits') }}</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>{{ t('pricing.parseRow') }}</td>
        <td class="num tabular">{{ t('pricing.perPage', { n: formatCount(rates.parse_per_page, locale) }) }}</td>
      </tr>
      <tr>
        <td>{{ t('pricing.retryRow') }}</td>
        <td class="num tabular">{{ t('pricing.perPage', { n: formatCount(rates.parse_per_page, locale) }) }}</td>
      </tr>
      <tr>
        <td>{{ t('pricing.reconvertRow') }}</td>
        <td class="num tabular">{{ t('pricing.perPage', { n: formatCount(rates.parse_per_page, locale) }) }}</td>
      </tr>
      <tr>
        <td>{{ t('pricing.chatRow') }}</td>
        <td class="num"><slot name="chat" /></td>
      </tr>
      <tr v-if="rates.web_search_per_call !== null">
        <td>{{ t('pricing.webSearchRow') }}</td>
        <td class="num tabular">{{ t('pricing.perCall', { n: formatCount(rates.web_search_per_call, locale) }) }}</td>
      </tr>
    </tbody>
  </table>
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

.how-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: var(--text-sm);
}

.how-table th,
.how-table td {
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

.how-table tbody tr:last-child td {
  border-bottom: 0;
}

.how-table th {
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.num {
  text-align: right !important;
  white-space: nowrap;
}
</style>
