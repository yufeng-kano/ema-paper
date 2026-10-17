<script setup lang="ts">
/**
 * The selected agent's panel on `/agents` (docs/web.md § Pages, docs/agents.md): one row of
 * controls — which model of Kano Proxy's live catalog it runs, for the chat agent its
 * effort, and its prompt — then, for the chat agent, its tools, skills and subagents. Every
 * pick saves on change and hands the server's answer back through `saved`, so the page's
 * tab list reads the same state these controls write.
 *
 * Nothing on this panel explains itself: labels, states and errors only (docs/web.md).
 */
import {
  AGENT_EFFORTS,
  listAgentModels,
  putAgentSettings,
  type AgentEffort,
  type AgentEntry,
  type ConnectionModel,
  type SavedAgentSettings,
} from '~/lib/api'

const CHAT_AGENT = 'chat'
const DEFAULT_EFFORT: AgentEffort = 'medium'

const props = defineProps<{
  agent: AgentEntry
  /** `limits.prompt_max_chars` from `GET /api/me`; null until the session has loaded. */
  promptMaxChars: number | null
}>()

const emit = defineEmits<{
  /** Named for the agent the request was made for: the reader may have switched
   *  agents while it was in flight, and the answer is not the new agent's. */
  saved: [agent: string, settings: SavedAgentSettings]
}>()

const { t, te } = useI18n()
const { resolve } = useApiError()

const title = computed(() =>
  te(`agents.names.${props.agent.name}`) ? t(`agents.names.${props.agent.name}`) : props.agent.name,
)
const settings = computed(() => props.agent.settings)

/** The chat agent has the effort, tools, skills and subagents; parse has the coordinate
 *  convention. */
const isChat = computed(() => props.agent.name === CHAT_AGENT)

// --- model ----------------------------------------------------------------------

const models = ref<ConnectionModel[] | null>(null)
const modelsError = ref<string | null>(null)
const modelPending = ref(false)
/** Request generation: only the newest load may fill the picker. */
let modelsSeq = 0

/** The catalog loads when the panel opens; opening it again is what reloads it. */
async function loadModels() {
  const seq = ++modelsSeq
  models.value = null
  modelsError.value = null
  try {
    const data = (await listAgentModels(props.agent.name)).data
    if (seq === modelsSeq) {
      models.value = [...data].sort((left, right) => left.id.localeCompare(right.id))
    }
  } catch (error) {
    if (seq === modelsSeq) {
      modelsError.value = await resolve(error)
    }
  }
}

/**
 * An agent still pointing at a provider connection — set on the former Settings page —
 * is moved back to Kano Proxy before its catalog loads. The picker would otherwise list
 * one upstream's models while parses ran, and were charged, on another.
 */
async function resolveUpstream() {
  if (settings.value.connection_id !== null) {
    try {
      emit('saved', props.agent.name, await putAgentSettings(props.agent.name, { connection_id: null }))
    } catch (error) {
      modelsError.value = await resolve(error)
      return
    }
  }
  await loadModels()
}

watch(() => props.agent.name, () => void resolveUpstream(), { immediate: true })

/** A stored model the catalog does not list (delisted, or not loaded) is still the
    current choice, so the picker shows it rather than silently blanking it. */
const modelInjected = computed(() => {
  const current = settings.value.model
  if (!current) {
    return null
  }
  return (models.value ?? []).some((model) => model.id === current) ? null : current
})

async function applyModel(value: string) {
  modelPending.value = true
  modelsError.value = null
  try {
    emit('saved', props.agent.name, await putAgentSettings(props.agent.name, { model: value || null }))
  } catch (error) {
    modelsError.value = await resolve(error)
  } finally {
    modelPending.value = false
  }
}

/** The convention the pipeline will ask the saved model for — a fact about the model
    (docs/parsing.md § Coordinate conventions), shown beside it and never a control. Only
    the parse agent has one: the chat agent reads pages, it does not transcribe them. */
const convention = computed(() =>
  settings.value.model && !isChat.value ? t(`agents.bbox.${props.agent.bbox.family}`) : null,
)

// --- effort (chat only) ---------------------------------------------------------

const effortPending = ref(false)
const effortError = ref<string | null>(null)
const effortChoice = computed(() => settings.value.effort ?? DEFAULT_EFFORT)

async function applyEffort(value: string) {
  effortPending.value = true
  effortError.value = null
  try {
    emit('saved', props.agent.name, await putAgentSettings(props.agent.name, { effort: value as AgentEffort }))
  } catch (error) {
    effortError.value = await resolve(error)
  } finally {
    effortPending.value = false
  }
}

// --- prompt ---------------------------------------------------------------------

const promptOpen = ref(false)

function onPromptSaved(saved: SavedAgentSettings) {
  promptOpen.value = false
  emit('saved', props.agent.name, saved)
}
</script>

