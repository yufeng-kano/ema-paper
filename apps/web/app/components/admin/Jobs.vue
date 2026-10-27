<script setup lang="ts">
/**
 * The console's *Jobs* screen (docs/admin.md § Pages, *Jobs*; § Overview, jobs and usage):
 * every job on record, newest first, filtered by status and paged, with the worker's state
 * beside the title — and, with `jobs.write`, *Requeue* on a row the API says is stuck, asked
 * first in a dialog. Never a file name or anything of the document: the API sends none.
 *
 * The filter and the page live in the route query (`?status=&page=`), so Back and a reload
 * return to them; nothing of the list is kept in browser storage.
 */
import { listAdminJobs, requeueAdminJob, type AdminJob, type AdminJobsResponse } from '~/lib/api'
import {
  JOB_STATUSES,
  adminFailure,
  formatElapsed,
  jobStatusOf,
  pageOf,
  requeueRefusalKey,
  type JobStatus,
} from '~/lib/admin'
import { formatCount, formatShortDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const { explain } = useAdminFailure()
const adminConsole = useAdminConsole()

const status = computed(() => jobStatusOf(route.query.status))
const page = computed(() => pageOf(route.query.page))
const canRequeue = computed(() => adminConsole.can('jobs', 'write'))

/** Moves the query; a filter starts again at the first page. */
function setQuery(patch: Record<string, string | null>, replace = false) {
  const query = Object.fromEntries(Object.entries({ ...route.query, ...patch }).filter(([, value]) => value !== null))
  void (replace ? router.replace({ query }) : router.push({ query }))
}

function chooseStatus(value: JobStatus | null) {
  if (value !== status.value) {
    setQuery({ status: value, page: null })
  }
}

function goToPage(next: number) {
  setQuery({ page: next > 1 ? String(next) : null })
}

// --- the list ------------------------------------------------------------------------------

const data = shallowRef<AdminJobsResponse | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)
/** When the list was read: what *created {duration} ago* counts to. */
const readAt = ref(Date.now())
/** Only the newest read may write. */
let seq = 0

