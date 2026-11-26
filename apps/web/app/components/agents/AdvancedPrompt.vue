<script setup lang="ts">
/**
 * **Advanced: replace the whole system prompt** (docs/web.md § Pages): the disclosure under
 * an agent's instructions over its **Prompt** button, with a note on what replacing the
 * prompt gives up. Closed unless the prompt is not the default: a reader who replaced it
 * should see that they did, and where to undo it.
 */
import type { AgentEntry, SavedAgentSettings } from '~/lib/api'

const props = defineProps<{
  agent: AgentEntry
  /** The agent's name as the page shows it, for the prompt dialog's title. */
  title: string
  /** `limits.prompt_max_chars`; null until the session has loaded. */
  maxChars: number | null
  /** What replacing this agent's prompt gives up. */
  note: string
}>()

const emit = defineEmits<{ saved: [settings: SavedAgentSettings] }>()

const { t } = useI18n()

const bodyId = useId()
const open = ref(props.agent.settings.prompt !== null)
</script>

<template>
  <div class="advanced">
    <button
      type="button"
      class="disclosure"
      :aria-expanded="open"
      :aria-controls="bodyId"
      @click="open = !open"
    >
      <UiIcon :name="open ? 'chevron-down' : 'chevron-right'" class="disclosure-glyph" />
      {{ t('agents.instructions.advanced') }}
    </button>
    <div v-show="open" :id="bodyId" class="advanced-body">
      <p class="advanced-note">{{ note }}</p>
      <AgentsPromptField :agent="agent" :title="title" :max-chars="maxChars" @saved="emit('saved', $event)" />
    </div>
  </div>
</template>

<style scoped>
.advanced {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* A disclosure is a quiet row, not a button box: it opens a part of this section. */
.disclosure {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  height: var(--control-height-sm);
  margin-left: calc(-1 * var(--space-2));
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.disclosure:hover {
  background: var(--rail);
  color: var(--ink);
}

.disclosure-glyph {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--faint);
}

.advanced-body {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: var(--space-3) var(--space-4);
}

/* What replacing the prompt gives up: bounded like a banner, neutral like one. */
.advanced-note {
  flex: 1 1 20rem;
  min-width: 0;
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--hair);
  background: var(--paper);
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}
</style>
