<script setup lang="ts">
/**
 * The console's *Redeem codes* screen (docs/admin.md § Pages, *Redeem codes*; § Redeem
 * codes): the codes, masked — the API never sends one whole after it was created — newest
 * first, the active ones or all, searched by label and paged. With `codes.write`, *Create
 * codes* opens the create dialog and then the one dialog that shows the codes in full, and
 * *Disable* asks first.
 *
 * The filter, the search and the page live in the route query (`?status=all&q=&page=`), so
 * Back and a reload return to them; nothing of the list is kept in browser storage.
 */
import { disableAdminCode, listAdminCodes, type AdminCode, type AdminCodesCreated, type AdminCodesList } from '~/lib/api'
import { CODE_CLOSING_DAYS, adminFailure, daysUntil, formatRedeemBy, pageOf } from '~/lib/admin'
import { formatCount, formatDate } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

/** How long typing rests before the search runs. */
const SEARCH_DELAY_MS = 300
/** The longest search the field takes: a label is at most 255 characters. */
const SEARCH_MAX = 255

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const adminConsole = useAdminConsole()
const { explain } = useAdminFailure()

const canWrite = computed(() => adminConsole.can('codes', 'write'))

const showAll = computed(() => route.query.status === 'all')
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q.trim() : ''))
const page = computed(() => pageOf(route.query.page))

/** Moves the query; a filter or a search starts again at the first page. */
function setQuery(patch: Record<string, string | null>, replace = false) {
  const query = Object.fromEntries(Object.entries({ ...route.query, ...patch }).filter(([, value]) => value !== null))
  void (replace ? router.replace({ query }) : router.push({ query }))
}

function chooseAll(all: boolean) {
  if (all !== showAll.value) {
    setQuery({ status: all ? 'all' : null, page: null })
  }
}

function goToPage(next: number) {
  setQuery({ page: next > 1 ? String(next) : null })
}

// --- the search, typed ahead of the query ------------------------------------------------

const search = ref(q.value)
let searchTimer: ReturnType<typeof setTimeout> | undefined

watch(search, (value) => {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    const next = value.trim()
    if (next !== q.value) {
      setQuery({ q: next || null, page: null }, true)
    }
  }, SEARCH_DELAY_MS)
})

// Back and Forward move the query under the field: the field follows.
watch(q, (value) => {
  if (value !== search.value.trim()) {
    search.value = value
  }
})

onBeforeUnmount(() => clearTimeout(searchTimer))

// --- the list --------------------------------------------------------------------------

const data = shallowRef<AdminCodesList | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)
/** Only the newest read may write. */
let seq = 0

