<script setup lang="ts">
/**
 * The selected agent's panel on `/agents` (docs/web.md § Pages, docs/agents.md): one row of
 * controls — the model it runs (the deployment's parse model as a fact; for the chat agent a
 * dropdown of the offered models), then for the chat agent its effort, for the parse agent
 * its prompt — then, for the parse agent, its fallback for failed pages and the format check
 * (the `check` agent's settings, which have no tab of their own);
 * for the chat agent, its instructions (with the whole prompt as their advanced option), its
 * memory, its tools, skills and subagents.
 * Every save hands the server's answer back through `saved`, so the page's tab list reads
 * the same state these controls write.
 *
 * Nothing on this panel explains itself beyond the few lines that state a consequence the
 * controls cannot show: labels, states and errors only (docs/web.md).
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
  /** The `check` agent, whose settings the parse panel shows; null while the registry has
   *  none (an older API). */
  check: AgentEntry | null
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

/** The chat agent has the effort, instructions, memory, tools, skills and subagents; parse
 *  has the coordinate convention and its prompt in the row. */
const isChat = computed(() => props.agent.name === CHAT_AGENT)

// --- model ----------------------------------------------------------------------

/** The chat agent's offered models (docs/billing.md § Models); the parse agent's model is
 *  a fact of the deployment and loads nothing. */
const models = ref<ConnectionModel[] | null>(null)
const modelsError = ref<string | null>(null)
const modelPending = ref(false)
/** Request generation: only the newest load may fill the picker. */
let modelsSeq = 0

/** The list loads when the panel opens; opening it again is what reloads it. */
async function loadModels() {
  const seq = ++modelsSeq
  models.value = null
  modelsError.value = null
  try {
    const data = (await listAgentModels(props.agent.name)).data
    if (seq === modelsSeq) {
      models.value = data
    }
  } catch (error) {
    if (seq === modelsSeq) {
      modelsError.value = await resolve(error)
    }
  }
}

watch(
  () => props.agent.name,
  () => {
    if (isChat.value) {
      void loadModels()
    }
  },
  { immediate: true },
)

/** A stored model the list does not carry (delisted, or not loaded) is still the current
    choice, so the picker shows it rather than silently blanking it. */
const modelInjected = computed(() => {
  const current = settings.value.model
  if (!current) {
    return null
  }
  return (models.value ?? []).some((model) => model.id === current) ? null : current
})

/** The chat agent's current model: the reader's pick, or with none the list's first —
    the default only the live list can name (docs/billing.md § Models). */
const modelChoice = computed(() => settings.value.model ?? models.value?.[0]?.id ?? '')

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

/** The convention the pipeline will ask the parse model for — a fact about the model
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

// --- idle compaction (chat only) -------------------------------------------------

/** Whether the worker compacts the reader's long conversations before their cache expires
 *  (docs/chat.md § Compaction, *Idle compaction*): on unless turned off. */
const idleOn = computed(() => settings.value.idle_compact ?? true)
const idlePending = ref(false)
const idleError = ref<string | null>(null)

async function applyIdleCompact(value: boolean) {
  if (idlePending.value) {
    return
  }
  idlePending.value = true
  idleError.value = null
  try {
    emit('saved', props.agent.name, await putAgentSettings(props.agent.name, { idle_compact: value }))
  } catch (error) {
    idleError.value = await resolve(error)
  } finally {
    idlePending.value = false
  }
}

// --- prompt ---------------------------------------------------------------------

/** The prompt dialog answers for the agent it was opened on. */
function onPromptSaved(saved: SavedAgentSettings) {
  emit('saved', props.agent.name, saved)
}

/** A section's own save, already named for its agent. */
function onSectionSaved(agent: string, saved: SavedAgentSettings) {
  emit('saved', agent, saved)
}
</script>

