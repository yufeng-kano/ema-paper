<script setup lang="ts">
/**
 * General (docs/web.md § Pages, `/settings/<section>`): the theme, the language and the
 * reader's body margins.
 *
 * Theme is a three-way segmented control, kept in `localStorage` by `useTheme`. Language is
 * a native select whose change is navigation — the locale lives in the URL — so it goes
 * through `switchLocalePath` and keeps the reader on this section. The margins are the
 * result viewer's own state (`useViewerLayout`), so a viewer on screen follows a step at
 * once (docs/web.md § Result viewer, *Body margins*).
 */
import { THEMES, type Theme } from '~/composables/useTheme'
import { PAPER_MARGINS } from '~/composables/useViewerLayout'
import type { IconName } from '~/lib/icons'

const { t, locale, locales } = useI18n()
const switchLocalePath = useSwitchLocalePath()
const { theme, set } = useTheme()
const { paperMargin } = useViewerLayout()

const THEME_ICONS: Record<Theme, IconName> = { system: 'monitor', light: 'sun', dark: 'moon' }

const languageId = useId()

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
      <SettingsRow :label="t('settings.reader.margins.label')" :description="t('settings.reader.margins.description')">
        <UiSegmented role="radiogroup" :aria-label="t('settings.reader.margins.label')">
          <button
            v-for="step in PAPER_MARGINS"
            :key="step"
            type="button"
            role="radio"
            class="segment"
            :class="{ active: paperMargin === step }"
            :aria-checked="paperMargin === step"
            @click="paperMargin = step"
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
