<script setup lang="ts">
import {
  fileJob,
  getJobResult,
  getLibrary,
  listJobs,
  type JobResult,
  type JobStatus,
  type JobSummary,
  type LibraryDocument,
  type LibraryFolder,
} from '~/lib/api'
import { formatCount, formatShortDateTime } from '~/lib/format'
import type { FolderId } from '~/lib/library'
import type { MenuItem, Point } from '~/lib/menu'
import type { TableColumn } from '~/lib/table'

definePageMeta({ middleware: 'authed' })

const JOB_LIMIT = 50

const { t, locale } = useI18n()

const { data, pending, errorMessage, refresh } = useAuthedData(() => listJobs(JOB_LIMIT))
const { resolve } = useApiError()
const localePath = useLocalePath()

/**
 * The library, read beside the jobs: it says which parses are already files, and it is the
 * tree the Add to Files picker draws (docs/web.md § History). Its failure is deliberately
 * silent — the history is still the history without it; only the filing actions go.
 */
const library = useAuthedData(() => getLibrary())

function refreshAll() {
  void refresh()
  void library.refresh()
}

/** Each job's files, newest first — the order `GET /api/library` already returns. A
 *  markdown document has no job and files nothing here. */
const filesByJob = computed(() => {
  const map = new Map<string, LibraryDocument[]>()
  for (const document of library.data.value?.documents ?? []) {
    if (document.job_id === null) {
      continue
    }
    const files = map.get(document.job_id)
    if (files) {
      files.push(document)
    } else {
      map.set(document.job_id, [document])
    }
  }
  return map
})

/** "In Files · Papers" per filed job, naming the folder of its newest file. */
const filedLabels = computed(() => {
  const names = new Map(library.data.value?.folders.map((folder) => [folder.id, folder.name]))
  const labels = new Map<string, string>()
  for (const [jobId, files] of filesByJob.value) {
    const folderId = files[0]!.folder_id
    const folder = (folderId !== null && names.get(folderId)) || t('files.root')
    labels.set(jobId, t('history.inFiles', { folder }))
  }
  return labels
})

/** The row whose result is open. A parsed document is a document, so it gets a dialog rather
    than a row that grows to twenty lines and pushes every other job off the screen. */
const openJob = ref<JobSummary | null>(null)
const results = reactive(new Map<string, JobResult>())
const resultErrors = reactive(new Map<string, string>())
/**
 * Job ids with a result request in flight. A set, not one shared "currently loading" id:
 * the viewer can be closed and reopened on a different job while the first request is
 * still running, and a single id cannot say which of them is pending. It also makes a
 * reopen cheap — the job is already being fetched, so no second request is started.
 */
const inFlight = reactive(new Set<string>())

/** Never color alone: each tone ships with the status word beside it. Succeeded is the
    accent — a finished job is the data this page is about — and running sits outside the
    danger family, because a job in flight is not a problem. */
const STATUS_TONE: Record<JobStatus, 'neutral' | 'info' | 'ok' | 'danger'> = {
  queued: 'neutral',
  running: 'info',
  succeeded: 'ok',
  failed: 'danger',
}

const STATUSES = ['succeeded', 'running', 'queued', 'failed'] as const

/** The rail's tally. Every status is listed even at zero: an absent row would read as a
    status this account cannot produce. */
const tally = computed(() =>
  STATUSES.map((status) => ({
    status,
    tone: STATUS_TONE[status],
    count: data.value?.jobs.filter((job) => job.status === status).length ?? 0,
  })),
)

const columns = computed<TableColumn<JobSummary>[]>(() => [
  { key: 'file', header: t('history.columnFile') },
  { key: 'status', header: t('history.columnStatus') },
  { key: 'model', header: t('history.columnModel') },
  { key: 'pages', header: t('history.columnPages'), numeric: true, width: '80px' },
  { key: 'credits', header: t('history.columnCredits'), numeric: true, width: '90px' },
  { key: 'created', header: t('history.columnCreated'), numeric: true },
])

