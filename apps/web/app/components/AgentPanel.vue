<script setup lang="ts">
/**
 * The selected agent's panel on `/agents` (docs/web.md § Pages, docs/agents.md): which
 * model of Kano Proxy's live catalog it runs, and which prompt. Every pick saves on change
 * and hands the server's answer back through `saved`, so the page's tab list reads the same
 * state these controls write. Prompts belong to one agent, so their dialog lives here.
 *
 * Nothing on this panel explains itself: labels, states and errors only (docs/web.md).
 */
import {
  AGENT_EFFORTS,
  createPrompt,
  deletePrompt,
  listAgentModels,
  putAgentSettings,
  updatePrompt,
  type AgentEffort,
  type AgentEntry,
  type ConnectionModel,
  type PromptPreset,
  type SavedAgentSettings,
} from '~/lib/api'

/**
 * The `chat` agent's tools — always these four, always in this order (docs/chat.md §
 * Tools). A fact about the agent, shown as read-only chips; nothing here is chosen.
 */
const CHAT_TOOLS = ['bash', 'str_replace_based_edit_tool', 'view_page', 'view_figure'] as const
const CHAT_AGENT = 'chat'
const DEFAULT_EFFORT: AgentEffort = 'medium'

const props = defineProps<{
  agent: AgentEntry
  /** Every prompt of the user's; this panel shows its own agent's. */
  prompts: PromptPreset[]
  promptsLoaded: boolean
  /** `limits.prompt_max_chars` from `GET /api/me`; null until the session has loaded. */
  promptMaxChars: number | null
}>()

const emit = defineEmits<{
  saved: [SavedAgentSettings]
  'prompts-changed': []
}>()

const { t, te } = useI18n()
const { resolve } = useApiError()

const title = computed(() =>
  te(`agents.names.${props.agent.name}`) ? t(`agents.names.${props.agent.name}`) : props.agent.name,
)
const settings = computed(() => props.agent.settings)

// --- model ----------------------------------------------------------------------

const models = ref<ConnectionModel[] | null>(null)
const modelsPending = ref(false)
const modelsError = ref<string | null>(null)
const modelPending = ref(false)
/** Request generation: only the newest load may fill the picker. */
let modelsSeq = 0

async function loadModels() {
  const seq = ++modelsSeq
  models.value = null
  modelsError.value = null
  modelsPending.value = true
  try {
    const data = (await listAgentModels(props.agent.name)).data
    if (seq === modelsSeq) {
      models.value = [...data].sort((left, right) => left.id.localeCompare(right.id))
    }
  } catch (error) {
    if (seq === modelsSeq) {
      modelsError.value = await resolve(error)
    }
  } finally {
    if (seq === modelsSeq) {
      modelsPending.value = false
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
      emit('saved', await putAgentSettings(props.agent.name, { connection_id: null }))
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
    emit('saved', await putAgentSettings(props.agent.name, { model: value || null }))
  } catch (error) {
    modelsError.value = await resolve(error)
  } finally {
    modelPending.value = false
  }
}

/** The chat agent has the effort and tools rows; parse has the coordinate convention. */
const isChat = computed(() => props.agent.name === CHAT_AGENT)

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
    emit('saved', await putAgentSettings(props.agent.name, { effort: value as AgentEffort }))
  } catch (error) {
    effortError.value = await resolve(error)
  } finally {
    effortPending.value = false
  }
}

// --- prompt ---------------------------------------------------------------------

const prompts = computed(() => props.prompts.filter((row) => row.agent === props.agent.name))
const promptChoice = computed(() =>
  settings.value.prompt_preset_id == null ? '' : String(settings.value.prompt_preset_id),
)
const selectedPrompt = computed(
  () => prompts.value.find((row) => row.id === settings.value.prompt_preset_id) ?? null,
)
const promptPending = ref(false)
const promptError = ref<string | null>(null)
const confirmingPromptDelete = ref(false)

async function applyPromptChoice(value: string) {
  promptPending.value = true
  promptError.value = null
  try {
    emit('saved', await putAgentSettings(props.agent.name, { prompt_preset_id: value ? Number(value) : null }))
  } catch (error) {
    promptError.value = await resolve(error)
  } finally {
    promptPending.value = false
  }
}

const promptModal = ref<null | { mode: 'create' } | { mode: 'edit'; id: number }>(null)
const promptName = ref('')
const promptText = ref('')
const promptTextarea = ref<HTMLTextAreaElement | null>(null)
const promptModalPending = ref(false)
const promptModalError = ref<string | null>(null)

/** The three substitutions a template may carry (docs/parsing.md § Prompts). */
const PROMPT_TOKENS = ['{page}', '{bbox_instructions}', '{bbox_format}'] as const

function openPromptCreate() {
  promptName.value = ''
  // The agent's shipped default is the honest starting point for a custom prompt.
  promptText.value = props.agent.default_prompt
  promptModalError.value = null
  promptModal.value = { mode: 'create' }
}

function openPromptEdit() {
  const row = selectedPrompt.value
  if (!row) {
    return
  }
  promptName.value = row.name
  promptText.value = row.text
  promptModalError.value = null
  promptModal.value = { mode: 'edit', id: row.id }
}