<template>
  <div class="rows">
    <!-- Model, effort and prompt: one row that wraps on a narrow window. ------------ -->
    <div class="row reveal-on-hover">
      <div class="controls">
        <UiField v-slot="{ id }" class="model" :label="t('agents.modelLabel')">
          <UiSelect
            :id="id"
            :model-value="settings.model ?? ''"
            :disabled="modelPending || (!models && !settings.model)"
            @update:model-value="applyModel"
          >
            <option value="">{{ t('common.notSet') }}</option>
            <option v-if="modelInjected" :value="modelInjected">{{ modelInjected }}</option>
            <option v-for="model in models ?? []" :key="model.id" :value="model.id">
              {{ model.name ? `${model.name} · ${model.id}` : model.id }}
            </option>
          </UiSelect>
        </UiField>
        <UiField v-if="isChat" v-slot="{ id }" class="effort" :label="t('agents.effortLabel')">
          <UiSelect :id="id" :model-value="effortChoice" :disabled="effortPending" @update:model-value="applyEffort">
            <option v-for="effort in AGENT_EFFORTS" :key="effort" :value="effort">{{ t(`agents.effort.${effort}`) }}</option>
          </UiSelect>
        </UiField>
        <!-- A button under a field label, named by the label and its state together. -->
        <div class="prompt">
          <span :id="`prompt-label-${agent.name}`" class="prompt-label">{{ t('agents.promptLabel') }}</span>
          <UiButton
            variant="secondary"
            aria-haspopup="dialog"
            :aria-labelledby="`prompt-label-${agent.name} prompt-state-${agent.name}`"
            @click="promptOpen = true"
          >
            <span :id="`prompt-state-${agent.name}`">
              {{ settings.prompt === null ? t('agents.promptDefault') : t('agents.promptEdited') }}
            </span>
            <UiIcon name="edit" class="prompt-glyph" />
          </UiButton>
        </div>
      </div>
      <p v-if="convention" class="convention mono reveal">{{ convention }}</p>
      <p v-if="isChat" class="note">{{ t('agents.cacheNote') }}</p>
      <UiBanner v-if="modelsError" class="row-banner" tone="error">{{ modelsError }}</UiBanner>
      <UiBanner v-if="effortError" class="row-banner" tone="error">{{ effortError }}</UiBanner>
    </div>

    <!-- Tools (chat): read-only facts. The sandbox's state is on the Usage page. ---- -->
    <div v-if="isChat" class="row">
      <p class="tools-label">{{ t('agents.toolsLabel') }}</p>
      <ul class="tools" :aria-label="t('agents.toolsLabel')">
        <li v-for="tool in agent.tools" :key="tool" class="chip mono">{{ tool }}</li>
      </ul>
    </div>

    <template v-if="isChat">
      <AgentsSkillsSection :max-chars="promptMaxChars" />
      <AgentsSubagentsSection :models="models" :max-chars="promptMaxChars" />
    </template>

    <AgentsPromptDialog
      v-if="promptOpen"
      :agent="agent"
      :title="title"
      :max-chars="promptMaxChars"
      @close="promptOpen = false"
      @saved="onPromptSaved"
    />
  </div>
</template>

<style scoped>
/* A reading column, not the region's full width: a row stretched across a desktop puts
   its last control a screen away from its first. */
.rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-7);
  max-width: 44rem;
  /* The panel body's own top padding is zero, for tables that start at the header's rule. */
  padding-top: var(--space-6);
}

.row {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* One row of same-height controls, aligned on their bottom edge under the field labels. */
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

/* An effort picker is five short words; it does not need the model picker's width. */
.effort {
  flex: 0 1 8rem;
  min-width: 6rem;
}

.prompt {
  display: grid;
  flex: 0 0 auto;
  gap: var(--space-2);
}

/* `UiField`'s label, for a control that is a button rather than a field. */
.prompt-label {
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-label);
  text-transform: uppercase;
  color: var(--faint);
}

.prompt-glyph {
  width: 14px;
  height: 14px;
  margin-left: var(--space-2);
  vertical-align: -2px;
  color: var(--muted);
}

/* A fact about the model, read when the reader looks for it: visible on hover or focus,
   always visible where there is no hover to give. */
.convention {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-2xs);
}

.reveal {
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease);
}

.reveal-on-hover:hover .reveal,
.reveal-on-hover:focus-within .reveal {
  opacity: 1;
}

@media (hover: none) {
  .reveal {
    opacity: 1;
  }
}

.row-banner {
  max-width: 72ch;
}

/* The one line under the row: a consequence of changing it, not an explanation of it. */
.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

/* The tools row has the field label's treatment with no control under it. */
.tools-label {
  margin: 0;
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-label);
  text-transform: uppercase;
  color: var(--faint);
}

.tools {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

/* A chip that is a fact: paper, a hairline, the mono of the name it carries — and no
   hover, because it does nothing. */
.chip {
  display: inline-flex;
  align-items: center;
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  color: var(--ink-soft);
  font-size: var(--text-2xs);
}
</style>
