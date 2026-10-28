<script setup lang="ts">
/**
 * The public pricing page (docs/web.md § Pages, `/pricing`; the contract is
 * docs/billing.md): the plans from `GET /api/billing/catalog`, read when the page opens, and
 * how credits work, the top-up pack when the catalog sells one, and the questions. No price
 * and no plan is typed here: Free is the catalog's `free`, a paid plan is on sale only when
 * Pay prices it, a pack likewise, and the rates are the deployment's.
 *
 * Prerendered in both locales like the sign-in page, and full-bleed like it: a visitor
 * lands here signed out, and a signed-in reader is not sent away.
 */
import { createCheckout, getBillingCatalog, type CatalogPlan, type CatalogResponse } from '~/lib/api'
import { formatBytes, formatCount, formatMinorAmount, packValidity } from '~/lib/format'
import { SETTINGS_QUERY, keepAfterSignIn } from '~/lib/settings'
import type { TableColumn } from '~/lib/table'

definePageMeta({ layout: false })

const { t, locale } = useI18n()
const localePath = useLocalePath()
const { resolve } = useApiError()

useSeoMeta({
  title: () => t('pricing.seoTitle'),
  description: () => t('pricing.seoDescription'),
  ogTitle: () => t('pricing.seoTitle'),
  ogDescription: () => t('pricing.seoDescription'),
  ogType: 'website',
  twitterCard: 'summary',
})

const catalog = ref<CatalogResponse | null>(null)
const loadError = ref<string | null>(null)

onMounted(async () => {
  try {
    catalog.value = await getBillingCatalog()
  } catch (error) {
    loadError.value = await resolve(error)
  }
})

/** Null until the catalog answers: no rate is ever assumed (docs/web.md § Rules). */
const parsePerPage = computed(() => catalog.value?.rates.parse_per_page ?? null)
const webSearchPerCall = computed(() => catalog.value?.rates.web_search_per_call ?? null)

// --- the top-up pack ----------------------------------------------------------------------

/** The first pack on sale, or null: the pack card, the paid card's line and the fifth
 *  question stand only then (docs/billing.md § Top-up packs). No button here: a pack is
 *  bought where the reader is signed in, the Usage section or the credits dialog. */
const pack = computed(() => catalog.value?.packs[0] ?? null)
const packPrice = computed(() => (pack.value ? formatMinorAmount(pack.value.amount, pack.value.currency, locale.value) : ''))
/** `12 months`, or the days when the validity is not a whole number of months. */
const validity = computed(() => {
  if (!pack.value) {
    return ''
  }
  const { unit, n } = packValidity(pack.value.valid_days)
  return t(`topUp.${unit}`, { n: formatCount(n, locale.value) }, n)
})

/** `per month`, `per year`, or `per 3 months` — as Pay reports the cycle. */
function cycleOf(plan: CatalogPlan): string {
  const { interval, frequency } = plan.cycle
  if (frequency === 1 && (interval === 'month' || interval === 'year')) {
    return t(`pricing.cycle.${interval}`)
  }
  return t('pricing.cycle.other', { n: formatCount(frequency, locale.value), interval })
}

/** The Usage section, where *Upgrade* is; a signed-out visitor is sent to sign in first
 *  and lands there after it (the destination kept for this tab). */
const usagePath = computed(() => `${localePath('/files')}?${SETTINGS_QUERY}=usage`)

// --- a signed-in reader -------------------------------------------------------------------

const auth = useAuth()
/** `free` or the paid plan's id once `GET /api/me` answers; null for a signed-out visitor,
 *  whose cards keep their public actions (docs/web.md § Pages, `/pricing`). */
const myPlan = computed(() => auth.me.value?.plan ?? null)

const checkingOut = ref<string | null>(null)
const checkoutError = ref<{ plan: string; message: string } | null>(null)

