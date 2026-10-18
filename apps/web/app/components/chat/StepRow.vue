<script setup lang="ts">
/**
 * One tool step of a group or a subagent's card (docs/web.md § The agent panel): its line,
 * a control that opens its detail — a shell step's whole command, its output and its exit;
 * a write's diff card with **Revert**.
 */
import type { CardState, EditCard, ToolStep } from '~/lib/thread'

const props = defineProps<{
  step: ToolStep
  open: boolean
  cardState: (card: EditCard) => CardState
  /** A turn is unfinished: the revisions route refuses a revert under one. */
  locked: boolean
}>()

const emit = defineEmits<{
  toggle: []
  'toggle-edit': [card: EditCard]
}>()

const { t } = useI18n()

/** Where a shell step ran and how it ended. */
const meta = computed(() => {
  const { step } = props
  const parts: string[] = []
  if (step.step.cwd) {
    parts.push(t('chat.stepInDir', { dir: step.step.cwd.replace(/^\/workspace\//, '') }))
  }
  if (step.status === 'ok') {
    parts.push(t('chat.toolOk'))
  } else if (step.status === 'error') {
    parts.push(t('chat.toolFailed'))
  }
  return parts.join(' · ')
})
</script>

<template>
  <div class="step">
    <button
      type="button"
      class="row"
      :class="{ open }"
      :aria-expanded="open"
      :title="step.command ?? undefined"
      @click="emit('toggle')"
    >
      <ChatStepLine :step="step" :state="step.card ? cardState(step.card) : null" />
    </button>

    <div v-if="open" class="detail">
      <ChatDiffCard
        v-if="step.card"
        :card="step.card"
        :state="cardState(step.card)"
        :locked="locked"
        @toggle="emit('toggle-edit', step.card)"
      />
      <div v-else class="box">
        <div v-if="step.command" class="command-line">
          <code class="mono command">$ {{ step.command }}</code>
          <UiCopyButton :text="step.command" size="xs" :label="t('chat.stepCopyCommand')" />
        </div>
        <pre v-if="step.output" class="mono output">{{ step.output }}</pre>
        <p v-else-if="step.status !== 'pending'" class="meta">{{ t('chat.stepNoOutput') }}</p>
        <p v-if="meta" class="meta">{{ meta }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.step {
  display: flex;
  flex-direction: column;
}

.row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-width: 0;
  min-height: 28px;
  padding: var(--space-1) var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-xs);
  text-align: left;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.row:hover,
.row.open {
  background: var(--rail);
}

.detail {
  margin: 2px 0 6px 30px;
}

/* A shell step's detail on the sunken surface: the command, what it printed, how it ended. */
.box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--space-2) var(--space-2) var(--space-2) 10px;
  border-radius: var(--radius);
  background: var(--paper-sunken);
}

.command-line {
  display: flex;
  align-items: flex-start;
  gap: var(--space-2);
}

.command {
  flex: 1 1 auto;
  min-width: 0;
  padding-top: 3px;
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* The output scrolls past a few lines: a long one is read in the shell, not the panel. */
.output {
  max-height: 12rem;
  margin: 0;
  padding-top: 6px;
  overflow-y: auto;
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--text-2xs);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.meta {
  margin: 0;
  color: var(--faint);
  font-size: var(--text-2xs);
}
</style>
