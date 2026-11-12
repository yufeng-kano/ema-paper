<script setup lang="ts">
/**
 * The product mark and name, linking home — the same in both shells.
 *
 * The mark is the app icon itself, `public/favicon.svg`, so the page and the browser tab show
 * one icon. It is a logo with its own colors, never the accent, in both appearances
 * (docs/web.md § Design system). `?v=` is the icons' version, raised with nuxt.config.ts's.
 *
 * `lg` is the sign-in page's app icon: the mark large over the name, as a product page
 * introduces an app. Everything else takes the sidebar's cut. Without `to` — the sign-in
 * page's hero, where there is nowhere to go — it is a plain block rather than a link to the
 * page you are already on.
 */
withDefaults(defineProps<{ to?: string; size?: 'md' | 'lg' }>(), {
  size: 'md',
  to: undefined,
})

const { t } = useI18n()
</script>

<template>
  <NuxtLink v-if="to" class="brand" :class="size" :to="to">
    <img class="brand-mark" src="/favicon.svg?v=3" alt="">
    <span class="brand-name">{{ t('app.name') }}</span>
  </NuxtLink>
  <div v-else class="brand" :class="size">
    <img class="brand-mark" src="/favicon.svg?v=3" alt="">
    <span class="brand-name">{{ t('app.name') }}</span>
  </div>
</template>

<style scoped>
.brand {
  display: flex;
  align-items: center;
  min-width: 0;
  flex-shrink: 0;
  font-family: var(--font-display);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.01em;
}

.brand.md {
  gap: 10px;
  font-size: var(--display-2xs);
}

.brand.lg {
  flex-direction: column;
  gap: var(--space-4);
  font-size: var(--display-sm);
}

.brand-mark {
  display: block;
  flex-shrink: 0;
}

.brand.md .brand-mark {
  width: 28px;
  height: 28px;
}

/* The rounded square is drawn in the SVG (rx 14 of 64). */
.brand.lg .brand-mark {
  width: 80px;
  height: 80px;
}

/* The wordmark never wraps — it is a name, and a two-line one reads as two words. */
.brand-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
