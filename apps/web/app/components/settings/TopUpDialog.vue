<script setup lang="ts">
/**
 * The pack before its checkout (docs/web.md § Pages, *Top-up credits*): what the first pack
 * on sale gives and costs as the catalog says — its credits and validity this deployment's,
 * its price Pay's — the rules a buyer should know, and *Go to payment*, which starts Pay's
 * checkout in this tab (docs/billing.md § Top-up packs). Nothing is charged here.
 */
const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()

const topUp = reactive(useTopUp())
onMounted(() => {
  void topUp.load()
})
</script>

<template>
  <UiModal :title="t('topUp.title')" size="sm" :busy="topUp.buying" @close="emit('close')">
    <UiSkeleton v-if="!topUp.loaded" :rows="2" />
    <template v-else-if="topUp.pack">
      <div class="pack">
        <div class="pack-head">
          <p class="pack-credits tabular">{{ t('topUp.credits', { n: topUp.credits }, topUp.pack.credits) }}</p>
          <p class="pack-price tabular">{{ topUp.price }}</p>
        </div>
        <p class="pack-terms">{{ t('topUp.oneTime', { validity: topUp.validity }) }}</p>
      </div>
      <ul class="rules">
        <li>{{ t('topUp.afterPlan') }}</li>
        <li>{{ t('topUp.expires') }}</li>
        <li>{{ t('topUp.refund') }}</li>
      </ul>
      <p class="note">{{ t('topUp.goesToPay') }}</p>
    </template>
    <p v-else class="note">{{ t('topUp.notOnSale') }}</p>
    <UiBanner v-if="topUp.error" class="error" tone="error">{{ topUp.error }}</UiBanner>
    <template #footer>
      <UiButton variant="ghost" :disabled="topUp.buying" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton v-if="topUp.pack" variant="primary" :loading="topUp.buying" @click="topUp.buy">
        {{ t('topUp.pay') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
/* The pack as one sunken card: its credits large at the left, its price at the right. */
.pack {
  display: grid;
  gap: var(--space-1);
  margin-top: var(--space-2);
  padding: var(--space-4);
  border-radius: var(--radius-lg);
  background: var(--paper-sunken);
}

.pack-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
}

.pack-credits {
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
}

.pack-price {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.pack-terms {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.rules {
  display: grid;
  gap: var(--space-1);
  margin: var(--space-4) 0 0;
  padding: 0 0 0 var(--space-4);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.note {
  margin-top: var(--space-4);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.error {
  margin-top: var(--space-3);
}
</style>
