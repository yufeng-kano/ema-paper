<script setup lang="ts">
/**
 * One section of the settings dialog (docs/web.md § Pages, `/settings/<section>`): the bold
 * title with an optional quiet line under it and the section's one action beside it, then
 * the section's grouped lists and tables.
 *
 * `--panel-x` is zeroed here so a `UiPanel` inside a section — the keys table — aligns with
 * the title instead of indenting by the gutter a full-width screen needs.
 */
defineProps<{
  title: string
  /** A fact about the section's data, under the title: "3 active", "OAuth · no key". */
  eyebrow?: string
}>()

defineSlots<{
  default: () => unknown
  /** Right of the title: the section's primary action (Create key), or nothing. */
  actions?: () => unknown
}>()
</script>

<template>
  <section class="section">
    <header class="head">
      <div class="heading">
        <h1 class="title">{{ title }}</h1>
        <p v-if="eyebrow" class="meta">{{ eyebrow }}</p>
      </div>
      <div v-if="$slots.actions" class="actions">
        <slot name="actions" />
      </div>
    </header>
    <div class="body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.section {
  --panel-x: 0px;

  display: flex;
  flex-direction: column;
  min-width: 0;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding-bottom: var(--space-2);
}

.heading {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.title {
  font-size: var(--display-md);
  line-height: 1.15;
  overflow-wrap: anywhere;
}

.meta {
  color: var(--muted);
  font-size: var(--text-sm);
}

.actions {
  flex-shrink: 0;
}

.body {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
</style>
