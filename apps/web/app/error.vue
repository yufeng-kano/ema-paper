<script setup lang="ts">
/**
 * Nuxt's error page (docs/web.md § Pages, a missing page): the public shell around a large
 * title — a missing page, or anything else that went wrong — one line, and the way back to
 * the sign-in page. Nuxt answers with the error's own status, so a missing page stays a 404
 * to a crawler. The link keeps its href for a crawler or a new tab; a click also clears the
 * error, which a plain navigation would leave standing over the next page.
 */
import type { NuxtError } from '#app'

const props = defineProps<{ error: NuxtError }>()

const { t } = useI18n()
const localePath = useLocalePath()
const localeHead = useLocaleHead()

// Nuxt renders this page in place of app.vue, so the tab title and the document's language
// are set here; no canonical or alternates, which a page that does not exist has none of.
useHead(() => ({ title: t('app.name'), htmlAttrs: { lang: localeHead.value.htmlAttrs?.lang } }))

const notFound = computed(() => props.error.statusCode === 404)
</script>

<template>
  <PublicShell>
    <main class="message">
      <h1 class="title">{{ notFound ? t('errorPage.notFoundTitle') : t('errorPage.failedTitle') }}</h1>
      <p class="body">{{ notFound ? t('errorPage.notFoundBody') : t('errorPage.failedBody') }}</p>
      <a class="home" :href="localePath('/')" @click.prevent="clearError({ redirect: localePath('/') })">
        {{ t('errorPage.home', { name: t('app.name') }) }}
      </a>
    </main>
  </PublicShell>
</template>

<style scoped>
.message {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-5);
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
  padding: var(--space-14) var(--space-6);
  text-align: center;
}

.title {
  font-size: var(--display-2xl);
  line-height: 1.1;
}

.body {
  max-width: 36em;
  color: var(--muted);
  font-size: var(--text-lg);
  line-height: 1.55;
}

.home {
  color: var(--accent);
  font-weight: var(--weight-medium);
}

@media (width <= 640px) {
  .message {
    padding: var(--space-10) var(--space-4);
  }

  .title {
    font-size: var(--display-lg);
  }
}
</style>
