<script setup lang="ts">
/**
 * The rows of one subject in one rounded box, as the system's grouped lists draw them
 * (docs/web.md § Pages, *The settings dialog*): an optional heading over the box, with a
 * figure or a control at its right and a quiet line under it, the rows inside the box with
 * hairlines between them, and an optional quiet line under the box.
 *
 * The rows are `SettingsRow`s, or rows of the caller's own that draw the same hairline
 * (`.row + .row`): the box sets the inset, the row the rhythm.
 */
defineProps<{
  /** Over the box: what the rows are about. */
  title?: string
  /** Under the heading, over the box: one muted line on what the rows measure. */
  description?: string
  /** No box: the group holds a table or a paragraph, which bring their own surface. */
  plain?: boolean
}>()

defineSlots<{
  default: () => unknown
  /** Right of the heading: one small control for the whole group (a refresh) or its one
   *  figure (the top-up balance), or nothing. */
  actions?: () => unknown
  /** Under the box: quiet lines about the group as a whole, an error, a refresh. */
  foot?: () => unknown
}>()
</script>

<template>
  <section class="group">
    <header v-if="title || $slots.actions" class="group-head">
      <h2 v-if="title" class="group-title">{{ title }}</h2>
      <div v-if="$slots.actions" class="group-actions">
        <slot name="actions" />
      </div>
    </header>
    <p v-if="description" class="group-description">{{ description }}</p>
    <div class="group-box" :class="{ plain }">
      <slot />
    </div>
    <div v-if="$slots.foot" class="group-foot">
      <slot name="foot" />
    </div>
  </section>
</template>

<style scoped>
.group {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
  margin-top: var(--space-6);
}

.group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  min-height: var(--control-height-xs);
  padding: 0 var(--space-1);
}

.group-title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  letter-spacing: normal;
}

.group-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
}

.group-description {
  padding: 0 var(--space-1);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.group-box {
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 0 var(--space-4);
  border-radius: var(--radius-lg);
  background: var(--paper-sunken);
}

.group-box.plain {
  padding: 0;
  border-radius: 0;
  background: none;
}

.group-foot {
  display: grid;
  gap: var(--space-1);
  min-width: 0;
  padding: 0 var(--space-1);
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}
</style>
