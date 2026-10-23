<script setup lang="ts">
/**
 * One section of the settings dialog (docs/web.md § Pages, `/settings/<section>`): the serif
 * title with an optional eyebrow over it and the section's one action beside it, then the
 * section's rows and tables.
 *
 * `--panel-x` is zeroed here so a `UiPanel` inside a section — the keys table, the usage
 * sections — aligns with the title instead of indenting by the panel gutter the full-width
 * screens need.
 */
defineProps<{
  title: string
  /** A fact about the section's data, uppercase over the title: "3 active", "Last 30 days". */
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
        <p v-if="eyebrow" class="eyebrow">{{ eyebrow }}</p>
        <h1 class="title">{{ title }}</h1>
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
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-4);
  padding-bottom: var(--space-6);
  border-bottom: 1px solid var(--line);
}

.heading {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
}

.title {
  font-size: var(--display-md);
  line-height: 1.1;
  overflow-wrap: anywhere;
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