/** A chip inserts its token at the caret, replacing a selection, and hands focus back. */
async function insertToken(token: string) {
  const el = promptTextarea.value
  if (!el) {
    promptText.value += token
    return
  }
  const start = el.selectionStart
  const end = el.selectionEnd
  promptText.value = promptText.value.slice(0, start) + token + promptText.value.slice(end)
  await nextTick()
  el.focus()
  el.setSelectionRange(start + token.length, start + token.length)
}

/** Counted as the server counts: the trimmed text, in code points rather than UTF-16 units. */
const promptLength = computed(() => [...promptText.value.trim()].length)
const promptOverCap = computed(
  () => props.promptMaxChars !== null && promptLength.value > props.promptMaxChars,
)
const promptSaveDisabled = computed(
  () => !promptName.value.trim() || !promptText.value.trim() || promptOverCap.value,
)

async function submitPrompt() {
  const modal = promptModal.value
  if (!modal || promptModalPending.value || promptSaveDisabled.value) {
    return
  }
  promptModalPending.value = true
  promptModalError.value = null
  try {
    if (modal.mode === 'create') {
      const created = await createPrompt({
        agent: props.agent.name,
        name: promptName.value.trim(),
        text: promptText.value.trim(),
      })
      promptModal.value = null
      emit('prompts-changed')
      await applyPromptChoice(String(created.id))
    } else {
      await updatePrompt(modal.id, { name: promptName.value.trim(), text: promptText.value.trim() })
      promptModal.value = null
      emit('prompts-changed')
    }
  } catch (error) {
    promptModalError.value = await resolve(error)
  } finally {
    promptModalPending.value = false
  }
}

async function removePrompt() {
  const row = selectedPrompt.value
  if (!row) {
    return
  }
  promptPending.value = true
  promptError.value = null
  try {
    await deletePrompt(row.id)
    confirmingPromptDelete.value = false
    // The server already points this agent back at its default prompt.
    emit('saved', { ...settings.value, prompt_preset_id: null, bbox: props.agent.bbox })
    emit('prompts-changed')
  } catch (error) {
    promptError.value = await resolve(error)
  } finally {
    promptPending.value = false
  }
}
</script>

