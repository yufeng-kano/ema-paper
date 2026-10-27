<script setup lang="ts">
/**
 * *Add to group* on the console's *Users* screen (docs/admin.md § Pages, *Users*): a select
 * of the groups the operator may add the account to — those whose permissions they all hold,
 * which do not have it yet — and *Add*, which sends the account's id to the members route.
 * A refusal stays in the dialog, which cannot be dismissed, nor left by Back, while it sends.
 */
import { addAdminGroupMember, type AdminGroup, type AdminUserDetail } from '~/lib/api'

const props = defineProps<{
  user: AdminUserDetail['user']
  groups: AdminGroup[]
}>()

const emit = defineEmits<{ close: []; added: [] }>()

const { t } = useI18n()
const router = useRouter()
const { explain } = useAdminFailure()

const groupId = ref(String(props.groups[0]?.id ?? ''))
const pending = ref(false)
const error = ref<string | null>(null)

watch(groupId, () => {
  error.value = null
})

async function submit() {
  if (pending.value || !groupId.value) {
    return
  }
  pending.value = true
  error.value = null
  const release = router.beforeEach(() => false)
  let failure: unknown = null
  try {
    await addAdminGroupMember(Number(groupId.value), { user_id: props.user.id })
  } catch (caught) {
    failure = caught
  } finally {
    release()
  }
  if (!failure) {
    pending.value = false
    emit('added')
    return
  }
  error.value = await explain(failure, t('admin.users.addGroup.notFound'))
  pending.value = false
}
</script>

<template>
  <UiModal
    :title="t('admin.users.addGroup.title', { name: user.name?.trim() || user.email })"
    :busy="pending"
    @close="emit('close')"
  >
    <form id="admin-add-group-form" class="form" novalidate @submit.prevent="submit">
      <UiField v-slot="{ id, describedBy }" :label="t('admin.users.addGroup.group')">
        <UiSelect :id="id" v-model="groupId" :described-by="describedBy" :disabled="pending">
          <option v-for="group in groups" :key="group.id" :value="String(group.id)">{{ group.name }}</option>
        </UiSelect>
      </UiField>
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
    </form>
    <template #footer>
      <UiButton variant="ghost" :disabled="pending" @click="emit('close')">{{ t('common.cancel') }}</UiButton>
      <UiButton variant="primary" type="submit" form="admin-add-group-form" :loading="pending" :disabled="!groupId">
        {{ t('admin.users.addGroup.add') }}
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
</style>
