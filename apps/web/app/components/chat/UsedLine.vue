<script setup lang="ts">
/**
 * What a finished answer's turn was given beyond the document — *Used: your instructions ·
 * 3 memories* — from its `turn_start` (docs/web.md § The agent panel, *What the answer used,
 * and memory lines*). Each part opens the card of what it names; a turn given neither has
 * no line at all.
 */
import type { UsedContext } from '~/lib/thread'

type UsedPart = 'instructions' | 'memories'

const props = defineProps<{
  used: UsedContext
  /** The part whose card is open. */
  expanded: UsedPart | null
}>()

const emit = defineEmits<{ open: [part: UsedPart, anchor: HTMLElement] }>()

const { t } = useI18n()

const count = computed(() => props.used.memoryIds.length)

function open(part: UsedPart, event: MouseEvent) {
  emit('open', part, event.currentTarget as HTMLElement)
}
</script>

<template>
  <p v-if="used.instructions || count" class="used">
    <UiIcon name="book-open" />
    <span>{{ t('chat.used.label') }}</span>
    <button
      v-if="used.instructions"
      type="button"
      class="link"
      aria-haspopup="dialog"
      :aria-expanded="expanded === 'instructions' ? 'true' : 'false'"
      @click="open('instructions', $event)"
    >
      {{ t('chat.used.instructions') }}
    </button>
    <span v-if="used.instructions && count" aria-hidden="true">·</span>
    <button
      v-if="count"
      type="button"
      class="link"
      aria-haspopup="dialog"
      :aria-expanded="expanded === 'memories' ? 'true' : 'false'"
      @click="open('memories', $event)"
    >
      {{ t('chat.used.memories', { n: count }, count) }}
    </button>
  </p>
</template>

<style scoped>
.used {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  column-gap: 6px;
  min-width: 0;
  margin: 0;
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.used > :deep(svg) {
  flex: none;
  width: 13px;
  height: 13px;
}

/* The line's own faint colour, as in the design: the underline is what says they open. */
.link {
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  text-decoration: underline;
  text-decoration-color: var(--edge);
  text-underline-offset: 3px;
  cursor: pointer;
  transition:
    color var(--duration-fast) var(--ease),
    text-decoration-color var(--duration-fast) var(--ease);
}

.link:hover,
.link[aria-expanded='true'] {
  color: var(--ink);
  text-decoration-color: currentcolor;
}
</style>