/** Pay's checkout for the plan, in this tab: the return lands on the Usage section, as
 *  that section's own *Upgrade* does (docs/billing.md § Kano Pay). */
async function upgrade(plan: CatalogPlan) {
  if (checkingOut.value) {
    return
  }
  checkingOut.value = plan.plan_id
  checkoutError.value = null
  try {
    const { url } = await createCheckout(plan.plan_id)
    window.location.assign(url)
  } catch (error) {
    checkoutError.value = { plan: plan.plan_id, message: await resolve(error) }
  } finally {
    checkingOut.value = null
  }
}

function price(plan: CatalogPlan): string {
  return formatMinorAmount(plan.amount, plan.currency, locale.value)
}

// --- the rates table ----------------------------------------------------------------------

interface RateRow {
  model: string
  input: number
  cache_read: number
  cache_write: number
  output: number
}

const rateRows = computed<RateRow[]>(() =>
  Object.entries(catalog.value?.rates.chat ?? {}).map(([model, rate]) => ({ model, ...rate })),
)

const rateColumns = computed<TableColumn<RateRow>[]>(() => [
  { key: 'model', header: t('pricing.columnModel') },
  { key: 'input', header: t('pricing.columnInput'), numeric: true },
  { key: 'cache_read', header: t('pricing.columnCacheRead'), numeric: true },
  { key: 'cache_write', header: t('pricing.columnCacheWrite'), numeric: true },
  { key: 'output', header: t('pricing.columnOutput'), numeric: true },
])

function rate(value: number): string {
  return new Intl.NumberFormat(locale.value, { maximumFractionDigits: 3 }).format(value)
}

/** The fifth question is the pack's, asked only while one is on sale. */
const questions = computed(() => (pack.value ? ['1', '2', '3', '4', '5'] : ['1', '2', '3', '4']))
</script>

