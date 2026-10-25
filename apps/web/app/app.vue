<script setup lang="ts">
const localeHead = useLocaleHead({ seo: true })
const { t } = useI18n()

useHead(() => ({
  // One tab title for the whole app — pages do not set their own, so switching
  // tabs never renames the tab. The sign-in page's SEO meta is the one override.
  title: t('app.name'),
  htmlAttrs: { lang: localeHead.value.htmlAttrs?.lang },
  link: [...(localeHead.value.link || [])],
  meta: [...(localeHead.value.meta || []), { property: 'og:site_name', content: t('app.name') }],
}))
</script>

<template>
  <!-- One shell: `layouts/default` is the signed-in fixed frame. The sign-in page is
       full-bleed and opts out with `layout: false` (docs/web.md § Design system). -->
  <NuxtLayout>
    <NuxtPage />
  </NuxtLayout>
</template>
