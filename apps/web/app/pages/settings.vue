<script setup lang="ts">
import {
  createConnection,
  createPrompt,
  deleteConnection,
  deleteOpenRouterKey,
  deletePrompt,
  listConnections,
  listModels,
  listProfiles,
  listPrompts,
  previewConnectionModels,
  putOpenRouterKey,
  putSettings,
  updateConnection,
  updatePrompt,
  type ConnectionModel,
} from '~/lib/api'
import { formatDateTime } from '~/lib/format'
import { modelLabel, sortModelsRecommendedFirst } from '~/lib/models'

definePageMeta({ middleware: 'authed' })

const { t, locale } = useI18n()

const auth = useAuth()
const { resolve } = useApiError()

const { data: catalog, errorMessage: catalogError } = useAuthedData(async () => {
  const [models, profiles] = await Promise.all([listModels(), listProfiles()])
  return { models: sortModelsRecommendedFirst(models.data), profiles: profiles.data }
})

const recommendedModels = computed(() => catalog.value?.models.filter((model) => model.recommended) ?? [])
const otherModels = computed(() => catalog.value?.models.filter((model) => !model.recommended) ?? [])

// --- providers: OpenRouter + the user's connections -------------------------

const {
  data: connectionsData,
  errorMessage: connectionsError,
  refresh: refreshConnections,
} = useAuthedData(() => listConnections())
const connections = computed(() => connectionsData.value?.connections ?? [])

/** '' is the built-in OpenRouter; otherwise a connection id as the `<select>` string. */
const providerChoice = ref('')
const providerPending = ref(false)
const providerError = ref<string | null>(null)
const providerMessage = ref<string | null>(null)

watch(
  () => auth.me.value?.settings.default_connection_id,
  (id) => {
    providerChoice.value = id == null ? '' : String(id)
  },
  { immediate: true },
)

const selectedConnection = computed(
  () => connections.value.find((row) => String(row.id) === providerChoice.value) ?? null,
)

/** A custom connection is selected but the connections list has not (yet) delivered its
    row — a failed list must not be mistaken for "OpenRouter is active", so the state line
    and the edit dialog hold back until the selection resolves. */
const providerUnresolved = computed(
  () => providerChoice.value !== '' && selectedConnection.value === null,
)

/** The provider picker waits for the connections list: switching against an unloaded
    list would be flying blind. */
const targetBusy = computed(() => providerPending.value || !connectionsData.value)

/** Switching providers also clears the OpenRouter model/profile default: what runs next
    is the new provider's own profile (a connection carries its model on the row). */
async function applyProvider(value: string) {
  const previous = providerChoice.value
  providerChoice.value = value
  providerPending.value = true
  providerError.value = null
  providerMessage.value = null
  try {
    await putSettings({
      default_connection_id: value ? Number(value) : null,
      default_model: null,
      default_profile: null,
    })
    providerMessage.value = t('settings.providerSaved')
    await auth.refresh()
  } catch (error) {
    providerChoice.value = previous
    providerError.value = await resolve(error)
  } finally {
    providerPending.value = false
  }
}

// --- the provider dialog -----------------------------------------------------

/**
 * Every provider edit happens in this one dialog — the page itself carries no inline
 * key or model controls. The pencil beside the provider picker is context-sensitive: on
 * a custom connection it edits that connection as a full profile (name, URL, key and
 * model together), and on OpenRouter it opens the same dialog carrying the key and the
 * parsing default.
 */
const connectionModal = ref<null | { mode: 'create' } | { mode: 'edit'; id: number } | { mode: 'key' }>(null)
const connName = ref('')
const connUrl = ref('')
const connKey = ref('')
const connModel = ref('')
const connReportCost = ref(false)
const connPending = ref(false)
const connError = ref<string | null>(null)
const confirmingConnectionDelete = ref(false)

