<script setup lang="ts">
/**
 * *Create codes* (docs/admin.md § Pages, *Redeem codes*): the fields of `python -m
 * sightread.codes create`, checked as they are typed against the rules the API shares with
 * the CLI, and what the batch gives away at most before it is made. The answer — the codes
 * in plaintext — goes to the caller, which shows it once; a refusal stays in the dialog,
 * which cannot be dismissed — nor left by Back, nor reloaded onto a new build — while its
 * request runs.
 */
import { createAdminCodes, type AdminCodesCreated, type AdminCodesList } from '~/lib/api'
import { formatRedeemBy, planLabel } from '~/lib/admin'
import { formatCount } from '~/lib/format'

/** The largest number a code's fields hold: the database's integer. */
const INT_MAX = 2 ** 31 - 1
/** The longest label: the column's 255 characters. */
const LABEL_MAX = 255

const props = defineProps<{ options: AdminCodesList['create'] }>()

const emit = defineEmits<{ close: []; created: [created: AdminCodesCreated] }>()

const { t, locale } = useI18n()
const router = useRouter()
const { explain } = useAdminFailure()

const kind = ref<'credits' | 'plan'>('credits')
const credits = ref('')
const validDays = ref(String(props.options.valid_days))
const planId = ref(props.options.plans[0] ?? '')
const days = ref('')
const maxUses = ref('1')
const count = ref('1')
const redeemBy = ref('')
const label = ref('')
const pending = ref(false)
const error = ref<string | null>(null)

// A new build's reload would drop the answer — the codes in plaintext — on its way here.
useBuildHold(pending)

/** Today, UTC, as the date field writes it: the first day a new code can count to. */
const today = new Date().toISOString().slice(0, 10)
/** A date before it: the picker starts at today, but typing is not held to that, and the
 *  server refuses a code born expired. Dates as `YYYY-MM-DD` compare as strings. */
const pastDate = computed(() => !!redeemBy.value && redeemBy.value < today)

/** A field's whole number from 1 to `max`, else null. */
function wholeNumber(value: string, max: number): number | null {
  const n = /^\d+$/.test(value.trim()) ? Number(value.trim()) : Number.NaN
  return n >= 1 && n <= max ? n : null
}

const creditsN = computed(() => wholeNumber(credits.value, INT_MAX))
const validDaysN = computed(() => wholeNumber(validDays.value, props.options.max_days))
const daysN = computed(() => wholeNumber(days.value, props.options.max_days))
const maxUsesN = computed(() => wholeNumber(maxUses.value, INT_MAX))
const countN = computed(() => wholeNumber(count.value, props.options.max_count))

/** Said under a field once something is typed in it that is not a number it takes. */
function numberError(value: string, n: number | null, max: number): string | undefined {
  return value.trim() && n === null ? t('admin.codes.wholeNumber', { max: formatCount(max, locale.value) }) : undefined
}

const ready = computed(
  () =>
    countN.value !== null &&
    maxUsesN.value !== null &&
    !pastDate.value &&
    (kind.value === 'credits' ? creditsN.value !== null && validDaysN.value !== null : !!planId.value && daysN.value !== null),
)

/** The most the batch gives away, every code redeemed by as many accounts as it takes. */
const batch = computed(() => {
  if (!ready.value) {
    return null
  }
  const n = countN.value!
  const uses = maxUsesN.value!
  const codesText = t('admin.codes.batchCodes', { n: formatCount(n, locale.value) }, n)
  const usesText = t('admin.codes.batchUses', { n: formatCount(uses, locale.value) }, uses)
  if (kind.value === 'credits') {
    const each = creditsN.value!
    const total = n * uses * each
    const usd = new Intl.NumberFormat(locale.value, { style: 'currency', currency: 'USD' }).format(
      total * Number(props.options.credit_usd),
    )
    return t('admin.codes.batchCredits', {
      count: codesText,
      uses: usesText,
      credits: t('admin.codes.batchCreditsN', { n: formatCount(each, locale.value) }, each),
      total: t('admin.codes.batchCreditsN', { n: formatCount(total, locale.value) }, total),
      usd,
    })
  }
  const each = daysN.value!
  const total = n * uses * each
  return t('admin.codes.batchPlan', {
    count: codesText,
    uses: usesText,
    grant: t('admin.codes.grantPlan', { n: formatCount(each, locale.value), plan: planLabel(planId.value) }, each),
    total: t('admin.codes.batchDays', { n: formatCount(total, locale.value) }, total),
  })
})

async function submit() {
  if (pending.value || !ready.value) {
    return
  }
  pending.value = true
  error.value = null
  const fields = {
    count: countN.value!,
    max_uses: maxUsesN.value!,
    redeem_by: redeemBy.value || undefined,
    label: label.value.trim(),
  }
  // Back would close the dialog while the request runs, and the codes would land unseen.
  const release = router.beforeEach(() => false)
  let created: AdminCodesCreated | null = null
  let failure: unknown = null
  try {
    created = await createAdminCodes(
      kind.value === 'credits'
        ? { kind: 'credits', credits: creditsN.value!, valid_days: validDaysN.value!, ...fields }
        : { kind: 'plan', plan_id: planId.value, days: daysN.value!, ...fields },
    )
  } catch (caught) {
    failure = caught
  } finally {
    release()
  }
  if (created) {
    pending.value = false
    emit('created', created)
    return
  }
  error.value = await explain(failure)
  pending.value = false
}
</script>

