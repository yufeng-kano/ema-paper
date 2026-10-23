<script setup lang="ts">
/**
 * The settings sheet (docs/web.md § Pages, `/settings/<section>`).
 *
 * Not a page in the signed-in shell but a sheet over the desk: `layout: false`, no sidebar,
 * one centred panel of a section list and the open section's column. Each section is its
 * own path, so a section can be linked and the Kano Proxy connect flow has somewhere to
 * return to. The X — and Escape, when no dialog, menu or drawer is open to take it — goes
 * back to the route the account menu opened the sheet from, or to `/files` on a deep link.
 */
import type { IconName } from '~/lib/icons'

definePageMeta({ layout: false, middleware: 'authed' })

const SECTIONS = [
  { key: 'general', icon: 'settings' },
  { key: 'account', icon: 'user' },
  { key: 'agent', icon: 'agent' },
  { key: 'keys', icon: 'keys' },
  { key: 'mcp', icon: 'connect' },
  { key: 'usage', icon: 'dashboard' },
] as const satisfies readonly { key: string; icon: IconName }[]
type SectionKey = (typeof SECTIONS)[number]['key']

const { t } = useI18n()
const route = useRoute()
const localePath = useLocalePath()
const returnTo = useSettingsReturn()

const section = computed<SectionKey | null>(() => {
  const raw = route.params.section
  const key = Array.isArray(raw) ? raw[0] : raw
  return SECTIONS.some((entry) => entry.key === key) ? (key as SectionKey) : null
})

// An unknown section is not an error page: it opens General, as the menu does.
watch(
  section,
  (current) => {
    if (!current) {
      void navigateTo(localePath('/settings/general'), { replace: true })
    }
  },
  { immediate: true },
)

function close() {
  const to = returnTo.value ?? localePath('/files')
  returnTo.value = null
  void navigateTo(to)
}

/** Escape closes the sheet from anywhere in it, unless something over it — a dialog, a
 *  menu, the *What the agent receives* drawer — is open and owns that Escape. */
function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing) {
    return
  }
  if (document.querySelector('[aria-modal="true"], [role="menu"], .drawer')) {
    return
  }
  close()
}

onMounted(() => document.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => document.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="desk">
    <div class="sheet">
      <aside class="sections">
        <p class="eyebrow sections-label">{{ t('settings.title') }}</p>
        <nav class="section-nav" :aria-label="t('settings.title')">
          <NuxtLink
            v-for="entry in SECTIONS"
            :key="entry.key"
            class="section-item"
            :class="{ active: section === entry.key }"
            :to="localePath(`/settings/${entry.key}`)"
            :aria-current="section === entry.key ? 'page' : undefined"
          >
            <UiIcon :name="entry.icon" />
            <span class="section-label">{{ t(`settings.sections.${entry.key}`) }}</span>
          </NuxtLink>
        </nav>
      </aside>

      <div class="content">
        <UiButton
          class="close"
          variant="ghost"
          size="sm"
          icon-only
          :label="t('common.close')"
          @click="close"
        >
          <template #icon><UiIcon name="close" /></template>
        </UiButton>

        <main id="content" class="column" tabindex="-1">
          <SettingsGeneralSection v-if="section === 'general'" />
          <SettingsAccountSection v-else-if="section === 'account'" />
          <SettingsAgentSection v-else-if="section === 'agent'" />
          <SettingsKeysSection v-else-if="section === 'keys'" />
          <SettingsMcpSection v-else-if="section === 'mcp'" />
          <SettingsUsageSection v-else-if="section === 'usage'" />
        </main>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* The desk, with the sheet centred on it: the same frame the viewer's full dialog has,
   as a page of its own rather than an overlay, since there is no page behind it. */
.desk {
  display: grid;
  place-items: center;
  height: 100dvh;
  padding: var(--space-6);
  background: var(--desk);
  overflow: hidden;
}

.sheet {
  display: grid;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  width: 100%;
  max-width: 1180px;
  height: 100%;
  max-height: 860px;
  min-height: 0;
  background: var(--paper);
  border: 1px solid var(--line-strong);
  box-shadow: var(--shadow-dialog);
}

/* --- The section list -------------------------------------------------------- */

.sections {
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: var(--space-7) var(--space-5);
  background: var(--rail);
  border-right: 1px solid var(--line);
  overflow-y: auto;
}

.sections-label {
  padding: 0 10px;
  margin-bottom: var(--space-3);
}

.section-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* The sidebar's nav row, exactly: a fill and a weight step say "open", nothing else. */
.section-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--control-height-sm);
  padding: 0 var(--space-2) 0 10px;
  border-radius: var(--radius);
  color: var(--muted);
  font-size: var(--text-sm);
  white-space: nowrap;
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

.content {
  position: relative;
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  min-width: 0;
  min-height: 0;
}

.close {
  position: absolute;
  top: var(--space-4);
  right: var(--space-4);
  z-index: 1;
}

.column {
  min-width: 0;
  min-height: 0;
  padding: var(--space-10) var(--space-12) var(--space-12);
  overflow-y: auto;
  overscroll-behavior: contain;
}

/* The section's content is a reading column, not a full-width data region. */
.column > :deep(.section) {
  max-width: 760px;
}

@media (max-width: 900px) {
  /* The sheet fills the viewport and the section list becomes one row of tabs over the
     content, scrolling sideways when they do not fit. */
  .desk {
    padding: 0;
  }

  .sheet {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
    max-height: none;
    border: 0;
    box-shadow: none;
  }

  .sections {
    padding: var(--space-4) var(--space-4) 0;
    border-right: 0;
    border-bottom: 1px solid var(--line);
    overflow: visible;
  }

  .sections-label {
    /* The close button sits over the row's right end. */
    padding-right: var(--space-10);
  }

  .section-nav {
    flex-direction: row;
    gap: var(--space-1);
    padding-bottom: var(--space-3);
    overflow-x: auto;
    overscroll-behavior-x: contain;
  }

  .column {
    padding: var(--space-6) var(--space-4) var(--space-10);
  }
}
</style>