<template>
  <PublicShell :title="t('pricing.nav')">
    <main class="content">
      <section class="hero">
        <p class="eyebrow">{{ t('pricing.eyebrow') }}</p>
        <h1 class="title">{{ t('pricing.title') }}</h1>
        <p class="lede">{{ t('pricing.lede') }}</p>
      </section>

      <section class="plans" :aria-label="t('pricing.plans')">
        <UiBanner v-if="loadError" tone="error">{{ loadError }}</UiBanner>
        <template v-else>
          <!-- Free: from the catalog's `free`, the deployment's own numbers. -->
          <article v-if="catalog" class="plan">
            <p class="eyebrow">{{ t('pricing.free') }}</p>
            <p class="price">{{ t('pricing.freePrice') }}</p>
            <p class="for">{{ t('pricing.freeFor') }}</p>
            <ul class="gives">
              <li>{{ t('pricing.creditsPerMonth', { n: formatCount(catalog.free.credits, locale) }, catalog.free.credits) }}</li>
              <li>{{ t('pricing.storageKept', { size: formatBytes(catalog.free.storage_bytes, locale) }) }}</li>
              <li>{{ t('pricing.jobsAtOnce', { n: formatCount(catalog.free.max_jobs, locale) }, catalog.free.max_jobs) }}</li>
              <li>{{ t('pricing.api') }}</li>
              <li>{{ catalog.free.sandbox ? t('pricing.chatSandbox') : t('pricing.chatNoSandbox') }}</li>
            </ul>
            <div class="actions">
              <p v-if="myPlan === 'free'" class="current">{{ t('pricing.currentPlan') }}</p>
              <NuxtLink v-else-if="myPlan === null" class="cta secondary" :to="localePath('/')">
                {{ t('pricing.startFree') }}
              </NuxtLink>
            </div>
          </article>
          <div v-else class="plan" aria-busy="true" :aria-label="t('pricing.loading')">
            <UiSkeleton :rows="5" />
          </div>

          <!-- Each plan on sale: Pay's price as the catalog gives it. -->
          <template v-if="catalog && catalog.available">
            <article v-for="plan in catalog.plans" :key="plan.plan_id" class="plan paid">
              <p class="eyebrow accent">{{ plan.name }}</p>
              <p class="price">
                {{ price(plan) }}<span class="cycle"> {{ t('pricing.perCycle', { cycle: cycleOf(plan) }) }}</span>
              </p>
              <p class="for">{{ t('pricing.paidFor') }}</p>
              <ul class="gives">
                <li>{{ t('pricing.creditsPerCycle', { n: formatCount(plan.credits, locale), cycle: cycleOf(plan) }, plan.credits) }}</li>
                <li v-if="pack">{{ t('pricing.topUpGives') }}</li>
                <li>{{ t('pricing.storageKept', { size: formatBytes(plan.storage_bytes, locale) }) }}</li>
                <li>{{ t('pricing.jobsAtOnce', { n: formatCount(plan.max_jobs, locale) }, plan.max_jobs) }}</li>
                <li>{{ t('pricing.api') }}</li>
                <li>{{ plan.sandbox ? t('pricing.chatSandbox') : t('pricing.chatNoSandbox') }}</li>
              </ul>
              <div class="actions">
                <template v-if="myPlan === plan.plan_id">
                  <p class="current">{{ t('pricing.currentPlan') }}</p>
                  <NuxtLink class="cta secondary" :to="usagePath">{{ t('pricing.manage') }}</NuxtLink>
                </template>
                <button
                  v-else-if="myPlan === 'free'"
                  type="button"
                  class="cta"
                  :disabled="checkingOut !== null"
                  :aria-busy="checkingOut === plan.plan_id"
                  @click="upgrade(plan)"
                >
                  {{ t('pricing.upgrade') }}
                </button>
                <NuxtLink v-else-if="myPlan === null" class="cta" :to="usagePath" @click="keepAfterSignIn(usagePath)">
                  {{ t('pricing.upgrade') }}
                </NuxtLink>
              </div>
              <UiBanner v-if="checkoutError?.plan === plan.plan_id" tone="error">{{ checkoutError.message }}</UiBanner>
            </article>
          </template>
          <p v-else-if="catalog" class="not-on-sale">{{ t('pricing.notOnSale') }}</p>
          <div v-else class="plan" aria-hidden="true">
            <UiSkeleton :rows="5" />
          </div>
        </template>
      </section>

      <!-- The pack, when the catalog sells one: Pay's price, this deployment's credits and
           validity, and where a Pro reader buys it. -->
      <section v-if="pack" class="pack-section" :aria-label="t('pricing.packEyebrow')">
        <article class="plan pack">
          <p class="eyebrow accent">{{ t('pricing.packEyebrow') }}</p>
          <p class="for">{{ t('pricing.packFor') }}</p>
          <p class="price">{{ t('pricing.packCredits', { n: formatCount(pack.credits, locale) }, pack.credits) }}</p>
          <p class="pack-price">{{ t('pricing.packPrice', { price: packPrice }) }}</p>
          <ul class="gives">
            <li>{{ t('pricing.packValid', { validity }) }}</li>
            <li>{{ t('pricing.packAfterPlan') }}</li>
            <li>{{ t('pricing.packRefund') }}</li>
          </ul>
          <p class="pack-how">{{ t('pricing.packHow') }}</p>
        </article>
      </section>

      <section class="how">
        <div class="how-head">
          <h2 class="section-title">{{ t('pricing.howTitle') }}</h2>
          <p class="section-lede">{{ pack ? t('pricing.howLedePack', { validity }) : t('pricing.howLede') }}</p>
        </div>
        <UiSkeleton v-if="parsePerPage === null && !loadError" class="sheet" :rows="4" />
        <table v-else-if="parsePerPage !== null" class="how-table sheet">
          <thead>
            <tr>
              <th scope="col">{{ t('pricing.columnAction') }}</th>
              <th scope="col" class="num">{{ t('pricing.columnCredits') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{{ t('pricing.parseRow') }}</td>
              <td class="num tabular">{{ t('pricing.perPage', { n: formatCount(parsePerPage, locale) }) }}</td>
            </tr>
            <tr>
              <td>{{ t('pricing.retryRow') }}</td>
              <td class="num tabular">{{ t('pricing.perPage', { n: formatCount(parsePerPage, locale) }) }}</td>
            </tr>
            <tr>
              <td>{{ t('pricing.reconvertRow') }}</td>
              <td class="num tabular">{{ t('pricing.perPage', { n: formatCount(parsePerPage, locale) }) }}</td>
            </tr>
            <tr>
              <td>{{ t('pricing.chatRow') }}</td>
              <td class="num">
                {{ t('pricing.chatRate') }}
                <NuxtLink class="rates-link" :to="localePath('/rates')">{{ t('pricing.ratesLink') }}</NuxtLink>
              </td>
            </tr>
            <tr v-if="webSearchPerCall !== null">
              <td>{{ t('pricing.webSearchRow') }}</td>
              <td class="num tabular">{{ t('pricing.perCall', { n: formatCount(webSearchPerCall, locale) }) }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="rateRows.length" class="rates">
        <div class="how-head">
          <h2 class="section-title">{{ t('pricing.ratesTitle') }}</h2>
          <p class="section-lede">{{ t('pricing.ratesLede') }}</p>
        </div>
        <!-- Wider than a phone: the sheet scrolls sideways, the page does not. -->
        <div class="sheet rates-sheet">
          <UiDataTable
            :columns="rateColumns"
            :rows="rateRows"
            :row-key="(row) => row.model"
            :caption="t('pricing.ratesTitle')"
            density="tight"
          >
            <template #cell-model="{ row }"><span class="mono">{{ row.model }}</span></template>
            <template #cell-input="{ row }">{{ rate(row.input) }}</template>
            <template #cell-cache_read="{ row }">{{ rate(row.cache_read) }}</template>
            <template #cell-cache_write="{ row }">{{ rate(row.cache_write) }}</template>
            <template #cell-output="{ row }">{{ rate(row.output) }}</template>
          </UiDataTable>
        </div>
      </section>

      <section class="faq">
        <h2 class="section-title">{{ t('pricing.faqTitle') }}</h2>
        <dl class="questions">
          <div v-for="q in questions" :key="q" class="question">
            <dt>{{ t(`pricing.q${q}`) }}</dt>
            <dd>{{ t(`pricing.a${q}`, { validity }) }}</dd>
          </div>
        </dl>
      </section>
    </main>

  </PublicShell>
</template>

<style scoped>
/* Apple's product-page rhythm on the desk: a centered hero, paper cards, centered section
   heads over content the width of the cards. */
.content {
  display: flex;
  flex-direction: column;
  gap: var(--space-14);
  width: 100%;
  max-width: 920px;
  margin: 0 auto;
  padding: var(--space-14) var(--space-6) var(--space-12);
}

.hero {
  display: grid;
  justify-items: center;
  gap: var(--space-3);
  text-align: center;
}

.title {
  max-width: 16em;
  font-size: var(--display-3xl);
  line-height: 1.08;
  text-wrap: balance;
}

.lede,
.section-lede,
.for {
  color: var(--muted);
  line-height: 1.55;
}

.lede {
  max-width: 34em;
  font-size: var(--text-lg);
  text-wrap: pretty;
}

/* Two cards side by side, stacking on a phone; the paid one carries an accent ring. */
.plans {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-5);
  align-items: stretch;
}

.plan {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-8);
  border-radius: var(--radius-card);
  background: var(--paper);
  box-shadow: var(--shadow-pane);
}

.plan.paid {
  box-shadow:
    0 0 0 2px var(--accent),
    var(--shadow-pane);
}

.plan .eyebrow {
  font-size: var(--text-sm);
}

.eyebrow.accent {
  color: var(--accent);
}

.price {
  font-size: var(--display-xl);
  font-weight: var(--weight-bold);
  letter-spacing: var(--tracking-tight);
  line-height: 1.1;
}

.cycle {
  color: var(--muted);
  font-size: var(--text-base);
  font-weight: var(--weight-normal);
  letter-spacing: normal;
}

/* What the plan gives, as a spec list: one line each, hairlines between. */
.gives {
  margin: var(--space-3) 0 var(--space-6);
  padding: 0;
  list-style: none;
  font-size: var(--text-sm);
}

.gives li {
  padding: 10px 0;
  border-top: 1px solid var(--line);
}

.gives li:last-child {
  border-bottom: 1px solid var(--line);
}

/* The card's action at its foot: a link, the checkout button, or *Current plan*. */
.actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  margin-top: auto;
}

