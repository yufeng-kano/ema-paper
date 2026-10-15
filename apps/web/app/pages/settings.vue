<script setup lang="ts">
/**
 * Settings (docs/web.md § Pages): Kano Proxy, the built-in upstream, then one section per
 * agent from the registry. The page owns what agents share — the Kano Proxy connection
 * and the provider connections with their dialog — and each `AgentSection` owns its
 * agent's upstream, model and prompt picks.
 */
import {
  createConnection,
  deleteConnection,
  deleteKano,
  getKano,
  kanoConnectUrl,
  listAgents,
  listConnections,
  listPrompts,
  putAgentSettings,
  updateConnection,
  type AgentSettings,
} from '~/lib/api'

definePageMeta({ middleware: 'authed' })

const { t, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const { resolve } = useApiError()

const {
  data: agentsData,
  errorMessage: agentsError,
  refresh: refreshAgents,
} = useAuthedData(() => listAgents())
/** Saved settings land here from the server's answer, so the rail follows every pick. */
const agents = ref(agentsData.value?.agents ?? [])
watch(agentsData, (data) => {
  agents.value = data?.agents ?? []
})

function setAgentSettings(name: string, settings: AgentSettings) {
  agents.value = agents.value.map((agent) => (agent.name === name ? { ...agent, settings } : agent))
}

const {
  data: connectionsData,
  errorMessage: connectionsError,
  refresh: refreshConnections,
} = useAuthedData(() => listConnections())
const connections = computed(() => connectionsData.value?.connections ?? [])

const {
  data: promptsData,
  errorMessage: promptsError,
  refresh: refreshPrompts,
} = useAuthedData(() => listPrompts())
const prompts = computed(() => promptsData.value?.prompts ?? [])

// --- Kano Proxy (docs/auth.md § 6) ------------------------------------------------

const { data: kano, errorMessage: kanoError, refresh: refreshKano } = useAuthedData(() => getKano())

/** Connect and Reconnect are full-page navigations into the server's redirect flow, like
    sign-in. The locale rides along so the callback returns to this language. */
const connectHref = computed(() => kanoConnectUrl(locale.value))

const KANO_OUTCOMES = ['connected', 'denied', 'error'] as const
type KanoOutcome = (typeof KANO_OUTCOMES)[number]

/** What the connect callback came back with, shown once: the query is replaced out of
    the URL, so a refresh does not repeat it. */
const kanoOutcome = ref<KanoOutcome | null>(null)
onMounted(() => {
  const value = route.query.kano
  if (typeof value === 'string' && (KANO_OUTCOMES as readonly string[]).includes(value)) {
    kanoOutcome.value = value as KanoOutcome
  }
  if (value !== undefined) {
    const query = { ...route.query }
    delete query.kano
    void router.replace({ query })
  }
})

const confirmingDisconnect = ref(false)
const disconnectPending = ref(false)
const disconnectError = ref<string | null>(null)

async function disconnectKano() {
  disconnectPending.value = true
  disconnectError.value = null
  try {
    await deleteKano()
    confirmingDisconnect.value = false
    kanoOutcome.value = null
    await refreshKano()
  } catch (error) {
    disconnectError.value = await resolve(error)
  } finally {
    disconnectPending.value = false
  }
}

// --- the connection dialog -----------------------------------------------------------

/**
 * Name, base URL, API key and the cost flag — no model: a connection is shared by every
 * agent, and the model is each agent's own pick. `agent` is who asked for a new one, so
 * the connection can be selected for it once saved.
 */
const connectionModal = ref<null | { mode: 'create'; agent: string } | { mode: 'edit'; id: number }>(null)
const connName = ref('')
const connUrl = ref('')
const connKey = ref('')
const connReportCost = ref(false)
const connPending = ref(false)
const connError = ref<string | null>(null)

function resetConnectionDraft() {
  connName.value = ''
  connUrl.value = ''
  connKey.value = ''
  connReportCost.value = false
  connError.value = null
}

function openConnectionCreate(agent: string) {
  resetConnectionDraft()
  connectionModal.value = { mode: 'create', agent }
}

function openConnectionEdit(id: number) {
  const row = connections.value.find((entry) => entry.id === id)
  if (!row) {
    return
  }
  resetConnectionDraft()
  connName.value = row.name
  connUrl.value = row.base_url
  connReportCost.value = row.report_cost
  connectionModal.value = { mode: 'edit', id }
}

/**
 * Closes the dialog and drops every draft it held — the key above all: a cancelled dialog
 * must not leave a typed credential in hidden state, one Save away from being submitted.
 */
function closeConnectionModal() {
  connectionModal.value = null
  resetConnectionDraft()
}

/** Escape, the overlay and Cancel. A save in flight refuses dismissal: a dialog dismissed
    mid-save could be reopened on a fresh draft that the old request would then close. */
function dismissConnectionModal() {
  if (!connPending.value) {
    closeConnectionModal()
  }
}

const connectionSaveDisabled = computed(() => {
  const modal = connectionModal.value
  if (!modal || !connName.value.trim() || !connUrl.value.trim()) {
    return true
  }
  return modal.mode === 'create' && !connKey.value.trim()
})

async function submitConnection() {
  const modal = connectionModal.value
  if (!modal || connPending.value || connectionSaveDisabled.value) {
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
        report_cost: connReportCost.value,
      })
      closeConnectionModal()
      await refreshConnections()
      // A freshly added connection is what the user came to use — select it right away.
      setAgentSettings(modal.agent, await putAgentSettings(modal.agent, { connection_id: created.id }))
    } else {
      await updateConnection(modal.id, {
        name: connName.value.trim(),
        base_url: connUrl.value.trim(),
        ...(connKey.value.trim() ? { api_key: connKey.value.trim() } : {}),
        report_cost: connReportCost.value,
      })
      closeConnectionModal()
      await refreshConnections()
    }
  } catch (error) {
    connError.value = await resolve(error)
  } finally {
    connPending.value = false
  }
}

