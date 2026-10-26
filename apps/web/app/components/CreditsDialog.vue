<script setup lang="ts">
/**
 * The 402 of a parse or a send (docs/billing.md § Refusals, docs/web.md § Files): what the
 * action needs, what is left and when the period resets, with the way to the pricing page,
 * where *Upgrade* is — or, for a reader who can buy a top-up pack (`pack_checkout_available`,
 * `GET /api/me`), the pack itself, read from the catalog when the dialog opens: *Top up N
 * credits, price* starts its checkout in this tab; *See plans* stands in while the catalog
 * has not answered or sells none (docs/billing.md § Top-up packs). Nothing was queued and
 * nothing was charged.
 */
import { formatCount, formatDate } from '~/lib/format'
import type { CreditsRefusal } from '~/lib/api'

const props = defineProps<{
  refusal: CreditsRefusal
  /** Which action was refused: the dialog's title and its first line. */
  action: 'parse' | 'chat'
}>()

const emit = defineEmits<{ close: [] }>()

const { t, locale } = useI18n()
const localePath = useLocalePath()
const auth = useAuth()

/** An overdrawn period reads 0, never a negative balance (docs/billing.md § A chat turn);
 *  what it is short is counted from the real one, which the overdraft is still owed from. */
const remaining = computed(() => Math.max(0, props.refusal.remaining))
const short = computed(() => Math.max(0, props.refusal.need - props.refusal.remaining))

/** A reader on a paid plan is offered the pack; a Free reader keeps *See plans*. */
const canTopUp = computed(() => auth.me.value?.pack_checkout_available === true)
const topUp = reactive(useTopUp())
onMounted(() => {
  if (canTopUp.value) {
    void topUp.load()
  }
})

/** The pricing page, where the plans are presented and the checkout starts. */
function seePlans() {
  emit('close')
  void navigateTo(localePath('/pricing'))
}
</script>

<template>
  <UiModal :title="t('credits.title')" size="sm" @close="emit('close')">
    <p class="lede">{{ t(`credits.${action}`) }}</p>
    <dl class="figures tabular">
      <div class="figure">
        <dt>{{ t('credits.needs') }}</dt>
        <dd>{{ t('credits.n', { n: formatCount(refusal.need, locale) }, refusal.need) }}</dd>
      </div>
      <div class="figure">
        <dt>{{ t('credits.left') }}</dt>
        <dd class="short">{{ t('credits.n', { n: formatCount(remaining, locale) }, remaining) }}</dd>
      </div>
    </dl>
    <p class="note" role="status">
      {{ t('credits.shortBy', { n: formatCount(short, locale), date: formatDate(refusal.period_end, locale) }, short) }}
      <template v-if="canTopUp && topUp.pack">{{ t('credits.packNote', { validity: topUp.validity }) }}</template>
    </p>
    <UiBanner v-if="topUp.error" class="error" tone="error">{{ topUp.error }}</UiBanner>
    <template #footer>
      <UiButton variant="ghost" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton v-if="canTopUp && topUp.pack" :loading="topUp.buying" @click="topUp.buy">
        {{ t('credits.topUp', { n: topUp.credits, price: topUp.price }) }}
      </UiButton>
      <UiButton v-else @click="seePlans">{{ t('credits.seePlans') }}</UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.lede {
  color: var(--muted);
  line-height: 1.7;
}

/* Two facts, ruled above and below, the figures at the right. */
.figures {
  display: grid;
  gap: var(--space-2);
  margin: var(--space-4) 0;
  padding: var(--space-3) 0;
  border-top: 1px solid var(--hair);
  border-bottom: 1px solid var(--hair);
}

.figure {
  display: flex;
  justify-content: space-between;
  gap: var(--space-4);
}

.figure dt {
  color: var(--muted);
}

.figure dd {
  margin: 0;
  font-weight: var(--weight-medium);
}

/* The balance that fell short, in the danger hue: a cost figure that is not enough. */
.short {
  color: var(--danger);
}

.note {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.error {
  margin-top: var(--space-3);
}
</style>
