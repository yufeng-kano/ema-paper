<script setup lang="ts">
/**
 * One account on the console's *Users* screen (docs/admin.md § Pages, *Users*): who it is,
 * its plan, its day bank, its credits by source, its storage, its newest jobs — the file's
 * name, the pages and the status, never a word of the content — and, with `groups.read`, the
 * groups it is in. The buttons say what the operator may do; the
 * screen opens the dialogs.
 */
import type { AdminUserDetail } from '~/lib/api'
import { formatBytes, formatCount, formatDate, formatShortDateTime } from '~/lib/format'

const props = defineProps<{
  detail: AdminUserDetail
  /** `users.write`. */
  canGrant: boolean
  /** A paid plan is configured: there are days to give. */
  canGrantDays: boolean
  /** `groups.write`, and a group the operator may add the account to. */
  canAddToGroup: boolean
}>()

const emit = defineEmits<{ grant: [kind: 'credits' | 'days']; addToGroup: [] }>()

const { t, te, locale } = useI18n()

const user = computed(() => props.detail.user)
const displayName = computed(() => user.value.name?.trim() || user.value.email)
const initial = computed(() => displayName.value.charAt(0).toUpperCase())

const planLine = computed(() => {
  const plan = props.detail.plan
  return plan.source === 'subscription' || plan.source === 'grant'
    ? `${plan.id} · ${t(`admin.users.paidBy.${plan.source}`)}`
    : t('admin.users.free')
})

/** A Pay subscription's state, said only when it is not simply paying. */
const subscriptionLine = computed(() => {
  const subscription = props.detail.subscription
  if (!subscription) {
    return null
  }
  if (subscription.status !== 'active') {
    return t('admin.users.subscriptionState', { status: subscription.status })
  }
  return subscription.cancel_at_period_end ? t('admin.users.cancelsAtEnd') : null
})

const banked = computed(() => props.detail.day_bank?.days_banked ?? 0)

const storageShare = computed(() => {
  const storage = props.detail.storage
  return storage && storage.quota_bytes > 0 ? storage.bytes / storage.quota_bytes : 0
})

function jobTone(status: string): 'ok' | 'danger' | 'info' | 'neutral' {
  if (status === 'succeeded') return 'ok'
  if (status === 'failed') return 'danger'
  return status === 'running' ? 'info' : 'neutral'
}

function jobStatus(status: string): string {
  return te(`admin.users.jobStatus.${status}`) ? t(`admin.users.jobStatus.${status}`) : status
}

function jobLine(job: AdminUserDetail['jobs']['recent'][number]): string {
  const time = formatShortDateTime(job.finished_at ?? job.created_at, locale.value)
  return job.page_count === null
    ? time
    : t('admin.users.jobPages', { n: formatCount(job.page_count, locale.value), time }, job.page_count)
}
</script>

