<script setup lang="ts">
import { getUsage } from '~/lib/api'
import { formatCost, formatCount, formatDay } from '~/lib/format'
import type { TableColumn } from '~/lib/table'
import { summarizeUsage, type UsageRow } from '~/lib/usage'

definePageMeta({ middleware: 'authed' })

const USAGE_DAYS = 30

const { t, locale } = useI18n()

const { data, pending, errorMessage, refresh } = useAuthedData(() => getUsage(USAGE_DAYS))
const summary = computed(() => (data.value ? summarizeUsage(data.value) : null))

/**
 * The per-day table. The meter column carries no visible header — it ranks the cost column
 * beside it — but keeps an accessible name, since a blank `<th>` is an unnamed column to a
 * screen reader reading the table's structure.
 */
const dayColumns = computed<TableColumn<UsageRow>[]>(() => [
  { key: 'label', header: t('dashboard.columnDate') },
  { key: 'share', header: '', srHeader: t('dashboard.columnShare'), width: '120px' },
  { key: 'cost', header: t('dashboard.columnCost'), numeric: true },
  // Estimates and unpriced calls get columns of their own, and only in a period that has
  // any: they are never summed into the billed cost beside them (docs/web.md § Pages).
  ...(summary.value?.totalEstimatedCalls
    ? [{ key: 'estimated', header: t('dashboard.columnEstimated'), numeric: true }]
    : []),
  ...(summary.value?.totalUnpricedCalls
    ? [{ key: 'unpriced', header: t('dashboard.columnUnpriced'), numeric: true }]
    : []),
  { key: 'prompt', header: t('dashboard.columnPrompt'), numeric: true },
  { key: 'completion', header: t('dashboard.columnCompletion'), numeric: true },
])

/** Billed cost, or a dash where nothing was billed: $0 would claim a bill that never existed. */
function billedCost(row: UsageRow): string {
  return row.billedCalls > 0 ? formatCost(row.cost, locale.value) : t('dashboard.none')
}
</script>

