<script setup lang="ts">
/**
 * The parse agent's **Format check** (docs/web.md § Pages, docs/parsing.md § Format check):
 * the `check` agent's settings, shown in the parse panel rather than as a tab of their own.
 * The switch that checks every Files upload before it opens, on unless the reader turns it
 * off; the model — the parse's own unless one is picked — and the effort, each saved at
 * once; the reader's instructions for the check, edited in place and saved with Save, as the
 * chat agent's are; and, as their advanced option, the whole prompt.
 */
import {
  AGENT_EFFORTS,
  putAgentSettings,
  type AgentEffort,
  type AgentEntry,
  type ConnectionModel,
  type SavedAgentSettings,
} from '~/lib/api'

const DEFAULT_EFFORT: AgentEffort = 'medium'

const props = defineProps<{
  /** The `check` agent's registry entry. */
  agent: AgentEntry
  /** The panel's live catalog; null while it loads. */
  models: ConnectionModel[] | null
  /** `limits.prompt_max_chars`; null until the session has loaded. */
  promptMaxChars: number | null
}>()

const emit = defineEmits<{
  /** Named for the agent the request was made for, like every control on the panel. */
  saved: [agent: string, settings: SavedAgentSettings]
}>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const labelId = useId()
const noteId = useId()
const countId = useId()
const advancedId = useId()

const title = computed(() => t('agents.names.check'))
const settings = computed(() => props.agent.settings)
const error = ref<string | null>(null)

// --- the switch -----------------------------------------------------------------------

/** The click being saved; the stored value otherwise. Put back when the save fails. */
const autoChoice = ref<boolean | null>(null)
const autoOn = computed(() => autoChoice.value ?? settings.value.auto_check ?? true)

async function toggleAuto() {
  if (autoChoice.value !== null) {
    return
  }
  const name = props.agent.name
  const next = !autoOn.value
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

// --- model and effort -----------------------------------------------------------------

const modelPending = ref(false)
const effortPending = ref(false)
const effortChoice = computed(() => settings.value.effort ?? DEFAULT_EFFORT)

/** A stored model the catalog does not list is still the current choice. */
const modelInjected = computed(() => {
  const current = settings.value.model
  return current && !(props.models ?? []).some((model) => model.id === current) ? current : null
})

/** No model of its own (`''`, stored as null) is the parse's model. */
async function applyModel(value: string) {
  const name = props.agent.name
  modelPending.value = true
  error.value = null
  try {
    emit('saved', name, await putAgentSettings(name, { model: value || null }))
  } catch (failure) {
    error.value = await resolve(failure)
  } finally {
    modelPending.value = false
  }
}

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
/** Bumped to remount the editor on Cancel: it follows its `source` only when that changes. */
const editorKey = ref(0)
const editor = ref<{ focus: (where?: 'start' | 'end') => void } | null>(null)
const instructionsPending = ref(false)

/** Counted as the server counts: the trimmed text, in code points. */
const length = computed(() => [...text.value.trim()].length)
const overCap = computed(() => maxChars.value !== null && length.value > maxChars.value)
const dirty = computed(() => text.value.trim() !== savedInstructions.value.trim())

/** A click on the box below the last line still writes, as in the notes column. */
function onEditorClick(event: MouseEvent) {
  if (!(event.target instanceof Element) || !event.target.closest('.rich')) {
    editor.value?.focus('end')
  }
}

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
  editorKey.value += 1
}

// --- advanced: the whole prompt -------------------------------------------------------

/** Closed unless the prompt is not the default. */
const advancedOpen = ref(settings.value.prompt !== null || settings.value.prompt_preset_id !== null)
</script>