// --- the menu --------------------------------------------------------------

/** `fromKey` is how it was opened: only then does the menu open with a row highlighted. */
const menu = ref<{ job: JobSummary; at: Point; fromKey?: boolean } | null>(null)
const press = useLongPress()

function openMenu(job: JobSummary, at: Point, fromKey = false) {
  press.cancel()
  menu.value = { job, at, fromKey }
}

/** The keyboard's route in — the Menu key or `Shift+F10` on a focused filename. */
function openMenuFromKey(event: KeyboardEvent, job: JobSummary) {
  if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) {
    return
  }
  event.preventDefault()
  const box = (event.target as HTMLElement).getBoundingClientRect()
  openMenu(job, { x: box.left, y: box.bottom }, true)
}

function rowAttrs(job: JobSummary): Record<string, unknown> {
  return {
    onContextmenu: (event: MouseEvent) => {
      event.preventDefault()
      openMenu(job, { x: event.clientX, y: event.clientY })
    },
    onKeydown: (event: KeyboardEvent) => openMenuFromKey(event, job),
    ...press.on((at) => openMenu(job, at)),
  }
}

/**
 * Add stays offered on a parse that is already filed — one parse in two folders is a real
 * library — and is disabled, not hidden, where it cannot work, so every row's menu has the
 * same shape.
 */
const menuItems = computed<MenuItem[]>(() => {
  const job = menu.value?.job
  if (!job) {
    return []
  }
  const latest = filesByJob.value.get(job.job_id)?.[0]
  const items: MenuItem[] = [
    { key: 'open', label: t('files.open'), icon: 'file-text' },
    {
      key: 'add',
      label: t('history.addToFiles'),
      icon: 'folder-plus',
      separated: true,
      disabled: job.status === 'failed' || !library.data.value,
    },
  ]
  if (latest) {
    items.push({
      key: 'show',
      label: t('history.showInFiles'),
      icon: 'folder-open',
      to: localePath({ path: '/files', query: { document: String(latest.id) } }),
    })
  }
  return items
})

function runMenu(action: string) {
  const job = menu.value?.job
  if (!job) {
    return
  }
  if (action === 'open') {
    void showResult(job)
  } else if (action === 'add') {
    fileError.value = null
    filing.value = job
  }
}

// --- adding to Files -------------------------------------------------------

const filing = ref<JobSummary | null>(null)
const filingPending = ref(false)
const fileError = ref<string | null>(null)
/**
 * The last folder chosen, kept across visits within this page load: filing a run of API
 * parses is usually filing them into one place.
 */
const lastDestination = useState<FolderId>('history-file-destination', () => null)

const startFolder = computed<FolderId>(() =>
  library.data.value?.folders.some((folder) => folder.id === lastDestination.value)
    ? lastDestination.value
    : null,
)

/** A folder the picker made, into its tree at once. */
function addFolder(folder: LibraryFolder) {
  const current = library.data.value
  if (current) {
    library.data.value = { ...current, folders: [...current.folders, folder] }
  }
}

// A reload onto a new build would lose the reader's place or close a dialog mid-choice, so
// it waits for both to be over (docs/web.md § Rules).
useBuildHold(() => openJob.value !== null || filing.value !== null)

/** The dialog closes on success; the row's folder mark appearing is the confirmation. */
async function submitFile(destination: FolderId) {
  const job = filing.value
  if (!job) {
    return
  }
  filingPending.value = true
  fileError.value = null
  try {
    const created = await fileJob(job.job_id, destination)
    const current = library.data.value
    if (current) {
      library.data.value = { ...current, documents: [created, ...current.documents] }
    }
    lastDestination.value = destination
    filing.value = null
  } catch (error) {
    fileError.value = await resolve(error)
  } finally {
    filingPending.value = false
  }
}

