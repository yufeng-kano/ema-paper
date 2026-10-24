<script setup lang="ts">
/**
 * General (docs/web.md § Pages, `/settings/<section>`): the theme and the language.
 *
 * Theme is a three-way segmented control, kept in `localStorage` by `useTheme`. Language is
 * a native select whose change is navigation — the locale lives in the URL — so it goes
 * through `switchLocalePath` and keeps the reader on this section.
 */
import { THEMES, type Theme } from '~/composables/useTheme'
import type { IconName } from '~/lib/icons'

const { t, locale, locales } = useI18n()
const switchLocalePath = useSwitchLocalePath()
const { theme, set } = useTheme()

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
  </SettingsSection>
</template>

<style scoped>
.language {
  width: auto;
  min-width: 180px;
}
</style>
