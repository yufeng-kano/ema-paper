<script setup lang="ts">
/**
 * The packs a reader bought, newest first, as the Usage section and its packs dialog list
 * them (docs/web.md § Pages, *Top-up credits*): the pack — a dot, lit while it still gives
 * credits, over the day it was bought and the day it expires — then what it was granted,
 * what was spent from it and what is left, in words for a refunded or expired one
 * (docs/billing.md § Top-up packs).
 */
import type { BillingPack } from '~/lib/api'
import { formatCount, formatDate } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

defineProps<{ rows: BillingPack[] }>()

const { t, locale } = useI18n()

const columns = computed<TableColumn<BillingPack>[]>(() => [
  { key: 'pack', header: t('usage.columnPack') },
  { key: 'credits', header: t('usage.columnGranted'), numeric: true },
  { key: 'spent', header: t('usage.columnSpent'), numeric: true },
  { key: 'remaining', header: t('usage.columnLeft'), numeric: true },
])

/** What a pack has left — in words for one that gives nothing any more. */
function packLeft(pack: BillingPack): string {
  if (pack.active) {
    return formatCount(pack.remaining, locale.value)
  }
  return pack.status === 'refunded' ? t('usage.refunded') : t('usage.expired')
}
</script>

<template>
  <UiDataTable
    :columns="columns"
    :rows="rows"
    :row-key="(row) => row.id"
    :caption="t('usage.purchasesCaption')"
    density="tight"
  >
    <template #cell-pack="{ row }">
      <span class="pack">
        <span class="dot" :class="{ active: row.active }" aria-hidden="true" />
        <span class="dates">
          <span>{{ t('usage.packPurchased', { date: formatDate(row.purchased_at, locale) }) }}</span>
          <span class="expiry">{{ t('usage.packExpires', { date: formatDate(row.expires_at, locale) }) }}</span>
        </span>
      </span>
    </template>
    <template #cell-credits="{ row }">{{ formatCount(row.credits, locale) }}</template>
    <template #cell-spent="{ row }">{{ formatCount(row.spent, locale) }}</template>
    <template #cell-remaining="{ row }">
      <span :class="{ left: row.active }">{{ packLeft(row) }}</span>
    </template>
  </UiDataTable>
</template>

<style scoped>
.pack {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) 0;
}

/* Lit while the pack still gives credits; the words in the last column say why one is not. */
.dot {
  width: 8px;
  height: 8px;
  flex-shrink: 0;
  border-radius: var(--radius-full);
  background: var(--queued);
}

.dot.active {
  background: var(--accent);
}

.dates {
  display: grid;
  gap: 2px;
  line-height: 1.3;
}

.expiry {
  color: var(--muted);
  font-size: var(--text-xs);
}

/* The figure the reader came for. */
.left {
  font-weight: var(--weight-medium);
}
</style>
