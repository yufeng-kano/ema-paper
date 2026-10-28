<script setup lang="ts">
/**
 * Every pack the reader bought, newest first, when the Usage section lists only the newest
 * few (docs/web.md § Pages, *Top-up credits*): the same table, all of it, scrolling inside
 * the dialog, under the count and the balance and over the line on receipts and refunds.
 */
import type { BillingPacks } from '~/lib/api'
import { formatCount } from '~/lib/format'

defineProps<{ packs: BillingPacks }>()

const emit = defineEmits<{ close: [] }>()

const { t, locale } = useI18n()
</script>

<template>
  <UiModal :title="t('usage.purchases')" size="wide" @close="emit('close')">
    <p class="summary tabular">
      {{
        t(
          'usage.packsSummary',
          { n: formatCount(packs.items.length, locale), remaining: formatCount(packs.remaining, locale) },
          packs.items.length,
        )
      }}
    </p>
    <!-- Four short columns: the table's usual floor would only make the dialog scroll sideways. -->
    <div class="packs">
      <SettingsPacksTable :rows="packs.items" />
    </div>
    <p class="note">{{ t('usage.purchasesNote') }}</p>
    <template #footer>
      <UiButton @click="emit('close')">{{ t('common.close') }}</UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.summary {
  color: var(--muted);
  font-size: var(--text-sm);
}

.packs {
  margin-top: var(--space-3);
}

.packs :deep(table) {
  min-width: 0;
}

.note {
  margin-top: var(--space-4);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}
</style>
