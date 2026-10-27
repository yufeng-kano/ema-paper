<script setup lang="ts">
/**
 * *Edit* and *Restore to .env* for one setting (docs/admin.md § Pages, *Settings*; § Settings).
 *
 * Editing opens on the text in effect — a JSON table pretty-printed, its keys in their order
 * — in a text field, or a mono text area for a table, with `.env`'s text under it; *Save* is
 * offered once the text differs from what was opened, and sends it as typed. Restoring asks
 * first, showing the text `.env` holds.
 *
 * A 422 stays here in the start checks' words, and the text can be changed and sent again. A
 * 409 `impact` turns the dialog into its confirmation: what the change removes and, for each
 * count, what it does to those accounts, with *Save anyway* (*Restore anyway*), which sends
 * the same change with `confirm`, and *Back*. A change made closes the dialog with the
 * settings the API answered. The dialog cannot be dismissed, nor left by Back, while it sends.
 */
import {
  ApiRequestError,
  putAdminSetting,
  resetAdminSetting,
  type AdminSetting,
  type AdminSettingImpact,
  type AdminSettingsResponse,
} from '~/lib/api'
import { editableText, impactLines, isJsonSetting, settingImpact } from '~/lib/admin'
import { formatCount } from '~/lib/format'

/** The most a stored value may hold, in characters (docs/admin.md § Settings). */
const VALUE_MAX = 65_536

const props = defineProps<{
  setting: AdminSetting
  mode: 'edit' | 'restore'
}>()

const emit = defineEmits<{ close: []; saved: [settings: AdminSettingsResponse] }>()

const { t, locale } = useI18n()
const router = useRouter()
const { explain } = useAdminFailure()

const label = computed(() => t(`admin.settings.keys.${props.setting.key}`))
const json = computed(() => isJsonSetting(props.setting.key))
const opened = json.value ? editableText(props.setting.value) : props.setting.value
const text = ref(opened)
const pending = ref(false)
const error = ref<string | null>(null)
/** What the change takes away, once the API asked for a confirmation. */
const impact = shallowRef<AdminSettingImpact | null>(null)

const title = computed(() =>
  props.mode === 'edit'
    ? t('admin.settings.editTitle', { name: label.value })
    : t('admin.settings.restoreTitle', { name: label.value }),
)
const envText = computed(() => props.setting.env_value || t('admin.settings.empty'))
const changed = computed(() => text.value !== opened)

const removed = computed(() => {
  const found = impact.value?.removed
  if (!found) {
    return []
  }
  return [
    found.models.length ? t('admin.settings.removedModels', { list: found.models.join(', ') }) : null,
    found.plans.length ? t('admin.settings.removedPlans', { list: found.plans.join(', ') }) : null,
    found.packs.length ? t('admin.settings.removedPacks', { list: found.packs.join(', ') }) : null,
  ].filter((line): line is string => line !== null)
})

const lines = computed(() =>
  impact.value
    ? impactLines(impact.value).map(({ kind, n }) => t(`admin.settings.impact.${kind}`, { n: formatCount(n, locale.value) }, n))
    : [],
)

watch(text, () => {
  error.value = null
})

async function send(confirm: boolean) {
  if (pending.value || (props.mode === 'edit' && !changed.value)) {
    return
  }
  pending.value = true
  error.value = null
  // Back would close the dialog while the request runs, and its answer would land unseen.
  const release = router.beforeEach(() => false)
  let settings: AdminSettingsResponse | null = null
  let failure: unknown = null
  try {
    settings =
      props.mode === 'edit'
        ? await putAdminSetting(props.setting.key, text.value, confirm)
        : await resetAdminSetting(props.setting.key, confirm)
  } catch (caught) {
    failure = caught
  } finally {
    // Released before any failure is explained: a 401's way back to the landing page
    // must not be the navigation the guard refuses.
    release()
  }
  if (settings) {
    pending.value = false
    emit('saved', settings)
    return
  }
  const asked = settingImpact(failure)
  if (asked) {
    impact.value = asked
  } else if (failure instanceof ApiRequestError && failure.status === 422) {
    // The start checks' own words: which value, and why (never a secret's).
    impact.value = null
    error.value = failure.message
  } else {
    error.value = await explain(failure)
  }
  pending.value = false
}

