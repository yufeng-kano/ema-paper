<script setup lang="ts">
/**
 * The frame of a public page other than sign-in (docs/web.md § Pages, `/pricing` and the
 * policy pages): the sign-in page's brand panel reduced to a bar — the mark, the page's
 * name, *Sign in* (*Back to Files* once `GET /api/me` says someone is signed in) — the
 * page's own content between, and the public footer at the bottom. Full-bleed like the
 * sign-in page: a visitor lands here signed out, and a signed-in reader is not sent away.
 */
defineProps<{ title: string }>()

const { t } = useI18n()
const localePath = useLocalePath()
const auth = useAuth()
</script>

<template>
  <div class="public-page">
    <header class="bar">
      <AppBrand :to="localePath('/')" />
      <nav class="bar-nav" :aria-label="title">
        <span class="bar-current" aria-current="page">{{ title }}</span>
        <NuxtLink v-if="auth.me.value" class="bar-link" :to="localePath('/files')">
          {{ t('login.backToFiles') }}
        </NuxtLink>
        <NuxtLink v-else class="bar-link" :to="localePath('/')">{{ t('login.signIn') }}</NuxtLink>
      </nav>
    </header>

    <slot />

    <PublicFooter class="foot" />
  </div>
</template>

<style scoped>
.public-page {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: var(--paper);
  color: var(--ink);
}

.bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-6);
  border-bottom: 1px solid var(--line);
}

.bar-nav {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  font-size: var(--text-sm);
}

.bar-current {
  color: var(--ink);
  font-weight: var(--weight-medium);
}

.bar-link {
  color: var(--muted);
}

.bar-link:hover {
  color: var(--accent);
}

/* The footer takes the content's measure and sits at the bottom of a short page. */
.foot {
  width: 100%;
  max-width: 880px;
  margin: auto auto 0;
  padding: var(--space-5) var(--space-6);
}
</style>
