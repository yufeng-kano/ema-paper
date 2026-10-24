<script setup lang="ts">
/**
 * Usage (docs/web.md § Pages, `/settings/usage`; the contract is docs/billing.md), laid out
 * as Claude's own usage page: the plan and its action — *Upgrade* (a checkout) or *Manage
 * subscription* (Pay's portal) — then the usage limits, the period's credits and the
 * workspace's storage, each a bar with *N% used*, then the rates and the sandbox the plan
 * gives. Every figure is the API's: the section never names a price or a plan the API did not.
 */
import {
  createCheckout,
  createPortal,
  getBilling,
  getBillingCatalog,
  getSandbox,
  resetSandbox,
  stopSandbox,
  type ChatRate,
  type SandboxState,
} from '~/lib/api'
import { formatBytes, formatCount, formatDate, formatShortDateTime } from '~/lib/format'
import type { TableColumn } from '~/lib/table'

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const { data: billing, pending, errorMessage, refresh } = useAuthedData(() => getBilling())
const {
  data: sandbox,
  pending: sandboxPending,
  errorMessage: sandboxError,
  refresh: refreshSandbox,
} = useAuthedData(() => getSandbox())

// --- the plan ----------------------------------------------------------------------------

const free = computed(() => billing.value?.plan.id === 'free')

const planName = computed(() => {
  const plan = billing.value?.plan
  return !plan ? '' : plan.id === 'free' ? t('usage.free') : plan.name
})

/** One line of what the plan gives, from its four grants. */
const planGives = computed(() => {
  const plan = billing.value?.plan
  if (!plan) {
    return ''
  }
  return [
    t('usage.planCredits', { n: formatCount(plan.credits, locale.value) }, plan.credits),
    t('usage.planStorage', { size: formatBytes(plan.storage_bytes, locale.value) }),
    t('usage.planJobs', { n: formatCount(plan.max_jobs, locale.value) }, plan.max_jobs),
    plan.sandbox ? t('usage.planSandbox') : t('usage.planNoSandbox'),
  ].join(' · ')
})

/** The subscription's state under the plan: renewing, cancelled at the end, lapsed. */
const subscriptionLine = computed(() => {
  const subscription = billing.value?.subscription
  if (!subscription) {
    return null
  }
  const end = formatDate(subscription.period_end, locale.value)
  if (subscription.status !== 'active' && subscription.status !== 'trialing') {
    return t('usage.lapsed', { status: subscription.status })
  }
  return subscription.cancel_at_period_end ? t('usage.cancelAtEnd', { date: end }) : t('usage.renews', { date: end })
})

// --- the usage limits --------------------------------------------------------------------

/** An overdrawn period reads 100 % and 0 left, never a negative balance (docs/billing.md
 *  § A chat turn). */
const creditsShare = computed(() => {
  const period = billing.value?.period
  return period && period.granted > 0 ? Math.min(1, period.spent / period.granted) : 0
})

const remaining = computed(() => Math.max(0, billing.value?.period.remaining ?? 0))

const storageShare = computed(() => {
  const workspace = sandbox.value?.workspace
  return workspace && workspace.quota_bytes > 0 ? Math.min(1, workspace.bytes / workspace.quota_bytes) : 0
})

/** When the figures on screen were read: either read landing moves it. */
const updatedAt = ref<Date | null>(null)
watch([billing, sandbox], () => {
  updatedAt.value = new Date()
})

const updatedLabel = computed(() =>
  updatedAt.value ? new Intl.DateTimeFormat(locale.value, { timeStyle: 'short' }).format(updatedAt.value) : '',
)

/** The shell's balance follows the section's read, so the two never disagree. */
async function reload() {
  await Promise.all([refresh(), refreshSandbox(), auth.refresh()])
}

// --- the rates table ---------------------------------------------------------------------

interface RateRow extends ChatRate {
  model: string
}

const rateRows = computed<RateRow[]>(() =>
  Object.entries(billing.value?.rates.chat ?? {}).map(([model, rate]) => ({ model, ...rate })),
)