.current {
  margin: 0;
  color: var(--muted);
  font-weight: var(--weight-medium);
}

.cta {
  display: inline-flex;
  flex: 1 1 auto;
  align-items: center;
  justify-content: center;
  height: var(--control-height-lg);
  padding: 0 var(--space-5);
  border: 0;
  border-radius: var(--radius-full);
  font: inherit;
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  cursor: pointer;
  background: var(--accent);
  color: var(--accent-fg);
  text-decoration: none;
  box-shadow: inset 0 1px 0 var(--accent-highlight);
  transition: background var(--duration-fast) var(--ease);
}

.cta:hover {
  background: var(--accent-hover);
}

.cta:disabled {
  opacity: 0.6;
  cursor: progress;
}

.cta.secondary {
  background: var(--fill);
  color: var(--ink);
  box-shadow: none;
}

.cta.secondary:hover {
  background: var(--fill-strong);
}

.not-on-sale {
  align-self: center;
  color: var(--muted);
  font-size: var(--text-sm);
  text-align: center;
}

/* The pack: one card at the plans' width, centered under them. */
.pack-section {
  display: grid;
  justify-items: center;
}

.plan.pack {
  width: 100%;
  max-width: 448px;
}

.pack-price {
  color: var(--muted);
  font-size: var(--text-base);
}

