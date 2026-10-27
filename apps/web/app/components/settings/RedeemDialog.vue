<script setup lang="ts">
/**
 * A redeem code, typed and redeemed (docs/web.md § Pages, `/settings/usage`; the contract
 * is docs/billing.md § Redeem codes): one field, one line on what a code gives, *Redeem*.
 * A refusal is worded under the field from the error's type, in the reader's language; a
 * success turns the dialog into a receipt — the credits added and their expiry, or the days
 * of a plan and from when they run — and *Done*, after which the caller re-reads.
 */
import { redeemCode, type RedeemResult } from '~/lib/api'
import { formatCount, formatDate } from '~/lib/format'
import { presentCode, redeemErrorKey } from '~/lib/redeem'

const emit = defineEmits<{ close: []; redeemed: [RedeemResult] }>()

const { t, locale } = useI18n()
const { resolve } = useApiError()

const code = ref('')
const pending = ref(false)
const error = ref<string | null>(null)
const result = ref<RedeemResult | null>(null)

async function submit() {
  if (pending.value || !code.value.trim()) {
    return
  }
  pending.value = true
  error.value = null
  try {
    result.value = await redeemCode(code.value)
    emit('redeemed', result.value)
  } catch (failure) {
    const key = redeemErrorKey(failure)
    error.value = key ? t(key) : await resolve(failure)
  } finally {
    pending.value = false
  }
}

/** The receipt's headline: what was added. */
const headline = computed(() => {
  const granted = result.value
  if (!granted) {
    return ''
  }
  if (granted.kind === 'credits') {
    return t('redeem.credits', { n: formatCount(granted.credits, locale.value) }, granted.credits)
  }
  return t('redeem.days', { n: formatCount(granted.days, locale.value), plan: granted.plan_id }, granted.days)
})

/** The line under it: until when, or that the days wait. */
const note = computed(() => {
  const granted = result.value
  if (!granted) {
    return ''
  }
  if (granted.kind === 'credits') {
    return t('redeem.creditsNote', { date: formatDate(granted.expires_at, locale.value) })
  }
  const banked = formatCount(granted.days_banked, locale.value)
  if (granted.period_end === null) {
    return t('redeem.planWait', { n: banked }, granted.days_banked)
  }
  const now = t('redeem.planNow', { date: formatDate(granted.period_end, locale.value) })
  return granted.days_banked > 0 ? `${now} ${t('redeem.planMore', { n: banked }, granted.days_banked)}` : now
})
</script>

<template>
  <UiModal :title="result ? t('redeem.successTitle') : t('redeem.title')" size="sm" :busy="pending" @close="emit('close')">
    <div v-if="result" class="receipt">
      <span class="mark" aria-hidden="true"><UiIcon name="check" /></span>
      <p class="headline tabular">{{ headline }}</p>
      <p class="note" role="status">{{ note }}</p>
    </div>
    <form v-else id="redeem-form" class="form" @submit.prevent="submit">
      <UiField v-slot="{ id, describedBy, invalid }" :label="t('redeem.label')" :error="error ?? undefined">
        <UiTextInput
          :id="id"
          :model-value="code"
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending"
          :maxlength="64"
          mono
          required
          @update:model-value="code = presentCode($event)"
        />
      </UiField>
      <p class="help">{{ t('redeem.help') }}</p>
    </form>
    <template #footer>
      <template v-if="result">
        <UiButton variant="primary" @click="emit('close')">{{ t('redeem.done') }}</UiButton>
      </template>
      <template v-else>
        <UiButton variant="ghost" :disabled="pending" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
        <UiButton variant="primary" type="submit" form="redeem-form" :loading="pending" :disabled="!code.trim()">
          {{ t('redeem.submit') }}
        </UiButton>
      </template>
    </template>
  </UiModal>
</template>

<style scoped>
.form {
  display: grid;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.help {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}

/* The receipt: a check in a soft disc, the figure large, one line under it. */
.receipt {
  display: grid;
  justify-items: center;
  gap: var(--space-3);
  padding: var(--space-3) 0 var(--space-1);
  text-align: center;
}

.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: var(--radius-full);
  background: color-mix(in srgb, var(--success) 14%, transparent);
  color: var(--success);
}

.headline {
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  line-height: 1.1;
}

.note {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}
</style>
