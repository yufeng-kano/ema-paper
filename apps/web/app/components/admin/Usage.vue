<script setup lang="ts">
/**
 * The console's *Usage* screen (docs/admin.md § Pages, *Usage*; § Overview, jobs and usage):
 * what the model calls of every account cost, per UTC day and model, over a range — 7, 30 or
 * 90 days up to today, or the one a link names. Four figures and the daily cost over the
 * range, drawn by `AdminCostChart`, then the table that is the chart's data. The cost is the
 * estimate `usage_log` recorded; a billed amount gets a column of its own only in a range that
 * has one, and the calls without a cost are said under the table.
 *
 * The range lives in the route query (`?from=&to=`), so Back and a reload return to it.
 */
import { getAdminUsage, type AdminUsageResponse } from '~/lib/api'
import {
  USAGE_PRESETS,
  costDays,
  daysOf,
  formatUsd,
  formatUtcDay,
  presetOf,
  presetRange,
  usageRangeOf,
  utcDay,
} from '~/lib/admin'
import { formatCount } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

type UsageRow = AdminUsageResponse['rows'][number]

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const { explain } = useAdminFailure()

/** Today's UTC day, read again with every read, so a page left open past midnight moves on. */
const today = ref(utcDay(new Date()))
const range = computed(() => usageRangeOf(route.query, today.value))
const preset = computed(() => presetOf(range.value, today.value))

function choosePreset(days: number) {
  const next = presetRange(days, utcDay(new Date()))
  if (next.from !== range.value.from || next.to !== range.value.to) {
    void router.push({ query: { ...route.query, ...next } })
  }
}

// --- the figures ---------------------------------------------------------------------------

const data = shallowRef<AdminUsageResponse | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)
/** Only the newest read may write. */
let seq = 0

async function load(): Promise<void> {
  const mine = ++seq
  today.value = utcDay(new Date())
  pending.value = true
  try {
    const loaded = await getAdminUsage(range.value)
    if (mine !== seq) {
      return
    }
    data.value = loaded
    error.value = null
  } catch (failure) {
    // A range the API refuses — backwards, too long — says why above the figures.
    const message = await explain(failure)
    if (mine === seq) {
      data.value = null
      error.value = message
    }
  } finally {
    if (mine === seq) {
      pending.value = false
    }
  }
}

onMounted(load)
watch(() => [range.value.from, range.value.to], () => void load())

const headline = computed(() =>
  t('admin.usage.range', {
    from: formatUtcDay(range.value.from, locale.value),
    to: formatUtcDay(range.value.to, locale.value),
  }),
)

const figures = computed(() => {
  const totals = data.value?.totals
  if (!totals) {
    return []
  }
  return [
    { key: 'cost', label: t('admin.usage.cost'), value: formatUsd(totals.estimated_cost, locale.value) },
    { key: 'calls', label: t('admin.usage.calls'), value: formatCount(totals.calls, locale.value) },
    { key: 'input', label: t('admin.usage.input'), value: formatCount(totals.prompt_tokens, locale.value) },
    { key: 'output', label: t('admin.usage.output'), value: formatCount(totals.completion_tokens, locale.value) },
  ]
})

/** The chart's days: the answer's range, every day of it. */
const chartDays = computed(() => (data.value ? costDays(daysOf(data.value), data.value.rows) : []))

const chartLabel = computed(() =>
  data.value
    ? t('admin.usage.chart', {
        from: formatUtcDay(data.value.from, locale.value),
        to: formatUtcDay(data.value.to, locale.value),
      })
    : '',
)

// --- the table -----------------------------------------------------------------------------

/** A billed amount is historical: its column shows only in a range that has one. */
const billed = computed(() => (data.value?.totals.cost ?? 0) > 0)

