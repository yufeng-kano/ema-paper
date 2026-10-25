<script setup lang="ts">
/**
 * The product mark and name, linking home — the same in both shells.
 *
 * The mark is the `scan-text` glyph white on an app icon's rounded square, the accent lit
 * from the top as the system's own icons are: the brand is the one place besides data and
 * controls where the accent appears, and a monogram at this size was reading as a stray pair
 * of letters rather than as a mark (docs/web.md § Design system). Its blues are the light
 * accent's in both appearances (`--mark-top`, `--mark-bottom`), the favicon's own, so the
 * page and the browser tab show one icon.
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
    <span class="brand-mark" aria-hidden="true">
      <UiIcon name="scan-text" />
    </span>
    <span class="brand-name">{{ t('app.name') }}</span>
  </NuxtLink>
  <div v-else class="brand" :class="size">
    <span class="brand-mark" aria-hidden="true">
      <UiIcon name="scan-text" />
    </span>
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
  display: inline-grid;
  place-items: center;
  flex-shrink: 0;
  background: linear-gradient(180deg, var(--mark-top), var(--mark-bottom));
  box-shadow: inset 0 1px 0 var(--accent-highlight);
  color: var(--accent-fg);
}

.brand.md .brand-mark {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-mark);
}

.brand.lg .brand-mark {
  width: 80px;
  height: 80px;
  border-radius: var(--radius-xl);
  box-shadow:
    inset 0 1px 0 var(--accent-highlight),
    0 8px 24px var(--glass-shadow);
}

.brand.md .brand-mark :deep(svg) {
  width: 17px;
  height: 17px;
}

.brand.lg .brand-mark :deep(svg) {
  width: 46px;
  height: 46px;
}

/* The wordmark never wraps — it is a name, and a two-line one reads as two words. */
.brand-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
