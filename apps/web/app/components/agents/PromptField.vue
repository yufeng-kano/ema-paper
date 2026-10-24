<script setup lang="ts">
/**
 * The **Prompt** control (docs/web.md § Pages): a field label over a button saying the
 * prompt's state — *Default* or *Edited*, which a legacy preset still in force also reads
 * as — that opens the prompt dialog. The parse agent has it in its row of controls; the
 * chat agent only inside the advanced disclosure under its instructions, since replacing
 * the whole prompt is what the instructions spare a reader from doing.
 */
import type { AgentEntry, SavedAgentSettings } from '~/lib/api'

const props = defineProps<{
  agent: AgentEntry
  /** The agent's name as the page shows it. */
  title: string
  /** `limits.prompt_max_chars`; null until the session has loaded. */
  maxChars: number | null
}>()

const emit = defineEmits<{ saved: [settings: SavedAgentSettings] }>()

const { t } = useI18n()

const open = ref(false)
const edited = computed(() => props.agent.settings.prompt !== null || props.agent.settings.prompt_preset_id !== null)

function onSaved(saved: SavedAgentSettings) {
  open.value = false
  emit('saved', saved)
}
</script>

<template>
  <!-- A button under a field label, named by the label and its state together. -->
  <div class="prompt">
    <span :id="`prompt-label-${agent.name}`" class="prompt-label">{{ t('agents.promptLabel') }}</span>
    <UiButton
      variant="secondary"
      aria-haspopup="dialog"
      :aria-labelledby="`prompt-label-${agent.name} prompt-state-${agent.name}`"
      @click="open = true"
    >
      <span :id="`prompt-state-${agent.name}`">
        {{ edited ? t('agents.promptEdited') : t('agents.promptDefault') }}
      </span>
      <UiIcon name="edit" class="prompt-glyph" />
    </UiButton>

    <AgentsPromptDialog
      v-if="open"
      :agent="agent"
      :title="title"
      :max-chars="maxChars"
      @close="open = false"
      @saved="onSaved"
    />
  </div>
</template>

<style scoped>
.prompt {
  display: grid;
  flex: 0 0 auto;
  gap: var(--space-2);
}

/* `UiField`'s label, for a control that is a button rather than a field. */
.prompt-label {
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
}

.prompt-glyph {
  width: 14px;
  height: 14px;
  margin-left: var(--space-2);
  vertical-align: -2px;
  color: var(--muted);
}
</style>