const rateColumns = computed<TableColumn<RateRow>[]>(() => [
  { key: 'model', header: t('usage.columnModel') },
  { key: 'input', header: t('usage.columnInput'), numeric: true },
  { key: 'cache_read', header: t('usage.columnCacheRead'), numeric: true },
  { key: 'cache_write', header: t('usage.columnCacheWrite'), numeric: true },
  { key: 'output', header: t('usage.columnOutput'), numeric: true },
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
 * `/files?settings=billing`, which opens this section, where it reads the plan the webhook
 * or the refresh wrote (docs/billing.md § Kano Pay).
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
      actionError.value = t('usage.notOnSale')
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

// --- the sandbox -------------------------------------------------------------------------

/** The VM's controls: a runner this deployment has, on a plan that gives one. */
const sandboxAvailable = computed(
  () => sandbox.value !== null && sandbox.value.state !== 'unavailable' && sandbox.value.granted !== false,
)

function stateTone(state: SandboxState): 'ok' | 'info' | 'neutral' {
  return state === 'running' ? 'ok' : state === 'starting' ? 'info' : 'neutral'
}

const sandboxActionPending = ref(false)
const sandboxActionError = ref<string | null>(null)
const confirmingReset = ref(false)

async function runSandboxAction(action: () => Promise<void>) {
  sandboxActionPending.value = true
  sandboxActionError.value = null
  try {
    await action()
    confirmingReset.value = false
    await refreshSandbox()
  } catch (error) {
    sandboxActionError.value = await resolve(error)
  } finally {
    sandboxActionPending.value = false
  }
}
</script>

<template>
  <SettingsSection :title="t('settings.sections.usage')">
    <UiBanner v-if="errorMessage" class="state" tone="error">{{ errorMessage }}</UiBanner>
    <UiSkeleton v-if="!billing && !errorMessage" class="state" :rows="4" />

    <template v-else-if="billing">
      <SettingsGroup :title="t('usage.plan')">
        <div class="plan">
          <div class="plan-text">
            <p class="plan-name">{{ planName }}</p>
            <p class="plan-gives">{{ planGives }}</p>
          </div>
          <UiButton v-if="billing.subscription" variant="secondary" :loading="acting" @click="manage">
            {{ t('usage.manage') }}
          </UiButton>
          <UiButton v-else-if="free && billing.checkout_available" :loading="acting" @click="upgrade">
            {{ t('usage.upgrade') }}
          </UiButton>
        </div>
        <template
          v-if="subscriptionLine || billing.subscription || !billing.checkout_available || billing.pay_reachable === false || actionError"
          #foot
        >
          <p v-if="subscriptionLine">{{ subscriptionLine }}</p>
          <p v-if="billing.subscription">{{ t('usage.manageNote') }}</p>
          <p v-else-if="!billing.checkout_available">{{ t('usage.notOnSale') }}</p>
          <p v-if="billing.pay_reachable === false">{{ t('usage.payBehind') }}</p>
          <UiBanner v-if="actionError" tone="error">{{ actionError }}</UiBanner>
        </template>
      </SettingsGroup>

      <!-- One row per limit, as Claude draws its own: the name and a line under it, the
           bar, the share used. -->
      <SettingsGroup :title="t('usage.limits')">
        <div class="limit">
          <div class="limit-text">
            <p class="limit-name">{{ t('usage.credits') }}</p>
            <p class="limit-line">
              {{ t('usage.resets', { date: formatDate(billing.period.end, locale) }) }} ·
              {{
                t('usage.left', {
                  remaining: formatCount(remaining, locale),
                  granted: formatCount(billing.period.granted, locale),
                })
              }}
            </p>
          </div>
          <UiUsageBar class="limit-bar" :share="creditsShare" :label="t('usage.creditsMeter')" />
          <p class="limit-used tabular">{{ t('usage.used', { n: Math.round(creditsShare * 100) }) }}</p>
        </div>

        <div v-if="sandbox" class="limit">
          <div class="limit-text">
            <p class="limit-name">{{ t('usage.storage') }}</p>
            <p class="limit-line">
              {{
                t('usage.storageOf', {
                  used: formatBytes(sandbox.workspace.bytes, locale),
                  quota: formatBytes(sandbox.workspace.quota_bytes, locale),
                })
              }}
            </p>
          </div>
          <UiUsageBar class="limit-bar" :share="storageShare" :label="t('usage.storageMeter')" />
          <p class="limit-used tabular">{{ t('usage.used', { n: Math.round(storageShare * 100) }) }}</p>
        </div>
        <UiSkeleton v-else-if="!sandboxError" :rows="1" />

        <template #foot>
          <div class="updated">
            <span v-if="updatedAt">{{ t('usage.lastUpdated', { time: updatedLabel }) }}</span>
            <UiButton
              variant="ghost"
              size="xs"
              icon-only
              :label="t('common.refresh')"
              :loading="pending || sandboxPending"
              @click="reload"
            >
              <template #icon><UiIcon name="refresh" /></template>
            </UiButton>
          </div>
        </template>
      </SettingsGroup>

      <SettingsGroup :title="t('usage.rates')" plain>
        <p class="rate-line">
          {{ t('usage.rateLine', { n: formatCount(billing.rates.parse_per_page, locale) }, billing.rates.parse_per_page) }}
        </p>
        <UiDataTable
          v-if="rateRows.length"
          :columns="rateColumns"
          :rows="rateRows"
          :row-key="(row) => row.model"
          :caption="t('usage.ratesCaption')"
          density="tight"
        >
          <template #cell-model="{ row }"><span class="mono">{{ row.model }}</span></template>
          <template #cell-input="{ row }">{{ rate(row.input) }}</template>
          <template #cell-cache_read="{ row }">{{ rate(row.cache_read) }}</template>
          <template #cell-cache_write="{ row }">{{ rate(row.cache_write) }}</template>
          <template #cell-output="{ row }">{{ rate(row.output) }}</template>
        </UiDataTable>
        <p v-else class="rate-line">{{ t('usage.noRates') }}</p>
      </SettingsGroup>

      <SettingsGroup :title="t('usage.sandbox')">
        <UiSkeleton v-if="!sandbox && !sandboxError" :rows="1" />
        <div v-else-if="sandbox && sandboxAvailable" class="sandbox">
          <div class="sandbox-text">
            <UiStatusDot :tone="stateTone(sandbox.state)" :label="t(`usage.sandboxStates.${sandbox.state}`)" />
            <p class="limit-line">
              {{
                sandbox.last_used_at
                  ? t('usage.lastUsed', { time: formatShortDateTime(sandbox.last_used_at, locale) })
                  : t('usage.neverUsed')
              }}
            </p>
          </div>
          <div class="sandbox-actions">
            <UiButton
              v-if="sandbox.state !== 'stopped'"
              variant="secondary"
              size="sm"
              :disabled="sandboxActionPending"
              :loading="sandboxActionPending && !confirmingReset"
              @click="runSandboxAction(stopSandbox)"
            >
              {{ t('usage.stop') }}
            </UiButton>
            <UiButton variant="secondary" size="sm" :disabled="sandboxActionPending" @click="confirmingReset = true">
              {{ t('usage.reset') }}
            </UiButton>
          </div>
        </div>
        <!-- The workspace above is the reader's either way (docs/billing.md § Plans). -->
        <p v-else-if="sandbox" class="sandbox-note">
          {{ sandbox.granted === false ? t('usage.notInPlan') : t('usage.unavailable') }}
        </p>
        <template v-if="sandboxError || (sandboxActionError && !confirmingReset)" #foot>
          <UiBanner v-if="sandboxError" tone="error">{{ sandboxError }}</UiBanner>
          <UiBanner v-if="sandboxActionError && !confirmingReset" tone="error">{{ sandboxActionError }}</UiBanner>
        </template>
      </SettingsGroup>
    </template>

    <UiConfirmDialog
      v-if="confirmingReset"
      :title="t('usage.resetTitle')"
      :message="t('usage.resetConfirm')"
      :confirm-label="t('usage.reset')"
      :pending="sandboxActionPending"
      :error="sandboxActionError"
      @confirm="runSandboxAction(resetSandbox)"
      @cancel="confirmingReset = false"
    />
  </SettingsSection>
</template>

<style scoped>
.state {
  margin-top: var(--space-6);
}

/* --- The plan ------------------------------------------------------------------ */

.plan {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  min-width: 0;
  min-height: 64px;
  padding: var(--space-3) 0;
}

.plan-text {
  display: grid;
  flex: 1;
  gap: 2px;
  min-width: 0;
}

.plan-name {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.plan-gives {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

/* --- The usage limits ----------------------------------------------------------- */

.limit {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(120px, 1.3fr) 76px;
  align-items: center;
  gap: var(--space-2) var(--space-6);
  min-height: 60px;
  padding: var(--space-3) 0;
}

.limit + .limit {
  border-top: 1px solid var(--line);
}

.limit-text,
.sandbox-text {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.limit-name {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
}

.limit-line {
  color: var(--muted);
  font-size: var(--text-xs);
}

.limit-used {
  color: var(--muted);
  font-size: var(--text-xs);
  text-align: right;
  white-space: nowrap;
}

.updated {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}

/* --- The rates ------------------------------------------------------------------ */

.rate-line {
  padding: 0 var(--space-1) var(--space-2);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

/* --- The sandbox ---------------------------------------------------------------- */

.sandbox {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  min-height: 60px;
  padding: var(--space-3) 0;
}

.sandbox-actions {
  display: flex;
  gap: var(--space-2);
  flex-shrink: 0;
}

.sandbox-note {
  display: flex;
  align-items: center;
  min-height: 52px;
  padding: var(--space-3) 0;
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.5;
}

@media (max-width: 640px) {
  /* The bar takes the row's width under its name, the share beside it. */
  .limit {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .limit-text {
    grid-column: 1 / -1;
  }

  .plan,
  .sandbox {
    flex-wrap: wrap;
  }
}
</style>
