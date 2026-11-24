<script setup lang="ts">
/**
 * The public rates page (docs/web.md § Pages, `/rates`): what a parse and a chat turn cost,
 * and the chat models' credits per 1K tokens — every number from `GET /api/billing/catalog`'s
 * `rates`, read when the page opens; none is typed here. The Usage section links here in
 * place of a table, and so does the pricing page's chat row. Prerendered in both locales and
 * full-bleed like the pricing page; a signed-in reader is not sent away.
 */
import { getBillingCatalog, type BillingRates } from '~/lib/api'

definePageMeta({ layout: false })

const { t } = useI18n()
const { resolve } = useApiError()

useSeoMeta({
  title: () => t('rates.seoTitle'),
  description: () => t('rates.seoDescription'),
  ogTitle: () => t('rates.seoTitle'),
  ogDescription: () => t('rates.seoDescription'),
  ogType: 'website',
  twitterCard: 'summary',
})

const rates = ref<BillingRates | null>(null)
const loadError = ref<string | null>(null)

onMounted(async () => {
  try {
    rates.value = (await getBillingCatalog()).rates
  } catch (error) {
    loadError.value = await resolve(error)
  }
})

/** Until the catalog answers or fails: the tables draw their skeletons. */
const loading = computed(() => !rates.value && !loadError.value)
</script>

<template>
  <PublicShell :title="t('rates.nav')">
    <main class="content">
      <header class="head">
        <h1 class="title">{{ t('rates.title') }}</h1>
        <p class="lede">{{ t('rates.lede') }}</p>
      </header>

      <UiBanner v-if="loadError" tone="error">{{ loadError }}</UiBanner>

      <section class="block">
        <h2 class="section-title">{{ t('pricing.howTitle') }}</h2>
        <CreditsHowTable :rates="rates" :loading="loading">
          <template #chat>{{ t('rates.chatRate') }}</template>
        </CreditsHowTable>
      </section>

      <section class="block">
        <div class="block-head">
          <h2 class="section-title">{{ t('pricing.ratesTitle') }}</h2>
          <p class="section-lede">{{ t('pricing.ratesLede') }}</p>
        </div>
        <!-- Narrower than the page tables: five short columns of figures, inside the reading column. -->
        <ChatRatesTable :rates="rates" :loading="loading" table-min="32rem">
          <template #empty><p class="note">{{ t('rates.noRates') }}</p></template>
        </ChatRatesTable>
      </section>

      <p class="note">{{ t('rates.shownUnderAnswer') }}</p>
    </main>
  </PublicShell>
</template>

<style scoped>
/* A reading column over the desk. */
.content {
  display: flex;
  flex-direction: column;
  gap: var(--space-10);
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: var(--space-12) var(--space-6) var(--space-12);
}

.head {
  display: grid;
  gap: var(--space-3);
}

.title {
  font-size: var(--display-2xl);
  line-height: 1.1;
}

.lede {
  color: var(--muted);
  font-size: var(--text-lg);
  line-height: 1.6;
  text-wrap: pretty;
}

.block {
  display: grid;
  gap: var(--space-4);
}

.block-head {
  display: grid;
  gap: var(--space-1);
}

.section-title {
  font-size: var(--display-xs);
  letter-spacing: normal;
}

.section-lede,
.note {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

@media (max-width: 640px) {
  .content {
    gap: var(--space-8);
    padding: var(--space-6) var(--space-4) var(--space-10);
  }

  .title {
    font-size: var(--display-lg);
  }

  .lede {
    font-size: var(--text-base);
  }
}
</style>
