<script setup lang="ts">
/**
 * *Create group* and *Rename* (docs/admin.md § Pages, *Groups*): one name field, checked
 * against the name rule as it is typed, and its button. A new group starts with no
 * permissions; the caller selects it. A taken name is said under the field; a group deleted
 * by someone else while it was being renamed is the caller's to say (`gone`); any other
 * refusal stays in the dialog, which cannot be dismissed — nor left by Back — while its
 * request runs.
 */
import { createAdminGroup, updateAdminGroup, type AdminGroup } from '~/lib/api'
import { adminFailure, GROUP_NAME_MAX, isGroupName } from '~/lib/admin'

const props = withDefaults(defineProps<{ group?: AdminGroup | null }>(), { group: null })

const emit = defineEmits<{ close: []; saved: [group: AdminGroup]; gone: [] }>()

const { t } = useI18n()
const router = useRouter()
const { explain } = useAdminFailure()

const name = ref(props.group?.name ?? '')
const pending = ref(false)
/** The server's refusal of this name — a taken one — said under the field. */
const nameError = ref<string | null>(null)
const error = ref<string | null>(null)

const trimmed = computed(() => name.value.trim())
const ruleError = computed(() => (trimmed.value && !isGroupName(trimmed.value) ? t('admin.groups.nameRule') : null))
const unchanged = computed(() => props.group !== null && trimmed.value === props.group.name)

watch(name, () => {
  nameError.value = null
})

async function submit() {
  if (pending.value || !trimmed.value || ruleError.value || unchanged.value) {
    return
  }
  pending.value = true
  error.value = null
  // Back would close the dialog while the request runs, and its answer would land unseen.
  const release = router.beforeEach(() => false)
  let saved: AdminGroup | null = null
  let failure: unknown = null
  try {
    saved = props.group
      ? await updateAdminGroup(props.group.id, { name: trimmed.value })
      : await createAdminGroup({ name: trimmed.value, permissions: [] })
  } catch (caught) {
    failure = caught
  } finally {
    release()
  }
  if (saved) {
    pending.value = false
    emit('saved', saved)
    return
  }
  const refusal = adminFailure(failure)
  if (refusal.kind === 'notFound' && props.group) {
    pending.value = false
    emit('gone')
    return
  }
  const message = await explain(failure)
  if (refusal.kind === 'conflict' && refusal.reason === 'name_taken') {
    nameError.value = message
  } else {
    error.value = message
  }
  pending.value = false
}
</script>

<template>
  <UiModal :title="group ? t('admin.groups.renameTitle') : t('admin.groups.createTitle')" :busy="pending" @close="emit('close')">
    <form id="admin-group-form" class="form" novalidate @submit.prevent="submit">
      <UiField v-slot="{ id, describedBy, invalid }" :label="t('admin.groups.name')" :error="nameError ?? ruleError ?? undefined">
        <UiTextInput
          :id="id"
          v-model="name"
          :described-by="describedBy"
          :invalid="invalid"
          :disabled="pending"
          :maxlength="GROUP_NAME_MAX"
          mono
          required
        />
      </UiField>
      <p class="help">{{ t('admin.groups.nameHelp') }}</p>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>
    <template #footer>
      <UiButton variant="ghost" :disabled="pending" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton
        variant="primary"
        type="submit"
        form="admin-group-form"
        :loading="pending"
        :disabled="!trimmed || !!ruleError || unchanged"
      >
        {{ group ? t('admin.groups.rename') : t('admin.groups.create') }}
      </UiButton>
    </template>
  </UiModal>
</template>

<style scoped>
.form {
  display: grid;
  gap: var(--space-3);
  margin-top: var(--space-2);
}

.help {
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.6;
}
</style>
