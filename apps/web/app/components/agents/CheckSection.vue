<script setup lang="ts">
/**
 * The parse agent's **Format check** (docs/web.md § Pages, docs/parsing.md § Format check):
 * the `check` agent's settings, shown in the parse panel rather than as a tab of their own.
 * The switch that checks every Files upload before it opens, on unless the reader turns it
 * off; the model — the deployment's, a fact — and the effort, saved at once; the reader's
 * instructions for the check, edited in place and saved with Save, as the chat agent's are;
 * and, as their advanced option, the whole prompt.
 */
import { charLength } from '~/lib/agents'
import {
  AGENT_EFFORTS,
  putAgentSettings,
  type AgentEffort,
  type AgentEntry,
  type SavedAgentSettings,
} from '~/lib/api'

const props = defineProps<{
  /** The `check` agent's registry entry. */
  agent: AgentEntry
  /** `limits.prompt_max_chars`; null until the session has loaded. */
  promptMaxChars: number | null
}>()

const emit = defineEmits<{
  /** Named for the agent the request was made for, like every control on the panel. */
  saved: [agent: string, settings: SavedAgentSettings]
}>()

const { t } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const labelId = useId()
const noteId = useId()
const countId = useId()

const title = computed(() => t('agents.names.check'))
const settings = computed(() => props.agent.settings)
const error = ref<string | null>(null)

// --- the switch -----------------------------------------------------------------------

/** The click being saved; the stored value otherwise. Put back when the save fails. */
const autoChoice = ref<boolean | null>(null)
const autoOn = computed(() => autoChoice.value ?? settings.value.auto_check)

async function toggleAuto(next: boolean) {
  if (autoChoice.value !== null) {
    return
  }
  const name = props.agent.name
  autoChoice.value = next
  error.value = null
  try {
    emit('saved', name, await putAgentSettings(name, { auto_check: next }))
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    autoChoice.value = null
  }
}

// --- effort ---------------------------------------------------------------------------

const effortPending = ref(false)
const effortChoice = computed(() => settings.value.effort)

async function applyEffort(value: string) {
  const name = props.agent.name
  effortPending.value = true
  error.value = null
  try {
    emit('saved', name, await putAgentSettings(name, { effort: value as AgentEffort }))
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    effortPending.value = false
  }
}

// --- instructions ---------------------------------------------------------------------

/** `limits.instructions_max_chars`; null until the session has loaded. */
const maxChars = computed(() => auth.me.value?.limits.instructions_max_chars ?? null)
const savedInstructions = computed(() => settings.value.instructions ?? '')
const text = ref(savedInstructions.value)
const editor = ref<{ reset: () => void } | null>(null)
const instructionsPending = ref(false)

const length = computed(() => charLength(text.value))
const overCap = computed(() => maxChars.value !== null && length.value > maxChars.value)
const dirty = computed(() => text.value.trim() !== savedInstructions.value.trim())

async function saveInstructions() {
  if (instructionsPending.value || !dirty.value || overCap.value) {
    return
  }
  const name = props.agent.name
  instructionsPending.value = true
  error.value = null
  try {
    const result = await putAgentSettings(name, { instructions: text.value.trim() || null })
    emit('saved', name, result)
    // The text as the server cleaned it becomes the draft.
    text.value = result.instructions ?? ''
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    instructionsPending.value = false
  }
}

function cancelInstructions() {
  text.value = savedInstructions.value
  error.value = null
  editor.value?.reset()
}
</script>

<template>
  <section class="section" :aria-labelledby="labelId" :aria-describedby="noteId">
    <div class="intro">
      <div class="head">
        <h3 :id="labelId" class="agents-label">{{ t('agents.check.label') }}</h3>
        <UiSwitch :model-value="autoOn" :busy="autoChoice !== null" @update:model-value="toggleAuto">
          {{ t('agents.check.auto') }}
        </UiSwitch>
      </div>
      <p :id="noteId" class="agents-note">{{ t('agents.check.note') }}</p>
    </div>

    <div class="controls">
      <!-- The check's model is the deployment's: a fact under its label, not a control. -->
      <div class="model fact">
        <span class="agents-label">{{ t('agents.modelLabel') }}</span>
        <span v-if="settings.model" class="mono fact-value">{{ settings.model }}</span>
        <span v-else class="fact-value muted">{{ t('agents.modelNone') }}</span>
      </div>
      <UiField v-slot="{ id }" class="effort" :label="t('agents.effortLabel')">
        <UiSelect
          :id="id"
          :model-value="effortChoice"
          :disabled="effortPending"
          @update:model-value="applyEffort"
        >
          <option v-for="effort in AGENT_EFFORTS" :key="effort" :value="effort">{{ t(`agents.effort.${effort}`) }}</option>
        </UiSelect>
      </UiField>
    </div>

    <div class="instructions">
      <span class="agents-label">{{ t('agents.check.instructions') }}</span>
      <AgentsInstructionsEditor
        ref="editor"
        :source="savedInstructions"
        :placeholder="t('agents.check.placeholder')"
        :label="t('agents.check.instructions')"
        :described-by="countId"
        :editable="!instructionsPending"
        @change="text = $event"
      />
      <div class="commit">
        <AgentsCharCount :id="countId" :count="length" :max="maxChars" />
        <div class="commit-buttons">
          <UiButton variant="secondary" :disabled="!dirty || instructionsPending" @click="cancelInstructions">
            {{ t('common.cancel') }}
          </UiButton>
          <UiButton variant="primary" :loading="instructionsPending" :disabled="!dirty || overCap" @click="saveInstructions">
            {{ t('common.save') }}
          </UiButton>
        </div>
      </div>
    </div>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>

    <AgentsAdvancedPrompt
      :agent="agent"
      :title="title"
      :max-chars="promptMaxChars"
      :note="t('agents.check.advancedNote')"
      @saved="emit('saved', agent.name, $event)"
    />
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.intro {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* The label, and the switch at its right that turns the whole section on or off. */
.head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

/* Model and effort on one row, wrapping on a narrow window. */
.controls {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.model {
  flex: 1 1 14rem;
  min-width: 0;
}

/* The panel's fact treatment: the label, then the value where a control would be. */
.fact {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.fact-value {
  display: flex;
  align-items: center;
  min-height: var(--control-height);
  overflow: hidden;
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fact-value.muted {
  color: var(--muted);
}

.effort {
  flex: 0 1 8rem;
  min-width: 6rem;
}

.instructions {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.commit {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2) var(--space-4);
}

.commit-buttons {
  display: flex;
  gap: var(--space-2);
}
</style>
