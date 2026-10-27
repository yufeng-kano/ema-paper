<script setup lang="ts">
/**
 * The console's *Overview* (docs/admin.md § Pages, *Overview*; § Overview, jobs and usage):
 * what the deployment is doing, read in one request — four tiles (the queued, the running and
 * how many of them are stuck, the jobs failed lately, today's model cost), the services (the
 * worker, Kano Proxy, Kano Pay), the newest audit records for a caller who holds `audit.read`,
 * and today's usage per model. Every figure is the API's; *Refresh* reads them again.
 */
import { getAdminOverview, type AdminOverviewResponse, type AuditEntry } from '~/lib/api'
import { formatElapsed, formatUsd, formatUtcDay } from '~/lib/admin'
import { formatCount, formatShortDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

/** A row of today's usage: a model's, or the totals under them. */
type UsageRow = AdminOverviewResponse['usage']['models'][number] & { total: boolean }
type Tone = 'ok' | 'danger' | 'info' | 'neutral'

const { t, te, locale } = useI18n()
const localePath = useLocalePath()
const { explain } = useAdminFailure()

const data = shallowRef<AdminOverviewResponse | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)
/** When the figures were read: *updated {time}*, and what *waiting* counts to. */
const readAt = ref(Date.now())
let seq = 0

async function load(): Promise<void> {
  const mine = ++seq
  pending.value = true
  try {
    const loaded = await getAdminOverview()
    if (mine !== seq) {
      return
    }
    data.value = loaded
    readAt.value = Date.now()
    error.value = null
  } catch (failure) {
    const message = await explain(failure)
    if (mine === seq) {
      error.value = message
    }
  } finally {
    if (mine === seq) {
      pending.value = false
    }
  }
}

onMounted(load)

function count(value: number): string {
  return formatCount(value, locale.value)
}

function time(iso: string | number): string {
  return new Intl.DateTimeFormat(locale.value, { timeStyle: 'short' }).format(new Date(iso))
}

const headline = computed(() => {
  const overview = data.value
  if (!overview) {
    return ''
  }
  return t('admin.overview.meta', {
    date: formatUtcDay(overview.day, locale.value),
    accounts: t('admin.overview.accounts', { n: count(overview.accounts) }, overview.accounts),
    time: time(readAt.value),
  })
})

const tokensToday = computed(() => {
  const totals = data.value?.usage.totals
  return totals ? totals.prompt_tokens + totals.completion_tokens : 0
})

// --- the services ------------------------------------------------------------------------

interface ServiceRow {
  key: string
  name: string
  tone: Tone
  state: string
  line: string
  fact: string | null
}

const services = computed<ServiceRow[]>(() => {
  const overview = data.value
  if (!overview) {
    return []
  }
  const { worker } = overview
  const { kano_proxy: proxy, kano_pay: pay } = overview.connections
  const states = (key: string) => t(`admin.overview.states.${key}`)
  return [
    {
      key: 'worker',
      name: t('admin.overview.worker'),
      tone: worker.state === 'running' ? 'ok' : worker.state === 'stopped' ? 'danger' : 'neutral',
      state: states(worker.state),
      line:
        worker.state === 'running'
          ? worker.generation === null
            ? t('admin.overview.workerNoGeneration', { time: worker.since ? formatShortDateTime(worker.since, locale.value) : '—' })
            : t('admin.overview.workerSince', {
                generation: worker.generation,
                time: worker.since ? formatShortDateTime(worker.since, locale.value) : '—',
              })
          : worker.state === 'stopped'
            ? t('admin.overview.workerStopped')
            : t('admin.overview.workerUnknownLine'),
      fact: worker.state === 'running' ? t('admin.overview.holdsLock') : null,
    },
    {
      key: 'kano_proxy',
      name: t('admin.overview.kanoProxy'),
      tone: proxy.state === 'ok' ? 'ok' : proxy.state === 'failed' ? 'danger' : 'neutral',
      state: states(proxy.state),
      line:
        proxy.state === 'ok'
          ? t(
              'admin.overview.proxyModels',
              { n: count(proxy.models ?? 0), priced: count(proxy.priced ?? 0) },
              proxy.models ?? 0,
            )
          : proxy.state === 'failed'
            ? (proxy.message ?? '')
            : t('admin.overview.proxyUnconfigured'),
      fact: proxy.state === 'ok' && proxy.parse_model ? t('admin.overview.parseModel', { model: proxy.parse_model }) : null,
    },
    {
      key: 'kano_pay',
      name: t('admin.overview.kanoPay'),
      tone: pay.state === 'ok' ? 'ok' : pay.state === 'failed' ? 'danger' : 'neutral',
      state: states(pay.state),
      line:
        pay.state === 'ok'
          ? t('admin.overview.payOnSale', {
              plans: t('admin.overview.plans', { n: count(pay.plans ?? 0) }, pay.plans ?? 0),
              packs: t('admin.overview.packs', { n: count(pay.packs ?? 0) }, pay.packs ?? 0),
            })
          : pay.state === 'failed'
            ? (pay.message ?? '')
            : t('admin.overview.payUnconfigured'),
      fact:
        pay.state === 'ok'
          ? pay.checkout_enabled
            ? t('admin.overview.checkoutOpen')
            : t('admin.overview.checkoutClosed')
          : null,
    },
  ]
})

