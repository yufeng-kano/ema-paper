<script setup lang="ts">
/**
 * Account (docs/web.md § Pages, `/settings/<section>`): who is signed in, Sign out, and
 * Delete account. Nothing else: the app holds no profile of its own — the name and picture
 * are Google's.
 *
 * Deleting the account takes everything in it at once, with no undo (docs/web.md § Deleting
 * the account), so the button opens a dialog that asks for the address signed in before it
 * acts — typed, not clicked, because a confirmation a reader can click through by habit is
 * not one for an action this large.
 */
import { ApiRequestError, deleteAccount } from '~/lib/api'

const { t } = useI18n()
const auth = useAuth()
const localePath = useLocalePath()
const { resolve } = useApiError()

const user = computed(() => auth.me.value?.user ?? null)
const name = computed(() => user.value?.name?.trim() || user.value?.email || '')
const pictureFailed = ref(false)
const initial = computed(() => (name.value[0] ?? '?').toUpperCase())

const signingOut = ref(false)

async function signOut() {
  if (signingOut.value) {
    return
  }
  signingOut.value = true
  try {
    await auth.signOut()
    await navigateTo(localePath('/'))
  } finally {
    signingOut.value = false
  }
}

const deleteOpen = ref(false)
/** What the reader typed to confirm: the address signed in, in any letter case. */
const confirmEmail = ref('')
const deleting = ref(false)
/** Why the last attempt failed, said inside the dialog, which stays open. */
const deleteError = ref<string | null>(null)

const emailMatches = computed(
  () => !!user.value && confirmEmail.value.trim().toLowerCase() === user.value.email.trim().toLowerCase(),
)

function openDelete() {
  confirmEmail.value = ''
  deleteError.value = null
  deleteOpen.value = true
}

async function confirmDelete() {
  if (deleting.value || !emailMatches.value) {
    return
  }
  deleting.value = true
  deleteError.value = null
  try {
    await deleteAccount()
    // The answer already cleared the cookie; the page lets go of the session as Sign out does.
    auth.clearSession()
    await navigateTo(localePath('/'))
  } catch (error) {
    deleteError.value =
      error instanceof ApiRequestError && error.type === 'subscription_active'
        ? t('settings.account.subscriptionActive')
        : await resolve(error)
  } finally {
    deleting.value = false
  }
}
</script>

<template>
  <SettingsSection :title="t('settings.sections.account')">
    <SettingsGroup>
      <SettingsRow
        :label="t('settings.account.signedInAs')"
        :description="t('settings.account.signedInAsDescription')"
      >
        <div v-if="user" class="identity">
          <img
            v-if="user.picture && !pictureFailed"
            class="avatar"
            :src="user.picture"
            alt=""
            referrerpolicy="no-referrer"
            @error="pictureFailed = true"
          >
          <span v-else class="avatar avatar-initial" aria-hidden="true">{{ initial }}</span>
          <span class="who">
            <span class="name">{{ name }}</span>
            <span v-if="user.name && user.email" class="email">{{ user.email }}</span>
          </span>
        </div>
      </SettingsRow>

      <SettingsRow
        :label="t('settings.account.signOut')"
        :description="t('settings.account.signOutDescription')"
      >
        <UiButton variant="secondary" :loading="signingOut" @click="signOut">
          <template #icon><UiIcon name="sign-out" /></template>
          {{ t('settings.account.signOut') }}
        </UiButton>
      </SettingsRow>

      <SettingsRow
        :label="t('settings.account.delete')"
        :description="t('settings.account.deleteDescription')"
      >
        <UiButton variant="danger" :disabled="!user" @click="openDelete">
          {{ t('settings.account.delete') }}
        </UiButton>
      </SettingsRow>
    </SettingsGroup>

    <!-- `busy`: closing would not stop the request, and the account would go after the
         reader believed they had backed out of it. -->
    <UiModal v-if="deleteOpen" :title="t('settings.account.deleteTitle')" :busy="deleting" @close="deleteOpen = false">
      <form id="delete-account" class="delete-form" @submit.prevent="confirmDelete">
        <p class="message">{{ t('settings.account.deleteBody') }}</p>
        <UiField v-slot="{ id }" :label="t('settings.account.deleteConfirm')">
          <UiTextInput :id="id" v-model="confirmEmail" :placeholder="user?.email" :disabled="deleting" />
        </UiField>
        <UiBanner v-if="deleteError" tone="error">{{ deleteError }}</UiBanner>
      </form>
      <template #footer>
        <UiButton variant="ghost" :disabled="deleting" @click="deleteOpen = false">
          {{ t('common.cancel') }}
        </UiButton>
        <UiButton variant="danger" type="submit" form="delete-account" :loading="deleting" :disabled="!emailMatches">
          {{ t('settings.account.delete') }}
        </UiButton>
      </template>
    </UiModal>
  </SettingsSection>
</template>

<style scoped>
.delete-form {
  display: grid;
  gap: var(--space-4);
}

.message {
  color: var(--muted);
  line-height: 1.7;
}

.identity {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  min-width: 0;
}

.avatar {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  border-radius: var(--radius-full);
  object-fit: cover;
}

/* The sidebar's initial: white on the system's gray, as a contact without a photo. */
.avatar-initial {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(180deg, var(--faint), var(--muted));
  color: var(--accent-fg);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.who {
  display: grid;
  min-width: 0;
  text-align: right;
}

.name {
  overflow: hidden;
  color: var(--ink);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.email {
  overflow: hidden;
  color: var(--muted);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 640px) {
  .who {
    text-align: left;
  }
}
</style>
