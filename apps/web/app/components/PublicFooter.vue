<script setup lang="ts">
/**
 * The footer of the public pages (docs/web.md § Pages): the copyright, the links to the
 * pricing page, the rates page and the three policy pages, and the locale links. Each locale option is
 * written in its own language, so it is legible whichever catalog is loaded, and every link
 * is real — the locale lives in the URL, so a choice is bookmarkable and works without
 * JavaScript on a prerendered page.
 */
const { t, locale, locales } = useI18n()
const localePath = useLocalePath()
const switchLocalePath = useSwitchLocalePath()

/** A string, not a number: the interpolator runs numbers through `Intl.NumberFormat`, which
 *  would print the year with a thousands separator. */
const year = String(new Date().getFullYear())
const pages = ['pricing', 'rates', 'terms', 'privacy', 'refund'] as const
</script>

<template>
  <footer class="public-footer">
    <span>{{ t('login.copyright', { year, name: t('app.name') }) }}</span>
    <nav class="pages" :aria-label="t('footer.nav')">
      <NuxtLink v-for="page in pages" :key="page" class="link" :to="localePath(`/${page}`)">
        {{ t(`footer.${page}`) }}
      </NuxtLink>
    </nav>
    <nav class="locales" :aria-label="t('nav.language')">
      <NuxtLink
        v-for="option in locales"
        :key="option.code"
        class="link"
        :class="{ active: option.code === locale }"
        :to="switchLocalePath(option.code)"
        :aria-current="option.code === locale ? 'true' : undefined"
      >
        {{ option.name }}
      </NuxtLink>
    </nav>
  </footer>
</template>

<style scoped>
.public-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3) var(--space-5);
  padding-top: var(--space-5);
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--text-xs);
}

.pages,
.locales {
  display: inline-flex;
  flex-wrap: wrap;
  gap: var(--space-4);
}

.locales {
  flex-shrink: 0;
}

.link {
  color: var(--muted);
  white-space: nowrap;
  transition: color var(--duration-fast) var(--ease);
}

.link:hover {
  color: var(--accent);
}

/* The current locale is ink and a weight step, underlined in the accent — the accent's one
   navigational use, marking which of two states of the same page you are on. */
.link.active {
  color: var(--ink);
  font-weight: var(--weight-semibold);
  padding-bottom: 2px;
  border-bottom: 1px solid var(--accent);
}
</style>
