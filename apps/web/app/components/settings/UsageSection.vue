<script setup lang="ts">
/**
 * Usage (docs/web.md § Pages, `/settings/usage`; the contract is docs/billing.md), laid out
 * as Claude's own usage page: the plan and its action — *Upgrade* (a checkout) or *Manage
 * subscription* (Pay's portal) — then the usage limits, the period's credits and the
 * workspace's storage, each a bar with *N% used*, the top-up credits on a paid plan (the
 * packs' balance, the newest packs with every one in a dialog, and *Top up*, which opens
 * the pack's dialog before Pay's checkout — docs/billing.md § Top-up packs), one line on
 * the rates with the way to the rates page (no table here, since 2026-10-26), and the
 * sandbox the plan gives. Redeemed days of a plan are said under the plan, and the plan's foot
 * ends in the way to the redeem dialog, Free or paid (docs/billing.md § Redeem codes). Every
 * figure is the API's: the section never names a price or a plan the API did not.
 */
import {
  createCheckout,
  createPortal,
  getBilling,
  getBillingCatalog,
  getSandbox,
  resetSandbox,
  stopSandbox,
  type SandboxState,
} from '~/lib/api'
import { formatBytes, formatCount, formatDate, formatShortDateTime } from '~/lib/format'

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()
const localePath = useLocalePath()

const { data: billing, pending, errorMessage, refresh } = useAuthedData(() => getBilling())
const {
  data: sandbox,
  pending: sandboxPending,
  errorMessage: sandboxError,
  refresh: refreshSandbox,
} = useAuthedData(() => getSandbox())

// --- the plan ----------------------------------------------------------------------------

const free = computed(() => billing.value?.plan.id === 'free')

/** Whether a Pay subscription pays for the current period: the period says, not the row —
 *  a lapsed subscriber's row stands while their period is Free's or redeemed days'
 *  (docs/billing.md § Periods). */
const subscribed = computed(
  () => (billing.value?.period.source ?? (billing.value?.subscription ? 'subscription' : 'free')) === 'subscription',
)

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

/** Redeemed days under the plan (docs/billing.md § Periods, *Granted days*): the open granted
 *  period and how many days follow it, or how many wait while a paid subscription runs. */
const grantedLine = computed(() => {
  const grant = billing.value?.plan_grant
  if (!billing.value || !grant) {
    return null
  }
  const more = formatCount(grant.days_banked, locale.value)
  if (billing.value.period.source === 'grant' && grant.period_end) {
    const runs = t('usage.grantedRuns', { plan: grant.plan_id, date: formatDate(grant.period_end, locale.value) })
    return grant.days_banked > 0 ? `${runs} ${t('usage.grantedMore', { n: more }, grant.days_banked)}` : runs
  }
  return grant.days_banked > 0 ? t('usage.grantedBanked', { plan: grant.plan_id, n: more }, grant.days_banked) : null
})

const redeeming = ref(false)

// --- the top-up packs --------------------------------------------------------------------

/** A paid plan's period: the one a pack can be bought in (docs/billing.md § Top-up packs). */
const paid = computed(() => billing.value !== null && billing.value.plan.id !== 'free')

/** The extra credits group stands on a paid plan's period, packs or none, and for any reader
 *  holding one — a Free reader who redeemed credits (docs/billing.md § Redeem codes). */
const extras = computed(() => paid.value || (billing.value?.packs.items.length ?? 0) > 0)

/** The pack the catalog lists, read once the group appears — on sale or not, so the line
 *  under the heading says what a pack is while Pay's checkout is still closed. */
const topUp = reactive(useTopUp())
watch(
  paid,
  (isPaid) => {
    if (isPaid && !topUp.loaded) {
      void topUp.load()
    }
  },
  { immediate: true },
)

/** The line under the heading: the spending order, and the pack as the catalog describes it. */
const packsLine = computed(() => {
  const pack = topUp.listed ? t('usage.packsPack', { n: topUp.credits, validity: topUp.validity }) : ''
  return t('usage.packsLine', { pack }).trim()
})

/** The newest packs the group lists itself; the rest wait in the packs dialog, so the group
 *  never grows with the purchases (docs/web.md § Pages). */
const PACKS_SHOWN = 3
const recentPacks = computed(() => (billing.value?.packs.items ?? []).slice(0, PACKS_SHOWN))
const morePacks = computed(() => (billing.value?.packs.items.length ?? 0) > PACKS_SHOWN)

const showingAllPacks = ref(false)
const toppingUp = ref(false)

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