function back() {
  if (props.mode === 'restore') {
    emit('close')
    return
  }
  impact.value = null
}
</script>

<template>
  <UiModal :title="title" :size="json && mode === 'edit' && !impact ? 'wide' : 'sm'" :busy="pending" @close="emit('close')">
    <template #title>
      <div class="heading">
        <h2 class="title">{{ impact ? t('admin.settings.impactTitle') : title }}</h2>
        <p class="key mono">{{ setting.key }}</p>
      </div>
    </template>

    <div v-if="impact" class="impact">
      <p class="lead">{{ t('admin.settings.impactBody') }}</p>
      <ul class="lines">
        <li v-for="line in removed" :key="line" class="removed">{{ line }}</li>
        <li v-for="line in lines" :key="line">{{ line }}</li>
      </ul>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </div>

    <form v-else-if="mode === 'edit'" :id="`setting-form-${setting.key}`" class="form" novalidate @submit.prevent="send(false)">
      <template v-if="json">
        <label :for="`setting-text-${setting.key}`" class="field-label">{{ t('admin.settings.value') }}</label>
        <textarea
          :id="`setting-text-${setting.key}`"
          v-model="text"
          class="text"
          rows="16"
          spellcheck="false"
          :maxlength="VALUE_MAX"
          :disabled="pending"
        />
      </template>
      <UiField v-else v-slot="{ id, describedBy, invalid }" :label="t('admin.settings.value')">
        <UiTextInput
          :id="id"
          v-model="text"
          mono
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending"
          :maxlength="VALUE_MAX"
        />
      </UiField>
      <p class="env mono">{{ t('admin.settings.envNow', { value: envText }) }}</p>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>

    <div v-else class="restore">
      <p class="lead">{{ t('admin.settings.restoreBody') }}</p>
      <pre class="restored mono">{{ envText }}</pre>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </div>

    <template #footer>
      <template v-if="impact">
        <UiButton variant="ghost" :disabled="pending" @click="back">{{ t('admin.settings.back') }}</UiButton>
        <UiButton variant="danger" :loading="pending" @click="send(true)">
          {{ mode === 'edit' ? t('admin.settings.saveAnyway') : t('admin.settings.restoreAnyway') }}
        </UiButton>
      </template>
      <template v-else>
        <UiButton variant="ghost" :disabled="pending" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
        <UiButton
          v-if="mode === 'edit'"
          variant="primary"
          type="submit"
          :form="`setting-form-${setting.key}`"
          :loading="pending"
          :disabled="!changed"
        >
          {{ t('admin.settings.save') }}
        </UiButton>
        <UiButton v-else variant="primary" :loading="pending" @click="send(false)">
          {{ t('admin.settings.restore') }}
        </UiButton>
      </template>
    </template>
  </UiModal>
</template>

<style scoped>
.heading {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.title {
  font-size: var(--text-lg);
  letter-spacing: var(--tracking-heading);
}

.key {
  color: var(--muted);
  font-size: var(--text-2xs);
  overflow-wrap: anywhere;
}

.form,
.impact,
.restore {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.field-label {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
}

.text {
  width: 100%;
  min-width: 0;
  min-height: 16rem;
  padding: var(--space-3) 14px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-family: var(--mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  resize: vertical;
  transition: border-color var(--duration-fast) var(--ease);
}

.text:disabled {
  background: var(--paper-sunken);
  color: var(--muted);
  cursor: not-allowed;
}

.env {
  color: var(--muted);
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

.lead {
  color: var(--ink-soft);
  line-height: 1.6;
}

.lines {
  display: grid;
  gap: var(--space-1);
  padding-left: var(--space-5);
  color: var(--ink-soft);
  font-size: var(--text-sm);
  line-height: 1.6;
  list-style: disc;
}

.removed {
  font-weight: var(--weight-medium);
  overflow-wrap: anywhere;
}

.restored {
  max-height: 12rem;
  margin: 0;
  overflow: auto;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
