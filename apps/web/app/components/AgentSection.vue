<script setup lang="ts">
/**
 * One agent's settings on `/settings` (docs/web.md § Pages, docs/agents.md): which
 * upstream it runs on, which model of that upstream's live catalog, and which prompt.
 * Every pick saves on change and hands the server's answer back through `saved`, so the
 * page's rail summary reads the same state these controls write.
 *
 * The connection dialog is the page's — a connection is shared by every agent — so the
 * add / edit / delete buttons beside the upstream picker only ask for it. Prompts belong
 * to one agent, so their dialog lives here.
 */
import {
  createPrompt,
  deletePrompt,
  listAgentModels,
  putAgentSettings,
  updatePrompt,
  type AgentEntry,
  type AgentSettings,
  type ConnectionModel,
  type KanoState,
  type PromptPreset,
  type ProviderConnection,
} from '~/lib/api'

const props = defineProps<{
  agent: AgentEntry
  connections: ProviderConnection[]
  /** False until the connections list has answered: a missing row is then "loading". */
  connectionsLoaded: boolean
  /** Every prompt of the user's; this section shows its own agent's. */
  prompts: PromptPreset[]
  promptsLoaded: boolean
  kano: KanoState | null
}>()

const emit = defineEmits<{
  saved: [AgentSettings]
  'add-connection': []
  'edit-connection': [number]
  'delete-connection': [number]
  'prompts-changed': []
}>()

const { t, te } = useI18n()
const { resolve } = useApiError()

const title = computed(() =>
  te(`settings.agents.${props.agent.name}`) ? t(`settings.agents.${props.agent.name}`) : props.agent.name,
)
const settings = computed(() => props.agent.settings)

// --- upstream -------------------------------------------------------------------

/** '' is Kano Proxy; otherwise a connection id as the `<select>` string. */
const upstreamChoice = computed(() =>
  settings.value.connection_id == null ? '' : String(settings.value.connection_id),
)
const upstreamPending = ref(false)
const upstreamError = ref<string | null>(null)
const upstreamMessage = ref<string | null>(null)

const selectedConnection = computed(
  () => props.connections.find((row) => row.id === settings.value.connection_id) ?? null,
)

/** A connection is selected but its row has not arrived — not Kano Proxy, not anything
    yet. Showing Kano Proxy here would report configuration the backend never returned. */
const upstreamUnresolved = computed(
  () => settings.value.connection_id != null && selectedConnection.value === null,
)

/** The server clears the model when the upstream changes: it belonged to the other
    catalog. The picker then reloads the new upstream's catalog. */
async function applyUpstream(value: string) {
  upstreamPending.value = true
  upstreamError.value = null
  upstreamMessage.value = null
  try {
    emit('saved', await putAgentSettings(props.agent.name, { connection_id: value ? Number(value) : null }))
    upstreamMessage.value = t('settings.upstreamSaved')
  } catch (error) {
    upstreamError.value = await resolve(error)
  } finally {
    upstreamPending.value = false
  }
}

// --- model ----------------------------------------------------------------------

/** Kano Proxy is selected but cannot serve a catalog: not configured or not connected. */
const kanoBlocked = computed(
  () => settings.value.connection_id == null && !!props.kano && !(props.kano.available && props.kano.connected),
)

const models = ref<ConnectionModel[] | null>(null)
const modelsPending = ref(false)
const modelsError = ref<string | null>(null)
const modelPending = ref(false)
const modelMessage = ref<string | null>(null)
/** Request generation: only the newest load may fill the picker — a slow answer for the
    previous upstream would list the wrong catalog. */
let modelsSeq = 0