// --- recent changes ----------------------------------------------------------------------

function actorName(row: AuditEntry): string {
  return row.actor ? row.actor.name?.trim() || row.actor.email : t('admin.deletedAccount')
}

/** The catalog's words for an action; one it does not know prints as its key. */
function actionLabel(action: string): string {
  return te(`admin.audit.actions.${action}`) ? t(`admin.audit.actions.${action}`) : action
}

/** What the target is called now; a job, or one gone, by its type and id. */
function targetLabel(row: AuditEntry): string {
  if (row.target_label) {
    return row.target_label
  }
  const type = te(`admin.audit.targets.${row.target_type}`) ? t(`admin.audit.targets.${row.target_type}`) : row.target_type
  return `${type} ${row.target_id}`
}

// --- today's usage -----------------------------------------------------------------------

const usageRows = computed<UsageRow[]>(() => {
  const usage = data.value?.usage
  if (!usage || !usage.models.length) {
    return []
  }
  return [...usage.models.map((row) => ({ ...row, total: false })), { ...usage.totals, model: '', total: true }]
})

const usageColumns = computed<TableColumn<UsageRow>[]>(() => [
  { key: 'model', header: t('admin.usage.model') },
  { key: 'calls', header: t('admin.usage.calls'), numeric: true, value: (row) => count(row.calls) },
  { key: 'input', header: t('admin.usage.input'), numeric: true, value: (row) => count(row.prompt_tokens) },
  { key: 'output', header: t('admin.usage.output'), numeric: true, value: (row) => count(row.completion_tokens) },
  {
    key: 'cost',
    header: t('admin.usage.costUsd'),
    numeric: true,
    value: (row) => formatUsd(row.estimated_cost, locale.value),
  },
])

function usageRowAttrs(row: UsageRow): Record<string, unknown> {
  return row.total ? { class: 'total' } : {}
}
</script>

