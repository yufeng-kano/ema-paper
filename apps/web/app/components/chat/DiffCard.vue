<script setup lang="ts">
/**
 * One edit as a diff card: applied as made, undone from its footer (docs/web.md § The agent
 * panel). Removed lines on the danger wash, added ones on the accent wash, wrapped rather
 * than scrolled sideways — a panel this narrow has no room for a second scrollbar.
 */
import { basename } from '~/lib/steps'
import type { CardState, EditCard } from '~/lib/thread'

defineProps<{
  card: EditCard
  state: CardState
  /** A turn is unfinished: the revisions route refuses a revert under one. */
  locked: boolean
}>()

const emit = defineEmits<{ toggle: [] }>()

const { t } = useI18n()
</script>

<template>
  <div class="card">
    <div class="diff mono">
      <div v-for="(line, index) in card.lines" :key="index" class="diff-line" :class="line.kind">
        <span class="diff-sign" aria-hidden="true">{{
          line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '
        }}</span>
        <span class="diff-text">{{ line.text }}</span>
      </div>
    </div>
    <div class="card-foot">
      <span class="card-state">
        {{ state.reverted ? t('chat.reverted') : t('chat.applied') }}
        <span class="mono card-path">· {{ basename(card.path) }}</span>
      </span>
      <button
        v-if="card.commit"
        type="button"
        class="text-link"
        :disabled="state.pending || locked"
        @click="emit('toggle')"
      >
        {{ state.reverted ? t('chat.applyAgain') : t('chat.revert') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.card {
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  overflow: hidden;
  background: var(--paper);
}

.diff {
  display: flex;
  flex-direction: column;
  max-height: 240px;
  overflow-y: auto;
  padding: var(--space-2) 0;
  font-size: var(--text-2xs);
  line-height: 1.6;
}

.diff-line {
  display: flex;
  gap: var(--space-2);
  min-height: 1.6em;
  padding: 0 var(--space-3);
}

.diff-line.removed {
  background: var(--diff-removed);
  color: var(--danger);
}

.diff-line.added {
  background: var(--diff-added);
  color: var(--accent);
}

.diff-line.hunk {
  color: var(--faint);
}

.diff-line.context {
  color: var(--muted);
}

.diff-sign {
  flex-shrink: 0;
  width: 1ch;
}

.diff-text {
  min-width: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-1) var(--space-2) var(--space-1) var(--space-3);
  border-top: 1px solid var(--line);
  font-size: var(--text-xs);
  color: var(--faint);
}

.card-state {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-path {
  font-size: var(--text-2xs);
}

.text-link {
  flex-shrink: 0;
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--accent);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.text-link:hover {
  background: var(--rail);
}

.text-link:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
