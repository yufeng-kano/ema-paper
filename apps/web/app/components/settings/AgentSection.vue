<script setup lang="ts">
/**
 * Agent (docs/web.md § Pages, `/settings/agent`): every agent runs on the user's own Kano
 * Proxy, so the section opens with the Kano Proxy row — the connect button while there is
 * no key, the key's tail and Disconnect once there is one. Connected, the agents follow as
 * a tab row over the selected agent's panel (`AgentPanel`) and nothing else. The `check`
 * agent is no tab: its settings are the parse panel's *Format check*.
 */
import {
  deleteKano,
  getKano,
  kanoConnectUrl,
  listAgents,
  type AgentEntry,
  type SavedAgentSettings,
} from '~/lib/api'

const { t, te, locale } = useI18n()
const route = useRoute()
const router = useRouter()
const auth = useAuth()
const { resolve } = useApiError()

// --- Kano Proxy (docs/auth.md § 6) ------------------------------------------------

const { data: kano, errorMessage: kanoError, refresh: refreshKano } = useAuthedData(() => getKano())

/** Connect is a full-page navigation into the server's redirect flow, like sign-in. The
    locale rides along so the callback returns to this language. */
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

/** The masked key's tail — what tells two keys apart, and all the line needs. */
const keyTail = computed(() => {
  const masked = kano.value?.masked ?? ''
  const index = masked.lastIndexOf('-')
  return index === -1 ? masked : masked.slice(index + 1)
})

// --- agents -------------------------------------------------------------------------

const {
  data: agentsData,
  errorMessage: agentsError,
} = useAuthedData(() => listAgents())
/** Saved settings land here from the server's answer, so the tabs follow every pick. */
const agents = ref<AgentEntry[]>(agentsData.value?.agents ?? [])
watch(agentsData, (data) => {
  agents.value = data?.agents ?? []
})

const contextVersion = useAgentContextVersion()

/** Every settings save of the panel comes through here, so this is also where *What the
 *  agent receives* learns that the parts it lists may have changed. */
function setAgentSettings(name: string, saved: SavedAgentSettings) {
  const { bbox, ...settings } = saved
  agents.value = agents.value.map((agent) =>
    agent.name === name ? { ...agent, settings, bbox } : agent,
  )
  contextVersion.changed()
}

const promptMaxChars = computed(() => auth.me.value?.limits.prompt_max_chars ?? null)

const CHECK_AGENT = 'check'
/** The agents that are tabs; the check agent's settings are a section of the parse panel. */
const tabs = computed(() => agents.value.filter((agent) => agent.name !== CHECK_AGENT))
const checkAgent = computed(() => agents.value.find((agent) => agent.name === CHECK_AGENT) ?? null)

/** The tab that is open: the first agent until one is picked. */
const selectedName = ref<string | null>(null)
const selected = computed(
  () => tabs.value.find((agent) => agent.name === selectedName.value) ?? tabs.value[0] ?? null,
)

function agentTitle(agent: AgentEntry): string {
  return te(`agents.names.${agent.name}`) ? t(`agents.names.${agent.name}`) : agent.name
}