<template>
  <section class="section" :aria-labelledby="labelId" :aria-describedby="noteId">
    <div class="intro">
      <div class="head">
        <h3 :id="labelId" class="section-label">{{ t('agents.check.label') }}</h3>
        <button
          type="button"
          role="switch"
          class="switch"
          :aria-checked="autoOn"
          :aria-busy="autoChoice !== null || undefined"
          @click="toggleAuto"
        >
          {{ t('agents.check.auto') }}
          <span class="track" aria-hidden="true"><span class="knob" /></span>
        </button>
      </div>
      <p :id="noteId" class="note">{{ t('agents.check.note') }}</p>
    </div>

    <div class="controls">
      <UiField v-slot="{ id }" class="model" :label="t('agents.modelLabel')">
        <UiSelect
          :id="id"
          :model-value="settings.model ?? ''"
          :disabled="modelPending"
          @update:model-value="applyModel"
        >
          <option value="">{{ t('agents.check.sameModel') }}</option>
          <option v-if="modelInjected" :value="modelInjected">{{ modelInjected }}</option>
          <option v-for="model in models ?? []" :key="model.id" :value="model.id">
            {{ model.name ? `${model.name} · ${model.id}` : model.id }}
          </option>
        </UiSelect>
      </UiField>
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
      <span class="field-label">{{ t('agents.check.instructions') }}</span>
      <div class="editor md-side" @click="onEditorClick">
        <RichEditor
          :key="editorKey"
          ref="editor"
          variant="notes"
          :source="savedInstructions"
          :placeholder="t('agents.check.placeholder')"
          :label="t('agents.check.instructions')"
          :described-by="countId"
          :editable="!instructionsPending"
          @change="text = $event"
        />
      </div>
      <div class="commit">
        <span :id="countId" class="count tabular" :class="{ over: overCap }">
          {{
            maxChars === null
              ? length.toLocaleString(locale)
              : t('agents.instructions.count', {
                count: length.toLocaleString(locale),
                max: maxChars.toLocaleString(locale),
              })
          }}
        </span>
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

    <div class="advanced">
      <button
        type="button"
        class="disclosure"
        :aria-expanded="advancedOpen"
        :aria-controls="advancedId"
        @click="advancedOpen = !advancedOpen"
      >
        <UiIcon :name="advancedOpen ? 'chevron-down' : 'chevron-right'" class="disclosure-glyph" />
        {{ t('agents.instructions.advanced') }}
      </button>
      <div v-show="advancedOpen" :id="advancedId" class="advanced-body">
        <p class="advanced-note">{{ t('agents.check.advancedNote') }}</p>
        <AgentsPromptField
          :agent="agent"
          :title="title"
          :max-chars="promptMaxChars"
          @saved="emit('saved', agent.name, $event)"
        />
      </div>
    </div>
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

/* The field label's treatment: a section of the panel, not a heading of the page. */
.section-label,
.field-label {
  margin: 0;
  font-family: inherit;
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-label);
  text-transform: uppercase;
  color: var(--faint);
}

/* A consequence the controls cannot show, in the cache line's voice. */
.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

/* The memory section's switch: the words, then a square track whose knob says on or off. */
.switch {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height-sm);
  padding: 0 var(--space-1) 0 var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.switch:hover {
  background: var(--rail);
}

/* Not disabled while it saves — a disabled button drops the keyboard's focus — only busy. */
.switch[aria-busy='true'] {
  cursor: progress;
}

.track {
  position: relative;
  display: inline-block;
  flex-shrink: 0;
  width: 30px;
  height: 18px;
  border: 1px solid var(--muted);
  border-radius: var(--radius);
  background: var(--paper);
  transition:
    background var(--duration-fast) var(--ease),
    border-color var(--duration-fast) var(--ease);
}

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 12px;
  height: 12px;
  border-radius: 2px;
  background: var(--muted);
}

.switch[aria-checked='true'] .track {
  border-color: var(--accent);
  background: var(--accent);
}

.switch[aria-checked='true'] .knob {
  left: 14px;
  background: var(--accent-fg);
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

.effort {
  flex: 0 1 8rem;
  min-width: 6rem;
}

.instructions {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* The chat agent's instructions box: the notes' editor in a field's hairline box. */
.editor {
  --block-heading: var(--display-2xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);
  --rich-gap: var(--space-3);

  min-height: 7rem;
  padding: var(--space-4);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  cursor: text;
}

.editor:focus-within {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.editor :deep(.md-h2:not([data-level='1'])) {
  padding-top: 0;
  border-top: none;
}

.editor :deep(.md-p),
.editor :deep(.md-list) {
  line-height: 1.7;
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

.count {
  color: var(--faint);
  font-size: var(--text-xs);
}

/* Over the cap is the one state the count exists to show. */
.count.over {
  color: var(--danger);
}

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