// The dialog's live model catalog, fetched through the backend with the typed key (or
// the stored one while editing) — the model half of the profile is picked from here.
const dialogModels = ref<ConnectionModel[] | null>(null)
const dialogModelsPending = ref(false)
const dialogModelsError = ref<string | null>(null)
/** Request generation: only the newest load may fill the picker — a slow answer from a
    previous URL/key draft would show the wrong catalog. */
let dialogModelsSeq = 0

/** Create mode needs a typed URL and key before there is anything to fetch with. */
const canLoadDialogModels = computed(() => {
  const modal = connectionModal.value
  if (!modal || modal.mode === 'key') {
    return false
  }
  if (!connUrl.value.trim()) {
    return false
  }
  return modal.mode === 'edit' || connKey.value.trim().length > 0
})

async function loadDialogModels() {
  const modal = connectionModal.value
  if (!modal || modal.mode === 'key' || !canLoadDialogModels.value) {
    return
  }
  const seq = ++dialogModelsSeq
  dialogModelsPending.value = true
  dialogModelsError.value = null
  try {
    const key = connKey.value.trim()
    const data = (
      await previewConnectionModels({
        base_url: connUrl.value.trim(),
        // A typed key wins; otherwise the edit dialog reuses the stored one server-side.
        ...(key ? { api_key: key } : {}),
        ...(modal.mode === 'edit' ? { connection_id: modal.id } : {}),
      })
    ).data
    if (seq === dialogModelsSeq && connectionModal.value === modal) {
      dialogModels.value = data
    }
  } catch (error) {
    if (seq === dialogModelsSeq && connectionModal.value === modal) {
      dialogModelsError.value = await resolve(error)
    }
  } finally {
    if (seq === dialogModelsSeq && connectionModal.value === modal) {
      dialogModelsPending.value = false
    }
  }
}

/** The current model may predate the loaded catalog (or the endpoint delisted it) —
    the picker still has to show what is stored rather than silently blanking it. */
const dialogModelInjected = computed(() => {
  const current = connModel.value
  if (!current) {
    return null
  }
  return (dialogModels.value ?? []).some((model) => model.id === current) ? null : current
})

function openConnectionCreate() {
  connName.value = ''
  connUrl.value = ''
  connKey.value = ''
  connModel.value = ''
  connReportCost.value = false
  connError.value = null
  dialogModels.value = null
  dialogModelsError.value = null
  connectionModal.value = { mode: 'create' }
}

/** The pencil: edits the selected connection, or the OpenRouter profile when that is
 *  what is selected. OpenRouter is `providerChoice === ''` — a *missing* row means the
 *  connections list has not answered yet, and treating that as OpenRouter would open the
 *  wrong dialog over someone's selected connection. The button is disabled in that
 *  window anyway; this is the second lock. */
function openProviderEdit() {
  connName.value = ''
  connUrl.value = ''
  connKey.value = ''
  connModel.value = ''
  connReportCost.value = false
  connError.value = null
  dialogModels.value = null
  dialogModelsError.value = null

  if (providerChoice.value === '') {
    keyInput.value = ''
    orSelection.value = selection.value
    connectionModal.value = { mode: 'key' }
    return
  }
  const row = selectedConnection.value
  if (!row) {
    return
  }
  connName.value = row.name
  connUrl.value = row.base_url
  connModel.value = row.model ?? ''
  connReportCost.value = row.report_cost
  connectionModal.value = { mode: 'edit', id: row.id }
  // The stored key serves the fetch, so editing starts with the catalog already coming.
  loadDialogModels()
}

/**
 * Closes the dialog and drops every draft it held — the keys above all: a cancelled
 * dialog must not leave a typed credential sitting in hidden state, one Save away from
 * being submitted. Every close path goes through here: the button, the overlay, Escape
 * and a successful save.
 */
function closeConnectionModal() {
  connectionModal.value = null
  connName.value = ''
  connUrl.value = ''
  connKey.value = ''
  connModel.value = ''
  connReportCost.value = false
  connError.value = null
  dialogModels.value = null
  dialogModelsError.value = null
  keyInput.value = ''
  orSelection.value = ''
}

