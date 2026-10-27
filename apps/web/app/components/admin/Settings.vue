<script setup lang="ts">
/**
 * The console's *Settings* screen (docs/admin.md § Pages, *Settings*; § Settings): the twelve
 * settings of `.env` the database may override, grouped in two columns — models, credits and
 * the Free plan at the left; prices, plans and packs at the right, then the keys of `.env`,
 * each only set or unset. A banner says when the stored values no longer fit `.env`.
 *
 * Each setting is changed on its own, from its dialog, and the screen then shows the settings
 * the API answered; nothing of it is kept in browser storage.
 */
import { getAdminSettings, type AdminSetting, type AdminSettingsResponse } from '~/lib/api'
import { SETTING_GROUPS } from '~/lib/admin'
import { formatCount } from '~/lib/format'

const { t, locale } = useI18n()
const adminConsole = useAdminConsole()
const { explain } = useAdminFailure()

const canWrite = computed(() => adminConsole.can('settings', 'write'))

const data = shallowRef<AdminSettingsResponse | null>(null)
const pending = ref(false)
const error = ref<string | null>(null)
/** Only the newest read may write. */
let seq = 0

async function load(): Promise<void> {
  const mine = ++seq
  pending.value = true
  try {
    const loaded = await getAdminSettings()
    if (mine === seq) {
      data.value = loaded
      error.value = null
    }
  } catch (failure) {
    const message = await explain(failure)
    if (mine === seq) {
      error.value = message
    }
  } finally {
    if (mine === seq) {
      pending.value = false
    }
  }
}

onMounted(load)

/** The groups of each column, each with the settings the API answered for its keys. */
const columns = computed(() => {
  const byKey = new Map((data.value?.settings ?? []).map((setting) => [setting.key, setting]))
  return (['start', 'end'] as const).map((column) =>
    SETTING_GROUPS.filter((group) => group.column === column)
      .map((group) => ({
        key: group.key,
        settings: group.settings
          .map((key) => byKey.get(key))
          .filter((setting): setting is AdminSetting => setting !== undefined),
      }))
      .filter((group) => group.settings.length > 0),
  )
})

const subtitle = computed(() =>
  data.value
    ? t('admin.settings.subtitle', { n: formatCount(data.value.cache_seconds, locale.value) }, data.value.cache_seconds)
    : '',
)

/** The setting a dialog is open on, and for what. */
const dialog = shallowRef<{ setting: AdminSetting; mode: 'edit' | 'restore' } | null>(null)

function onSaved(settings: AdminSettingsResponse) {
  // A change made shows the settings it answered; a read still running is older.
  seq += 1
  pending.value = false
  data.value = settings
  error.value = null
  dialog.value = null
}
</script>

<template>
  <UiScreen>
    <template #toolbar>
      <UiButton icon-only :label="t('common.refresh')" :loading="pending" @click="load">
        <template #icon><UiIcon name="refresh" /></template>
      </UiButton>
    </template>

    <template #title>
      <UiPageHeader :title="t('admin.settings.title')">
        <span v-if="data">{{ subtitle }}</span>
      </UiPageHeader>
    </template>

    <UiPanel>
      <UiBanner v-if="error" class="state" tone="error">
        {{ error }}
        <template #actions>
          <UiButton size="sm" @click="load">{{ t('common.retry') }}</UiButton>
        </template>
      </UiBanner>
      <UiSkeleton v-if="!data && !error" class="state" :rows="6" />
      <template v-else-if="data">
        <UiBanner v-if="data.ignored" class="state" tone="warn">
          {{ t('admin.settings.ignored', { message: data.ignored }) }}
        </UiBanner>
        <div class="layout">
          <div v-for="(column, index) in columns" :key="index" class="column">
            <section v-for="group in column" :key="group.key" class="group">
              <h2 class="group-title">{{ t(`admin.settings.groups.${group.key}`) }}</h2>
              <AdminSettingRow
                v-for="setting in group.settings"
                :key="setting.key"
                :setting="setting"
                :ceilings="data.ceilings"
                :can-write="canWrite"
                @edit="dialog = { setting, mode: 'edit' }"
                @restore="dialog = { setting, mode: 'restore' }"
              />
            </section>
            <section v-if="index === columns.length - 1" class="group">
              <h2 class="group-title">{{ t('admin.settings.groups.keys') }}</h2>
              <ul class="secrets">
                <li v-for="secret in data.secrets" :key="secret.key" class="secret">
                  <span class="mono secret-key">{{ secret.key }}</span>
                  <UiStatusDot
                    :tone="secret.set ? 'ok' : 'neutral'"
                    :label="secret.set ? t('admin.settings.secretSet') : t('admin.settings.secretUnset')"
                  />
                </li>
              </ul>
              <p class="note">{{ t('admin.settings.secretsNote') }}</p>
            </section>
          </div>
        </div>
      </template>
    </UiPanel>

    <AdminSettingDialog
      v-if="dialog && canWrite"
      :key="`${dialog.mode}-${dialog.setting.key}`"
      :setting="dialog.setting"
      :mode="dialog.mode"
      @close="dialog = null"
      @saved="onSaved"
    />
  </UiScreen>
</template>

<style scoped>
.state {
  margin: var(--space-4) 0;
}

/* Two columns, as the approved board draws them; stacked under the breakpoint. */
.layout {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: var(--space-6);
  padding-top: var(--space-1);
}

.column {
  display: grid;
  gap: var(--space-6);
  min-width: 0;
}

/* On paper, as every list of the app: the tables' sticky headers are paper too. */
.group {
  display: grid;
  min-width: 0;
}

.group-title {
  margin-bottom: var(--space-1);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  letter-spacing: normal;
}

.secrets {
  display: grid;
  gap: var(--space-2);
  margin: var(--space-2) 0;
  padding: 0;
  list-style: none;
}

.secret {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-width: 0;
  font-size: var(--text-sm);
}

.secret-key {
  min-width: 0;
  overflow-wrap: anywhere;
  font-size: var(--text-xs);
}

.note {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

@media (max-width: 900px) {
  .layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