const columns = computed<TableColumn<UsageRow>[]>(() => [
  { key: 'date', header: t('admin.usage.date') },
  { key: 'model', header: t('admin.usage.model') },
  { key: 'calls', header: t('admin.usage.calls'), numeric: true, value: (row) => formatCount(row.calls, locale.value) },
  {
    key: 'input',
    header: t('admin.usage.input'),
    numeric: true,
    value: (row) => formatCount(row.prompt_tokens, locale.value),
  },
  {
    key: 'output',
    header: t('admin.usage.output'),
    numeric: true,
    value: (row) => formatCount(row.completion_tokens, locale.value),
  },
  {
    key: 'cost',
    header: t('admin.usage.costUsd'),
    numeric: true,
    value: (row) => formatUsd(row.estimated_cost, locale.value),
  },
  ...(billed.value
    ? [
        {
          key: 'billed',
          header: t('admin.usage.billed'),
          numeric: true,
          value: (row: UsageRow) => formatUsd(row.cost, locale.value),
        },
      ]
    : []),
])

/** A day is named on its first row only: the rows that start a day. */
const dayStarts = computed(() => {
  const rows = data.value?.rows ?? []
  return new Set(rows.filter((row, index) => index === 0 || rows[index - 1]?.date !== row.date))
})
</script>

<template>
  <UiScreen>
    <template #toolbar-start>
      <UiSegmented glass role="radiogroup" :aria-label="t('admin.usage.rangeLabel')">
        <button
          v-for="days in USAGE_PRESETS"
          :key="days"
          type="button"
          role="radio"
          class="segment"
          :class="{ active: preset === days }"
          :aria-checked="preset === days"
          @click="choosePreset(days)"
        >
          {{ t('admin.usage.days', { n: days }, days) }}
        </button>
      </UiSegmented>
    </template>

    <template #toolbar>
      <UiButton icon-only :label="t('common.refresh')" :loading="pending" @click="load">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.usage.title')">
        <span class="tabular">{{ headline }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="error" class="state" tone="error">{{ error }}</UiBanner>
      <UiSkeleton v-if="!data && !error" class="state" />
      <template v-else-if="data">
        <section class="overview">
          <dl class="figures">
            <div v-for="figure in figures" :key="figure.key" class="figure">
              <dt>{{ figure.label }}</dt>
              <dd class="tabular">{{ figure.value }}</dd>
            </div>
          </dl>
          <AdminCostChart v-if="data.rows.length" :days="chartDays" :label="chartLabel" />
        </section>

        <UiEmptyState v-if="!data.rows.length" :title="t('admin.usage.empty')" />
        <template v-else>
          <UiDataTable
            class="usage-table"
            density="tight"
            :columns="columns"
            :rows="data.rows"
            :row-key="(row) => `${row.date}|${row.model}`"
            :caption="t('admin.usage.caption')"
          >
            <template #cell-date="{ row }">
              <span v-if="dayStarts.has(row)" class="day tabular">{{ formatUtcDay(row.date, locale) }}</span>
            </template>
            <template #cell-model="{ row }">
              <span class="model mono cell-block" :title="row.model">{{ row.model }}</span>
            </template>
          </UiDataTable>
          <footer class="foot">
            <p>{{ t('admin.usage.note') }}</p>
            <p v-if="data.totals.unpriced_calls">
              {{
                t(
                  'admin.usage.unpriced',
                  { n: formatCount(data.totals.unpriced_calls, locale) },
                  data.totals.unpriced_calls,
                )
              }}
            </p>
          </footer>
        </template>
      </template>
    </UiPanel>
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

.overview {
  display: grid;
  gap: var(--space-3);
  margin: var(--space-2) 0 var(--space-5);
  padding: var(--space-4) var(--space-4) var(--space-3);
  border-radius: var(--radius-lg);
  background: var(--paper-sunken);
}

.figures {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, max-content));
  gap: var(--space-2) var(--space-6);
  margin: 0;
}

.figure {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.figure dt {
  color: var(--muted);
  font-size: var(--text-xs);
}

.figure dd {
  margin: 0;
  color: var(--ink);
  font-size: var(--display-md);
  font-weight: var(--weight-semibold);
  line-height: 1.2;
}

.usage-table {
  --table-min: 720px;
}

.day {
  font-weight: var(--weight-medium);
}

.model {
  max-width: 28ch;
}

.foot {
  display: grid;
  gap: var(--space-1);
  padding: var(--space-3) var(--cell-x) 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

@media (max-width: 900px) {
  .figures {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
