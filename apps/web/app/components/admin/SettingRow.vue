<script setup lang="ts">
/**
 * One setting on the console's *Settings* screen (docs/admin.md § Pages, *Settings*): its
 * words and its key, the text in effect — a table when it reads as one — and where it comes
 * from, `.env`'s text beside a database value and who stored it when, and, with
 * `settings.write`, *Edit* and *Restore to .env*. The sandbox's sites are chips, with a
 * warning while the server gives the sandbox no network. Everything shown is the API's.
 */
import type { AdminSetting, AdminSettingsResponse } from '~/lib/api'
import {
  hostEntries,
  isHostsSetting,
  isJsonSetting,
  packRows,
  planRows,
  priceRows,
  type PackRow,
  type PlanRow,
  type PriceRow,
} from '~/lib/admin'
import { formatBytes, formatCount, formatDate } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

const props = defineProps<{
  setting: AdminSetting
  ceilings: AdminSettingsResponse['ceilings']
  /** Whether `SANDBOX_NETWORK` names a network: without one the sandbox's sites apply to nothing. */
  sandboxNetwork: boolean
  canWrite: boolean
}>()

const emit = defineEmits<{ edit: []; restore: [] }>()

const { t, locale } = useI18n()

const label = computed(() => t(`admin.settings.keys.${props.setting.key}`))
const fromDatabase = computed(() => props.setting.source === 'database')

/** The line under the words, with the facts some keys' lines carry. */
const note = computed(() => {
  const key = props.setting.key
  if (key === 'CREDIT_USD') {
    const usd = Number(props.setting.value)
    const amount = Number.isFinite(usd)
      ? new Intl.NumberFormat(locale.value, { style: 'currency', currency: 'USD', maximumFractionDigits: 8 }).format(usd)
      : props.setting.value
    return t('admin.settings.notes.CREDIT_USD', { usd: amount })
  }
  if (key === 'FREE_STORAGE_BYTES') {
    return t('admin.settings.notes.FREE_STORAGE_BYTES', {
      size: formatBytes(props.ceilings.WORKSPACE_QUOTA_BYTES, locale.value),
    })
  }
  if (key === 'FREE_MAX_JOBS') {
    return t('admin.settings.notes.FREE_MAX_JOBS', { n: formatCount(props.ceilings.MAX_JOBS_PER_USER, locale.value) })
  }
  return t(`admin.settings.notes.${key}`)
})

const prices = computed(() => (props.setting.key === 'MODEL_PRICES_JSON' ? priceRows(props.setting.value) : null))
const plans = computed(() => (props.setting.key === 'KANO_PAY_PLANS_JSON' ? planRows(props.setting.value) : null))
const packs = computed(() => (props.setting.key === 'KANO_PAY_PACKS_JSON' ? packRows(props.setting.value) : null))
const hosts = computed(() => (isHostsSetting(props.setting.key) ? hostEntries(props.setting.value) : null))
/** `.env`'s text beside a database value: the sandbox's sites as a list, anything else as it stands. */
const envText = computed(() => {
  const value = props.setting.env_value
  return isHostsSetting(props.setting.key) ? hostEntries(value).join(', ') : value
})
/** A JSON setting whose text is no table the page can draw: shown as it stands. */
const raw = computed(() => isJsonSetting(props.setting.key) && !prices.value && !plans.value && !packs.value)
const bytes = computed(() => {
  const value = Number(props.setting.value)
  return props.setting.key === 'FREE_STORAGE_BYTES' && Number.isSafeInteger(value)
    ? formatBytes(value, locale.value)
    : null
})

const priceColumns = computed<TableColumn<PriceRow>[]>(() => [
  { key: 'model', header: t('admin.settings.prices.model'), value: (row) => row.model },
  { key: 'input', header: t('admin.settings.prices.input'), numeric: true, value: (row) => row.input },
  { key: 'output', header: t('admin.settings.prices.output'), numeric: true, value: (row) => row.output },
  { key: 'cacheRead', header: t('admin.settings.prices.cacheRead'), numeric: true, value: (row) => row.cache_read },
  { key: 'cacheWrite', header: t('admin.settings.prices.cacheWrite'), numeric: true, value: (row) => row.cache_write },
])

const planColumns = computed<TableColumn<PlanRow>[]>(() => [
  { key: 'plan', header: t('admin.settings.plans.plan'), value: (row) => row.id },
  {
    key: 'credits',
    header: t('admin.settings.plans.credits'),
    numeric: true,
    value: (row) => formatCount(row.credits, locale.value),
  },
  {
    key: 'storage',
    header: t('admin.settings.plans.storage'),
    numeric: true,
    value: (row) => formatBytes(row.storageBytes, locale.value),
  },
  {
    key: 'jobs',
    header: t('admin.settings.plans.jobs'),
    numeric: true,
    value: (row) => formatCount(row.maxJobs, locale.value),
  },
  {
    key: 'sandbox',
    header: t('admin.settings.plans.sandbox'),
    value: (row) => (row.sandbox ? t('admin.settings.plans.yes') : t('admin.settings.plans.no')),
  },
])

