<script setup lang="ts">
/**
 * A `subagent` call as a card rather than a step (docs/web.md § The agent panel): the
 * subagent's name in mono, its task, then how many steps it has taken, how long it has run
 * and what it does now — or, once done, that it reported back. Opening it shows its own
 * steps (from its `parent` events while it runs, from the result's record once stored) —
 * a write's diff card with **Revert** among them, as in a group — and the report it handed
 * the agent.
 */
import { parseResultMarkdown } from '~/lib/markdown'
import type { CardState, EditCard, SubagentView } from '~/lib/thread'

const props = defineProps<{
  card: SubagentView
  open: boolean
  /** When the panel first saw it running; with `now`, how long it has run so far. */
  startedAt: number | null
  now: number
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

const meta = computed(() => {
  const { card } = props
  const count = card.steps.filter((step) => step.type === 'tool').length
  const parts = [t('chat.subagentSteps', { n: count }, count)]
  const elapsed =
    card.elapsedMs ?? (card.status === 'running' && props.startedAt !== null ? props.now - props.startedAt : null)
  if (elapsed !== null) {
    parts.push(labels.duration(elapsed))
  }
  if (card.status === 'running') {
    const last = card.steps[card.steps.length - 1]
    parts.push(last?.type === 'tool' && last.status === 'pending' ? labels.doing(last.step) : t('chat.thinkingNow'))
  } else {
    parts.push(
      card.status === 'ok'
        ? t('chat.subagentReported')
        : card.status === 'error'
          ? t('chat.subagentFailed')
          : t('chat.subagentInterrupted'),
    )
  }
  return parts.join(' · ')
})

/** The report is the subagent's last answer: markdown, drawn like the agent's own. */
const reportBlocks = computed(() =>
  props.open && props.card.report ? parseResultMarkdown(props.card.report).flatMap((page) => page.blocks) : [],
)
</script>

<template>
  <div class="card">
    <button type="button" class="head" :aria-expanded="open" @click="emit('toggle')">
      <UiIcon name="agent" class="agent" />
      <span class="body">
        <span class="title-line">
          <span class="name mono">{{ card.name }}</span>
          <span class="task">{{ card.task }}</span>
        </span>
        <span class="meta">{{ meta }}</span>
      </span>
      <template v-if="card.status === 'running'">
        <UiSpinner class="mark running" :size="14" />
        <span class="sr-only">{{ t('chat.subagentRunning') }}</span>
      </template>
      <template v-else-if="card.status === 'ok'">
        <UiIcon name="check" class="mark ok" />
        <span class="sr-only">{{ t('chat.subagentDone') }}</span>
      </template>
      <template v-else-if="card.status === 'error'">
        <UiIcon name="x" class="mark error" />
        <span class="sr-only">{{ t('chat.subagentFailed') }}</span>
      </template>
    </button>

    <div v-if="open" class="inside">
      <template v-for="step in card.steps" :key="step.key">
        <p v-if="step.type === 'note'" class="note">{{ step.text }}</p>
        <ChatStepRow
          v-else-if="step.type === 'tool'"
          :step="step"
          :open="openSteps.has(step.key)"
          :card-state="cardState"
          :locked="locked"
          @toggle="emit('toggle-step', step.key)"
          @toggle-edit="emit('toggle-edit', $event)"
        />
      </template>
      <div v-if="card.report" class="report" :class="{ failed: card.status === 'error' }">
        <span class="report-label">{{ t('chat.subagentReport') }}</span>
        <div class="report-body">
          <MdBlock v-for="(block, index) in reportBlocks" :key="index" :block="block" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.card {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
}

.head {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  width: 100%;
  padding: 10px var(--space-3);
  border: 0;
  background: transparent;
  color: var(--ink);
  text-align: left;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.head:hover {
  background: var(--rail);
}

.agent {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
  margin-top: 1px;
  color: var(--muted);
}

.body {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.title-line {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

.name {
  flex-shrink: 0;
  color: var(--ink);
}

.task {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink-soft);
  font-size: var(--text-sm);
}

.meta {
  color: var(--faint);
  font-size: var(--text-xs);
}

.mark {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  margin-top: 2px;
}

.mark.running {
  color: var(--running);
}

.mark.ok {
  color: var(--accent);
}

.mark.error {
  color: var(--danger);
}

.inside {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px var(--space-2) var(--space-2);
  border-top: 1px solid var(--line);
  background: var(--paper-sunken);
}

.note {
  margin: 0;
  padding: 3px var(--space-2) 3px 30px;
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.report {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: 6px;
  padding: var(--space-2) var(--space-2) 2px;
  border-top: 1px solid var(--line);
}

.report-label {
  color: var(--faint);
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
}

/* The report's markdown one step below the answer's scale: it is the subagent's note to
   the agent, read inside a card. */
.report-body {
  --block-heading: var(--text-base);
  --block-subheading: var(--text-sm);
  --block-text: var(--text-xs);
  --block-text-sm: var(--text-2xs);

  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
  color: var(--ink-soft);
  overflow-wrap: anywhere;
}

.report.failed .report-body {
  color: var(--danger);
}
</style>
