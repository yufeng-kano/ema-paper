<script setup lang="ts">
/**
 * Billing (docs/web.md § Pages, `/settings/billing`; the contract is docs/billing.md): the
 * plan and its action — *Upgrade* (a checkout) or *Manage subscription* (Pay's portal) —
 * the period's credits as a meter with its reset date, the chat rates, and what the plan
 * gives the workspace and the chat agent. Every figure is `GET /api/billing`'s: the section
 * never names a price or a plan the API did not.
 */
import {
  createCheckout,
  createPortal,
  getBilling,
  getBillingCatalog,
  type BillingResponse,
  type ChatRate,
} from '~/lib/api'
import { formatBytes, formatCount, formatDate } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const { data, pending, errorMessage, refresh } = useAuthedData(() => getBilling())

const billing = computed<BillingResponse | null>(() => data.value)
const free = computed(() => billing.value?.plan.id === 'free')

const planName = computed(() => {
  const plan = billing.value?.plan
  return !plan ? '' : plan.id === 'free' ? t('billing.free') : plan.name
})

/** One line of what the plan gives, from its four grants. */
const planGives = computed(() => {
  const plan = billing.value?.plan
  if (!plan) {
    return ''
  }
  return [
    t('billing.creditsPerPeriod', { n: formatCount(plan.credits, locale.value) }, plan.credits),
    t('billing.storage', { size: formatBytes(plan.storage_bytes, locale.value) }),
    t('billing.jobsAtOnce', { n: formatCount(plan.max_jobs, locale.value) }, plan.max_jobs),
    plan.sandbox ? t('billing.sandboxYes') : t('billing.sandboxNo'),
  ].join(' · ')
})

/** The subscription's state under the plan line: renewing, cancelled at the end, lapsed. */
const subscriptionLine = computed(() => {
  const subscription = billing.value?.subscription
  if (!subscription) {
    return null
  }
  const end = formatDate(subscription.period_end, locale.value)
  if (subscription.status !== 'active' && subscription.status !== 'trialing') {
    return t('billing.lapsed', { status: subscription.status })
  }
  return subscription.cancel_at_period_end ? t('billing.cancelAtEnd', { date: end }) : t('billing.renews', { date: end })
})

const spentShare = computed(() => {
  const period = billing.value?.period
  return period && period.granted > 0 ? Math.min(1, period.spent / period.granted) : 0
})

// --- the rates table ---------------------------------------------------------------------

interface RateRow extends ChatRate {
  model: string
}

const rateRows = computed<RateRow[]>(() =>
  Object.entries(billing.value?.rates.chat ?? {}).map(([model, rate]) => ({ model, ...rate })),
)

const rateColumns = computed<TableColumn<RateRow>[]>(() => [
  { key: 'model', header: t('billing.columnModel') },
  { key: 'input', header: t('billing.columnInput'), numeric: true },
  { key: 'cache_read', header: t('billing.columnCacheRead'), numeric: true },
  { key: 'cache_write', header: t('billing.columnCacheWrite'), numeric: true },
  { key: 'output', header: t('billing.columnOutput'), numeric: true },
])

/** A rate to at most three decimals: 0.005 credits per 1K cache reads stays legible. */
function rate(value: number): string {
  return new Intl.NumberFormat(locale.value, { maximumFractionDigits: 3 }).format(value)
}

// --- checkout and portal ------------------------------------------------------------------

const acting = ref(false)
const actionError = ref<string | null>(null)

/**
 * Pay's checkout for the first plan on sale, in this tab: the return lands on
 * `/files?settings=billing`, where the section reads the plan the webhook or the refresh
 * wrote (docs/billing.md § Kano Pay).
 */
async function upgrade() {
  if (acting.value) {
    return
  }
  acting.value = true
  actionError.value = null
  try {
    const catalog = await getBillingCatalog()
    const plan = catalog.plans[0]
    if (!catalog.available || !plan) {
      actionError.value = t('billing.notOnSale')
      return
    }
    const { url } = await createCheckout(plan.plan_id)
    window.location.assign(url)
  } catch (error) {
    actionError.value = await resolve(error)
  } finally {
    acting.value = false
  }
}

/** Pay's portal, in this tab: one link, where the payment method, invoices and cancelling live. */
async function manage() {
  if (acting.value) {
    return
  }
  acting.value = true
  actionError.value = null
  try {
    const { url } = await createPortal()
    window.location.assign(url)
  } catch (error) {
    actionError.value = await resolve(error)
  } finally {
    acting.value = false
  }
}

/** The shell's balance follows the section's read, so the two never disagree. */
async function reload() {
  await Promise.all([refresh(), auth.refresh()])
}
</script>

