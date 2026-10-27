<script setup lang="ts">
/**
 * *Add credits* and *Add days* (docs/admin.md § Pages, *Users*; § Users and grants): one
 * dialog, a segmented *Credits · Days of a plan* to switch, the fields of the kind chosen, an
 * optional reason for the audit log, and one note on what follows and that a grant can't be
 * undone. The numbers are checked against the API's caps as they are typed.
 *
 * The dialog makes its `request_id` when it opens and sends it with every try, of credits or
 * of days, so a retry after a lost answer adds nothing twice. A refusal — a 4xx, after which
 * nothing was written — stays in the dialog, `plan_conflict` in the catalog's words, and the
 * fields can be changed. A failure that leaves the outcome unknown locks the kind, the
 * numbers and the reason to what was sent, for as long as the dialog is open: a retry is then
 * the same request, and the grant the API answers is the one shown. The dialog cannot be
 * dismissed, nor left by Back, while it sends.
 */
import { ApiRequestError, grantAdminUser, type AdminGrantFacts, type AdminUserDetail } from '~/lib/api'
import { adminFailure, grantNumber } from '~/lib/admin'
import { formatCount, formatDate } from '~/lib/format'

/** A grant's `reason` (docs/admin.md § Users and grants). */
const REASON_MAX = 200
const DAY_MS = 24 * 60 * 60 * 1000
const KINDS = ['credits', 'days'] as const

const props = defineProps<{
  user: AdminUserDetail['user']
  /** The account's credits available now. */
  available: number
  facts: AdminGrantFacts
  /** The paid plans: the days a grant can give. */
  plans: string[]
  kind: 'credits' | 'days'
}>()

const emit = defineEmits<{ close: []; granted: [] }>()

const { t, locale } = useI18n()
const router = useRouter()
const { explain } = useAdminFailure()

const requestId = crypto.randomUUID()

const kind = ref<'credits' | 'days'>(props.kind)
const credits = ref('')
const validDays = ref(String(props.facts.valid_days))
const plan = ref(props.plans[0] ?? '')
const days = ref('')
const reason = ref('')
const pending = ref(false)
/** A send whose answer never came: the grant may be made, so what was sent is kept. */
const unsettled = ref(false)
const error = ref<string | null>(null)

const creditCount = computed(() => grantNumber(credits.value, props.facts.max_credits))
const validCount = computed(() => grantNumber(validDays.value, props.facts.max_days))
const dayCount = computed(() => grantNumber(days.value, props.facts.max_days))

/** The rule, under a field whose text is not a number it takes. */
function ruleFor(text: string, value: number | null, max: number): string | undefined {
  return text.trim() && value === null ? t('admin.users.grant.numberRule', { max: formatCount(max, locale.value) }) : undefined
}

const ready = computed(() =>
  kind.value === 'credits' ? creditCount.value !== null && validCount.value !== null : dayCount.value !== null && !!plan.value,
)

const title = computed(() =>
  kind.value === 'credits' ? t('admin.users.grant.titleCredits') : t('admin.users.grant.titleDays'),
)

const subtitle = computed(() =>
  t(
    'admin.users.grant.subtitle',
    {
      name: props.user.name?.trim() || props.user.email,
      email: props.user.email,
      credits: formatCount(props.available, locale.value),
    },
    props.available,
  ),
)

