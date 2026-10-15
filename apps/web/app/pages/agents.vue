<script setup lang="ts">
/**
 * Agent (docs/web.md § Pages): every agent runs on the user's own Kano Proxy, so the page
 * has two states. Not connected, it is one control — the connect link, nothing else.
 * Connected, the rail lists the agents as tabs with the Kano Proxy line at its foot, and
 * the data region shows the selected agent's panel (`AgentPanel`) and nothing else.
 */
import {
  deleteKano,
  getKano,
  kanoConnectUrl,
  listAgents,
  listPrompts,
  type AgentEntry,
  type SavedAgentSettings,
} from '~/lib/api'

definePageMeta({ middleware: 'authed' })

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

function setAgentSettings(name: string, saved: SavedAgentSettings) {
  const { bbox, ...settings } = saved
  agents.value = agents.value.map((agent) =>
    agent.name === name ? { ...agent, settings, bbox } : agent,
  )
}

const {
  data: promptsData,
  errorMessage: promptsError,
  refresh: refreshPrompts,
} = useAuthedData(() => listPrompts())
const prompts = computed(() => promptsData.value?.prompts ?? [])

const promptMaxChars = computed(() => auth.me.value?.limits.prompt_max_chars ?? null)

/** The tab that is open: the first agent until one is picked. */
const selectedName = ref<string | null>(null)
const selected = computed(
  () => agents.value.find((agent) => agent.name === selectedName.value) ?? agents.value[0] ?? null,
)

function agentTitle(agent: AgentEntry): string {
  return te(`agents.names.${agent.name}`) ? t(`agents.names.${agent.name}`) : agent.name
}

/** Arrow keys move between tabs, as a tab list is expected to. */
function onTabKeydown(event: KeyboardEvent, index: number) {
  const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0
  if (!step || agents.value.length === 0) {
    return
  }
  event.preventDefault()
  const nextIndex = (index + step + agents.value.length) % agents.value.length
  const next = agents.value[nextIndex]
  if (!next) {
    return
  }
  selectedName.value = next.name
  const list = (event.currentTarget as HTMLElement).parentElement
  list?.querySelectorAll<HTMLElement>('[role="tab"]')[nextIndex]?.focus()
}
</script>

<template>
  <UiScreen v-if="!kano || !kano.connected" full>
    <!-- Not connected: one control on the whole screen. -->
    <UiRegion>
      <UiPanel section>
        <div class="connect">
          <UiBanner v-if="kanoError" tone="error">{{ kanoError }}</UiBanner>
          <UiSkeleton v-else-if="!kano" :rows="1" />
          <p v-else-if="!kano.available" class="state">{{ t('agents.kanoUnavailable') }}</p>
          <template v-else>
            <p v-if="kanoOutcome === 'denied'" class="outcome" role="status">
              {{ t('agents.kanoOutcomeDenied') }}
            </p>
            <p v-else-if="kanoOutcome === 'error'" class="outcome" role="status">
              {{ t('agents.kanoOutcomeError') }}
            </p>
            <UiButton variant="primary" :href="connectHref">{{ t('agents.kanoConnect') }}</UiButton>
          </template>
        </div>
      </UiPanel>
    </UiRegion>
  </UiScreen>

  <UiScreen v-else>
    <!-- Connected: the agents as tabs, the selected one's panel. -->
    <UiRail>
      <UiPageHeader :title="t('agents.headTitle')" />

      <UiBanner v-if="agentsError" tone="error">{{ agentsError }}</UiBanner>
      <UiSkeleton v-else-if="!agentsData" :rows="2" />
      <div v-else class="tabs" role="tablist" :aria-label="t('agents.tabs')" aria-orientation="vertical">
        <button
          v-for="(agent, index) in agents"
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

      <!-- The foot: what every agent runs on. The key's tail and Disconnect appear on
           hover or focus, and always where there is no hover. -->
      <div class="kano">
        <p class="kano-line">
          <UiStatusDot tone="ok" :label="t('agents.kanoConnected')" />
          <span>{{ t('agents.kanoConnected') }}</span>
          <span class="mono reveal">{{ keyTail }}</span>
        </p>
        <button
          type="button"
          class="text-link danger reveal"
          :disabled="disconnectPending"
          @click="confirmingDisconnect = true"
        >
          {{ t('agents.kanoDisconnect') }}
        </button>
      </div>
    </UiRail>

    <UiRegion>
      <UiPanel v-if="selected" :title="agentTitle(selected)" lead>
        <template #meta>
          <span class="mono io">{{ selected.input }} → {{ selected.output }}</span>
        </template>
        <UiBanner v-if="promptsError" class="panel-banner" tone="error">{{ promptsError }}</UiBanner>
        <AgentPanel
          :key="selected.name"
          :agent="selected"
          :prompts="prompts"
          :prompts-loaded="promptsData !== null"
          :prompt-max-chars="promptMaxChars"
          @saved="(saved) => setAgentSettings(selected!.name, saved)"
          @prompts-changed="refreshPrompts"
        />
      </UiPanel>
      <UiPanel v-else section>
        <UiBanner v-if="agentsError" tone="error">{{ agentsError }}</UiBanner>
        <UiSkeleton v-else :rows="3" />
      </UiPanel>
    </UiRegion>

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
  </UiScreen>
</template>

<style scoped>
/* --- Not connected ---------------------------------------------------------- */

.connect {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-4);
  min-height: 60vh;
}

.outcome {
  margin: 0;
  color: var(--danger);
  font-size: var(--text-sm);
}

.state {
  margin: 0;
  color: var(--muted);
}

/* --- Rail: the tabs ---------------------------------------------------------- */

.tabs {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* The same row the sidebar's nav uses: a fill and a step up in weight say "open",
   nothing else (docs/web.md § Design system). */
.tab {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--control-height);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink-soft);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
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
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* --- Rail: the Kano Proxy line ---------------------------------------------- */

.kano {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  margin-top: auto;
  padding-top: var(--space-4);
  border-top: 1px solid var(--line);
}

.kano-line {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  min-width: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

.kano-line .mono {
  font-size: var(--text-2xs);
}

.reveal {
  opacity: 0;
  transition: opacity var(--duration-fast) var(--ease);
}

.kano:hover .reveal,
.kano:focus-within .reveal {
  opacity: 1;
}

@media (hover: none) {
  .reveal {
    opacity: 1;
  }
}

.text-link {
  flex-shrink: 0;
  padding: 0;
  border: 0;
  background: none;
  color: var(--muted);
  font: inherit;
  font-size: var(--text-xs);
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

/* --- Region -------------------------------------------------------------------- */

.io {
  color: var(--muted);
  font-size: var(--text-xs);
}

.panel-banner {
  margin-bottom: var(--space-4);
  max-width: 72ch;
}

@media (max-width: 900px) {
  .kano {
    margin-top: var(--space-4);
  }
}
</style>