<template>
  <div class="rows">
    <!-- Model --------------------------------------------------------------- -->
    <div class="row reveal-on-hover">
      <div class="controls">
        <UiField v-slot="{ id }" class="grow" :label="t('agents.modelLabel')">
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
        <div class="control-buttons">
          <UiButton
            icon-only
            variant="ghost"
            :label="t('agents.loadModels')"
            :loading="modelsPending"
            @click="loadModels"
          >
            <template #icon><UiIcon name="refresh" /></template>
          </UiButton>
        </div>
      </div>
      <p v-if="convention" class="convention mono reveal">{{ convention }}</p>
      <UiBanner v-if="modelsError" class="row-banner" tone="error">{{ modelsError }}</UiBanner>
    </div>

    <!-- Effort (chat) ------------------------------------------------------- -->
    <div v-if="isChat" class="row">
      <div class="controls">
        <UiField v-slot="{ id }" class="grow effort" :label="t('agents.effortLabel')">
          <UiSelect
            :id="id"
            :model-value="effortChoice"
            :disabled="effortPending"
            @update:model-value="applyEffort"
          >
            <option v-for="effort in AGENT_EFFORTS" :key="effort" :value="effort">{{ effort }}</option>
          </UiSelect>
        </UiField>
      </div>
      <p class="note">{{ t('agents.effortNote') }}</p>
      <UiBanner v-if="effortError" class="row-banner" tone="error">{{ effortError }}</UiBanner>
    </div>

    <!-- Prompt -------------------------------------------------------------- -->
    <div class="row">
      <div class="controls">
        <UiField v-slot="{ id }" class="grow" :label="t('agents.promptLabel')">
          <UiSelect
            :id="id"
            :model-value="promptChoice"
            :disabled="promptPending || !promptsLoaded"
            @update:model-value="applyPromptChoice"
          >
            <option value="">{{ t('agents.promptDefaultOption') }}</option>
            <option v-for="row in prompts" :key="row.id" :value="String(row.id)">
              {{ row.name }}
            </option>
          </UiSelect>
        </UiField>
        <div class="control-buttons">
          <UiButton
            icon-only
            variant="ghost"
            :label="t('agents.promptAdd')"
            :disabled="promptPending"
            @click="openPromptCreate"
          >
            <template #icon><UiIcon name="plus" /></template>
          </UiButton>
          <template v-if="selectedPrompt">
            <UiButton
              icon-only
              variant="ghost"
              :label="t('agents.promptEdit')"
              :disabled="promptPending"
              @click="openPromptEdit"
            >
              <template #icon><UiIcon name="edit" /></template>
            </UiButton>
            <UiButton
              icon-only
              variant="ghost"
              class="danger"
              :label="t('agents.promptDelete')"
              :disabled="promptPending"
              @click="confirmingPromptDelete = true"
            >
              <template #icon><UiIcon name="trash" /></template>
            </UiButton>
          </template>
        </div>
      </div>
      <UiBanner v-if="promptError" class="row-banner" tone="error">{{ promptError }}</UiBanner>
    </div>

    <!-- Tools (chat): read-only facts. The sandbox's state is on the Usage page. ---- -->
    <div v-if="isChat" class="row">
      <p class="tools-label">{{ t('agents.toolsLabel') }}</p>
      <ul class="tools" :aria-label="t('agents.toolsLabel')">
        <li v-for="tool in CHAT_TOOLS" :key="tool" class="chip mono">{{ tool }}</li>
      </ul>
    </div>

    <UiModal
      v-if="promptModal"
      size="md"
      :title="t('agents.promptDialogTitle', { agent: title })"
      :busy="promptModalPending"
      @close="promptModal = null"
    >
      <form :id="`prompt-form-${agent.name}`" class="modal-form" @submit.prevent="submitPrompt">
        <UiField v-slot="{ id }" :label="t('agents.promptNameLabel')">
          <UiTextInput :id="id" v-model="promptName" required :disabled="promptModalPending" />
        </UiField>
        <UiField v-slot="{ id }" :label="t('agents.promptTextLabel')">
          <div class="tokens">
            <button
              v-for="token in PROMPT_TOKENS"
              :key="token"
              type="button"
              class="token mono"
              :aria-label="t('agents.promptInsertToken', { token })"
              :disabled="promptModalPending"
              @click="insertToken(token)"
            >
              {{ token }}
            </button>
          </div>
          <textarea
            :id="id"
            ref="promptTextarea"
            v-model="promptText"
            class="prompt-input"
            rows="14"
            spellcheck="false"
            :disabled="promptModalPending"
          />
        </UiField>
        <div class="prompt-foot">
          <button
            type="button"
            class="text-link"
            :disabled="promptModalPending"
            @click="promptText = agent.default_prompt"
          >
            {{ t('agents.promptReset') }}
          </button>
          <span v-if="promptMaxChars !== null" class="count mono tabular" :class="{ over: promptOverCap }">
            {{ t('agents.promptCount', { count: promptLength.toLocaleString(), max: promptMaxChars.toLocaleString() }) }}
          </span>
        </div>
        <UiBanner v-if="promptModalError" tone="error">{{ promptModalError }}</UiBanner>
      </form>
      <template #footer>
        <UiButton variant="ghost" :disabled="promptModalPending" @click="promptModal = null">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton
          variant="primary"
          type="submit"
          :form="`prompt-form-${agent.name}`"
          :loading="promptModalPending"
          :disabled="promptSaveDisabled"
        >
          {{ t('common.save') }}
        </UiButton>
      </template>
    </UiModal>

    <UiConfirmDialog
      v-if="confirmingPromptDelete"
      :title="t('agents.promptDelete')"
      :message="t('agents.promptDeleteConfirm')"
      :confirm-label="t('common.delete')"
      :pending="promptPending"
      :error="promptError"
      @confirm="removePrompt"
      @cancel="confirmingPromptDelete = false"
    />
  </div>
</template>

<style scoped>
/* A reading column, not the region's full width: these are two controls, and a row
   stretched across a desktop puts its button a screen away from its field. */
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

/* One row of same-height controls, aligned on their bottom edge under the field label. */
.controls {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.grow {
  flex: 1 1 12rem;
  min-width: 0;
}

.control-buttons {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
}

.control-buttons .danger {
  color: var(--danger);
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

/* The one line under the effort picker: a consequence of changing it, not an explanation
   of what it is. */
.note {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

/* An effort picker is five short words; it does not need the model picker's width. */
.effort {
  flex-basis: 12rem;
  max-width: 20rem;
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

/* --- the prompt dialog ------------------------------------------------------ */

.modal-form {
  display: grid;
  gap: var(--space-4);
}

.tokens {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
  margin-bottom: var(--space-2);
}

/* A chip is a button that types for you: paper, a hairline, the mono of the text it goes into. */
.token {
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 1px solid var(--hair);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  cursor: pointer;
  transition:
    border-color var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.token:hover {
  border-color: var(--edge);
  color: var(--ink);
}

.token:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

/* Same skin as UiTextInput's `.control`, sized for a prompt instead of one line. */
.prompt-input {
  width: 100%;
  min-width: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-family: var(--mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  resize: vertical;
  transition: border-color var(--duration-fast) var(--ease);
}

.prompt-input:disabled {
  background: var(--paper-sunken);
  color: var(--muted);
  cursor: not-allowed;
}

@media (pointer: coarse) {
  .prompt-input {
    font-size: 16px;
  }
}

.prompt-foot {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
}

.text-link {
  padding: 0;
  border: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: var(--text-xs);
  text-decoration: underline;
  text-underline-offset: 3px;
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease);
}

.text-link:hover {
  color: var(--ink);
}

.text-link:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.count {
  color: var(--muted);
  font-size: var(--text-2xs);
}

/* Over the cap is the one state the count exists to show. */
.count.over {
  color: var(--danger);
}
</style>