// --- reading ---------------------------------------------------------------

async function showResult(job: JobSummary) {
  openJob.value = job
  // A held partial snapshot is stale by definition — reopening re-reads it.
  const held = results.get(job.job_id)
  if ((held && !held.meta.partial) || inFlight.has(job.job_id)) {
    return
  }
  inFlight.add(job.job_id)
  resultErrors.delete(job.job_id)
  try {
    results.set(job.job_id, await getJobResult(job.job_id))
  } catch (error) {
    resultErrors.set(job.job_id, await resolve(error))
  } finally {
    inFlight.delete(job.job_id)
  }
}
</script>

<template>
  <UiScreen>
    <UiRail>
      <UiPageHeader :eyebrow="t('history.recent', { limit: JOB_LIMIT })" :title="t('history.headTitle')" />

      <dl v-if="data" class="tally">
        <div v-for="row in tally" :key="row.status" class="tally-row" :class="row.tone">
          <dt>
            <span class="dot" aria-hidden="true" />
            {{ t(`status.${row.status}`) }}
          </dt>
          <dd class="tabular">{{ row.count }}</dd>
        </div>
      </dl>
    </UiRail>

    <UiRegion>
      <!--
        One panel filling the region, rows scrolling inside it under a sticky header, so the
        page itself never grows past the viewport.

        A failed refresh keeps the rows it already has — the banner says what went wrong, and
        blanking the page on top of that helps nobody.
      -->
      <UiPanel :title="t('history.listTitle')" lead>
        <template #meta>
          <span>{{ t('history.newestFirst') }}</span>
          <UiButton
            variant="ghost"
            size="xs"
            icon-only
            :label="t('common.refresh')"
            :loading="pending"
            @click="refreshAll"
          >
            <template #icon><UiIcon name="refresh" /></template>
          </UiButton>
        </template>

        <UiBanner v-if="errorMessage" class="state" tone="error">{{ errorMessage }}</UiBanner>

          <!-- Not just `!data`: a failed first load leaves data null with `pending` back to
             false, and an animated skeleton beside a terminal error says the page is still
             working when it has stopped. -->
        <UiSkeleton v-if="!data && !errorMessage" class="state" :rows="6" />

        <UiEmptyState
          v-else-if="data && !data.jobs.length"
          :title="t('history.empty')"
          :body="t('history.emptyBody')"
        />

        <UiDataTable
          v-else-if="data"
          :columns="columns"
          :rows="data.jobs"
          :row-key="(job) => job.job_id"
          :caption="t('history.listTitle')"
          :row-attrs="rowAttrs"
          :class="{ holding: press.holding.value }"
        >
          <!-- The filename is the affordance: it is the row's identity and the thing worth
               opening, so it carries the underline rather than a separate icon button at the
               far end of the row. -->
          <template #cell-file="{ row }">
            <!-- Not `cell-block`: its `display: block` would put the folder mark on a line of
                 its own. The name clips itself instead. -->
            <div class="file-cell">
              <button
                type="button"
                class="filename"
                :class="{ failed: row.status === 'failed' }"
                :title="row.filename"
                @click="showResult(row)"
              >
                {{ row.filename }}
              </button>
              <!-- A fact about the row, not data: faint, and titled with where it lives. -->
              <span
                v-if="filedLabels.has(row.job_id)"
                class="filed"
                role="img"
                :title="filedLabels.get(row.job_id)"
                :aria-label="filedLabels.get(row.job_id)"
              >
                <UiIcon name="folder" />
              </span>
            </div>
          </template>
          <template #cell-status="{ row }">
            <UiStatusDot :tone="STATUS_TONE[row.status]" :label="t(`status.${row.status}`)" />
          </template>
          <template #cell-model="{ row }">
            <span class="model mono">{{ row.model }}</span>
          </template>
          <template #cell-pages="{ row }">
            {{ row.page_count ? `${row.pages_done}/${row.page_count}` : row.pages_done }}
          </template>
          <!-- What the parse cost; empty while its reservation is unsettled or for a job from
               before the ledger (docs/billing.md § Routes). -->
          <template #cell-credits="{ row }">
            <span class="tabular">{{ row.credits == null ? '' : formatCount(row.credits, locale) }}</span>
          </template>
          <template #cell-created="{ row }">
            <span class="created">{{ formatShortDateTime(row.created_at, locale) }}</span>
          </template>
        </UiDataTable>
      </UiPanel>
    </UiRegion>
    <!-- A running job answers with its finished pages — a static snapshot here, since this
         page does not poll; reopening refreshes it (docs/api.md § Partial results). -->
    <ResultViewer
      v-if="openJob"
      :key="openJob.job_id"
      :title="openJob.filename"
      :error="openJob.error"
      :result="results.get(openJob.job_id) ?? null"
      :pending="inFlight.has(openJob.job_id)"
      :error-message="resultErrors.get(openJob.job_id) ?? null"
      :figure-base="`/api/jobs/${openJob.job_id}/figures`"
      :job-id="openJob.job_id"
      @close="openJob = null"
    />

    <UiContextMenu
      v-if="menu"
      :at="menu.at"
      :items="menuItems"
      :label="t('files.menuFor', { name: menu.job.filename })"
      :focus-first="menu.fromKey"
      @select="runMenu"
      @close="menu = null"
    />

    <MoveDialog
      v-if="filing && library.data.value"
      :folders="library.data.value.folders"
      :name="filing.filename"
      :title="t('history.addTitle')"
      :action="t('history.add')"
      :start="startFolder"
      :pending="filingPending"
      :error="fileError"
      @choose="submitFile"
      @created="addFolder"
      @cancel="filing = null"
    />
  </UiScreen>