<template>
  <div class="account">
    <header class="head">
      <span class="disc" aria-hidden="true">{{ initial }}</span>
      <div class="who">
        <h2 class="name">{{ displayName }}</h2>
        <p class="line">{{ user.email }} · {{ t('admin.accountId', { id: user.id }) }}</p>
      </div>
    </header>

    <div v-if="canGrant || canAddToGroup" class="actions">
      <template v-if="canGrant">
        <UiButton variant="primary" size="sm" @click="emit('grant', 'credits')">{{ t('admin.users.addCredits') }}</UiButton>
        <UiButton v-if="canGrantDays" size="sm" @click="emit('grant', 'days')">{{ t('admin.users.addDays') }}</UiButton>
      </template>
      <UiButton v-if="canAddToGroup" size="sm" @click="emit('addToGroup')">{{ t('admin.users.addToGroup') }}</UiButton>
    </div>

    <section class="block">
      <h3 class="block-title">{{ t('admin.users.planTitle') }}</h3>
      <p class="value">{{ planLine }}</p>
      <p class="muted">{{ t('admin.users.periodEnds', { date: formatDate(detail.plan.end, locale) }) }}</p>
      <p v-if="subscriptionLine" class="muted">{{ subscriptionLine }}</p>
    </section>

    <section class="block">
      <h3 class="block-title">{{ t('admin.users.dayBankTitle') }}</h3>
      <p class="value">
        {{ t('admin.users.daysWaiting', { n: formatCount(banked, locale), plan: detail.day_bank?.plan_id ?? '' }, banked) }}
      </p>
      <p v-if="banked > 0 && detail.plan.source === 'subscription'" class="muted">
        {{ t('admin.users.daysWaitForSubscription') }}
      </p>
    </section>

    <section class="block">
      <h3 class="block-title">{{ t('admin.users.creditsTitle') }}</h3>
      <dl class="sources">
        <div v-for="entry in detail.credits.sources" :key="entry.source" class="source">
          <dt>
            <span>{{ t(`admin.users.sources.${entry.source}`) }}</span>
            <span v-if="entry.expires_at" class="muted expiry">
              {{ t('admin.users.expires', { date: formatDate(entry.expires_at, locale) }) }}
            </span>
          </dt>
          <dd class="tabular">{{ formatCount(entry.remaining, locale) }}</dd>
        </div>
        <div class="source total">
          <dt>{{ t('admin.users.total') }}</dt>
          <dd class="tabular">{{ formatCount(detail.credits.available, locale) }}</dd>
        </div>
      </dl>
    </section>

    <section class="block">
      <h3 class="block-title">{{ t('admin.users.storageTitle') }}</h3>
      <template v-if="detail.storage">
        <p class="value tabular">
          {{
            t('admin.users.storageUsed', {
              used: formatBytes(detail.storage.bytes, locale),
              quota: formatBytes(detail.storage.quota_bytes, locale),
            })
          }}
        </p>
        <UiUsageBar :share="storageShare" :label="t('admin.users.storageMeter')" />
      </template>
      <p v-else class="muted">{{ t('admin.users.unknown') }}</p>
    </section>

    <section class="block">
      <h3 class="block-title">
        {{ t('admin.users.recentJobs') }}
        <span class="muted count tabular">{{ formatCount(detail.jobs.total, locale) }}</span>
      </h3>
      <ul v-if="detail.jobs.recent.length" class="jobs">
        <li v-for="job in detail.jobs.recent" :key="job.id" class="job">
          <UiStatusDot :tone="jobTone(job.status)" :label="jobStatus(job.status)" />
          <span class="file" :title="job.filename">{{ job.filename }}</span>
          <span class="muted tabular">{{ jobLine(job) }}</span>
        </li>
      </ul>
      <p v-else class="muted">{{ t('admin.users.noJobs') }}</p>
      <p class="muted note">{{ t('admin.users.contentNote') }}</p>
    </section>

    <section v-if="detail.groups !== null" class="block">
      <h3 class="block-title">{{ t('admin.users.groupsTitle') }}</h3>
      <ul v-if="detail.groups.length" class="groups">
        <li v-for="group in detail.groups" :key="group.id">
          <span class="mono">{{ group.name }}</span>
          <span v-if="group.builtin" class="muted">{{ t('admin.users.builtin') }}</span>
        </li>
      </ul>
      <p v-else class="muted">{{ t('admin.users.noGroups') }}</p>
    </section>
  </div>
</template>

<style scoped>
.account {
  display: grid;
  gap: var(--space-5);
  min-width: 0;
}

.head {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
}

/* The account's initial on a dark disc. */
.disc {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  border-radius: var(--radius-full);
  background: var(--ink);
  color: var(--paper);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.who {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.name {
  overflow-wrap: anywhere;
  font-size: var(--text-lg);
  letter-spacing: var(--tracking-heading);
}

.line {
  color: var(--muted);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.block {
  display: grid;
  gap: var(--space-1);
  min-width: 0;
}

.block-title {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  margin-bottom: var(--space-1);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  letter-spacing: normal;
}

.value {
  font-size: var(--text-sm);
}

.muted {
  color: var(--muted);
  font-size: var(--text-xs);
}

.count {
  font-weight: var(--weight-normal);
}

.sources {
  display: grid;
  gap: var(--space-1);
  margin: 0;
}

.source {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
  font-size: var(--text-sm);
}

.source dt {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0 var(--space-2);
  min-width: 0;
}

.source dd {
  margin: 0;
}

.source.total {
  padding-top: var(--space-1);
  border-top: 1px solid var(--line);
  font-weight: var(--weight-semibold);
}

.jobs,
.groups {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.job {
  display: grid;
  gap: 2px;
  min-width: 0;
  font-size: var(--text-sm);
}

/* One line, whole in its tooltip: a name with no break in it would otherwise widen every
   grid track up to the panel and run past it. */
.file {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: var(--weight-medium);
}

.note {
  margin-top: var(--space-1);
  line-height: 1.6;
}

.groups li {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  font-size: var(--text-sm);
}
</style>
