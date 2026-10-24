<script setup lang="ts">
/**
 * Choose your language (docs/web.md § The account menu).
 *
 * A dialog rather than a submenu off the account menu: a submenu is a second floating surface
 * to position and dismiss, and this is a setting you touch once. Every option is a real link —
 * the locale lives in the URL, so switching it is navigation, and `switchLocalePath` keeps you
 * on the page you were reading.
 *
 * Each option says its language in *that* language, over the same name in the one you are
 * reading now: the first line is for the person looking for their own language, the second for
 * the person who cannot read the first.
 */
const emit = defineEmits<{ close: [] }>()

const { t, locale, locales } = useI18n()
const switchLocalePath = useSwitchLocalePath()
</script>

<template>
  <UiModal :title="t('language.title')" size="md" @close="emit('close')">
    <ul class="options">
      <li v-for="option in locales" :key="option.code">
        <NuxtLink
          class="option"
          :class="{ current: option.code === locale }"
          :to="switchLocalePath(option.code)"
          :aria-current="option.code === locale ? 'true' : undefined"
          @click="emit('close')"
        >
          <span class="native">{{ option.name ?? option.code }}</span>
          <span class="known">{{ t(`language.${option.code}`) }}</span>
          <UiIcon v-if="option.code === locale" class="mark" name="check" />
        </NuxtLink>
      </li>
    </ul>
  </UiModal>
</template>

<style scoped>
/* As many columns as fit: two locales today, and a third would not need this touched. */
.options {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.option {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 0 var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-lg);
  color: var(--ink);
  text-decoration: none;
  transition: background var(--duration-fast) var(--ease);
}

.option:hover {
  background: var(--fill-weak);
}

/* The one you are on: the same fill the sidebar's open row has, not a colour of its own. */
.option.current {
  background: var(--fill);
}

.native {
  grid-column: 1;
  overflow: hidden;
  font-size: var(--text-base);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.known {
  grid-column: 1;
  overflow: hidden;
  color: var(--faint);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* At the right of both lines: placed by hand, since an item with only a row would take the
   first column and push the name out of it. */
.mark {
  grid-column: 2;
  grid-row: 1 / span 2;
  width: 15px;
  height: 15px;
  color: var(--accent);
}
</style>
