<script setup lang="ts">
/**
 * The console's *Audit* screen (docs/admin.md § Pages, *Audit*; § The audit log): every
 * change made through the console, newest first, filtered by area and searched — an
 * address, a name or an id — and paged. A click or Enter on a row opens its `before` and
 * `after` under it, side by side, as the JSON the row holds; again closes it.
 *
 * The filter, the search and the page live in the route query (`?area=&q=&page=`), so Back
 * and a reload return to them; nothing of the log is kept in browser storage.
 */
import { listAdminAudit, type AuditEntry } from '~/lib/api'
import { AUDIT_FILTER_AREAS, auditAreaOf, pageOf, type AdminArea } from '~/lib/admin'
import { formatCount, formatShortDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

/** The longest search the field takes: an address is at most 320 characters. */
const SEARCH_MAX = 320

const { t, locale } = useI18n()
const { actorName, actionLabel, targetLabel } = useAdminLabels()
const route = useRoute()
const { setQuery, goToPage, withinPages } = useAdminQuery()

const area = computed(() => auditAreaOf(route.query.area))
const q = computed(() => (typeof route.query.q === 'string' ? route.query.q.trim() : ''))
const page = computed(() => pageOf(route.query.page))

function chooseArea(value: AdminArea | null) {
  if (value !== area.value) {
    setQuery({ area: value, page: null })
  }
}

const search = useQuerySearch(q, setQuery)

// --- the log -------------------------------------------------------------------------------

const { data, pending, error, load } = useAdminRead(
  () => listAdminAudit({ page: page.value, area: area.value ?? undefined, q: q.value || undefined }),
  { accept: (loaded) => withinPages(loaded, page.value) },
)

onMounted(load)

/** The rows whose before and after are open. */
const open = ref<number[]>([])

watch([area, q, page], () => {
  open.value = []
  void load()
})

function toggle(id: number) {
  open.value = open.value.includes(id) ? open.value.filter((other) => other !== id) : [...open.value, id]
}

const filtered = computed(() => area.value !== null || q.value !== '')

const headline = computed(() => {
  const total = data.value?.total ?? 0
  return t('admin.audit.count', { n: formatCount(total, locale.value) }, total)
})

const columns = computed<TableColumn<AuditEntry>[]>(() => [
  { key: 'time', header: t('admin.audit.time') },
  { key: 'who', header: t('admin.audit.who') },
  { key: 'action', header: t('admin.audit.action') },
  { key: 'target', header: t('admin.audit.target') },
  { key: 'permission', header: t('admin.audit.permission') },
  { key: 'ip', header: t('admin.audit.ip') },
])

function rowAttrs(row: AuditEntry): Record<string, unknown> {
  return {
    class: 'entry',
    tabindex: 0,
    onClick: () => toggle(row.id),
    onKeydown: (event: KeyboardEvent) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault()
        toggle(row.id)
      }
    },
  }
}

/** A side of the change as the row holds it, pretty-printed; null where there was none. */
function json(value: unknown): string | null {
  return value === null || value === undefined ? null : JSON.stringify(value, null, 2)
}
</script>

