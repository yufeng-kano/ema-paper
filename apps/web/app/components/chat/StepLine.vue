<script setup lang="ts">
/**
 * One step's line (docs/web.md § The agent panel): a glyph for its kind, a verb, its target
 * in mono and a detail, the lines an edit added, then its mark — a check, a cross, a
 * spinner while it runs. The reading of the call is `lib/steps.ts`; the words are the
 * catalog's. The row around it (a button, or a plain line in a subagent card) is the
 * caller's.
 */
import type { Step } from '~/lib/steps'
import type { CardState, SkillRunStep, StepStatus, ToolStep } from '~/lib/thread'

const props = defineProps<{
  step: ToolStep | SkillRunStep
  /** An edit the reader reverted says so in place of its check. */
  state?: CardState | null
}>()

const { t } = useI18n()
const labels = useStepLabels()

const view = computed<Step>(() =>
  props.step.type === 'tool' ? props.step.step : { kind: 'skill', target: props.step.name },
)
// The skill a command ran was loaded with the message: done before the answer began.
const status = computed<StepStatus>(() => (props.step.type === 'tool' ? props.step.status : 'ok'))
const detail = computed(() => labels.detail(view.value))
const added = computed(() => (props.step.type === 'tool' ? props.step.added : null))
</script>

<template>
  <UiIcon :name="labels.icon(view.kind)" class="glyph" />
  <span class="verb">{{ labels.verb(view) }}</span>
  <span v-if="view.target" class="target mono">{{ view.target }}</span>
  <span v-if="detail" class="detail">{{ detail }}</span>
  <span v-if="added" class="added mono tabular">+{{ added }}</span>
  <span class="spacer" />
  <span v-if="state?.reverted" class="reverted">{{ t('chat.reverted') }}</span>
  <template v-else>
    <UiSpinner v-if="status === 'pending'" class="mark running" :size="12" />
    <UiIcon v-else-if="status === 'ok'" name="check" class="mark ok" />
    <UiIcon v-else-if="status === 'error'" name="x" class="mark error" />
    <span v-if="status === 'ok' || status === 'error'" class="sr-only">
      {{ status === 'ok' ? t('chat.toolOk') : t('chat.toolFailed') }}
    </span>
  </template>
</template>

<style scoped>
.glyph {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--faint);
}

.verb {
  flex-shrink: 0;
}

.target {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink-soft);
}

.detail {
  flex-shrink: 0;
  color: var(--faint);
}

.added {
  flex-shrink: 0;
  font-size: var(--text-2xs);
  color: var(--accent);
}

.spacer {
  flex: 1 1 auto;
}

.mark {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
}

/* A finished call takes the accent, a failed one the danger hue — the mark is the data. */
.mark.ok {
  color: var(--accent);
}

.mark.error {
  color: var(--danger);
}

.mark.running {
  color: var(--running);
}

.reverted {
  flex-shrink: 0;
  color: var(--faint);
  font-size: var(--text-2xs);
}
</style>
