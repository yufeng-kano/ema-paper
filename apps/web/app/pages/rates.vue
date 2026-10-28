<script setup lang="ts">
/**
 * The public rates page (docs/web.md § Pages, `/rates`): what a parse and a chat turn cost,
 * and the chat models' credits per 1K tokens — every number from `GET /api/billing/catalog`'s
 * `rates`, read when the page opens; none is typed here. The Usage section links here in
 * place of a table, and so does the pricing page's chat row. Prerendered in both locales and
 * full-bleed like the pricing page; a signed-in reader is not sent away.
 */
import { getBillingCatalog, type BillingRates, type ChatRate } from '~/lib/api'
import { formatCount } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

definePageMeta({ layout: false })

const { t, locale } = useI18n()
const { resolve } = useApiError()

useSeoMeta({
  title: () => t('rates.seoTitle'),
  description: () => t('rates.seoDescription'),
  ogTitle: () => t('rates.seoTitle'),
  ogDescription: () => t('rates.seoDescription'),
  ogType: 'website',
  twitterCard: 'summary',
})

const rates = ref<BillingRates | null>(null)
const loadError = ref<string | null>(null)

onMounted(async () => {
  try {
    rates.value = (await getBillingCatalog()).rates
  } catch (error) {
    loadError.value = await resolve(error)
  }
})

interface RateRow extends ChatRate {
  model: string
}

const rateRows = computed<RateRow[]>(() =>
  Object.entries(rates.value?.chat ?? {}).map(([model, rate]) => ({ model, ...rate })),
)

const rateColumns = computed<TableColumn<RateRow>[]>(() => [
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
  <PublicShell :title="t('rates.nav')">
    <main class="content">
      <header class="head">
        <h1 class="title">{{ t('rates.title') }}</h1>
        <p class="lede">{{ t('rates.lede') }}</p>
      </header>

      <UiBanner v-if="loadError" tone="error">{{ loadError }}</UiBanner>

      <section class="block">
        <h2 class="section-title">{{ t('pricing.howTitle') }}</h2>
        <UiSkeleton v-if="!rates && !loadError" class="sheet" :rows="4" />
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
              <td class="num">{{ t('rates.chatRate') }}</td>
            </tr>
            <tr v-if="rates.web_search_per_call !== null">
              <td>{{ t('pricing.webSearchRow') }}</td>
              <td class="num tabular">
                {{ t('pricing.perCall', { n: formatCount(rates.web_search_per_call, locale) }) }}
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="block">
        <div class="block-head">
          <h2 class="section-title">{{ t('pricing.ratesTitle') }}</h2>
          <p class="section-lede">{{ t('pricing.ratesLede') }}</p>
        </div>
        <UiSkeleton v-if="!rates && !loadError" class="sheet" :rows="4" />
        <!-- Wider than a phone: the sheet scrolls sideways, the page does not. -->
        <div v-else-if="rateRows.length" class="sheet rates-sheet">
          <UiDataTable
            :columns="rateColumns"
            :rows="rateRows"
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
        <p v-else-if="rates" class="note">{{ t('rates.noRates') }}</p>
      </section>

      <p class="note">{{ t('rates.shownUnderAnswer') }}</p>
    </main>
  </PublicShell>
</template>

<style scoped>
/* A reading column over the desk, the tables on paper sheets as the pricing page draws them. */
.content {
  display: flex;
  flex-direction: column;
  gap: var(--space-10);
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: var(--space-12) var(--space-6) var(--space-12);
}

.head {
  display: grid;
  gap: var(--space-3);
}

.title {
  font-size: var(--display-2xl);
  line-height: 1.1;
}

.lede {
  color: var(--muted);
  font-size: var(--text-lg);
  line-height: 1.6;
  text-wrap: pretty;
}

.block {
  display: grid;
  gap: var(--space-4);
}

.block-head {
  display: grid;
  gap: var(--space-1);
}

.section-title {
  font-size: var(--display-xs);
  letter-spacing: normal;
}

.section-lede,
.note {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.sheet {
  overflow: hidden;
  border-radius: var(--radius-lg);
  background: var(--paper);
  box-shadow:
    0 0 0 0.5px var(--pane-edge),
    0 4px 14px var(--glass-shadow);
}

/* Narrower than the page tables: five short columns of figures, inside the reading column. */
.rates-sheet {
  --table-min: 32rem;

  overflow-x: auto;
  overscroll-behavior-x: contain;
  padding: var(--space-2) var(--space-3);
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

@media (max-width: 640px) {
  .content {
    gap: var(--space-8);
    padding: var(--space-6) var(--space-4) var(--space-10);
  }

  .title {
    font-size: var(--display-lg);
  }

  .lede {
    font-size: var(--text-base);
  }
}
</style>