<template>
  <div class="rows">
    <!-- Model, then effort (chat) or prompt (parse): one row that wraps on a narrow window. -->
    <div class="row reveal-on-hover">
      <div class="controls">
        <UiField v-if="isChat" v-slot="{ id }" class="model" :label="t('agents.modelLabel')">
          <UiSelect
            :id="id"
            :model-value="modelChoice"
            :disabled="modelPending || (!models && !settings.model)"
            @update:model-value="applyModel"
          >
            <option v-if="!modelChoice" value="">{{ t('agents.modelNone') }}</option>
            <option v-if="modelInjected" :value="modelInjected">{{ modelInjected }}</option>
            <option v-for="model in models ?? []" :key="model.id" :value="model.id">
              {{ model.name ? `${model.name} · ${model.id}` : model.id }}
            </option>
          </UiSelect>
        </UiField>
        <!-- The parse model is the deployment's: a fact under the label, not a control. -->
        <div v-else class="model fact">
          <span class="fact-label">{{ t('agents.modelLabel') }}</span>
          <span v-if="settings.model" class="mono fact-value">{{ settings.model }}</span>
          <span v-else class="fact-value muted">{{ t('agents.modelNone') }}</span>
        </div>
        <UiField v-if="isChat" v-slot="{ id }" class="effort" :label="t('agents.effortLabel')">
          <UiSelect :id="id" :model-value="effortChoice" :disabled="effortPending" @update:model-value="applyEffort">
            <option v-for="effort in AGENT_EFFORTS" :key="effort" :value="effort">{{ t(`agents.effort.${effort}`) }}</option>
          </UiSelect>
        </UiField>
        <!-- The chat agent's prompt is its instructions' advanced option, not a row control. -->
        <AgentsPromptField
          v-if="!isChat"
          :agent="agent"
          :title="title"
          :max-chars="promptMaxChars"
          @saved="onPromptSaved"
        />
      </div>
      <p v-if="convention" class="convention mono reveal">{{ convention }}</p>
      <p v-if="isChat" class="note">{{ t('agents.cacheNote') }}</p>
      <!-- The idle-compaction switch, after the cache line (docs/web.md § Pages). -->
      <template v-if="isChat">
        <div class="switch-line">
          <UiSwitch
            :model-value="idleOn"
            :label="t('agents.idleCompact.label')"
            :busy="idlePending"
            @update:model-value="applyIdleCompact"
          />
          <span class="switch-words">{{ t('agents.idleCompact.label') }}</span>
        </div>
        <p class="note">{{ t('agents.idleCompact.note') }}</p>
      </template>
      <UiBanner v-if="modelsError" class="row-banner" tone="error">{{ modelsError }}</UiBanner>
      <UiBanner v-if="effortError" class="row-banner" tone="error">{{ effortError }}</UiBanner>
      <UiBanner v-if="idleError" class="row-banner" tone="error">{{ idleError }}</UiBanner>
    </div>

    <template v-if="!isChat">
      <AgentsFallbackSection :agent="agent" @saved="onSectionSaved" />
      <AgentsCheckSection
        v-if="check"
        :agent="check"
        :prompt-max-chars="promptMaxChars"
        @saved="onSectionSaved"
      />
    </template>

    <template v-if="isChat">
      <AgentsInstructionsSection
        :agent="agent"
        :title="title"
        :prompt-max-chars="promptMaxChars"
        @saved="onSectionSaved"
      />
      <AgentsMemorySection :agent="agent" @saved="onSectionSaved" />
      <AgentsMemoryFileSection />

      <!-- Tools: read-only facts. The sandbox's state is on the Usage page. ---------- -->
      <div class="row">
        <p class="tools-label">{{ t('agents.toolsLabel') }}</p>
        <ul class="tools" :aria-label="t('agents.toolsLabel')">
          <li v-for="tool in agent.tools" :key="tool" class="chip mono">{{ tool }}</li>
        </ul>
      </div>

      <AgentsSkillsSection :max-chars="promptMaxChars" />
      <AgentsSubagentsSection :models="models" :max-chars="promptMaxChars" />
    </template>
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

/* The parse model, drawn as a field — its label, then the value where a control would be —
   so the row reads the same whichever agent is open. */
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
  min-height: var(--control-height);
  overflow: hidden;
  font-size: var(--text-sm);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.fact-value.muted {
  color: var(--muted);
}

/* An effort picker is five short words; it does not need the model picker's width. */
.effort {
  flex: 0 1 8rem;
  min-width: 6rem;
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

/* A switch beside its words, under the cache line. */
.switch-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
}

.switch-words {
  font-size: var(--text-sm);
}

/* The tools row has the field label's treatment with no control under it. */
.tools-label {
  margin: 0;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--muted);
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
