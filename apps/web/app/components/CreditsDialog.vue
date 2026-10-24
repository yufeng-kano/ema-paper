<script setup lang="ts">
/**
 * The 402 of a parse or a send (docs/billing.md § Refusals, docs/web.md § Files): what the
 * action needs, what is left and when the period resets, with the way to the pricing
 * page, where *Upgrade* is. Nothing was queued and nothing was charged.
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

/** An overdrawn period reads 0, never a negative balance (docs/billing.md § A chat turn);
 *  what it is short is counted from the real one, which the overdraft is still owed from. */
const remaining = computed(() => Math.max(0, props.refusal.remaining))
const short = computed(() => Math.max(0, props.refusal.need - props.refusal.remaining))

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
    </p>
    <template #footer>
      <UiButton variant="ghost" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton @click="seePlans">{{ t('credits.seePlans') }}</UiButton>
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
</style>
