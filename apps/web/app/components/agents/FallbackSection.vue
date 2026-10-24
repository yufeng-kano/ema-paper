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

const fallback = computed(() => props.agent.settings.fallback_model ?? null)
/** The click being saved; the stored value otherwise. Put back when the save fails. */
const autoChoice = ref<boolean | null>(null)
/** Drawn off with no fallback, whatever is stored: there is nowhere to send a page. */
const autoOn = computed(
  () => fallback.value !== null && (autoChoice.value ?? props.agent.settings.auto_fallback ?? false),
)
const error = ref<string | null>(null)

async function toggleAuto() {
  if (autoChoice.value !== null) {
    return
  }
  const name = props.agent.name
  const next = !autoOn.value
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
    <h3 :id="labelId" class="section-label">{{ t('agents.fallback.label') }}</h3>
    <div class="controls">
      <!-- The deployment's fallback: a fact under its label, not a control. -->
      <div class="model fact">
        <span class="fact-label">{{ t('agents.fallback.model') }}</span>
        <span v-if="fallback" class="mono fact-value">{{ fallback }}</span>
        <span v-else class="fact-value muted">{{ t('agents.fallback.none') }}</span>
      </div>
      <button
        type="button"
        role="switch"
        class="switch"
        :aria-checked="autoOn"
        :aria-busy="autoChoice !== null || undefined"
        :disabled="fallback === null"
        @click="toggleAuto"
      >
        {{ t('agents.fallback.auto') }}
        <span class="track" aria-hidden="true"><span class="knob" /></span>
      </button>
    </div>
    <p class="note">{{ t('agents.fallback.note') }}</p>
    <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
  </section>
</template>

<style scoped>
.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* The field label's treatment: a section of the panel, not a heading of the page. */
.section-label {
  margin: 0;
  font-family: inherit;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
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

.fact-label {
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
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

/* The memory section's switch: the words, then the system's capsule switch. */
.switch {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height-sm);
  padding: 0 var(--space-1) 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.switch:hover:not(:disabled) {
  background: var(--fill-weak);
}

/* Not disabled while it saves — a disabled button drops the keyboard's focus — only busy. */
.switch[aria-busy='true'] {
  cursor: progress;
}

.switch:disabled {
  color: var(--faint);
  cursor: not-allowed;
}

.track {
  position: relative;
  display: inline-block;
  flex-shrink: 0;
  width: 36px;
  height: 22px;
  border-radius: var(--radius-full);
  background: var(--fill-strong);
  transition: background var(--duration-fast) var(--ease);
}

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: var(--radius-full);
  background: var(--accent-fg);
  box-shadow: var(--shadow-segment);
  transition: left var(--duration-fast) var(--ease);
}

.switch[aria-checked='true'] .track {
  background: var(--accent);
}

.switch[aria-checked='true'] .knob {
  left: 16px;
}

.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}
</style>