/** What the credits will be: when they expire, and the model cost they stand for. */
const creditsNote = computed(() => {
  const count = creditCount.value ?? 0
  const expires = new Date(Date.now() + (validCount.value ?? props.facts.valid_days) * DAY_MS).toISOString()
  const usd = new Intl.NumberFormat(locale.value, {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(count * props.facts.credit_usd)
  return t(
    'admin.users.grant.creditsNote',
    { date: formatDate(expires, locale.value), n: formatCount(count, locale.value), usd },
    count,
  )
})

const submitLabel = computed(() => {
  if (kind.value === 'credits' && creditCount.value !== null) {
    return t('admin.users.grant.submitCredits', { n: formatCount(creditCount.value, locale.value) }, creditCount.value)
  }
  if (kind.value === 'days' && dayCount.value !== null) {
    return t('admin.users.grant.submitDays', { n: formatCount(dayCount.value, locale.value) }, dayCount.value)
  }
  return t('admin.users.grant.submit')
})

watch([kind, credits, validDays, plan, days], () => {
  error.value = null
})

async function submit() {
  if (pending.value || !ready.value) {
    return
  }
  pending.value = true
  error.value = null
  const note = reason.value.trim() || undefined
  // Back would close the dialog while the request runs, and its answer would land unseen.
  const release = router.beforeEach(() => false)
  let failure: unknown = null
  try {
    await grantAdminUser(
      props.user.id,
      kind.value === 'credits'
        ? { kind: 'credits', credits: creditCount.value!, valid_days: validCount.value!, request_id: requestId, reason: note }
        : { kind: 'days', plan: plan.value, days: dayCount.value!, request_id: requestId, reason: note },
    )
  } catch (caught) {
    failure = caught
  } finally {
    release()
  }
  if (!failure) {
    pending.value = false
    emit('granted')
    return
  }
  // A 4xx is a refusal and wrote nothing; anything else — no answer, a 5xx — may have
  // come after the commit.
  if (!(failure instanceof ApiRequestError && failure.status >= 400 && failure.status < 500)) {
    unsettled.value = true
  }
  const refusal = adminFailure(failure)
  if (refusal.kind === 'conflict' && refusal.reason === 'plan_conflict') {
    error.value = t('admin.users.grant.planConflict')
  } else {
    const explained = await explain(failure, t('admin.users.gone'))
    error.value = unsettled.value && explained ? `${explained} ${t('admin.users.grant.unsettled')}` : explained
  }
  pending.value = false
}
</script>

<template>
  <UiModal :title="title" size="md" :busy="pending" @close="emit('close')">
    <template #title>
      <div class="heading">
        <h2 class="title">{{ title }}</h2>
        <p class="subtitle">{{ subtitle }}</p>
      </div>
    </template>
    <form id="admin-grant-form" class="form" novalidate @submit.prevent="submit">
      <UiSegmented v-if="plans.length" role="radiogroup" :aria-label="t('admin.users.grant.kind')">
        <button
          v-for="option in KINDS"
          :key="option"
          type="button"
          role="radio"
          class="segment"
          :class="{ active: kind === option }"
          :aria-checked="kind === option"
          :disabled="pending || unsettled"
          @click="kind = option"
        >
          {{ t(`admin.users.grant.kinds.${option}`) }}
        </button>
      </UiSegmented>

      <div v-if="kind === 'credits'" class="pair">
        <UiField
          v-slot="{ id, describedBy, invalid }"
          :label="t('admin.users.grant.credits')"
          :error="ruleFor(credits, creditCount, facts.max_credits)"
        >
          <UiTextInput
            :id="id"
            v-model="credits"
            :described-by="describedBy"
            :invalid="invalid"
            :disabled="pending || unsettled"
            :maxlength="10"
            required
          />
        </UiField>
        <UiField
          v-slot="{ id, describedBy, invalid }"
          :label="t('admin.users.grant.validDays')"
          :error="ruleFor(validDays, validCount, facts.max_days)"
        >
          <UiTextInput
            :id="id"
            v-model="validDays"
            :described-by="describedBy"
            :invalid="invalid"
            :disabled="pending || unsettled"
            :maxlength="5"
            required
          />
        </UiField>
      </div>

      <div v-else class="pair">
        <UiField v-slot="{ id, describedBy }" :label="t('admin.users.grant.plan')">
          <UiSelect :id="id" v-model="plan" :described-by="describedBy" :disabled="pending || unsettled">
            <option v-for="option in plans" :key="option" :value="option">{{ option }}</option>
          </UiSelect>
        </UiField>
        <UiField
          v-slot="{ id, describedBy, invalid }"
          :label="t('admin.users.grant.days')"
          :error="ruleFor(days, dayCount, facts.max_days)"
        >
          <UiTextInput
            :id="id"
            v-model="days"
            :described-by="describedBy"
            :invalid="invalid"
            :disabled="pending || unsettled"
            :maxlength="5"
            required
          />
        </UiField>
      </div>

      <UiField v-slot="{ id, describedBy, invalid }" :label="t('admin.users.grant.reason')">
        <UiTextInput
          :id="id"
          v-model="reason"
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending || unsettled"
          :maxlength="REASON_MAX"
        />
      </UiField>
      <p class="help">{{ t('admin.users.grant.reasonNote') }}</p>

      <div class="note">
        <p>{{ kind === 'credits' ? creditsNote : t('admin.users.grant.daysNote') }}</p>
        <p class="final">{{ t('admin.users.grant.final') }}</p>
      </div>

      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>
    <template #footer>
      <UiButton variant="ghost" :disabled="pending" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton variant="primary" type="submit" form="admin-grant-form" :loading="pending" :disabled="!ready">
        {{ submitLabel }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.heading {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.title {
  font-size: var(--text-lg);
  letter-spacing: var(--tracking-heading);
}

.subtitle {
  color: var(--muted);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

.form {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
}

.help {
  margin-top: calc(-1 * var(--space-2));
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}

/* What follows, said once under the fields. */
.note {
  display: grid;
  gap: var(--space-1);
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: var(--paper-sunken);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.final {
  font-weight: var(--weight-semibold);
}

@media (max-width: 640px) {
  .pair {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
