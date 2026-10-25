<script setup lang="ts">
/**
 * General (docs/web.md § Pages, `/settings/<section>`): the theme, the language and the
 * reader's column widths.
 *
 * Theme is a three-way segmented control, kept in `localStorage` by `useTheme`. Language is
 * a native select whose change is navigation — the locale lives in the URL — so it goes
 * through `switchLocalePath` and keeps the reader on this section. The widths are the result
 * viewer's own state (`useViewerLayout`), so a viewer on screen follows a step at once
 * (docs/web.md § Result viewer, *Column widths*).
 */
import { THEMES, type Theme } from '~/composables/useTheme'
import { COLUMN_WIDTHS, WIDTH_STEPS, widthStep } from '~/lib/columns'
import type { IconName } from '~/lib/icons'

const { t, locale, locales } = useI18n()
const switchLocalePath = useSwitchLocalePath()
const { theme, set } = useTheme()
const { chosenWidth, chooseWidth } = useViewerLayout()

const THEME_ICONS: Record<Theme, IconName> = { system: 'monitor', light: 'sun', dark: 'moon' }

const languageId = useId()

/** Which step each card's chosen width is — never the width a narrow window draws it at —
 *  or null after a drag left it between them. */
const notesStep = computed(() => widthStep('notes', chosenWidth.notes))
const agentStep = computed(() => widthStep('agent', chosenWidth.agent))

function switchLanguage(code: string) {
  if (code !== locale.value) {
    void navigateTo(switchLocalePath(code as typeof locale.value))
  }
}
</script>

<template>
  <SettingsSection :title="t('settings.sections.general')">
    <SettingsGroup :title="t('settings.appearance')">
      <SettingsRow :label="t('settings.theme.label')" :description="t('settings.theme.description')">
        <UiSegmented role="radiogroup" :aria-label="t('settings.theme.label')">
          <button
            v-for="option in THEMES"
            :key="option"
            type="button"
            role="radio"
            class="segment"
            :class="{ active: theme === option }"
            :aria-checked="theme === option"
            @click="set(option)"
          >
            <UiIcon :name="THEME_ICONS[option]" />
            <span>{{ t(`settings.theme.${option}`) }}</span>
          </button>
        </UiSegmented>
      </SettingsRow>

      <SettingsRow :label="t('settings.language.label')" :description="t('settings.language.description')">
        <label class="sr-only" :for="languageId">{{ t('settings.language.label') }}</label>
        <UiSelect :id="languageId" class="language" :model-value="locale" @update:model-value="switchLanguage">
          <option v-for="option in locales" :key="option.code" :value="option.code">
            {{ option.name ?? option.code }}
          </option>
        </UiSelect>
      </SettingsRow>
    </SettingsGroup>

    <SettingsGroup :title="t('settings.reader.title')">
      <SettingsRow
        :label="t('settings.reader.notes.label')"
        :description="notesStep ? t('settings.reader.notes.description') : t('settings.reader.dragged')"
      >
        <UiSegmented role="radiogroup" :aria-label="t('settings.reader.notes.label')">
          <button
            v-for="step in WIDTH_STEPS"
            :key="step"
            type="button"
            role="radio"
            class="segment"
            :class="{ active: notesStep === step }"
            :aria-checked="notesStep === step"
            @click="chooseWidth('notes', COLUMN_WIDTHS.notes.steps[step])"
          >
            {{ t(`settings.reader.${step}`) }}
          </button>
        </UiSegmented>
      </SettingsRow>

      <SettingsRow
        :label="t('settings.reader.agent.label')"
        :description="agentStep ? t('settings.reader.agent.description') : t('settings.reader.dragged')"
      >
        <UiSegmented role="radiogroup" :aria-label="t('settings.reader.agent.label')">
          <button
            v-for="step in WIDTH_STEPS"
            :key="step"
            type="button"
            role="radio"
            class="segment"
            :class="{ active: agentStep === step }"
            :aria-checked="agentStep === step"
            @click="chooseWidth('agent', COLUMN_WIDTHS.agent.steps[step])"
          >
            {{ t(`settings.reader.${step}`) }}
          </button>
        </UiSegmented>
      </SettingsRow>

      <template #foot>
        <p>{{ t('settings.reader.foot') }}</p>
      </template>
    </SettingsGroup>
  </SettingsSection>
</template>

<style scoped>
.language {
  width: auto;
  min-width: 180px;
}
</style>
