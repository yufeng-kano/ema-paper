<script setup lang="ts">
/**
 * Preferences (docs/web.md § Pages, `/settings/<section>`): what a PDF or an image becomes,
 * and how it opens. *Convert on upload* is the parse agent's `auto_convert`, read with the
 * agents and saved on change (docs/agents.md) — off, an upload is kept as it is and
 * converted when the reader asks for Markdown in its viewer. *Open on* is the reader's
 * `default_view` (`PUT /api/me/preferences`): the view a converted PDF or image opens on
 * the first time, since a document remembers the view it was last read in (docs/web.md
 * § Result viewer, *Original and Markdown*). The session carries both (`GET /api/me`), so
 * the Files page and the viewer follow a save at once.
 */
import { listAgents, putAgentSettings, putPreferences, type AgentEntry, type DocumentView } from '~/lib/api'

const { t } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

const VIEWS: readonly DocumentView[] = ['original', 'markdown']

// --- convert on upload ------------------------------------------------------------------

const PARSE_AGENT = 'parse'
const { data: agentsData, errorMessage: agentsError } = useAuthedData(() => listAgents())
/** The parse agent's entry, as read, then as each save answers it. */
const parseAgent = ref<AgentEntry | null>(null)
watch(agentsData, (data) => {
  parseAgent.value = data?.agents.find((agent) => agent.name === PARSE_AGENT) ?? null
})

/** The click being saved; the stored value otherwise. Put back when the save fails. */
const convertChoice = ref<boolean | null>(null)
/** Off until the reader turns it on, as the server reads an unset switch. */
const convertOn = computed(() => convertChoice.value ?? parseAgent.value?.settings.auto_convert ?? false)
const convertError = ref<string | null>(null)

async function toggleConvert(next: boolean) {
  const agent = parseAgent.value
  if (convertChoice.value !== null || !agent) {
    return
  }
  convertChoice.value = next
  convertError.value = null
  try {
    const { bbox, ...settings } = await putAgentSettings(PARSE_AGENT, { auto_convert: next })
    parseAgent.value = { ...agent, settings, bbox }
    // The Files page reads the switch from the session (`GET /api/me`), so it follows at once.
    if (auth.me.value) {
      auth.me.value = { ...auth.me.value, auto_convert: settings.auto_convert ?? next }
    }
  } catch (failure) {
    convertError.value = await resolve(failure)
  } finally {
    convertChoice.value = null
  }
}

// --- open on ----------------------------------------------------------------------------

/** The choice being saved; the session's otherwise. Put back when the save fails. */
const viewChoice = ref<DocumentView | null>(null)
const defaultView = computed<DocumentView>(() => viewChoice.value ?? auth.me.value?.default_view ?? 'markdown')
const viewError = ref<string | null>(null)

async function chooseView(next: DocumentView) {
  if (viewChoice.value !== null || next === defaultView.value) {
    return
  }
  viewChoice.value = next
  viewError.value = null
  try {
    const saved = await putPreferences({ default_view: next })
    // The viewer reads the default from the session, so the next opening follows at once.
    if (auth.me.value) {
      auth.me.value = { ...auth.me.value, default_view: saved.default_view }
    }
  } catch (failure) {
    viewError.value = await resolve(failure)
  } finally {
    viewChoice.value = null
  }
}
</script>

<template>
  <SettingsSection :title="t('settings.sections.preferences')">
    <SettingsGroup :title="t('settings.preferences.title')">
      <SettingsRow
        :label="t('settings.preferences.autoConvert.label')"
        :description="t('settings.preferences.autoConvert.description')"
      >
        <UiSwitch
          :model-value="convertOn"
          :label="t('settings.preferences.autoConvert.label')"
          :busy="convertChoice !== null"
          :disabled="!parseAgent"
          @update:model-value="toggleConvert"
        />
        <template v-if="convertError || agentsError" #note>
          <UiBanner tone="error">{{ convertError ?? agentsError }}</UiBanner>
        </template>
      </SettingsRow>

      <SettingsRow
        :label="t('settings.preferences.defaultView.label')"
        :description="t('settings.preferences.defaultView.description')"
      >
        <UiSegmented role="radiogroup" :aria-label="t('settings.preferences.defaultView.label')">
          <button
            v-for="option in VIEWS"
            :key="option"
            type="button"
            role="radio"
            class="segment"
            :class="{ active: defaultView === option }"
            :aria-checked="defaultView === option"
            :disabled="viewChoice !== null"
            @click="chooseView(option)"
          >
            {{ t(`settings.preferences.defaultView.${option}`) }}
          </button>
        </UiSegmented>
        <template v-if="viewError" #note>
          <UiBanner tone="error">{{ viewError }}</UiBanner>
        </template>
      </SettingsRow>

      <template #foot>
        <p>{{ t('settings.preferences.foot') }}</p>
      </template>
    </SettingsGroup>
  </SettingsSection>
</template>