<template>
  <UiScreen>
    <template #toolbar-start>
      <UiSegmented class="filter-wide" glass role="radiogroup" :aria-label="t('admin.audit.filter')">
        <button
          type="button"
          role="radio"
          class="segment"
          :class="{ active: area === null }"
          :aria-checked="area === null"
          @click="chooseArea(null)"
        >
          {{ t('admin.audit.all') }}
        </button>
        <button
          v-for="option in AUDIT_FILTER_AREAS"
          :key="option"
          type="button"
          role="radio"
          class="segment"
          :class="{ active: area === option }"
          :aria-checked="area === option"
          @click="chooseArea(option)"
        >
          {{ t(`admin.areas.${option}`) }}
        </button>
      </UiSegmented>
      <!-- The same choice where six segments do not fit beside the search. -->
      <UiSelect
        class="filter-narrow"
        :model-value="area ?? ''"
        :aria-label="t('admin.audit.filter')"
        @update:model-value="chooseArea(auditAreaOf($event))"
      >
        <option value="">{{ t('admin.audit.all') }}</option>
        <option v-for="option in AUDIT_FILTER_AREAS" :key="option" :value="option">
          {{ t(`admin.areas.${option}`) }}
        </option>
      </UiSelect>
    </template>

    <template #toolbar>
      <UiSearchField
        v-model="search"
        class="audit-search"
        :label="t('admin.audit.search')"
        :placeholder="t('admin.audit.searchPlaceholder')"
        :maxlength="SEARCH_MAX"
      />
      <UiButton icon-only :label="t('common.refresh')" :loading="pending" @click="load">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.audit.title')">
        <span v-if="data" class="tabular">{{ headline }} · {{ t('admin.audit.immutable') }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="error" class="state" tone="error">{{ error }}</UiBanner>
      <UiSkeleton v-if="!data && !error" class="state" />
      <UiEmptyState
        v-else-if="data && !data.items.length"
        :title="filtered ? t('admin.audit.noMatch') : t('admin.audit.empty')"
      />
      <UiDataTable
        v-else-if="data"
        :columns="columns"
        :rows="data.items"
        :row-key="(row) => String(row.id)"
        :row-attrs="rowAttrs"
        :open="(row) => open.includes(row.id)"
        :caption="t('admin.audit.title')"
      >
        <template #cell-time="{ row }">
          <span class="muted tabular">{{ formatShortDateTime(row.created_at, locale) }}</span>
        </template>
        <template #cell-who="{ row }">
          <span v-if="actorName(row)" class="who cell-block">{{ actorName(row) }}</span>
          <span v-else class="muted">{{ t('admin.deletedAccount') }}</span>
        </template>
        <template #cell-action="{ row }">{{ actionLabel(row.action) }}</template>
        <template #cell-target="{ row }">
          <span class="target cell-block" :class="{ muted: !row.target_label }">{{ targetLabel(row) }}</span>
        </template>
        <template #cell-permission="{ row }">
          <span v-if="row.permission" class="mono">{{ row.permission }}</span>
          <span v-else class="muted">—</span>
        </template>
        <template #cell-ip="{ row }">
          <span v-if="row.ip" class="mono muted">{{ row.ip }}</span>
          <span v-else class="muted">—</span>
        </template>
        <template #detail="{ row }">
          <div class="change">
            <div class="side">
              <p class="side-label">{{ t('admin.audit.before') }}</p>
              <pre v-if="json(row.before) !== null" class="json mono">{{ json(row.before) }}</pre>
              <p v-else class="json none">—</p>
            </div>
            <div class="side">
              <p class="side-label">{{ t('admin.audit.after') }}</p>
              <pre v-if="json(row.after) !== null" class="json mono">{{ json(row.after) }}</pre>
              <p v-else class="json none">—</p>
            </div>
          </div>
        </template>
      </UiDataTable>

      <AdminListFoot v-if="data && data.total" :hint="t('admin.audit.hint')" :list="data" @page="goToPage" />
    </UiPanel>
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

.audit-search {
  width: 240px;
}

.muted {
  color: var(--muted);
}

.who {
  font-weight: var(--weight-medium);
}

.target {
  max-width: 28ch;
}

/* A row opens: the pointer says so, and the keyboard reaches it. */
:deep(tr.entry) {
  cursor: pointer;
}

.change {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
}

.side {
  display: grid;
  align-content: start;
  gap: var(--space-1);
  min-width: 0;
}

.side-label {
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.json {
  margin: 0;
  padding: 10px var(--space-3);
  border-radius: var(--radius-md);
  background: var(--paper);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.json.none {
  color: var(--faint);
}

@media (max-width: 1180px) {
  .filter-wide {
    display: none;
  }

  .filter-narrow {
    display: block;
  }
}

@media (max-width: 900px) {
  .audit-search {
    width: auto;
    flex: 1;
  }

  .change {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