const deletingConnection = ref<number | null>(null)
const deletePending = ref(false)
const deleteError = ref<string | null>(null)

async function removeConnection() {
  const id = deletingConnection.value
  if (id === null) {
    return
  }
  deletePending.value = true
  deleteError.value = null
  try {
    await deleteConnection(id)
    deletingConnection.value = null
    // The server pointed every agent on it back at Kano Proxy, without a model.
    await Promise.all([refreshConnections(), refreshAgents()])
  } catch (error) {
    deleteError.value = await resolve(error)
  } finally {
    deletePending.value = false
  }
}

// --- the rail's live summary -----------------------------------------------------------

/**
 * The parse agent's upstream, model and prompt in effect. A selected connection or preset
 * whose row has not arrived reads as loading — never as Kano Proxy or the default, which
 * would report configuration the backend never returned.
 */
const parse = computed(() => agents.value.find((agent) => agent.name === 'parse') ?? null)

const summaryUpstream = computed(() => {
  const id = parse.value?.settings.connection_id
  if (!parse.value || (id != null && !connectionsData.value)) {
    return t('common.loading')
  }
  if (id == null) {
    return t('settings.upstreamKano')
  }
  return connections.value.find((row) => row.id === id)?.name ?? t('common.loading')
})

const summaryModel = computed(() => {
  if (!parse.value) {
    return t('common.loading')
  }
  return parse.value.settings.model ?? t('common.notSet')
})

