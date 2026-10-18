<script setup lang="ts">
/**
 * The run of steps between two texts of an answer (docs/web.md § The agent panel): one
 * line saying what it did, counted by kind, with a chevron that opens it. While its turn is
 * live the line is the step in flight instead, beside a spinner. Opening a step shows its
 * detail (`StepRow`).
 *
 * What is open is the panel's to keep — it outlives the items, which are rebuilt on every
 * frame — so this only draws it and says what was clicked.
 */
import type { CardState, EditCard, StepItem } from '~/lib/thread'

const props = defineProps<{
  steps: StepItem[]
  live: boolean
  open: boolean
  openSteps: ReadonlySet<string>
  cardState: (card: EditCard) => CardState
  /** A turn is unfinished: the revisions route refuses a revert under one. */
  locked: boolean
}>()

const emit = defineEmits<{
  toggle: []
  'toggle-step': [key: string]
  'toggle-edit': [card: EditCard]
}>()

const { t } = useI18n()
const labels = useStepLabels()

const label = computed(() => {
  const counted = props.steps.filter((step) => step.type !== 'note').length
  if (props.live) {
    const last = props.steps[props.steps.length - 1]
    const action =
      last?.type === 'tool' && last.status === 'pending' ? labels.doing(last.step) : t('chat.thinkingNow')
    return counted ? t('chat.liveStep', { action, n: counted }) : action
  }
  // A group of notes alone is the model thinking before it answered.
  return labels.summary(props.steps) || t('chat.thought')
})
</script>

<template>
  <div class="group">
    <button type="button" class="group-head" :aria-expanded="open" @click="emit('toggle')">
      <UiSpinner v-if="live" class="live-mark" :size="13" />
      <span class="group-label">{{ label }}</span>
      <UiIcon :name="open ? 'chevron-down' : 'chevron-right'" class="chevron" />
    </button>

    <div v-if="open" class="steps">
      <template v-for="step in steps" :key="step.key">
        <p v-if="step.type === 'note'" class="note">{{ step.text }}</p>

        <div v-else-if="step.type === 'skill'" class="row">
          <ChatStepLine :step="step" />
        </div>

        <ChatStepRow
          v-else
          :step="step"
          :open="openSteps.has(step.key)"
          :card-state="cardState"
          :locked="locked"
          @toggle="emit('toggle-step', step.key)"
          @toggle-edit="emit('toggle-edit', $event)"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.group {
  display: flex;
  flex-direction: column;
  gap: 2px;
  /* The head is a 28px control on a line of 12px text: pulled into the thread's rhythm. */
  margin: -6px 0;
}

.group-head {
  display: inline-flex;
  align-self: flex-start;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  height: 28px;
  margin-left: -6px;
  padding: 0 var(--space-2) 0 6px;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-xs);
  text-align: left;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.group-head:hover {
  background: var(--rail);
}

.group-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.live-mark {
  flex-shrink: 0;
  color: var(--running);
}

.chevron {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
  color: var(--faint);
}

.steps {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* The model's note between two calls: muted, set in under the glyph column. */
.note {
  margin: 0;
  padding: var(--space-1) var(--space-2) var(--space-1) 30px;
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-width: 0;
  min-height: 28px;
  padding: var(--space-1) var(--space-2);
  color: var(--muted);
  font-size: var(--text-xs);
}
</style>
