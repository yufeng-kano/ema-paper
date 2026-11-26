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
import { disableAdminCode, listAdminCodes, type AdminCode, type AdminCodesCreated } from '~/lib/api'
import { CODE_CLOSING_DAYS, adminFailure, daysUntil, formatRedeemBy, pageOf } from '~/lib/admin'
import { formatCount, formatDate } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

/** The longest search the field takes: a label is at most 255 characters. */
const SEARCH_MAX = 255

const { t, locale } = useI18n()
const { personName, grantLabel } = useAdminLabels()
const route = useRoute()
const adminConsole = useAdminConsole()
const { explain } = useAdminFailure()
const { setQuery, goToPage, withinPages } = useAdminQuery()
const hold = useHeldRequest()

const canWrite = computed(() => adminConsole.can('codes', 'write'))

const showAll = computed(() => route.query.status === 'all')
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q.trim() : ''))
const page = computed(() => pageOf(route.query.page))

function chooseAll(all: boolean) {
  if (all !== showAll.value) {
    setQuery({ status: all ? 'all' : null, page: null })
  }
}

const search = useQuerySearch(q, setQuery)

// --- the list --------------------------------------------------------------------------

const { data, pending, error, load } = useAdminRead(
  () => listAdminCodes({ page: page.value, status: showAll.value ? 'all' : undefined, q: q.value || undefined }),
  { accept: (loaded) => withinPages(loaded, page.value) },
)

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
    return row.disabled_by ? t('admin.codes.disabledBy', { date, name: personName(row.disabled_by) }) : date
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
  const result = await hold(() => disableAdminCode(row.id))
  if ('failure' in result && adminFailure(result.failure).kind !== 'notFound') {
    disableError.value = await explain(result.failure)
  } else {
    disableTarget.value = null
    await load()
    if ('failure' in result && !error.value) {
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

      <AdminListFoot v-if="data && data.total" :hint="t('admin.codes.hint')" :list="data" @page="goToPage" />
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

@media (max-width: 900px) {
  .codes-search {
    width: auto;
    flex: 1;
  }
}
</style>