async function loadModels() {
  const seq = ++modelsSeq
  models.value = null
  modelsError.value = null
  // Kano Proxy's state not known yet, or it cannot answer: nothing to ask.
  if (settings.value.connection_id == null && (!props.kano || kanoBlocked.value)) {
    modelsPending.value = false
    return
  }
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

watch(
  () => [settings.value.connection_id, props.kano?.available, props.kano?.connected] as const,
  () => {
    void loadModels()
  },
  { immediate: true },
)

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
  modelMessage.value = null
  try {
    emit('saved', await putAgentSettings(props.agent.name, { model: value || null }))
    modelMessage.value = t('settings.modelSaved')
  } catch (error) {
    modelsError.value = await resolve(error)
  } finally {
    modelPending.value = false
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
const promptMessage = ref<string | null>(null)
const confirmingPromptDelete = ref(false)

async function applyPromptChoice(value: string) {
  promptPending.value = true
  promptError.value = null
  promptMessage.value = null
  try {
    emit('saved', await putAgentSettings(props.agent.name, { prompt_preset_id: value ? Number(value) : null }))
    promptMessage.value = t('settings.promptSelected')
  } catch (error) {
    promptError.value = await resolve(error)
  } finally {
    promptPending.value = false
  }
}

const promptModal = ref<null | { mode: 'create' } | { mode: 'edit'; id: number }>(null)
const promptName = ref('')
const promptText = ref('')
const promptModalPending = ref(false)
const promptModalError = ref<string | null>(null)

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

async function submitPrompt() {
  const modal = promptModal.value
  if (!modal || promptModalPending.value) {
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
    promptMessage.value = t('settings.promptSaved')
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
    emit('saved', { ...settings.value, prompt_preset_id: null })
    emit('prompts-changed')
  } catch (error) {
    promptError.value = await resolve(error)
  } finally {
    promptPending.value = false
  }
}
</script>

<template>
  <section class="group">
    <h2 class="group-title">{{ title }}</h2>

    <!-- Upstream ------------------------------------------------------------ -->
    <div class="controls">
      <UiField v-slot="{ id }" class="grow" :label="t('settings.upstreamLabel')">
        <UiSelect
          :id="id"
          :model-value="upstreamChoice"
          :disabled="upstreamPending || !connectionsLoaded"
          @update:model-value="applyUpstream"
        >
          <option value="">{{ t('settings.upstreamKano') }}</option>
          <option v-for="row in connections" :key="row.id" :value="String(row.id)">
            {{ row.name }}
          </option>
        </UiSelect>
      </UiField>

      <!-- Kept on one line beside the picker, so they read as this control's actions.
           Kano Proxy has nothing to edit here: its block above connects it. -->
      <div class="control-buttons">
        <UiButton
          icon-only
          :label="t('settings.addConnection')"
          :disabled="upstreamPending"
          @click="emit('add-connection')"
        >
          <template #icon><UiIcon name="plus" /></template>
        </UiButton>
        <template v-if="selectedConnection">
          <UiButton
            icon-only
            :label="t('settings.connectionEditTitle')"
            :disabled="upstreamPending"
            @click="emit('edit-connection', selectedConnection.id)"
          >
            <template #icon><UiIcon name="edit" /></template>
          </UiButton>
          <UiButton
            icon-only
            variant="danger"
            :label="t('settings.connectionDelete')"
            :disabled="upstreamPending"
            @click="emit('delete-connection', selectedConnection.id)"
          >
            <template #icon><UiIcon name="trash" /></template>
          </UiButton>
        </template>
      </div>
    </div>
    <UiSkeleton v-if="upstreamUnresolved" :rows="1" />
    <UiBanner v-if="upstreamError" class="group-banner" tone="error">{{ upstreamError }}</UiBanner>
    <UiBanner v-else-if="upstreamMessage" class="group-banner" tone="ok">{{ upstreamMessage }}</UiBanner>

    <!-- Model --------------------------------------------------------------- -->
    <div class="controls">
      <UiField v-slot="{ id }" class="grow" :label="t('settings.modelLabel')">
        <UiSelect
          :id="id"
          :model-value="settings.model ?? ''"
          :disabled="kanoBlocked || modelPending || (!models && !settings.model)"
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
          :label="t('settings.loadModels')"
          :disabled="kanoBlocked || upstreamUnresolved"
          :loading="modelsPending"
          @click="loadModels"
        >
          <template #icon><UiIcon name="refresh" /></template>
        </UiButton>
      </div>
    </div>
    <p v-if="kanoBlocked" class="state">
      {{ kano?.available ? t('settings.modelNeedsKano') : t('settings.kanoUnavailable') }}
    </p>
    <UiBanner v-if="modelsError" class="group-banner" tone="error">{{ modelsError }}</UiBanner>
    <UiBanner v-else-if="modelMessage" class="group-banner" tone="ok">{{ modelMessage }}</UiBanner>

    <!-- Prompt -------------------------------------------------------------- -->
    <div class="controls">
      <UiField v-slot="{ id }" class="grow" :label="t('settings.promptLabel')">
        <UiSelect
          :id="id"
          :model-value="promptChoice"
          :disabled="promptPending || !promptsLoaded"
          @update:model-value="applyPromptChoice"
        >
          <option value="">{{ t('settings.promptDefaultOption') }}</option>
          <option v-for="row in prompts" :key="row.id" :value="String(row.id)">
            {{ row.name }}
          </option>
        </UiSelect>
      </UiField>
      <div class="control-buttons">
        <UiButton icon-only :label="t('settings.promptAdd')" :disabled="promptPending" @click="openPromptCreate">
          <template #icon><UiIcon name="plus" /></template>
        </UiButton>
        <template v-if="selectedPrompt">
          <UiButton
            icon-only
            :label="t('settings.promptEditTitle')"
            :disabled="promptPending"
            @click="openPromptEdit"
          >
            <template #icon><UiIcon name="edit" /></template>
          </UiButton>
          <UiButton
            icon-only
            variant="danger"
            :label="t('settings.promptDelete')"
            :disabled="promptPending"
            @click="confirmingPromptDelete = true"
          >
            <template #icon><UiIcon name="trash" /></template>
          </UiButton>
        </template>
      </div>
    </div>
    <UiBanner v-if="promptError" class="group-banner" tone="error">{{ promptError }}</UiBanner>
    <UiBanner v-else-if="promptMessage" class="group-banner" tone="ok">{{ promptMessage }}</UiBanner>

    <UiModal
      v-if="promptModal"
      size="md"
      :title="promptModal.mode === 'create' ? t('settings.promptCreateTitle') : t('settings.promptEditTitle')"
      @close="promptModal = null"
    >
      <form :id="`prompt-form-${agent.name}`" class="modal-form" @submit.prevent="submitPrompt">
        <UiField v-slot="{ id }" :label="t('settings.promptNameLabel')">
          <UiTextInput :id="id" v-model="promptName" required />
        </UiField>
        <UiField v-slot="{ id }" :label="t('settings.promptTextLabel')">
          <textarea
            :id="id"
            v-model="promptText"
            class="prompt-input"
            rows="12"
            spellcheck="false"
            :disabled="promptModalPending"
          />
        </UiField>
        <!-- The placeholders and what a custom prompt replaces belong beside the text
             being written. -->
        <p class="note">{{ t('settings.promptNote') }}</p>
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
          :disabled="!promptName.trim() || !promptText.trim()"
        >
          {{ t('common.save') }}
        </UiButton>
      </template>
    </UiModal>

    <UiConfirmDialog
      v-if="confirmingPromptDelete"
      :title="t('settings.promptDelete')"
      :message="t('settings.promptDeleteConfirm')"
      :confirm-label="t('common.delete')"
      :pending="promptPending"
      @confirm="removePrompt"
      @cancel="confirmingPromptDelete = false"
    />
  </section>
</template>

<style scoped>
.group {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* The rule under a heading is what bounds the group — there is no card to do it. */
.group-title {
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--hair);
  font-size: var(--display-sm);
  letter-spacing: normal;
}

/* One row of same-height controls, aligned on their bottom edge under the field labels. */
.controls {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--space-4);
}

.grow {
  flex: 1 1 12rem;
  min-width: 0;
}

.control-buttons {
  display: flex;
  gap: var(--space-2);
  flex-wrap: nowrap;
  flex-shrink: 0;
}

.state {
  color: var(--muted);
  max-width: 72ch;
  overflow-wrap: anywhere;
}

.group-banner {
  max-width: 72ch;
}

.modal-form {
  display: grid;
  gap: var(--space-4);
}

.note {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.7;
  max-width: 60ch;
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
</style>