const summaryPrompt = computed(() => {
  const id = parse.value?.settings.prompt_preset_id
  if (!parse.value || (id != null && !promptsData.value)) {
    return t('common.loading')
  }
  if (id == null) {
    return t('settings.promptDefaultOption')
  }
  return prompts.value.find((row) => row.id === id)?.name ?? t('common.loading')
})
</script>
<template>
  <UiScreen>
    <UiRail>
      <UiPageHeader :eyebrow="t('settings.eyebrow')" :title="t('settings.headTitle')" />

      <!-- What the parse agent runs, opposite the controls that set it. -->
      <dl class="summary">
        <div class="summary-row">
          <dt class="eyebrow">{{ t('settings.upstreamLabel') }}</dt>
          <dd>{{ summaryUpstream }}</dd>
        </div>
        <div class="summary-row">
          <dt class="eyebrow">{{ t('settings.modelLabel') }}</dt>
          <dd class="mono">{{ summaryModel }}</dd>
        </div>
        <div class="summary-row last">
          <dt class="eyebrow">{{ t('settings.promptLabel') }}</dt>
          <dd>{{ summaryPrompt }}</dd>
        </div>
      </dl>
    </UiRail>

    <UiRegion>
      <UiPanel section>
        <div class="groups">
          <!-- Kano Proxy ------------------------------------------------------ -->
          <section class="group">
            <h2 class="group-title">{{ t('settings.kanoTitle') }}</h2>

            <UiBanner v-if="kanoOutcome === 'connected'" class="group-banner" tone="ok">
              {{ t('settings.kanoOutcomeConnected') }}
            </UiBanner>
            <UiBanner v-else-if="kanoOutcome === 'denied'" class="group-banner" tone="error">
              {{ t('settings.kanoOutcomeDenied') }}
            </UiBanner>
            <UiBanner v-else-if="kanoOutcome === 'error'" class="group-banner" tone="error">
              {{ t('settings.kanoOutcomeError') }}
            </UiBanner>

            <UiBanner v-if="kanoError" class="group-banner" tone="error">{{ kanoError }}</UiBanner>
            <UiSkeleton v-else-if="!kano" :rows="1" />
            <p v-else-if="!kano.available" class="state">{{ t('settings.kanoUnavailable') }}</p>
            <template v-else>
              <p class="state">
                {{ kano.connected ? t('settings.kanoConnected', { masked: kano.masked }) : t('settings.kanoNotConnected') }}
              </p>
              <div class="controls">
                <UiButton v-if="!kano.connected" variant="primary" :href="connectHref">
                  {{ t('settings.kanoConnect') }}
                </UiButton>
                <template v-else>
                  <UiButton :href="connectHref">{{ t('settings.kanoReconnect') }}</UiButton>
                  <UiButton variant="danger" :disabled="disconnectPending" @click="confirmingDisconnect = true">
                    {{ t('settings.kanoDisconnect') }}
                  </UiButton>
                </template>
              </div>
            </template>
          </section>

          <!-- One section per agent ------------------------------------------- -->
          <UiBanner v-if="agentsError" class="group-banner" tone="error">{{ agentsError }}</UiBanner>
          <UiBanner v-if="connectionsError" class="group-banner" tone="error">{{ connectionsError }}</UiBanner>
          <UiBanner v-if="promptsError" class="group-banner" tone="error">{{ promptsError }}</UiBanner>
          <UiSkeleton v-if="!agentsData && !agentsError" :rows="3" />
          <AgentSection
            v-for="agent in agents"
            :key="agent.name"
            :agent="agent"
            :connections="connections"
            :connections-loaded="connectionsData !== null"
            :prompts="prompts"
            :prompts-loaded="promptsData !== null"
            :kano="kano"
            @saved="(settings) => setAgentSettings(agent.name, settings)"
            @add-connection="openConnectionCreate(agent.name)"
            @edit-connection="openConnectionEdit"
            @delete-connection="(id) => (deletingConnection = id)"
            @prompts-changed="refreshPrompts"
          />
        </div>
      </UiPanel>
    </UiRegion>

    <UiModal
      v-if="connectionModal"
      size="md"
      :title="connectionModal.mode === 'create' ? t('settings.connectionCreateTitle') : t('settings.connectionEditTitle')"
      @close="dismissConnectionModal"
    >
      <form id="connection-form" class="modal-form" @submit.prevent="submitConnection">
        <UiField v-slot="{ id }" :label="t('settings.connectionNameLabel')">
          <UiTextInput :id="id" v-model="connName" required />
        </UiField>
        <UiField v-slot="{ id }" :label="t('settings.connectionUrlLabel')">
          <UiTextInput :id="id" v-model="connUrl" :placeholder="t('settings.connectionUrlPlaceholder')" required />
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
        <p v-if="connectionModal.mode === 'edit'" class="note">{{ t('settings.connectionKeyKeepNote') }}</p>

        <!-- Opt-in, never guessed from the URL: only the user knows whether this
             endpoint's usage.cost means anything (docs/parsing.md § Upstream usage). -->
        <label class="check">
          <input v-model="connReportCost" type="checkbox">
          <span>{{ t('settings.connectionReportCostLabel') }}</span>
        </label>
        <p class="note">{{ t('settings.connectionReportCostNote') }}</p>
        <p class="note">{{ t('settings.connectionUrlNote') }}</p>

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

    <UiConfirmDialog
      v-if="confirmingDisconnect"
      :title="t('settings.kanoDisconnect')"
      :message="t('settings.kanoDisconnectConfirm')"
      :confirm-label="t('settings.kanoDisconnect')"
      :pending="disconnectPending"
      :error="disconnectError"
      @confirm="disconnectKano"
      @cancel="confirmingDisconnect = false"
    />

    <UiConfirmDialog
      v-if="deletingConnection !== null"
      :title="t('settings.connectionDelete')"
      :message="t('settings.connectionDeleteConfirm')"
      :confirm-label="t('common.delete')"
      :pending="deletePending"
      :error="deleteError"
      @confirm="removeConnection"
      @cancel="deletingConnection = null"
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

.note {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.7;
  max-width: 60ch;
}
</style>
