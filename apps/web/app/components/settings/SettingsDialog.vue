<script setup lang="ts">
/**
 * The settings dialog (docs/web.md § Pages, *The settings dialog*).
 *
 * A `UiModal` over whichever screen the account menu was opened from — the screen stays
 * behind the scrim, so closing the dialog is just closing it. Inside, the section list at
 * the left and the open section's column at the right, each scrolling on its own. Which
 * section is open is the route's `?settings=<section>` query, so a section can be linked;
 * the dialog's own navigation
 * replaces the query rather than pushing it, so Back leaves the dialog in one step.
 *
 * Escape reaches this dialog only when nothing over it owns the key: `UiModal` yields to a
 * dialog opened on top of this one, to a menu, and to the *What the agent receives* drawer.
 */
import { SETTINGS_QUERY, SETTINGS_SECTIONS, type SettingsSection } from '~/lib/settings'

defineProps<{ section: SettingsSection }>()

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

function open(key: SettingsSection) {
  void router.replace({ query: { ...route.query, [SETTINGS_QUERY]: key } })
}

function close() {
  const { [SETTINGS_QUERY]: _settings, ...query } = route.query
  void router.replace({ query })
}
</script>

<template>
  <UiModal :title="t('settings.title')" size="lg" tall flush @close="close">
    <div class="sheet">
      <nav class="sections" :aria-label="t('settings.title')">
        <button
          v-for="entry in SETTINGS_SECTIONS"
          :key="entry.key"
          type="button"
          class="section-item"
          :class="{ active: section === entry.key }"
          :aria-current="section === entry.key ? 'page' : undefined"
          @click="open(entry.key)"
        >
          <UiIcon :name="entry.icon" />
          <span class="section-label">{{ t(`settings.sections.${entry.key}`) }}</span>
        </button>
      </nav>

      <div class="column">
        <SettingsGeneralSection v-if="section === 'general'" />
        <SettingsAccountSection v-else-if="section === 'account'" />
        <SettingsAgentSection v-else-if="section === 'agent'" />
        <SettingsKeysSection v-else-if="section === 'keys'" />
        <SettingsMcpSection v-else-if="section === 'mcp'" />
        <SettingsUsageSection v-else-if="section === 'usage'" />
      </div>
    </div>
  </UiModal>
</template>

<style scoped>
/* The modal's body is a bounded box (`tall`); the sheet splits it into the list and the
   column, each with its own scroll. */
.sheet {
  display: grid;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  height: 100%;
  min-height: 0;
}

/* --- The section list -------------------------------------------------------- */

.sections {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
  padding: var(--space-5) var(--space-4);
  background: var(--rail);
  border-right: 1px solid var(--line);
  overflow-y: auto;
}

/* The sidebar's nav row, exactly: a fill and a weight step say "open", nothing else. */
.section-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--control-height-sm);
  padding: 0 var(--space-2) 0 10px;
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-sm);
  text-align: left;
  white-space: nowrap;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.section-item:hover {
  background: var(--rail-active);
  color: var(--ink);
}

.section-item.active {
  background: var(--rail-active);
  color: var(--ink);
  font-weight: var(--weight-semibold);
}

.section-item :deep(svg) {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
}

/* --- The content column ------------------------------------------------------- */

.column {
  min-width: 0;
  min-height: 0;
  padding: var(--space-8) var(--space-10) var(--space-12);
  overflow-y: auto;
  overscroll-behavior: contain;
}

/* The section's content is a reading column, not a full-width data region. */
.column > :deep(.section) {
  max-width: 760px;
}

@media (max-width: 900px) {
  /* The section list becomes one row of tabs over the content, scrolling sideways when
     they do not fit. */
  .sheet {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
  }

  .sections {
    flex-direction: row;
    gap: var(--space-1);
    padding: var(--space-3) var(--space-3);
    border-right: 0;
    border-bottom: 1px solid var(--line);
    overflow-x: auto;
    overflow-y: hidden;
    overscroll-behavior-x: contain;
  }

  .section-item {
    flex-shrink: 0;
  }

  .column {
    padding: var(--space-6) var(--space-4) var(--space-10);
  }
}
</style>