/**
 * What Escape, the overlay and Cancel do — as opposed to `closeConnectionModal`, which is
 * how a *successful* save puts the dialog away.
 *
 * A save in flight refuses dismissal: those two paths bypass the disabled Cancel button,
 * and a dialog dismissed mid-save can be reopened on a fresh draft that the original
 * request's completion would then close, wiping a key the user had just typed.
 */
function dismissConnectionModal() {
  if (connPending.value) {
    return
  }
  closeConnectionModal()
}

const connectionSaveDisabled = computed(() => {
  const modal = connectionModal.value
  if (!modal) {
    return true
  }
  if (modal.mode === 'key') {
    // With a key already stored the dialog can save just the parsing default.
    return !keyInput.value.trim() && !openrouterKey.value?.present
  }
  if (modal.mode === 'create') {
    return !connName.value.trim() || !connUrl.value.trim() || !connKey.value.trim() || !connModel.value
  }
  return !connName.value.trim() || !connUrl.value.trim()
})

async function submitConnection() {
  const modal = connectionModal.value
  if (!modal || connPending.value || connectionSaveDisabled.value) {
    return
  }
  if (modal.mode === 'key') {
    await submitOpenRouterDialog()
    return
  }
  connPending.value = true
  connError.value = null
  try {
    if (modal.mode === 'create') {
      const created = await createConnection({
        name: connName.value.trim(),
        base_url: connUrl.value.trim(),
        api_key: connKey.value.trim(),
        model: connModel.value,
        report_cost: connReportCost.value,
      })
      closeConnectionModal()
      await refreshConnections()
      // A freshly added provider is what the user came to use — select it right away.
      await applyProvider(String(created.id))
    } else {
      await updateConnection(modal.id, {
        name: connName.value.trim(),
        base_url: connUrl.value.trim(),
        ...(connKey.value.trim() ? { api_key: connKey.value.trim() } : {}),
        ...(connModel.value ? { model: connModel.value } : {}),
        report_cost: connReportCost.value,
      })
      closeConnectionModal()
      await refreshConnections()
    }
    providerMessage.value = t('settings.connectionSaved')
  } catch (error) {
    connError.value = await resolve(error)
  } finally {
    connPending.value = false
  }
}

async function removeConnection() {
  const row = selectedConnection.value
  if (!row) {
    return
  }
  connPending.value = true
  providerError.value = null
  try {
    await deleteConnection(row.id)
    confirmingConnectionDelete.value = false
    await Promise.all([refreshConnections(), auth.refresh()])
  } catch (error) {
    providerError.value = await resolve(error)
  } finally {
    connPending.value = false
  }
}

// --- the OpenRouter profile: key + parsing default ---------------------------

/**
 * The stored OpenRouter default as the dialog's `<select>` value. One choice, not two: a
 * parsing default is a model *and* the format it is prompted for, so the dropdown lists
 * preset profiles (which pair them) plus raw models. Encoded as `profile:<id>` /
 * `model:<id>` / ''.
 */
const selection = ref('')
/** The dialog's draft of `selection`, committed on Save — never while picking. */
const orSelection = ref('')

watch(
  () => auth.me.value?.settings,
  (settings) => {
    if (settings?.default_connection_id != null) {
      return
    }
    const profile = settings?.default_profile
    const model = settings?.default_model
    selection.value = profile ? `profile:${profile}` : model ? `model:${model}` : ''
  },
  { immediate: true },
)

/** Same injection rule as the connection dialog: a stored model missing from the
    catalog still has to be visible as the current choice. */
const orModelInjected = computed(() => {
  const value = orSelection.value
  if (!value.startsWith('model:')) {
    return null
  }
  const id = value.slice('model:'.length)
  return (catalog.value?.models ?? []).some((model) => model.id === id) ? null : id
})

const openrouterKey = computed(() => auth.me.value?.openrouter_key ?? null)
const keyInput = ref('')
const confirmingDelete = ref(false)

/** Key first, then the default: a refused key keeps the dialog open with the error, and
    the default is not silently committed alongside it. */
