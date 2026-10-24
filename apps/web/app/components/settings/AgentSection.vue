<script setup lang="ts">
/**
 * Agent (docs/web.md § Pages, `/settings/agent`): every agent runs on the operator's Kano
 * Proxy, so there is nothing to connect — the section is a tab row over the selected
 * agent's panel (`AgentPanel`) and nothing else. The `check` agent is no tab: its settings
 * are the parse panel's *Format check*.
 */
import { listAgents, type AgentEntry, type SavedAgentSettings } from '~/lib/api'

const { t, te } = useI18n()
const auth = useAuth()

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
    <UiBanner v-if="agentsError" class="agents-state" tone="error">{{ agentsError }}</UiBanner>
    <UiSkeleton v-else-if="!agentsData" class="agents-state" :rows="2" />
    <template v-else>
      <UiSegmented class="tabs" role="tablist" :aria-label="t('agents.tabs')">
        <button
          v-for="(agent, index) in tabs"
          :key="agent.name"
          type="button"
          role="tab"
          class="segment"
          :class="{ active: selected?.name === agent.name }"
          :aria-selected="selected?.name === agent.name"
          :tabindex="selected?.name === agent.name ? 0 : -1"
          @click="selectedName = agent.name"
          @keydown="onTabKeydown($event, index)"
        >
          {{ agentTitle(agent) }}
        </button>
      </UiSegmented>

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
  </SettingsSection>
</template>

<style scoped>
.agents-state {
  margin-top: var(--space-5);
}

/* --- The tabs ------------------------------------------------------------------ */

.tabs {
  align-self: flex-start;
  margin-top: var(--space-5);
}

/* --- The selected agent ---------------------------------------------------------- */

/* The containing block of *What the agent receives*, which anchors to this box's right
   edge (docs/web.md § Pages). */
.agent-region {
  position: relative;
  padding-top: var(--space-6);
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