/** Arrow keys move between tabs, as a tab list is expected to. */
function onTabKeydown(event: KeyboardEvent, index: number) {
  const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0
  if (!step || tabs.value.length === 0) {
    return
  }
  event.preventDefault()
  const nextIndex = (index + step + tabs.value.length) % tabs.value.length
  const next = tabs.value[nextIndex]
  if (!next) {
    return
  }
  selectedName.value = next.name
  const list = (event.currentTarget as HTMLElement).parentElement
  list?.querySelectorAll<HTMLElement>('[role="tab"]')[nextIndex]?.focus()
}
</script>
<template>
  <SettingsSection :title="t('agents.headTitle')">
    <!-- What every agent runs on. Not connected, the row is the connect control; connected,
         the key's tail and Disconnect. -->
    <SettingsRow :label="t('agents.kanoConnected')">
      <UiSkeleton v-if="!kano && !kanoError" class="kano-wait" :rows="1" />
      <template v-else-if="kano?.connected">
        <p class="kano-line">
          <UiStatusDot class="kano-status" tone="ok" :label="t('agents.kanoConnectedState')" />
          <span class="mono kano-tail">{{ keyTail }}</span>
        </p>
        <button
          type="button"
          class="text-link danger"
          :disabled="disconnectPending"
          @click="confirmingDisconnect = true"
        >
          {{ t('agents.kanoDisconnect') }}
        </button>
      </template>
      <p v-else-if="kano && !kano.available" class="state">{{ t('agents.kanoUnavailable') }}</p>
      <UiButton v-else-if="kano" variant="primary" :href="connectHref">
        {{ t('agents.kanoConnect') }}
      </UiButton>

      <template v-if="kanoError || kanoOutcome === 'denied' || kanoOutcome === 'error'" #note>
        <UiBanner v-if="kanoError" tone="error">{{ kanoError }}</UiBanner>
        <p v-else-if="kanoOutcome === 'denied'" class="outcome" role="status">
          {{ t('agents.kanoOutcomeDenied') }}
        </p>
        <p v-else class="outcome" role="status">{{ t('agents.kanoOutcomeError') }}</p>
      </template>
    </SettingsRow>

    <template v-if="kano?.connected">
      <UiBanner v-if="agentsError" class="agents-state" tone="error">{{ agentsError }}</UiBanner>
      <UiSkeleton v-else-if="!agentsData" class="agents-state" :rows="2" />
      <template v-else>
        <div class="tabs" role="tablist" :aria-label="t('agents.tabs')">
          <button
            v-for="(agent, index) in tabs"
            :key="agent.name"
            type="button"
            role="tab"
            class="tab"
            :class="{ active: selected?.name === agent.name }"
            :aria-selected="selected?.name === agent.name"
            :tabindex="selected?.name === agent.name ? 0 : -1"
            @click="selectedName = agent.name"
            @keydown="onTabKeydown($event, index)"
          >
            <span class="tab-label">{{ agentTitle(agent) }}</span>
            <UiStatusDot
              :tone="agent.settings.model ? 'ok' : 'neutral'"
              :label="agent.settings.model ? t('agents.modelChosen') : t('agents.modelMissing')"
            />
          </button>
        </div>

        <!-- Positioned: the panel's drawers anchor to this box's right edge. -->
        <div v-if="selected" class="agent-region">
          <div class="agent-head">
            <h2 class="agent-title">{{ agentTitle(selected) }}</h2>
            <span class="mono io">{{ selected.input }} → {{ selected.output }}</span>
          </div>
          <AgentPanel
            :key="selected.name"
            :agent="selected"
            :check="checkAgent"
            :prompt-max-chars="promptMaxChars"
            @saved="setAgentSettings"
          />
        </div>
      </template>
    </template>

    <UiConfirmDialog
      v-if="confirmingDisconnect"
      :title="t('agents.kanoDisconnect')"
      :message="t('agents.kanoDisconnectConfirm')"
      :confirm-label="t('agents.kanoDisconnect')"
      :pending="disconnectPending"
      :error="disconnectError"
      @confirm="disconnectKano"
      @cancel="confirmingDisconnect = false"
    />
  </SettingsSection>
</template>

<style scoped>
/* --- The Kano Proxy row -------------------------------------------------------- */

.kano-wait {
  width: 160px;
}

.kano-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  color: var(--muted);
  font-size: var(--text-sm);
}

.kano-status {
  flex-shrink: 0;
}

.kano-status :deep(.label) {
  color: var(--muted);
}

.kano-tail {
  font-size: var(--text-xs);
}

.text-link {
  flex-shrink: 0;
  padding: 0;
  border: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: var(--text-sm);
  text-decoration: underline;
  text-underline-offset: 3px;
  cursor: pointer;
}

.text-link.danger {
  color: var(--danger);
}

.text-link:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.state {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-sm);
}

.outcome {
  margin: 0;
  color: var(--danger);
  font-size: var(--text-sm);
}

/* --- The tabs ------------------------------------------------------------------ */

.agents-state {
  margin-top: var(--space-6);
}

.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  padding: var(--space-6) 0 var(--space-2);
}

/* The same row the sidebar's nav uses: a fill and a step up in weight say "open",
   nothing else (docs/web.md § Design system). */
.tab {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink-soft);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.tab:hover {
  background: var(--rail-active);
  color: var(--ink);
}

.tab.active {
  background: var(--rail-active);
  color: var(--ink);
  font-weight: var(--weight-medium);
}

.tab-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* --- The selected agent ---------------------------------------------------------- */

/* The containing block of *What the agent receives*, which anchors to this box's right
   edge (docs/web.md § Pages). */
.agent-region {
  position: relative;
  padding-top: var(--space-4);
}

.agent-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-4);
  padding-bottom: var(--space-3);
}

.agent-title {
  font-size: var(--display-sm);
}

.io {
  color: var(--muted);
  font-size: var(--text-xs);
}
</style>