async function submitOpenRouterDialog() {
  connPending.value = true
  connError.value = null
  try {
    const candidate = keyInput.value.trim()
    if (candidate) {
      await putOpenRouterKey(candidate)
    }
    if (orSelection.value !== selection.value) {
      const value = orSelection.value
      await putSettings({
        default_model: value.startsWith('model:') ? value.slice('model:'.length) : null,
        default_profile: value.startsWith('profile:') ? value.slice('profile:'.length) : null,
      })
    }
    closeConnectionModal()
    await auth.refresh()
    providerMessage.value = t('settings.openrouterSaved')
  } catch (error) {
    connError.value = await resolve(error)
  } finally {
    connPending.value = false
  }
}

async function removeOpenRouterKey() {
  connPending.value = true
  providerError.value = null
  try {
    await deleteOpenRouterKey()
    confirmingDelete.value = false
    await auth.refresh()
  } catch (error) {
    providerError.value = await resolve(error)
  } finally {
    connPending.value = false
  }
}

// --- the rail's live summary -------------------------------------------------

/**
 * What is in effect right now, as three lines in the rail. It reads the same state the
 * dialogs write, so it changes as they do — that is the point of putting it opposite
 * the controls rather than under each group as a "currently: …" line.
 */
const summaryProvider = computed(() => {
  // Same rule as the controls: a *missing* row is a connections list that has not answered,
  // not OpenRouter. Reporting configuration the backend never returned is exactly the
  // fabrication the rail is here to avoid.
  if (providerUnresolved.value) {
    return t('common.loading')
  }
  return selectedConnection.value?.name ?? t('settings.providerOpenRouter')
})

const summaryModel = computed(() => {
  if (providerUnresolved.value) {
    return t('common.loading')
  }
  if (selectedConnection.value) {
    return selectedConnection.value.model ?? t('common.notSet')
  }
  if (selection.value.startsWith('model:')) {
    return selection.value.slice('model:'.length)
  }
  if (selection.value.startsWith('profile:')) {
    const id = selection.value.slice('profile:'.length)
    const profile = catalog.value?.profiles.find((entry) => entry.id === id)
    if (profile) {
      return profile.model ?? profile.name
    }
    // A stored profile the catalog has not described yet. "Not set" here would be the same
    // fabrication as calling an unresolved provider OpenRouter: a choice *is* stored.
    return t('common.loading')
  }
  return t('common.notSet')
})

// --- prompt presets ---------------------------------------------------------

const {
  data: promptsData,
  errorMessage: promptsError,
  refresh: refreshPrompts,
} = useAuthedData(() => listPrompts())
const prompts = computed(() => promptsData.value?.prompts ?? [])

/** What "default" means right now — shown as the starting point for a new prompt. */
const defaultPrompt = computed(() => auth.me.value?.defaults.system_prompt ?? '')

/** '' is the shipped default; otherwise a preset id as the `<select>` string. */
const promptChoice = ref('')
const promptPending = ref(false)
const promptError = ref<string | null>(null)
const promptMessage = ref<string | null>(null)
const confirmingPromptDelete = ref(false)

watch(
  () => auth.me.value?.settings.prompt_preset_id,
  (id) => {
    promptChoice.value = id == null ? '' : String(id)
  },
  { immediate: true },
)

const selectedPrompt = computed(
  () => prompts.value.find((row) => String(row.id) === promptChoice.value) ?? null,
)

/** Same rule again: a stored preset whose row has not arrived is unresolved, not the
 *  default prompt. */
const promptUnresolved = computed(
  () => promptChoice.value !== '' && selectedPrompt.value === null,
)

const summaryPrompt = computed(() => {
  if (promptUnresolved.value) {
    return t('common.loading')
  }
  return selectedPrompt.value?.name ?? t('settings.promptDefaultOption')
})

