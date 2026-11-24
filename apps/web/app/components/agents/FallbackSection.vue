<script setup lang="ts">
/**
 * The parse agent's **Failed pages** (docs/web.md § Pages, docs/parsing.md § Fallback model):
 * the deployment's fallback model, a fact, and the switch that sends a page the run model
 * blocked there on its own — off until the reader turns it on, and usable only while the
 * deployment names a fallback. The fallback is also the retry dialog's first choice.
 */
import { putAgentSettings, type AgentEntry, type SavedAgentSettings } from '~/lib/api'

const props = defineProps<{
  agent: AgentEntry
}>()

const emit = defineEmits<{
  /** Named for the agent the request was made for, like every control on the panel. */
  saved: [agent: string, settings: SavedAgentSettings]
}>()

const { t } = useI18n()
const { resolve } = useApiError()

const labelId = useId()

const fallback = computed(() => props.agent.settings.fallback_model)
/** The click being saved; the stored value otherwise. Put back when the save fails. */
const autoChoice = ref<boolean | null>(null)
/** Drawn off with no fallback, whatever is stored: there is nowhere to send a page. */
const autoOn = computed(
  () => fallback.value !== null && (autoChoice.value ?? props.agent.settings.auto_fallback),
)
const error = ref<string | null>(null)

async function toggleAuto(next: boolean) {
  if (autoChoice.value !== null) {
    return
  }
  const name = props.agent.name
  autoChoice.value = next
  error.value = null
  try {
    emit('saved', name, await putAgentSettings(name, { auto_fallback: next }))
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    autoChoice.value = null
  }
}
</script>

<template>
  <section class="section" :aria-labelledby="labelId">
    <h3 :id="labelId" class="agents-label">{{ t('agents.fallback.label') }}</h3>
    <div class="controls">
      <!-- The deployment's fallback: a fact under its label, not a control. -->
      <div class="model fact">
        <span class="agents-label">{{ t('agents.fallback.model') }}</span>
        <span v-if="fallback" class="mono fact-value">{{ fallback }}</span>
        <span v-else class="fact-value muted">{{ t('agents.fallback.none') }}</span>
      </div>
      <UiSwitch
        :model-value="autoOn"
        :busy="autoChoice !== null"
        :disabled="fallback === null"
        @update:model-value="toggleAuto"
      >
        {{ t('agents.fallback.auto') }}
      </UiSwitch>
    </div>
    <p class="agents-note">{{ t('agents.fallback.note') }}</p>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* The picker and its switch on one row, wrapping on a narrow window. */
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
  min-height: var(--control-height-sm);
  overflow: hidden;
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fact-value.muted {
  color: var(--muted);
}
</style>