const packColumns = computed<TableColumn<PackRow>[]>(() => [
  { key: 'pack', header: t('admin.settings.packs.pack'), value: (row) => row.id },
  {
    key: 'credits',
    header: t('admin.settings.packs.credits'),
    numeric: true,
    value: (row) => formatCount(row.credits, locale.value),
  },
  {
    key: 'validFor',
    header: t('admin.settings.packs.validFor'),
    numeric: true,
    value: (row) => t('admin.settings.packs.days', { n: formatCount(row.validDays, locale.value) }, row.validDays),
  },
])

const changedBy = computed(() => {
  const { updated_at: at, updated_by: by } = props.setting
  if (!at) {
    return null
  }
  return t('admin.settings.changedBy', {
    name: by ? by.name?.trim() || t('admin.accountId', { id: by.id }) : t('admin.deletedAccount'),
    date: formatDate(at, locale.value),
  })
})
</script>

<template>
  <article class="setting" :aria-label="label">
    <header class="head">
      <div class="words">
        <h3 class="label">{{ label }}</h3>
        <span class="key mono">{{ setting.key }}</span>
      </div>
      <span class="source" :class="setting.source">{{ t(`admin.settings.sources.${setting.source}`) }}</span>
    </header>
    <p class="note">{{ note }}</p>

    <!-- A table scrolls sideways in its own box where the column is narrower than it. -->
    <div v-if="prices && prices.length" class="table">
      <UiDataTable
        density="tight"
        :columns="priceColumns"
        :rows="prices"
        :row-key="(row) => row.model"
        :caption="label"
      />
    </div>
    <div v-else-if="plans && plans.length" class="table">
      <UiDataTable
        density="tight"
        :columns="planColumns"
        :rows="plans"
        :row-key="(row) => row.id"
        :caption="label"
      />
    </div>
    <div v-else-if="packs && packs.length" class="table">
      <UiDataTable
        density="tight"
        :columns="packColumns"
        :rows="packs"
        :row-key="(row) => row.id"
        :caption="label"
      />
    </div>
    <ul v-else-if="hosts && hosts.length" class="hosts" :aria-label="label">
      <li v-for="host in hosts" :key="host" class="host mono">{{ host }}</li>
    </ul>
    <p v-else-if="hosts" class="value muted">{{ t('admin.settings.hosts.none') }}</p>
    <p v-else-if="plans" class="value muted">{{ t('admin.settings.plans.none') }}</p>
    <p v-else-if="packs" class="value muted">{{ t('admin.settings.packs.none') }}</p>
    <template v-else-if="raw">
      <p class="note">{{ t('admin.settings.raw') }}</p>
      <pre class="raw mono">{{ setting.value }}</pre>
    </template>
    <p v-else class="value">
      <span v-if="!setting.value" class="muted">{{ t('admin.settings.empty') }}</span>
      <template v-else>
        <span class="mono">{{ setting.value }}</span>
        <span v-if="bytes" class="muted"> · {{ bytes }}</span>
      </template>
    </p>

    <p v-if="hosts && !sandboxNetwork" class="off">{{ t('admin.settings.hosts.off') }}</p>

    <div v-if="fromDatabase" class="origin">
      <span class="env mono">{{ t('admin.settings.envValue', { value: envText || t('admin.settings.empty') }) }}</span>
      <span v-if="changedBy">{{ changedBy }}</span>
    </div>

    <div v-if="canWrite" class="actions">
      <UiButton size="sm" :aria-label="t('admin.settings.editNamed', { name: label })" @click="emit('edit')">
        {{ t('admin.settings.edit') }}
      </UiButton>
      <UiButton
        v-if="fromDatabase"
        size="sm"
        variant="ghost"
        :aria-label="t('admin.settings.restoreNamed', { name: label })"
        @click="emit('restore')"
      >
        {{ t('admin.settings.restore') }}
      </UiButton>
    </div>
  </article>
</template>

<style scoped>
.setting {
  display: grid;
  gap: var(--space-1);
  min-width: 0;
  padding: var(--space-3) 0;
  border-top: 1px solid var(--line-soft);
}

.setting:first-of-type {
  border-top: 0;
}

.head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
}

.words {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

.label {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  letter-spacing: normal;
}

.key {
  color: var(--muted);
  font-size: var(--text-2xs);
  overflow-wrap: anywhere;
}

/* Where the value comes from: `.env` in the system gray, the database in the accent's tint —
   the approved board's two labels. */
.source {
  flex-shrink: 0;
  padding: 1px var(--space-2);
  border-radius: var(--radius-full);
  background: var(--fill);
  color: var(--muted);
  font-size: var(--text-2xs);
  font-weight: var(--weight-medium);
}

.source.database {
  background: var(--accent-tint);
  color: var(--accent);
}

.note {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.value {
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

.muted {
  color: var(--muted);
}

/* The sandbox's sites: one mono chip each, wrapping, the board's system fill. */
.hosts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: var(--space-1) 0 0;
  padding: 0;
  list-style: none;
}

.host {
  padding: 3px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--fill);
  color: var(--ink);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

/* The list has no effect without a network: a warning in the row, the board's orange. */
.off {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  background: var(--caution-tint);
  color: var(--caution-ink);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.table {
  --table-min: 0;

  min-width: 0;
  margin-top: var(--space-1);
  overflow-x: auto;
}

.raw {
  max-height: 12rem;
  margin: 0;
  overflow: auto;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.origin {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1) var(--space-3);
  color: var(--muted);
  font-size: var(--text-xs);
}

.env {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-1);
}
</style>