async function load(): Promise<void> {
  const mine = ++seq
  pending.value = true
  try {
    const loaded = await listAdminJobs({ page: page.value, status: status.value ?? undefined })
    if (mine !== seq) {
      return
    }
    const last = Math.max(1, Math.ceil(loaded.total / loaded.page_size))
    if (!loaded.items.length && page.value > last) {
      // A page past the end — a link from a longer list: its last page instead.
      setQuery({ page: last > 1 ? String(last) : null }, true)
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
watch([status, page], () => void load())

/** Every job on record, whatever the filter. */
const onRecord = computed(() => {
  const counts = data.value?.counts
  return counts ? JOB_STATUSES.reduce((sum, key) => sum + counts[key], 0) : 0
})

const workerLine = computed(() => {
  const worker = data.value?.worker
  if (!worker || worker.state === 'unknown') {
    return t('admin.jobs.workerUnknown')
  }
  if (worker.state === 'stopped') {
    return t('admin.jobs.workerStopped')
  }
  return worker.generation === null
    ? t('admin.jobs.workerRunningNoGeneration')
    : t('admin.jobs.workerRunning', { generation: worker.generation })
})

/** The filter's segments: every status but Done carries its count. */
const segments = computed(() =>
  JOB_STATUSES.map((key) => ({
    key,
    label: t(`admin.jobs.statuses.${key}`),
    count: key === 'succeeded' ? null : formatCount(data.value?.counts[key] ?? 0, locale.value),
  })),
)

const columns = computed<TableColumn<AdminJob>[]>(() => [
  { key: 'time', header: t('admin.jobs.time') },
  { key: 'account', header: t('admin.jobs.account') },
  { key: 'pages', header: t('admin.jobs.pages'), numeric: true },
  { key: 'model', header: t('admin.jobs.model') },
  { key: 'status', header: t('admin.jobs.status') },
  { key: 'failure', header: t('admin.jobs.failure') },
  ...(canRequeue.value
    ? [{ key: 'requeue', header: '', srHeader: t('admin.jobs.requeue'), align: 'end' as const, width: '7.5rem' }]
    : []),
])

const TONES: Record<JobStatus, 'ok' | 'danger' | 'info' | 'neutral'> = {
  queued: 'neutral',
  running: 'info',
  succeeded: 'ok',
  failed: 'danger',
}

/** What follows a row's status: a queued job's age — from `created_at`, which a retry or
 *  a requeue keeps, so not how long it has waited — a running one's generation, or that it
 *  is stuck. */
function statusNote(job: AdminJob): string | null {
  if (job.status === 'queued') {
    const age = readAt.value - new Date(job.created_at).getTime()
    return t('admin.jobs.created', { duration: formatElapsed(age, locale.value) })
  }
  if (job.status === 'running') {
    if (job.stuck) {
      return t('admin.jobs.stuck')
    }
    return job.worker_generation === null ? null : t('admin.jobs.generation', { generation: job.worker_generation })
  }
  return null
}

function pagesLabel(job: AdminJob): string {
  const done = formatCount(job.pages_done, locale.value)
  return job.pages === null ? done : `${done} / ${formatCount(job.pages, locale.value)}`
}

// --- requeue -------------------------------------------------------------------------------

const requeuing = ref<AdminJob | null>(null)
const requeuePending = ref(false)
const requeueError = ref<string | null>(null)

function askRequeue(job: AdminJob) {
  requeuing.value = job
  requeueError.value = null
}

function closeRequeue() {
  if (!requeuePending.value) {
    requeuing.value = null
  }
}

async function confirmRequeue(): Promise<void> {
  const job = requeuing.value
  if (!job) {
    return
  }
  requeuePending.value = true
  requeueError.value = null
  try {
    await requeueAdminJob(job.id)
    requeuing.value = null
  } catch (failure) {
    // A refusal is said in the dialog; the list is read again behind it, so the row shows
    // what the job is now.
    const refused = adminFailure(failure)
    const key = refused.kind === 'conflict' ? requeueRefusalKey(refused.reason) : null
    requeueError.value = key ? t(key) : await explain(failure, t('admin.jobs.gone'))
  } finally {
    requeuePending.value = false
  }
  void load()
}
</script>

<template>
  <UiScreen>
    <template #toolbar-start>
      <UiSegmented class="filter-wide" glass role="radiogroup" :aria-label="t('admin.jobs.filter')">
        <button
          type="button"
          role="radio"
          class="segment"
          :class="{ active: status === null }"
          :aria-checked="status === null"
          @click="chooseStatus(null)"
        >
          {{ t('admin.jobs.all') }}
        </button>
        <button
          v-for="segment in segments"
          :key="segment.key"
          type="button"
          role="radio"
          class="segment"
          :class="{ active: status === segment.key }"
          :aria-checked="status === segment.key"
          @click="chooseStatus(segment.key)"
        >
          {{ segment.label }}
          <span v-if="segment.count !== null" class="segment-count tabular">{{ segment.count }}</span>
        </button>
      </UiSegmented>
      <!-- The same choice where five segments do not fit. -->
      <UiSelect
        class="filter-narrow"
        :model-value="status ?? ''"
        :aria-label="t('admin.jobs.filter')"
        @update:model-value="chooseStatus(jobStatusOf($event))"
      >
        <option value="">{{ t('admin.jobs.all') }}</option>
        <option v-for="segment in segments" :key="segment.key" :value="segment.key">
          {{ segment.count === null ? segment.label : `${segment.label} ${segment.count}` }}
        </option>
      </UiSelect>
    </template>

    <template #toolbar>
      <UiButton icon-only :label="t('common.refresh')" :loading="pending" @click="load">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.jobs.title')">
        <span v-if="data" class="tabular">
          {{ t('admin.jobs.count', { n: formatCount(onRecord, locale) }, onRecord) }} · {{ workerLine }}
        </span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="error" class="state" tone="error">{{ error }}</UiBanner>
      <UiSkeleton v-if="!data && !error" class="state" />
      <UiEmptyState
        v-else-if="data && !data.items.length"
        :title="status ? t('admin.jobs.noMatch') : t('admin.jobs.empty')"
      />
      <UiDataTable
        v-else-if="data"
        class="jobs-table"
        :columns="columns"
        :rows="data.items"
        :row-key="(row) => row.id"
        :caption="t('admin.jobs.title')"
      >
        <template #cell-time="{ row }">
          <span class="muted tabular">{{ formatShortDateTime(row.created_at, locale) }}</span>
        </template>
        <template #cell-account="{ row }">
          <span class="account cell-block" :class="{ muted: !row.email }">
            {{ row.email ?? t('admin.accountId', { id: row.user_id }) }}
          </span>
        </template>
        <template #cell-pages="{ row }">{{ pagesLabel(row) }}</template>
        <template #cell-model="{ row }">
          <span class="model mono cell-block" :title="row.model">{{ row.model }}</span>
        </template>
        <template #cell-status="{ row }">
          <span class="status">
            <UiStatusDot :tone="TONES[row.status]" :label="t(`admin.jobs.statuses.${row.status}`)" />
            <span v-if="statusNote(row)" class="note" :class="{ stuck: row.stuck }">{{ statusNote(row) }}</span>
          </span>
        </template>
        <template #cell-failure="{ row }">
          <span v-if="row.error" class="failure muted cell-block" :title="row.error">{{ row.error }}</span>
          <span v-else class="faint">—</span>
        </template>
        <template #cell-requeue="{ row }">
          <UiButton v-if="row.stuck" variant="secondary" size="xs" @click="askRequeue(row)">
            {{ t('admin.jobs.requeue') }}
          </UiButton>
        </template>
      </UiDataTable>

      <footer v-if="data" class="foot">
        <span>{{ t('admin.jobs.hint') }}</span>
        <UiPager
          v-if="data.total"
          :page="data.page"
          :page-size="data.page_size"
          :total="data.total"
          @update:page="goToPage"
        />
      </footer>
    </UiPanel>

    <!-- Asked first: a requeued job's failed and unfinished pages are paid for again. -->
    <UiModal v-if="requeuing" :title="t('admin.jobs.requeueTitle')" :busy="requeuePending" @close="closeRequeue">
      <p class="message">{{ t('admin.jobs.requeueBody') }}</p>
      <UiBanner v-if="requeueError" class="dialog-error" tone="error">{{ requeueError }}</UiBanner>
      <template #footer>
        <UiButton variant="ghost" :disabled="requeuePending" @click="closeRequeue">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton variant="primary" :loading="requeuePending" @click="confirmRequeue">
          {{ t('admin.jobs.requeue') }}
        </UiButton>
      </template>
    </UiModal>
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

.filter-narrow {
  display: none;
  width: auto;
}

.segment-count {
  color: var(--muted);
}

.jobs-table {
  --table-min: 860px;
}

.muted {
  color: var(--muted);
}

.faint {
  color: var(--faint);
}

.account {
  max-width: 24ch;
}

.model {
  max-width: 22ch;
}

.status {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
}

.note {
  color: var(--muted);
  font-size: var(--text-xs);
}

.note.stuck {
  color: var(--danger);
}

.failure {
  max-width: 32ch;
}

.foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2) var(--space-4);
  padding: var(--space-3) var(--cell-x) 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

.message {
  color: var(--muted);
  line-height: 1.7;
}

.dialog-error {
  margin-top: var(--space-4);
}

@media (max-width: 900px) {
  .filter-wide {
    display: none;
  }

  .filter-narrow {
    display: block;
  }
}
</style>
