<script setup lang="ts">
/**
 * The frame of every public page (docs/web.md § Pages, `/`, `/pricing` and the policy pages):
 * the public bar — the mark and name at the left; at the right the page's name, and *Sign in*
 * (*Back to Files* once `GET /api/me` says someone is signed in) as the bar's one accent
 * capsule — the page's own content on the desk between, and the public footer at the bottom.
 * The sign-in page passes no title: the bar offers *Pricing* there instead, and no sign-in,
 * the page being the sign-in. Full-bleed: a visitor lands here signed out, and a signed-in
 * reader is not sent away.
 */
defineProps<{
  /** The page's name, marked current in the bar. */
  title?: string
}>()

const { t } = useI18n()
const localePath = useLocalePath()
const auth = useAuth()
</script>

<template>
  <div class="public-page">
    <!-- Floats over the page as it scrolls under it: the system's translucent bar. -->
    <header class="bar">
      <div class="bar-inner">
        <AppBrand :to="localePath('/')" />
        <nav class="bar-nav" :aria-label="t('footer.nav')">
          <template v-if="title">
            <span class="bar-current" aria-current="page">{{ title }}</span>
            <NuxtLink v-if="auth.me.value" class="bar-action" :to="localePath('/files')">
              {{ t('login.backToFiles') }}
            </NuxtLink>
            <NuxtLink v-else class="bar-action" :to="localePath('/')">{{ t('login.signIn') }}</NuxtLink>
          </template>
          <NuxtLink v-else class="bar-link" :to="localePath('/pricing')">{{ t('footer.pricing') }}</NuxtLink>
        </nav>
      </div>
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
  background: var(--desk);
  color: var(--ink);
}

.bar {
  position: sticky;
  top: 0;
  z-index: 10;
  border-bottom: 1px solid var(--line);
  background: var(--glass);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
}

@supports not (backdrop-filter: blur(1px)) {
  .bar {
    background: var(--paper);
  }
}

.bar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  width: 100%;
  max-width: 1024px;
  height: 52px;
  margin: 0 auto;
  padding: 0 var(--space-6);
}

.bar-nav {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  min-width: 0;
  font-size: var(--text-sm);
}

.bar-current {
  overflow: hidden;
  color: var(--ink);
  font-weight: var(--weight-semibold);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bar-link {
  color: var(--muted);
  transition: color var(--duration-fast) var(--ease);
}

.bar-link:hover {
  color: var(--ink);
}

/* The bar's one action: a small accent capsule, as a product page's bar carries its *Buy*. */
.bar-action {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  height: 28px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-full);
  background: var(--accent);
  color: var(--accent-fg);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  white-space: nowrap;
  transition: background var(--duration-fast) var(--ease);
}

.bar-action:hover {
  background: var(--accent-hover);
}

/* The footer takes the content's measure and sits at the bottom of a short page. */
.foot {
  width: 100%;
  max-width: 1024px;
  margin: auto auto 0;
  padding: var(--space-5) var(--space-6) calc(var(--space-6) + env(safe-area-inset-bottom, 0px));
}

@media (max-width: 640px) {
  .bar-inner,
  .foot {
    padding-inline: var(--space-4);
  }
}
</style>