<template>
  <UiScreen>
    <UiRail>
      <UiPageHeader :eyebrow="t('dashboard.period', { days: USAGE_DAYS })" :title="t('dashboard.headTitle')" />

      <!-- Figures on rules, not in boxes. Cost is the accent and the largest of them: it is
           the number this page exists to answer. -->
      <dl v-if="summary" class="totals">
        <div class="total">
          <dt class="eyebrow">{{ t('dashboard.totalCost') }}</dt>
          <!-- A period with calls but none billed has no billed figure, not a $0 one. -->
          <dd class="figure cost tabular">
            {{
              summary.totalBilledCalls || summary.isEmpty
                ? formatCost(summary.totalCost, locale)
                : t('dashboard.none')
            }}
          </dd>
        </div>
        <!-- An estimate is a figure of its own, never added to the billed one above, and
             not in the accent: it is what an endpoint said, not what anyone was charged. -->
        <div v-if="summary.totalEstimatedCalls" class="total">
          <dt class="eyebrow">{{ t('dashboard.totalEstimatedCost') }}</dt>
          <dd class="figure tabular">
            {{ t('dashboard.estimatedValue', { cost: formatCost(summary.totalEstimatedCost, locale) }) }}
          </dd>
          <dd class="total-note">{{ t('dashboard.totalEstimatedNote') }}</dd>
        </div>
        <div v-if="summary.totalUnpricedCalls" class="total">
          <dt class="eyebrow">{{ t('dashboard.totalUnpriced') }}</dt>
          <dd class="figure tabular">{{ formatCount(summary.totalUnpricedCalls, locale) }}</dd>
          <dd class="total-note">{{ t('dashboard.totalUnpricedNote') }}</dd>
        </div>
        <div class="total">
          <dt class="eyebrow">{{ t('dashboard.totalPromptTokens') }}</dt>
          <dd class="figure tabular">{{ formatCount(summary.totalPromptTokens, locale) }}</dd>
        </div>
        <div class="total">
          <dt class="eyebrow">{{ t('dashboard.totalCompletionTokens') }}</dt>
          <dd class="figure tabular">{{ formatCount(summary.totalCompletionTokens, locale) }}</dd>
        </div>
      </dl>
    </UiRail>

    <!-- Two independent collections, so two panels, each scrolling its own rows: one
         scrollbar dragging both would make the reader scroll past the days to reach the
         models. -->
    <UiRegion split>
      <UiPanel :title="t('dashboard.perDay')" lead>
        <!-- Refresh lives in the header of the panel it reloads: the design has no sticky
             top bar for it to sit in, and both tables come from the one request. -->
        <template #meta>
          <span v-if="summary && !summary.isEmpty">
            {{ t('dashboard.daysRecorded', { count: summary.days.length }) }}
          </span>
          <UiButton
            variant="ghost"
            size="xs"
            icon-only
            :label="t('common.refresh')"
            :loading="pending"
            @click="refresh"
          >
            <template #icon><UiIcon name="refresh" /></template>
          </UiButton>
        </template>

        <!-- A failed refresh keeps the figures it already has: the banner says what went
             wrong, and blanking the page on top of that helps nobody. -->
        <UiBanner v-if="errorMessage" tone="error">{{ errorMessage }}</UiBanner>

        <UiSkeleton v-if="!summary && !errorMessage" :rows="4" />

        <UiEmptyState
          v-else-if="summary?.isEmpty"
          :title="t('dashboard.empty')"
          :body="t('dashboard.emptyBody')"
        />

        <UiDataTable
          v-else-if="summary"
          :columns="dayColumns"
          :rows="summary.days"
          :row-key="(row) => row.label"
          :caption="t('dashboard.perDay')"
          density="tight"
        >
          <template #cell-label="{ row }">{{ formatDay(row.label, locale) }}</template>
          <template #cell-share="{ row }">
            <UiUsageBar :share="row.costShare" :label="formatDay(row.label, locale)" />
          </template>
          <template #cell-cost="{ row }">{{ billedCost(row) }}</template>
          <template #cell-estimated="{ row }">
            {{
              row.estimatedCalls
                ? t('dashboard.estimatedValue', { cost: formatCost(row.estimatedCost, locale) })
                : t('dashboard.none')
            }}
          </template>
          <template #cell-unpriced="{ row }">
            {{ row.unpricedCalls ? formatCount(row.unpricedCalls, locale) : t('dashboard.none') }}
          </template>
          <template #cell-prompt="{ row }">{{ formatCount(row.promptTokens, locale) }}</template>
          <template #cell-completion="{ row }">
            {{ formatCount(row.completionTokens, locale) }}
          </template>
        </UiDataTable>
      </UiPanel>

      <!-- Not a table: a model id is long and its two token figures are secondary to its
           cost, so each model gets a small block of its own instead of five columns whose
           first one would set the width of all of them. -->
      <UiPanel :title="t('dashboard.perModel')" sunken divided>
        <template v-if="summary?.models.length" #meta>
          {{ t('dashboard.modelsRecorded', { count: summary.models.length }) }}
        </template>

        <ul v-if="summary?.models.length" class="models">
          <li v-for="row in summary.models" :key="row.label" class="model">
            <div class="model-main">
              <!-- Ellipsized to keep the row rhythm, but never lost: two models sharing a
                 long provider prefix must stay tellable apart, and this figure is what
                 attributes the cost beside it. -->
            <p class="model-id mono" :title="row.label">{{ row.label }}</p>
              <UiUsageBar :share="row.costShare" :label="row.label" />
              <p class="model-tokens tabular">
                {{
                  t('dashboard.modelTokens', {
                    prompt: formatCount(row.promptTokens, locale),
                    completion: formatCount(row.completionTokens, locale),
                  })
                }}
              </p>
              <p v-if="row.estimatedCalls" class="model-tokens tabular">
                {{ t('dashboard.modelEstimated', { cost: formatCost(row.estimatedCost, locale) }) }}
              </p>
              <p v-if="row.unpricedCalls" class="model-tokens tabular">
                {{ t('dashboard.modelUnpriced', { count: formatCount(row.unpricedCalls, locale) }) }}
              </p>
            </div>
            <p class="model-cost tabular">{{ billedCost(row) }}</p>
          </li>
        </ul>
      </UiPanel>
    </UiRegion>
  </UiScreen>
</template>

<style scoped>
/* Each figure is a band between rules — the stack's own bottom rule closes it so it does
   not trail off into the rail's whitespace, however many figures the period shows. */
.totals {
  display: flex;
  flex-direction: column;
  margin: 0;
  border-bottom: 1px solid var(--line);
}

.total {
  padding: var(--space-5) 0;
  border-top: 1px solid var(--line);
}

.total-note {
  margin: var(--space-2) 0 0;
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--muted);
}

.figure {
  margin: var(--space-2) 0 0;
  font-family: var(--font-display);
  font-size: var(--display-lg);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-tight);
  line-height: 1.1;
}

/* Cost is data, and the largest thing in the rail. */
.figure.cost {
  font-size: var(--display-2xl);
  color: var(--accent);
}

.models {
  list-style: none;
  margin: 0;
  padding: 0;
}

.model {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px;
  align-items: baseline;
  gap: var(--space-4);
  padding: var(--space-4) 0;
  border-top: 1px solid var(--hair);
}

.model-main {
  min-width: 0;
}

.model-id {
  margin-bottom: var(--space-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink);
}

.model-tokens {
  margin-top: var(--space-2);
  font-size: var(--text-2xs);
  color: var(--faint);
}

.model-cost {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--display-sm);
  font-weight: var(--weight-semibold);
  text-align: right;
}
</style>