<template>
  <UiScreen>
    <template #toolbar>
      <UiButton icon-only :label="t('common.refresh')" :loading="pending" @click="load">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.overview.title')">
        <span v-if="data" class="tabular">{{ headline }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="error" class="state" tone="error">
        {{ error }}
        <template #actions>
          <UiButton size="sm" variant="secondary" :loading="pending" @click="load">{{ t('common.refresh') }}</UiButton>
        </template>
      </UiBanner>
      <UiSkeleton v-if="!data && !error" class="state" />

      <div v-if="data" class="overview">
        <div class="tiles">
          <div class="tile">
            <p class="tile-label">{{ t('admin.overview.queued') }}</p>
            <p class="tile-value tabular">{{ count(data.jobs.queued) }}</p>
            <p class="tile-foot">
              {{
                data.jobs.oldest_queued_at
                  ? t('admin.overview.oldestWaiting', {
                      duration: formatElapsed(readAt - new Date(data.jobs.oldest_queued_at).getTime(), locale),
                    })
                  : t('admin.overview.nothingWaiting')
              }}
            </p>
          </div>
          <div class="tile">
            <p class="tile-label">{{ t('admin.overview.running') }}</p>
            <p class="tile-value tabular">{{ count(data.jobs.running) }}</p>
            <p class="tile-foot">
              <template v-if="data.jobs.stuck === null">{{ t('admin.overview.workerUnknown') }}</template>
              <template v-else-if="data.jobs.stuck > 0">
                <span class="stuck">{{ t('admin.overview.stuck', { n: count(data.jobs.stuck) }) }}</span>
                ·
                <NuxtLink :to="localePath({ path: '/admin/jobs', query: { status: 'running' } })">
                  {{ t('admin.overview.seeJobs') }}
                </NuxtLink>
              </template>
              <template v-else>{{ t('admin.overview.noneStuck') }}</template>
            </p>
          </div>
          <div class="tile">
            <p class="tile-label">{{ t('admin.overview.failed', { n: data.jobs.failed_hours }) }}</p>
            <p class="tile-value tabular">{{ count(data.jobs.failed) }}</p>
            <p class="tile-foot">
              <template v-if="data.jobs.failed && data.jobs.last_failed_at">
                {{ t('admin.overview.lastFailed', { time: formatShortDateTime(data.jobs.last_failed_at, locale) }) }}
                ·
                <NuxtLink :to="localePath({ path: '/admin/jobs', query: { status: 'failed' } })">
                  {{ t('admin.overview.seeJobs') }}
                </NuxtLink>
              </template>
              <template v-else>{{ t('admin.overview.noneFailed') }}</template>
            </p>
          </div>
          <div class="tile">
            <p class="tile-label">{{ t('admin.overview.costToday') }}</p>
            <p class="tile-value tabular">{{ formatUsd(data.usage.totals.estimated_cost, locale) }}</p>
            <p class="tile-foot">
              {{ t('admin.overview.calls', { n: count(data.usage.totals.calls) }, data.usage.totals.calls) }}
              ·
              {{ t('admin.overview.tokens', { n: count(tokensToday) }, tokensToday) }}
            </p>
          </div>
        </div>

        <div class="boxes" :class="{ single: !data.audit }">
          <SettingsGroup class="box" :title="t('admin.overview.services')">
            <div v-for="service in services" :key="service.key" class="row service">
              <div class="service-text">
                <p class="service-head">
                  <span class="service-name">{{ service.name }}</span>
                  <UiStatusDot :tone="service.tone" :label="service.state" />
                </p>
                <p class="service-line">{{ service.line }}</p>
              </div>
              <span v-if="service.fact" class="service-fact">{{ service.fact }}</span>
            </div>
          </SettingsGroup>

          <SettingsGroup v-if="data.audit" class="box" :title="t('admin.overview.recent')">
            <template #actions>
              <NuxtLink class="link" :to="localePath('/admin/audit')">{{ t('admin.overview.allRecords') }}</NuxtLink>
            </template>
            <p v-if="!data.audit.length" class="row muted">{{ t('admin.overview.noRecords') }}</p>
            <div v-for="entry in data.audit" :key="entry.id" class="row record">
              <span class="record-time tabular">{{ formatShortDateTime(entry.created_at, locale) }}</span>
              <p class="record-text">
                <span class="record-who">{{ actorName(entry) }}</span>
                {{ actionLabel(entry.action) }}
                <span class="muted">{{ targetLabel(entry) }}</span>
              </p>
            </div>
          </SettingsGroup>
        </div>

        <SettingsGroup :title="t('admin.overview.today')" plain>
          <template #actions>
            <NuxtLink class="link" :to="localePath('/admin/usage')">{{ t('admin.overview.seeDays') }}</NuxtLink>
          </template>
          <UiEmptyState v-if="!usageRows.length" compact :title="t('admin.overview.noCalls')" />
          <UiDataTable
            v-else
            class="usage-table"
            density="tight"
            :columns="usageColumns"
            :rows="usageRows"
            :row-key="(row) => (row.total ? '' : row.model)"
            :row-attrs="usageRowAttrs"
            :caption="t('admin.overview.todayCaption')"
          >
            <template #cell-model="{ row }">
              <span v-if="row.total" class="total-label">{{ t('admin.usage.total') }}</span>
              <span v-else class="model mono cell-block" :title="row.model">{{ row.model }}</span>
            </template>
          </UiDataTable>
          <template #foot>
            <p>{{ t('admin.usage.note') }}</p>
            <p v-if="data.usage.totals.unpriced_calls">
              {{
                t(
                  'admin.usage.unpriced',
                  { n: count(data.usage.totals.unpriced_calls) },
                  data.usage.totals.unpriced_calls,
                )
              }}
            </p>
          </template>
        </SettingsGroup>
      </div>
    </UiPanel>
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

.overview {
  display: grid;
  gap: var(--space-2);
  padding-bottom: var(--space-6);
}

.tiles {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: var(--space-4);
  margin-top: var(--space-2);
}

.tile {
  display: grid;
  align-content: start;
  gap: var(--space-1);
  min-width: 0;
  padding: var(--space-4);
  border-radius: var(--radius-lg);
  background: var(--paper-sunken);
}

.tile-label,
.tile-foot {
  color: var(--muted);
  font-size: var(--text-xs);
}

.tile-value {
  color: var(--ink);
  font-size: var(--display-lg);
  font-weight: var(--weight-semibold);
  line-height: 1.15;
}

.tile-foot a,
.link {
  color: var(--accent);
  text-decoration: none;
}

.tile-foot a:hover,
.link:hover {
  text-decoration: underline;
}

.stuck {
  color: var(--danger);
}

.boxes {
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
  gap: 0 var(--space-4);
}

.boxes.single {
  grid-template-columns: minmax(0, 1fr);
}

.row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-height: 52px;
  padding: var(--space-3) 0;
}

.row + .row {
  border-top: 1px solid var(--line);
}

.service-text {
  display: grid;
  flex: 1;
  gap: 2px;
  min-width: 0;
}

.service-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-1) var(--space-3);
  font-size: var(--text-sm);
}

.service-name {
  color: var(--ink);
  font-weight: var(--weight-medium);
}

.service-line,
.service-fact,
.record-time {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.service-line {
  overflow-wrap: anywhere;
}

.service-fact {
  flex-shrink: 0;
  max-width: 40%;
  text-align: right;
}

.record {
  min-height: 42px;
  padding: var(--space-2) 0;
}

.record-time {
  flex-shrink: 0;
  width: 7.5rem;
}

.record-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.record-who {
  font-weight: var(--weight-medium);
}

.muted {
  color: var(--muted);
}

.usage-table {
  --table-min: 560px;
}

.model {
  max-width: 32ch;
}

.total-label {
  font-weight: var(--weight-semibold);
}

:deep(tr.total td) {
  font-weight: var(--weight-semibold);
}

@media (max-width: 900px) {
  .tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .boxes {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