<template>
  <UiModal :title="t('admin.codes.createTitle')" size="md" :busy="pending" @close="emit('close')">
    <template #title>
      <div class="heading">
        <h2 class="heading-title">{{ t('admin.codes.createTitle') }}</h2>
        <p class="heading-sub">{{ t('admin.codes.createSubtitle') }}</p>
      </div>
    </template>

    <form id="admin-codes-form" class="form" novalidate @submit.prevent="submit">
      <div v-if="options.plans.length" class="wide kind">
        <span id="admin-codes-kind" class="group-label">{{ t('admin.codes.kindField') }}</span>
        <UiSegmented role="radiogroup" aria-labelledby="admin-codes-kind">
          <button
            type="button"
            role="radio"
            class="segment"
            :class="{ active: kind === 'credits' }"
            :aria-checked="kind === 'credits'"
            :disabled="pending"
            @click="kind = 'credits'"
          >
            {{ t('admin.codes.kinds.credits') }}
          </button>
          <button
            type="button"
            role="radio"
            class="segment"
            :class="{ active: kind === 'plan' }"
            :aria-checked="kind === 'plan'"
            :disabled="pending"
            @click="kind = 'plan'"
          >
            {{ t('admin.codes.kinds.plan') }}
          </button>
        </UiSegmented>
      </div>

      <template v-if="kind === 'credits'">
        <UiField v-slot="{ id, describedBy, invalid }" :label="t('admin.codes.credits')" :error="numberError(credits, creditsN, INT_MAX)">
          <UiTextInput
            :id="id"
            v-model="credits"
            inputmode="numeric"
            :described-by="describedBy"
            :invalid="invalid"
            :disabled="pending"
            required
          />
        </UiField>
        <UiField
          v-slot="{ id, describedBy, invalid }"
          :label="t('admin.codes.validDays')"
          :error="numberError(validDays, validDaysN, options.max_days)"
        >
          <UiTextInput
            :id="id"
            v-model="validDays"
            inputmode="numeric"
            :described-by="describedBy"
            :invalid="invalid"
            :disabled="pending"
            required
          />
        </UiField>
      </template>
      <template v-else>
        <UiField v-slot="{ id, describedBy }" :label="t('admin.codes.plan')">
          <UiSelect :id="id" v-model="planId" :described-by="describedBy" :disabled="pending">
            <option v-for="plan in options.plans" :key="plan" :value="plan">{{ planLabel(plan) }}</option>
          </UiSelect>
        </UiField>
        <UiField v-slot="{ id, describedBy, invalid }" :label="t('admin.codes.days')" :error="numberError(days, daysN, options.max_days)">
          <UiTextInput
            :id="id"
            v-model="days"
            inputmode="numeric"
            :described-by="describedBy"
            :invalid="invalid"
            :disabled="pending"
            required
          />
        </UiField>
      </template>

      <UiField v-slot="{ id, describedBy, invalid }" :label="t('admin.codes.maxUses')" :error="numberError(maxUses, maxUsesN, INT_MAX)">
        <UiTextInput
          :id="id"
          v-model="maxUses"
          inputmode="numeric"
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending"
          required
        />
      </UiField>
      <UiField
        v-slot="{ id, describedBy, invalid }"
        :label="t('admin.codes.count', { max: formatCount(options.max_count, locale) })"
        :error="numberError(count, countN, options.max_count)"
      >
        <UiTextInput
          :id="id"
          v-model="count"
          inputmode="numeric"
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending"
          required
        />
      </UiField>

      <UiField
        v-slot="{ id, describedBy, invalid }"
        :label="t('admin.codes.redeemByField')"
        :error="pastDate ? t('admin.codes.pastDate') : undefined"
      >
        <UiTextInput
          :id="id"
          v-model="redeemBy"
          type="date"
          :min="today"
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending"
        />
      </UiField>
      <UiField v-slot="{ id, describedBy }" :label="t('admin.codes.labelField')">
        <UiTextInput :id="id" v-model="label" :maxlength="LABEL_MAX" :described-by="describedBy" :disabled="pending" />
      </UiField>

      <p class="wide help">{{ t('admin.codes.help') }}</p>

      <section class="wide batch" :aria-label="t('admin.codes.batch')">
        <h3 class="batch-title">{{ t('admin.codes.batch') }}</h3>
        <p v-if="batch" class="tabular">{{ batch }}</p>
        <p>
          {{
            redeemBy ? t('admin.codes.batchUntil', { date: formatRedeemBy(redeemBy, locale) }) : t('admin.codes.batchOpen')
          }}
        </p>
        <p v-if="kind === 'credits' && validDaysN !== null">
          {{ t('admin.codes.batchValid', { n: formatCount(validDaysN, locale) }, validDaysN) }}
        </p>
        <p>{{ t('admin.codes.batchOnce') }}</p>
      </section>

      <UiBanner v-if="error" class="wide" tone="error">{{ error }}</UiBanner>
    </form>

    <template #footer>
      <UiButton variant="ghost" :disabled="pending" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton variant="primary" type="submit" form="admin-codes-form" :loading="pending" :disabled="!ready">
        {{ t('admin.codes.submit', { n: formatCount(countN ?? 1, locale) }, countN ?? 1) }}
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

.heading-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-heading);
}

.heading-sub {
  color: var(--muted);
  font-size: var(--text-xs);
}

/* Two fields a row, the pairs the CLI's flags make; one on a phone. */
.form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.wide {
  grid-column: 1 / -1;
}

.kind {
  display: grid;
  justify-items: start;
  gap: var(--space-2);
}

/* As `UiField` draws its label: a radio group has no control a `<label>` can point at. */
.group-label {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--ink-soft);
}

.help {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.batch {
  display: grid;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  font-size: var(--text-xs);
  line-height: 1.6;
}

.batch-title {
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

@media (max-width: 480px) {
  .form {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