/** What the used storage is made of, a part holding nothing left out (docs/web.md § Pages). */
const storageParts = computed(() => {
  const workspace = sandbox.value?.workspace
  if (!workspace) {
    return []
  }
  const parts = [
    ['usage.storageFiles', workspace.files_bytes],
    ['usage.storageTrash', workspace.trash_bytes],
    ['usage.storageRevisions', workspace.history_bytes],
  ] as const
  return parts
    .filter(([, bytes]) => bytes > 0)
    .map(([key, bytes]) => t(key, { size: formatBytes(bytes, locale.value) }))
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
          <div class="plan-actions">
            <UiButton v-if="billing.subscription" variant="secondary" :loading="acting" @click="manage">
              {{ t('usage.manage') }}
            </UiButton>
            <!-- The same checkout: *Upgrade* on Free, *Subscribe* on redeemed days, whose rest
                 waits for the subscription to end (docs/billing.md § Periods, *Granted days*). -->
            <UiButton v-if="!subscribed && billing.checkout_available" :loading="acting" @click="upgrade">
              {{ free ? t('usage.upgrade') : t('usage.subscribe') }}
            </UiButton>
          </div>
        </div>
        <template #foot>
          <p v-if="subscriptionLine">{{ subscriptionLine }}</p>
          <p v-if="grantedLine">{{ grantedLine }}</p>
          <p v-if="billing.subscription">{{ t('usage.manageNote') }}</p>
          <p v-else-if="!billing.checkout_available">{{ t('usage.notOnSale') }}</p>
          <p v-if="billing.pay_reachable === false">{{ t('usage.payBehind') }}</p>
          <UiBanner v-if="actionError" tone="error">{{ actionError }}</UiBanner>
          <p>
            {{ t('usage.redeemPrompt') }}
            <button type="button" class="redeem-link" @click="redeeming = true">{{ t('usage.redeemLink') }}</button>
          </p>
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
            <!-- A line breaks between parts, never inside one. -->
            <p v-if="storageParts.length" class="limit-line">
              <template v-for="(part, index) in storageParts" :key="part">
                {{ index ? ' · ' : '' }}
                <span class="limit-part">{{ part }}</span>
              </template>
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

      <!-- The extra credits — packs bought and codes redeemed — on a paid plan or once there is
           one: what they hold together, the newest few (every one in a dialog), and the way to
           buy one — a dialog before Pay's checkout (docs/web.md § Pages). The table brings its
           own surface; the empty row takes the box. -->
      <SettingsGroup
        v-if="extras"
        :title="t('usage.packs')"
        :description="packsLine"
        :plain="billing.packs.items.length > 0"
      >
        <template #actions>
          <span class="packs-total tabular">
            {{ t('usage.packsTotal', { n: formatCount(billing.packs.remaining, locale) }, billing.packs.remaining) }}
          </span>
        </template>
        <SettingsRow
          v-if="!billing.packs.items.length"
          :label="t('usage.packsNone')"
          :description="t('usage.packsNoneNote')"
        />
        <template v-else>
          <SettingsPacksTable :rows="recentPacks" />
          <div v-if="morePacks" class="packs-more">
            <UiButton variant="ghost" size="sm" @click="showingAllPacks = true">
              {{ t('usage.packsAll', { n: formatCount(billing.packs.items.length, locale) }) }}
            </UiButton>
          </div>
        </template>
        <template #foot>
          <div class="packs-foot">
            <p>{{ t('usage.purchasesNote') }}</p>
            <UiButton v-if="billing.pack_checkout_available" variant="primary" @click="toppingUp = true">
              {{ t('usage.topUp') }}
            </UiButton>
            <p v-else-if="paid" class="packs-closed">{{ t('usage.packsNotOnSale') }}</p>
          </div>
        </template>
      </SettingsGroup>

      <!-- One line: the parse rate, and the rates page for the models' table. -->
      <SettingsGroup :title="t('usage.rates')" plain>
        <p class="rate-line">
          {{ t('usage.rateLine', { n: formatCount(billing.rates.parse_per_page, locale) }, billing.rates.parse_per_page) }}
          <NuxtLink class="rate-link" :to="localePath('/rates')">{{ t('usage.ratesLink') }}</NuxtLink>
        </p>
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

    <SettingsTopUpDialog v-if="toppingUp" @close="toppingUp = false" />
    <SettingsRedeemDialog v-if="redeeming" @close="redeeming = false" @redeemed="reload" />
    <SettingsPacksDialog v-if="showingAllPacks && billing" :packs="billing.packs" @close="showingAllPacks = false" />

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

.plan-actions {
  display: flex;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: var(--space-2);
}

/* The way to the redeem dialog, in the foot's own line. */
.redeem-link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent);
  font: inherit;
  cursor: pointer;
}

.redeem-link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
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

.limit-part {
  white-space: nowrap;
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

/* --- The top-up credits ----------------------------------------------------------- */

/* The balance the packs hold, at the heading's right. */
.packs-total {
  color: var(--ink);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  white-space: nowrap;
}

.packs-more {
  display: flex;
  justify-content: center;
  padding: var(--space-1) 0;
}

/* The note at the left, the way to buy at the right. */
.packs-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
}

.packs-closed {
  white-space: nowrap;
}

/* --- The rates ------------------------------------------------------------------ */

.rate-line {
  padding: 0 var(--space-1) var(--space-2);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.rate-link {
  color: var(--accent);
  white-space: nowrap;
}

.rate-link:hover {
  text-decoration: underline;
  text-underline-offset: 3px;
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
  .sandbox,
  .packs-foot {
    flex-wrap: wrap;
  }
}
</style>