async function applyPromptChoice(value: string) {
  const previous = promptChoice.value
  promptChoice.value = value
  promptPending.value = true
  promptError.value = null
  promptMessage.value = null
  try {
    await putSettings({ prompt_preset_id: value ? Number(value) : null })
    promptMessage.value = t('settings.promptSelected')
    await auth.refresh()
  } catch (error) {
    promptChoice.value = previous
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
  // The shipped default is the honest starting point for a custom prompt.
  promptText.value = defaultPrompt.value
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
        name: promptName.value.trim(),
        text: promptText.value.trim(),
      })
      promptModal.value = null
      await refreshPrompts()
      await applyPromptChoice(String(created.id))
    } else {
      await updatePrompt(modal.id, {
        name: promptName.value.trim(),
        text: promptText.value.trim(),
      })
      promptModal.value = null
      await refreshPrompts()
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
    await Promise.all([refreshPrompts(), auth.refresh()])
  } catch (error) {
    promptError.value = await resolve(error)
  } finally {
    promptPending.value = false
  }
}
</script>
<template>
  <UiScreen>
    <UiRail>
      <UiPageHeader :eyebrow="t('settings.eyebrow')" :title="t('settings.headTitle')" />

      <!-- What is in effect, opposite the controls that set it. It updates as they do. -->
      <dl class="summary">
        <div class="summary-row">
          <dt class="eyebrow">{{ t('settings.providerTitle') }}</dt>
          <dd>{{ summaryProvider }}</dd>
        </div>
        <div class="summary-row">
          <dt class="eyebrow">{{ t('settings.summaryModel') }}</dt>
          <dd class="mono">{{ summaryModel }}</dd>
        </div>
        <div class="summary-row last">
          <dt class="eyebrow">{{ t('settings.summaryPrompt') }}</dt>
          <dd>{{ summaryPrompt }}</dd>
        </div>
      </dl>
    </UiRail>

    <UiRegion>
      <UiPanel section>
        <div class="groups">
          <!-- Provider ------------------------------------------------------ -->
          <!-- A provider is a profile: endpoint, key and model are edited together in
               the dialog behind the pencil. The page only picks, reports and deletes. -->
          <section class="group">
            <h2 class="group-title">{{ t('settings.providerTitle') }}</h2>

            <UiBanner v-if="connectionsError" class="group-banner" tone="error">
              {{ connectionsError }}
            </UiBanner>

            <div class="controls">
              <UiField v-slot="{ id }" class="grow" :label="t('settings.providerLabel')" label-hidden>
                <UiSelect
                  :id="id"
                  :model-value="providerChoice"
                  :disabled="targetBusy"
                  @update:model-value="applyProvider"
                >
                  <option value="">{{ t('settings.providerOpenRouter') }}</option>
                  <option v-for="row in connections" :key="row.id" :value="String(row.id)">
                    {{ row.name }}
                  </option>
                </UiSelect>
              </UiField>

              <!-- Kept on one line beside the picker: at a wider basis the two buttons wrap
                   onto a line of their own and stop reading as this control's actions. -->
              <div class="control-buttons">
                <UiButton
                  icon-only
                  :label="t('settings.addProvider')"
                  :disabled="providerPending"
                  @click="openConnectionCreate"
                >
                  <template #icon><UiIcon name="plus" /></template>
                </UiButton>
                <UiButton
                  icon-only
                  :label="
                    providerChoice === ''
                      ? t('settings.keyDialogTitle')
                      : t('settings.connectionEditTitle')
                  "
                  :disabled="providerPending || connPending || providerUnresolved"
                  @click="openProviderEdit"
                >
                  <template #icon><UiIcon name="edit" /></template>
                </UiButton>
                <!-- A connection's deletion, beside the pencil that edits it. Icon-only:
                     the ConfirmDialog behind it carries the visible words. The connection
                     itself has no inline state line — its profile is read in the rail
                     summary and edited in the dialog. -->
                <UiButton
                  v-if="selectedConnection"
                  icon-only
                  variant="danger"
                  :label="t('settings.connectionDelete')"
                  :disabled="connPending"
                  @click="confirmingConnectionDelete = true"
                >
                  <template #icon><UiIcon name="trash" /></template>
                </UiButton>
              </div>
            </div>

            <!-- OpenRouter's own read-only line: only the stored state is worth one.
                 Absence explains itself — the dialog asks for a key. -->
            <template v-if="!selectedConnection && !providerUnresolved">
              <p v-if="openrouterKey?.present && openrouterKey.updated_at" class="state">
                {{
                  t('settings.openrouterStored', {
                    masked: openrouterKey.masked,
                    updated: formatDateTime(openrouterKey.updated_at, locale),
                  })
                }}
              </p>
              <div v-if="openrouterKey?.present" class="controls">
                <UiButton variant="danger" :disabled="connPending" @click="confirmingDelete = true">
                  {{ t('settings.openrouterDelete') }}
                </UiButton>
              </div>
            </template>

            <!-- A selected connection whose row has not arrived (list failed or loading)
                 must not fall through to the OpenRouter state line. -->
            <UiSkeleton v-if="providerUnresolved" :rows="1" />

            <UiBanner v-if="providerError" class="group-banner" tone="error">{{ providerError }}</UiBanner>
            <UiBanner v-else-if="providerMessage" class="group-banner" tone="ok">{{ providerMessage }}</UiBanner>
          </section>

          <!-- System prompt ------------------------------------------------- -->
          <section class="group">
            <h2 class="group-title">{{ t('settings.promptTitle') }}</h2>

            <UiBanner v-if="promptsError" class="group-banner" tone="error">{{ promptsError }}</UiBanner>

            <div class="controls">
              <UiField v-slot="{ id }" class="grow" :label="t('settings.promptLabel')" label-hidden>
                <UiSelect
                  :id="id"
                  :model-value="promptChoice"
                  :disabled="promptPending"
                  @update:model-value="applyPromptChoice"
                >
                  <option value="">{{ t('settings.promptDefaultOption') }}</option>
                  <option v-for="row in prompts" :key="row.id" :value="String(row.id)">
                    {{ row.name }}
                  </option>
                </UiSelect>
              </UiField>

              <UiButton :disabled="promptPending" @click="openPromptCreate">
                <template #icon><UiIcon name="plus" /></template>
                {{ t('settings.promptAdd') }}
              </UiButton>
              <UiButton
                v-if="selectedPrompt"
                icon-only
                :label="t('settings.promptEditTitle')"
                :disabled="promptPending"
                @click="openPromptEdit"
              >
                <template #icon><UiIcon name="edit" /></template>
              </UiButton>
              <UiButton
                v-if="selectedPrompt"
                variant="danger"
                :disabled="promptPending"
                @click="confirmingPromptDelete = true"
              >
                {{ t('settings.promptDelete') }}
              </UiButton>
            </div>

            <UiBanner v-if="promptError" class="group-banner" tone="error">{{ promptError }}</UiBanner>
            <UiBanner v-else-if="promptMessage" class="group-banner" tone="ok">{{ promptMessage }}</UiBanner>
          </section>
        </div>
      </UiPanel>
    </UiRegion>
    <!-- One dialog, three titles: a new connection, an existing one, or the OpenRouter
         profile. A connection dialog carries name, URL, key and model; the OpenRouter
         dialog carries the key and the parsing default. -->
    <UiModal
      v-if="connectionModal"
      size="md"
      :title="
        connectionModal.mode === 'create'
          ? t('settings.connectionCreateTitle')
          : connectionModal.mode === 'edit'
            ? t('settings.connectionEditTitle')
            : t('settings.keyDialogTitle')
      "
      @close="dismissConnectionModal"
    >
      <form id="connection-form" class="modal-form" @submit.prevent="submitConnection">
        <template v-if="connectionModal.mode !== 'key'">
          <UiField v-slot="{ id }" :label="t('settings.connectionNameLabel')">
            <UiTextInput :id="id" v-model="connName" required />
          </UiField>
          <UiField v-slot="{ id }" :label="t('settings.connectionUrlLabel')">
            <UiTextInput
              :id="id"
              v-model="connUrl"
              :placeholder="t('settings.connectionUrlPlaceholder')"
              required
            />
          </UiField>
          <UiField v-slot="{ id }" :label="t('settings.connectionKeyLabel')">
            <UiTextInput
              :id="id"
              v-model="connKey"
              type="password"
              autocomplete="off"
              :required="connectionModal.mode === 'create'"
            />
          </UiField>
          <p v-if="connectionModal.mode === 'edit'" class="note">
            {{ t('settings.connectionKeyKeepNote') }}
          </p>

          <!-- The model half of the profile, fixed here at creation and re-picked here on
               edit — never inline on the settings page. -->
          <UiField v-slot="{ id }" :label="t('settings.connectionModelLabel')">
            <div class="model-row">
              <UiSelect :id="id" v-model="connModel" class="grow" :disabled="!dialogModels && !connModel">
                <option value="" disabled>{{ t('common.notSet') }}</option>
                <option v-if="dialogModelInjected" :value="dialogModelInjected">
                  {{ dialogModelInjected }}
                </option>
                <option v-for="model in dialogModels ?? []" :key="model.id" :value="model.id">
                  {{ model.name ? `${model.name} · ${model.id}` : model.id }}
                </option>
              </UiSelect>
              <UiButton
                :disabled="!canLoadDialogModels"
                :loading="dialogModelsPending"
                @click="loadDialogModels"
              >
                <template #icon><UiIcon name="refresh" /></template>
                {{ t('settings.loadModels') }}
              </UiButton>
            </div>
          </UiField>
          <UiBanner v-if="dialogModelsError" tone="error">{{ dialogModelsError }}</UiBanner>

          <!-- Opt-in, never guessed from the URL: only the user knows whether this
               endpoint's usage.cost means anything (docs/parsing.md § Upstream usage). -->
          <label class="check">
            <input v-model="connReportCost" type="checkbox">
            <span>{{ t('settings.connectionReportCostLabel') }}</span>
          </label>
          <p class="note">{{ t('settings.connectionReportCostNote') }}</p>

          <p class="note">{{ t('settings.connectionUrlNote') }}</p>
        </template>

        <template v-else>
          <UiField v-slot="{ id }" :label="t('settings.connectionKeyLabel')">
            <UiTextInput
              :id="id"
              v-model="keyInput"
              type="password"
              autocomplete="off"
              :placeholder="t('settings.openrouterPlaceholder')"
              :required="!openrouterKey?.present"
            />
          </UiField>
          <p v-if="openrouterKey?.present" class="note">
            {{ t('settings.connectionKeyKeepNote') }}
          </p>

          <!-- The OpenRouter half of "a provider is a profile": the parsing default is
               picked here, beside the key it runs on. -->
          <UiBanner v-if="catalogError" tone="error">{{ catalogError }}</UiBanner>
          <UiSkeleton v-else-if="!catalog" :rows="1" />
          <UiField v-else v-slot="{ id }" :label="t('settings.defaultLabel')">
            <UiSelect :id="id" v-model="orSelection">
              <option value="">{{ t('common.notSet') }}</option>
              <optgroup :label="t('settings.recommendedGroup')">
                <option
                  v-for="profile in catalog.profiles"
                  :key="profile.id"
                  :value="`profile:${profile.id}`"
                  :disabled="!profile.available"
                >
                  {{
                    profile.available
                      ? `${profile.name} · ${profile.model}`
                      : t('settings.profileUnavailable', { name: profile.name })
                  }}
                </option>
              </optgroup>
              <optgroup v-if="orModelInjected" :label="t('settings.customGroup')">
                <option :value="`model:${orModelInjected}`">{{ orModelInjected }}</option>
              </optgroup>
              <optgroup v-if="recommendedModels.length" :label="t('settings.recommendedModelsGroup')">
                <option v-for="model in recommendedModels" :key="model.id" :value="`model:${model.id}`">
                  {{ modelLabel(model) }}
                </option>
              </optgroup>
              <optgroup v-if="otherModels.length" :label="t('settings.otherModelsGroup')">
                <option v-for="model in otherModels" :key="model.id" :value="`model:${model.id}`">
                  {{ modelLabel(model) }}
                </option>
              </optgroup>
            </UiSelect>
          </UiField>
          <p class="note">{{ t('settings.customNote') }}</p>
        </template>

        <UiBanner v-if="connError" tone="error">{{ connError }}</UiBanner>
      </form>
      <template #footer>
        <UiButton variant="ghost" :disabled="connPending" @click="dismissConnectionModal">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton
          variant="primary"
          type="submit"
          form="connection-form"
          :loading="connPending"
          :disabled="connectionSaveDisabled"
        >
          {{ t('common.save') }}
        </UiButton>
      </template>
    </UiModal>

    <UiModal
      v-if="promptModal"
      size="md"
      :title="
        promptModal.mode === 'create' ? t('settings.promptCreateTitle') : t('settings.promptEditTitle')
      "
      @close="promptModal = null"
    >
      <form id="prompt-form" class="modal-form" @submit.prevent="submitPrompt">
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
        <!-- The placeholders and what a custom prompt overrides belong here, beside the text
             being written — not on the settings page, where they explained a dropdown. -->
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
          form="prompt-form"
          :loading="promptModalPending"
          :disabled="!promptName.trim() || !promptText.trim()"
        >
          {{ t('common.save') }}
        </UiButton>
      </template>
    </UiModal>

    <UiConfirmDialog
      v-if="confirmingDelete"
      :title="t('settings.openrouterDelete')"
      :message="t('settings.openrouterDeleteConfirm')"
      :confirm-label="t('common.delete')"
      :pending="connPending"
      @confirm="removeOpenRouterKey"
      @cancel="confirmingDelete = false"
    />

    <UiConfirmDialog
      v-if="confirmingConnectionDelete"
      :title="t('settings.connectionDelete')"
      :message="t('settings.connectionDeleteConfirm')"
      :confirm-label="t('common.delete')"
      :pending="connPending"
      @confirm="removeConnection"
      @cancel="confirmingConnectionDelete = false"
    />

    <UiConfirmDialog
      v-if="confirmingPromptDelete"
      :title="t('settings.promptDelete')"
      :message="t('settings.promptDeleteConfirm')"
      :confirm-label="t('common.delete')"
      :pending="promptPending"
      @confirm="removePrompt"
      @cancel="confirmingPromptDelete = false"
    />
  </UiScreen>
