<script setup lang="ts">
/**
 * What a finished answer changed, summed up in one card (docs/web.md § The agent panel):
 * the files, how many edits, the lines they added — with **View**, which opens the turn's
 * groups and every edit in them, and **Revert all**, which the panel carries out one
 * revision at a time.
 */
import { addedLines, basename } from '~/lib/steps'
import type { CardState, EditCard } from '~/lib/thread'

const props = defineProps<{
  cards: EditCard[]
  cardState: (card: EditCard) => CardState
  /** A turn is unfinished: the revisions route refuses a revert under one. */
  locked: boolean
  /** A Revert all or an Apply again of this card is under way. */
  pending: boolean
}>()

const emit = defineEmits<{ view: []; 'toggle-all': [] }>()

const { t } = useI18n()

const states = computed(() => props.cards.map((card) => props.cardState(card)))
const allReverted = computed(() => states.value.every((state) => state.reverted))
const files = computed(() => [...new Set(props.cards.map((card) => basename(card.path)))].join(', '))
/** The lines still applied: a reverted edit no longer adds its own. */
const added = computed(() =>
  props.cards.reduce((total, card, index) => total + (states.value[index]?.reverted ? 0 : addedLines(card.lines)), 0),
)
const label = computed(() => {
  const n = props.cards.length
  return allReverted.value ? t('chat.changesReverted', { n }, n) : t('chat.changes', { n }, n)
})
const busy = computed(() => props.pending || states.value.some((state) => state.pending))
</script>

<template>
  <div class="changes">
    <UiIcon name="edit" class="glyph" />
    <span class="summary">
      <span class="summary-line">
        <span class="files mono" :title="files">{{ files }}</span>
        <span v-if="added" class="added mono tabular">{{ t('chat.changesAdded', { n: added }, added) }}</span>
      </span>
      <span class="label">{{ label }}</span>
    </span>
    <button type="button" class="action" @click="emit('view')">{{ t('chat.changesView') }}</button>
    <button type="button" class="action accent" :disabled="locked || busy" @click="emit('toggle-all')">
      {{ allReverted ? t('chat.applyAgain') : t('chat.revertAll') }}
    </button>
  </div>
</template>

<style scoped>
.changes {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 44px;
  padding: var(--space-1) var(--space-1) var(--space-1) var(--space-3);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  color: var(--muted);
  font-size: var(--text-xs);
}

.glyph {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--faint);
}

/* The files over what the turn did to them: two short lines, so the actions keep their
   room in a narrow column. */
.summary {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-width: 0;
}

.summary-line {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-width: 0;
}

.files,
.label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.files {
  color: var(--ink);
}

.added {
  flex-shrink: 0;
  font-size: var(--text-2xs);
  color: var(--accent);
}

.action {
  flex-shrink: 0;
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.action:hover {
  background: var(--rail);
}

.action.accent {
  color: var(--accent);
}

.action:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
</style>