<template>
  <SettingsSection :title="t('settings.sections.billing')">
    <template #actions>
      <UiButton
        variant="ghost"
        size="sm"
        icon-only
        :label="t('common.refresh')"
        :loading="pending"
        @click="reload"
      >
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <p class="lede">{{ t('billing.lede') }}</p>
    <UiBanner v-if="errorMessage" tone="error">{{ errorMessage }}</UiBanner>
    <UiSkeleton v-if="!billing && !errorMessage" :rows="4" />

    <template v-else-if="billing">
      <SettingsRow :label="t('billing.plan')" :description="t('billing.planDescription')">
        <div class="plan">
          <div class="plan-text">
            <p class="plan-name">{{ planName }}</p>
            <p class="plan-gives">{{ planGives }}</p>
          </div>
          <UiButton v-if="billing.subscription" variant="secondary" :loading="acting" @click="manage">
            {{ t('billing.manage') }}
          </UiButton>
          <UiButton v-else-if="free && billing.checkout_available" :loading="acting" @click="upgrade">
            {{ t('billing.upgrade') }}
          </UiButton>
        </div>
        <template #note>
          <p v-if="subscriptionLine" class="note">{{ subscriptionLine }}</p>
          <p v-if="billing.subscription" class="note">{{ t('billing.manageNote') }}</p>
          <p v-else-if="!billing.checkout_available" class="note">{{ t('billing.notOnSale') }}</p>
          <p v-if="billing.pay_reachable === false" class="note">{{ t('billing.payBehind') }}</p>
          <UiBanner v-if="actionError" tone="error">{{ actionError }}</UiBanner>
        </template>
      </SettingsRow>

      <SettingsRow :label="t('billing.credits')" :description="t('billing.creditsDescription')">
        <p class="balance tabular">
          <span class="remaining">{{ formatCount(billing.period.remaining, locale) }}</span>
          <span class="of">
            {{
              t('billing.remainingOf', {
                remaining: formatCount(billing.period.remaining, locale),
                granted: formatCount(billing.period.granted, locale),
              })
            }}
          </span>
        </p>
        <template #note>
          <div class="meter-row">
            <UiUsageBar class="meter" :share="spentShare" :label="t('billing.meter')" />
            <span class="resets">{{ t('billing.resets', { date: formatDate(billing.period.end, locale) }) }}</span>
          </div>
          <p class="note">
            {{
              t('billing.rateLine', { n: formatCount(billing.rates.parse_per_page, locale) }, billing.rates.parse_per_page)
            }}
          </p>
        </template>
      </SettingsRow>

      <SettingsRow :label="t('billing.rates')" :description="t('billing.ratesDescription')">
        <template #note>
          <UiDataTable
            v-if="rateRows.length"
            :columns="rateColumns"
            :rows="rateRows"
            :row-key="(row) => row.model"
            :caption="t('billing.rates')"
            density="tight"
          >
            <template #cell-model="{ row }"><span class="mono">{{ row.model }}</span></template>
            <template #cell-input="{ row }">{{ rate(row.input) }}</template>
            <template #cell-cache_read="{ row }">{{ rate(row.cache_read) }}</template>
            <template #cell-cache_write="{ row }">{{ rate(row.cache_write) }}</template>
            <template #cell-output="{ row }">{{ rate(row.output) }}</template>
          </UiDataTable>
          <p v-else class="note">{{ t('billing.noRates') }}</p>
        </template>
      </SettingsRow>

      <SettingsRow :label="t('billing.plan_gives')" :description="t('billing.planGivesDescription')">
        <template #note>
          <p class="fact">{{ t('billing.storageFact', { size: formatBytes(billing.plan.storage_bytes, locale) }) }}</p>
          <p class="fact">{{ billing.plan.sandbox ? t('billing.sandboxFact') : t('billing.noSandboxFact') }}</p>
        </template>
      </SettingsRow>
    </template>
  </SettingsSection>
</template>

<style scoped>
.lede {
  padding: var(--space-5) 0 var(--space-2);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.plan {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-width: 0;
}

.plan-text {
  display: grid;
  gap: var(--space-1);
  min-width: 0;
}

.plan-name {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.plan-gives,
.note,
.fact {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.fact {
  color: var(--ink);
}

/* The balance: the figure in the accent — a cost figure — and the rest of the line muted. */
.balance {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  white-space: nowrap;
}

.remaining {
  color: var(--accent);
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
}

.of {
  color: var(--muted);
  font-size: var(--text-sm);
}

.meter-row {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  margin-bottom: var(--space-2);
}

.meter {
  flex: 1 1 auto;
  min-width: 0;
}

.resets {
  flex-shrink: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

@media (max-width: 640px) {
  .plan {
    flex-wrap: wrap;
  }
}
</style>