</template>

<style scoped>
.tally {
  display: flex;
  flex-direction: column;
  margin: 0;
}

.tally-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--line);
}

.tally-row:last-child {
  border-bottom: 1px solid var(--line);
}

.tally-row dt {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--ink);
}

.tally-row dd {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--display-sm);
  font-weight: var(--weight-semibold);
}

.dot {
  width: 7px;
  height: 7px;
  flex-shrink: 0;
  border-radius: var(--radius-full);
  background: var(--queued);
}

.tally-row.ok .dot {
  background: var(--accent);
}

.tally-row.info .dot {
  background: var(--running);
}

/* Failed is the one state a reader scans for, so it colours the word and the figure too,
   not only the dot. */
.tally-row.danger .dot {
  background: var(--danger);
}

.tally-row.danger dt,
.tally-row.danger dd {
  color: var(--danger);
}

.state {
  margin-top: var(--space-4);
}

/* A link that happens to be a button: it opens a dialog, so it is a button, but it reads as
   the filename it is with the underline this design gives a link. */
.filename {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0;
  border: none;
  border-bottom: 1px solid var(--edge);
  background: none;
  color: var(--ink);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition:
    color var(--duration-fast) var(--ease),
    border-color var(--duration-fast) var(--ease);
}

.filename:hover {
  color: var(--accent);
  border-color: var(--accent);
}

.filename.failed {
  color: var(--danger);
  border-color: var(--danger-edge);
}

/* A held row is not selected: a long press that starts a text selection cannot become a
   menu. Only while a touch is down — with a mouse the menu is a right-click, and a filename,
   model id or error stays copyable. The callout is touch-only anyway, so it is off always. */
:deep(tbody tr) {
  -webkit-touch-callout: none;
}

.holding :deep(tbody tr) {
  user-select: none;
}

.file-cell {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.filed {
  display: inline-flex;
  flex-shrink: 0;
  color: var(--faint);
}

.filed :deep(svg) {
  width: 13px;
  height: 13px;
}

.model {
  color: var(--muted);
  white-space: nowrap;
}

.created {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
</style>
