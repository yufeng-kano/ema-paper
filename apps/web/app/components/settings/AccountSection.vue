<script setup lang="ts">
/**
 * Account (docs/web.md § Pages, `/settings/<section>`): who is signed in, and Sign out.
 * Nothing else: the app holds no profile of its own — the name and picture are Google's —
 * and there is no account deletion to offer yet.
 */
const { t } = useI18n()
const auth = useAuth()
const localePath = useLocalePath()

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
    </SettingsGroup>
  </SettingsSection>
</template>

<style scoped>
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