</template>

<style scoped>
/* --- Rail ----------------------------------------------------------------- */

.summary {
  display: flex;
  flex-direction: column;
  margin: 0;
}

.summary-row {
  padding: var(--space-4) 0;
  border-top: 1px solid var(--line);
}

.summary-row.last {
  border-bottom: 1px solid var(--line);
}

.summary-row dd {
  margin: var(--space-2) 0 0;
  color: var(--ink);
  font-size: var(--text-base);
  overflow-wrap: anywhere;
}

.summary-row dd.mono {
  font-size: var(--text-xs);
}

/* --- Groups --------------------------------------------------------------- */

/* A reading column, not the region's full width: these are forms, and a control row
   stretched across a desktop puts its button a screen away from its field. */
.groups {
  display: flex;
  flex-direction: column;
  gap: var(--space-10);
  max-width: 52rem;
}

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

/*
 * One row of controls that are all the same height. The fields render label-less here, so
 * the row aligns on its baseline edge and every control's height comes from
 * `--control-height` — nothing here re-states a pixel value.
 */
.controls {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: var(--space-4);
}

/* 12rem, not 18rem: at the wider basis the icon buttons beside the picker wrapped to a line
   of their own on a normal laptop. */
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

/* --- Dialogs -------------------------------------------------------------- */

.modal-form {
  display: grid;
  gap: var(--space-4);
}

/* A native checkbox beside its label; the whole line is the click target. Ink, not the
   accent: a setting being on is not data. */
.check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--ink);
  cursor: pointer;
}

.check input {
  margin: 0;
  accent-color: var(--ink);
}

/* The model picker and its fetch button share the field's one row. */
.model-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
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