.pack-how {
  margin-top: auto;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.rates-link {
  color: var(--accent);
}

.rates-link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
}

.how,
.rates,
.faq {
  display: grid;
  gap: var(--space-6);
}

.how-head {
  display: grid;
  justify-items: center;
  gap: var(--space-2);
  text-align: center;
}

.section-title {
  font-size: var(--display-lg);
  text-align: center;
}

.section-lede {
  max-width: 40em;
  font-size: var(--text-base);
}

/* A table on the desk sits on a paper sheet with the plans' lift, at the grouped list's
   radius. */
.sheet {
  overflow: hidden;
  border-radius: var(--radius-lg);
  background: var(--paper);
  box-shadow:
    0 0 0 0.5px var(--pane-edge),
    0 4px 14px var(--glass-shadow);
}

.rates-sheet {
  overflow-x: auto;
  overscroll-behavior-x: contain;
  padding: var(--space-2) var(--space-3);
}

.how-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  font-size: var(--text-sm);
}

.how-table th,
.how-table td {
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}

.how-table tbody tr:last-child td {
  border-bottom: 0;
}

.how-table th {
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.num {
  text-align: right !important;
  white-space: nowrap;
}

.questions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-8) var(--space-10);
  margin: 0;
}

.question dt {
  margin-bottom: var(--space-2);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.question dd {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

@media (max-width: 640px) {
  .content {
    gap: var(--space-11);
    padding: var(--space-10) var(--space-4) var(--space-10);
  }

  .title {
    font-size: var(--display-lg);
  }

  .lede {
    font-size: var(--text-base);
  }

  .plan {
    padding: var(--space-6);
  }

  .section-title {
    font-size: var(--display-md);
  }
}
</style>
