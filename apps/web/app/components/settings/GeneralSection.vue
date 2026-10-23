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
    <h2 class="group">{{ t('settings.appearance') }}</h2>

    <SettingsRow :label="t('settings.theme.label')" :description="t('settings.theme.description')">
      <div class="segmented" role="radiogroup" :aria-label="t('settings.theme.label')">
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
      </div>
    </SettingsRow>

    <SettingsRow :label="t('settings.language.label')" :description="t('settings.language.description')">
      <label class="sr-only" :for="languageId">{{ t('settings.language.label') }}</label>
      <UiSelect :id="languageId" class="language" :model-value="locale" @update:model-value="switchLanguage">
        <option v-for="option in locales" :key="option.code" :value="option.code">
          {{ option.name ?? option.code }}
        </option>
      </UiSelect>
    </SettingsRow>
  </SettingsSection>
</template>

<style scoped>
/* A group heading over its rows: the serif, one step below the section title. */
.group {
  padding: var(--space-8) 0 var(--space-2);
  font-size: var(--display-xs);
}

/* Three choices in one box, the chosen one lifted to paper — the same vocabulary as the
   file library's tabs, not three buttons in a row. */
.segmented {
  display: inline-flex;
  padding: 3px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper-sunken);
}

.segment {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  height: calc(var(--control-height-sm) - 6px);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.segment:hover {
  color: var(--ink);
}

.segment.active {
  background: var(--paper);
  color: var(--ink);
  font-weight: var(--weight-medium);
  box-shadow: 0 0 0 1px var(--line-strong);
}

.segment :deep(svg) {
  width: 14px;
  height: 14px;
}

.language {
  width: auto;
  min-width: 180px;
}
</style>