async function load(): Promise<void> {
  const mine = ++seq
  pending.value = true
  try {
    const loaded = await listAdminCodes({
      page: page.value,
      status: showAll.value ? 'all' : undefined,
      q: q.value || undefined,
    })
    if (mine !== seq) {
      return
    }
    const last = Math.max(1, Math.ceil(loaded.total / loaded.page_size))
    if (!loaded.items.length && page.value > last) {
      // A page past the end — a link from a longer list: its last page instead, which the
      // query's change reads.
      setQuery({ page: last > 1 ? String(last) : null }, true)
      return
    }
    data.value = loaded
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

watch([showAll, q, page], () => {
  void load()
})

const headline = computed(() => {
  const summary = data.value?.summary
  if (!summary) {
    return ''
  }
  return [
    t('admin.codes.active', { n: formatCount(summary.active, locale.value) }, summary.active),
    t('admin.codes.labels', { n: formatCount(summary.labels, locale.value) }, summary.labels),
    t('admin.codes.redeemed', { n: formatCount(summary.redeemed_this_month, locale.value) }, summary.redeemed_this_month),
  ].join(' · ')
})

const emptyTitle = computed(() => {
  if (q.value) {
    return t('admin.codes.noMatch')
  }
  return showAll.value ? t('admin.codes.empty') : t('admin.codes.emptyActive')
})

const columns = computed<TableColumn<AdminCode>[]>(() => [
  { key: 'code', header: t('admin.codes.code') },
  { key: 'kind', header: t('admin.codes.kind') },
  { key: 'grants', header: t('admin.codes.grants') },
  { key: 'used', header: t('admin.codes.used'), numeric: true, align: 'end' },
  { key: 'redeemBy', header: t('admin.codes.redeemBy') },
  { key: 'label', header: t('admin.codes.label') },
  { key: 'status', header: t('admin.codes.status') },
  ...(canWrite.value
    ? [{ key: 'disable', header: '', srHeader: t('admin.codes.disable'), align: 'end' as const, width: '96px' }]
    : []),
])

// --- labels ----------------------------------------------------------------------------

/** What a code grants: credits and how long they last, or days of a plan by its id. */
function grantLabel(row: { kind: string; credits: number | null; valid_days: number | null; plan_id: string | null; days: number | null }): string {
  if (row.kind === 'credits') {
    const days = row.valid_days ?? 0
    return t(
      'admin.codes.grantCredits',
      { credits: formatCount(row.credits ?? 0, locale.value), n: formatCount(days, locale.value) },
      days,
    )
  }
  const days = row.days ?? 0
  return t('admin.codes.grantPlan', { n: formatCount(days, locale.value), plan: row.plan_id ?? '' }, days)
}

// `plan_missing` is the operator's to fix — configure the plan again or disable the code — so it
// stands out as `disabled` does.
const TONES = { active: 'ok', disabled: 'danger', expired: 'neutral', used_up: 'neutral', plan_missing: 'danger' } as const

/** What follows the status word: when an active code's last day comes soon, or when and by
 *  whom a disabled one was disabled. */
function statusNote(row: AdminCode): string | null {
  if (row.status === 'active' && row.redeem_by) {
    const days = daysUntil(row.redeem_by, new Date())
    return days >= 0 && days <= CODE_CLOSING_DAYS ? t('admin.codes.closes', { n: days }, days) : null
  }
  if (row.status === 'disabled' && row.disabled_at) {
    const date = formatDate(row.disabled_at, locale.value)
    const name = row.disabled_by?.name?.trim() || (row.disabled_by ? t('admin.accountId', { id: row.disabled_by.id }) : null)
    return name ? t('admin.codes.disabledBy', { date, name }) : date
  }
  return null
}

// --- disabling -------------------------------------------------------------------------

/** The code *Disable* asks about. */
const disableTarget = ref<AdminCode | null>(null)
const disabling = ref(false)
const disableError = ref<string | null>(null)

function askDisable(row: AdminCode) {
  disableError.value = null
  disableTarget.value = row
}

async function confirmDisable() {
  const row = disableTarget.value
  if (!row || disabling.value) {
    return
  }
  disabling.value = true
  disableError.value = null
  // Back would leave the dialog while the request runs, and its answer would land unseen.
  const release = router.beforeEach(() => false)
  let failure: unknown = null
  try {
    await disableAdminCode(row.id)
  } catch (caught) {
    failure = caught
  } finally {
    release()
  }
  if (failure && adminFailure(failure).kind !== 'notFound') {
    disableError.value = await explain(failure)
  } else {
    disableTarget.value = null
    await load()
    if (failure && !error.value) {
      // Codes are never deleted, so this is a list read from another deployment's state:
      // read again, and said over it — after the read, which clears what was said before.
      error.value = t('admin.codes.gone')
    }
  }
  disabling.value = false
}

// --- creating --------------------------------------------------------------------------

const createOpen = ref(false)
/** The answer that holds the codes in plaintext: kept only while its dialog is open. */
const created = shallowRef<AdminCodesCreated | null>(null)

// The create dialog's build hold goes as it closes, and the created dialog's comes as it
// opens — after, in the same render — so a reload that waited on the request would land
// between them and take the codes. This watcher runs before that render (a pre-flush
// watcher of this component), so the hand-off is never unheld. The created dialog's own
// hold stays the last to go on Done, after its `beforeunload` prompt is gone.
useBuildHold(() => created.value !== null)

function onCreated(answer: AdminCodesCreated) {
  createOpen.value = false
  created.value = answer
  void load()
}
</script>

<template>
  <UiScreen>
    <template #toolbar-start>
      <UiSegmented glass role="radiogroup" :aria-label="t('admin.codes.filter')">
        <button
          type="button"
          role="radio"
          class="segment"
          :class="{ active: !showAll }"
          :aria-checked="!showAll"
          @click="chooseAll(false)"
        >
          {{ t('admin.codes.filterActive') }}
        </button>
        <button
          type="button"
          role="radio"
          class="segment"
          :class="{ active: showAll }"
          :aria-checked="showAll"
          @click="chooseAll(true)"
        >
          {{ t('admin.codes.filterAll') }}
        </button>
      </UiSegmented>
    </template>

    <template #toolbar>
      <UiSearchField
        v-model="search"
        class="codes-search"
        :label="t('admin.codes.search')"
        :placeholder="t('admin.codes.searchPlaceholder')"
        :maxlength="SEARCH_MAX"
      />
      <UiButton icon-only :label="t('common.refresh')" :loading="pending" @click="load">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
      <UiButton v-if="canWrite" variant="primary" size="lg" :disabled="!data" @click="createOpen = true">
        <template #icon><UiIcon name="plus" /></template>
        {{ t('admin.codes.create') }}
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.codes.title')">
        <span v-if="data" class="tabular">{{ headline }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="error" class="state" tone="error">{{ error }}</UiBanner>
      <UiSkeleton v-if="!data && !error" class="state" />
      <UiEmptyState v-else-if="data && !data.items.length" :title="emptyTitle" />
      <UiDataTable
        v-else-if="data"
        :columns="columns"
        :rows="data.items"
        :row-key="(row) => String(row.id)"
        :caption="t('admin.codes.title')"
      >
        <template #cell-code="{ row }">
          <span class="mono">{{ row.masked }}</span>
        </template>
        <template #cell-kind="{ row }">{{ t(`admin.codes.kinds.${row.kind}`) }}</template>
        <template #cell-grants="{ row }">
          <span class="cell-block grants">{{ grantLabel(row) }}</span>
        </template>
        <template #cell-used="{ row }">
          <span class="tabular">{{ formatCount(row.uses, locale) }} / {{ formatCount(row.max_uses, locale) }}</span>
        </template>
        <template #cell-redeemBy="{ row }">
          <span v-if="row.redeem_by" class="tabular">{{ formatRedeemBy(row.redeem_by, locale) }}</span>
          <span v-else class="muted">—</span>
        </template>
        <template #cell-label="{ row }">
          <span v-if="row.label" class="chip cell-block">{{ row.label }}</span>
        </template>
        <template #cell-status="{ row }">
          <span class="status">
            <UiStatusDot :tone="TONES[row.status]" :label="t(`admin.codes.statuses.${row.status}`)" />
            <span v-if="statusNote(row)" class="note">· {{ statusNote(row) }}</span>
          </span>
        </template>
        <template #cell-disable="{ row }">
          <UiButton
            v-if="row.status !== 'disabled'"
            size="xs"
            :label="t('admin.codes.disableNamed', { code: row.masked })"
            @click="askDisable(row)"
          >
            {{ t('admin.codes.disable') }}
          </UiButton>
          <span v-else class="muted">—</span>
        </template>
      </UiDataTable>

      <footer v-if="data && data.total" class="foot">
        <span>{{ t('admin.codes.hint') }}</span>
        <UiPager :page="data.page" :page-size="data.page_size" :total="data.total" @update:page="goToPage" />
      </footer>
    </UiPanel>

    <UiConfirmDialog
      v-if="disableTarget"
      :title="t('admin.codes.disableTitle', { code: disableTarget.masked })"
      :message="t('admin.codes.disableBody')"
      :confirm-label="t('admin.codes.disable')"
      :pending="disabling"
      :error="disableError"
      @confirm="confirmDisable"
      @cancel="disableTarget = null"
    />

    <AdminCodesCreateDialog
      v-if="createOpen && data"
      :options="data.create"
      @close="createOpen = false"
      @created="onCreated"
    />
    <AdminCodesCreatedDialog v-if="created" :created="created" @close="created = null" />
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

.codes-search {
  width: 220px;
}

.muted {
  color: var(--muted);
}

.grants {
  max-width: 32ch;
}

/* A label is the operator's word for a batch: a quiet chip, as the boards draw it. */
.chip {
  display: inline-block;
  max-width: 20ch;
  padding: 1px var(--space-2);
  border-radius: var(--radius-full);
  background: var(--fill);
  font-size: var(--text-xs);
  vertical-align: middle;
}

.status {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}

.note {
  color: var(--muted);
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

@media (max-width: 900px) {
  .codes-search {
    width: auto;
    flex: 1;
  }
}
</style>
