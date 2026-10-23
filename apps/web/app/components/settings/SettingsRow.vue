<script setup lang="ts">
/**
 * One row of a settings section: a label and its one-line description at the left, the
 * control at the right, a hairline above (docs/web.md § Pages, `/settings/<section>`).
 *
 * The description says the consequence the control does not show by itself — where a
 * language applies, what signing out ends — and nothing else; a row with nothing to add
 * has none.
 */
defineProps<{
  label: string
  description?: string
}>()

defineSlots<{
  /** The control. */
  default: () => unknown
  /** Under the control: a status or error line the row owns. */
  note?: () => unknown
}>()
</script>

<template>
  <div class="row">
    <div class="text">
      <p class="label">{{ label }}</p>
      <p v-if="description" class="description">{{ description }}</p>
    </div>
    <div class="control">
      <slot />
    </div>
    <div v-if="$slots.note" class="note">
      <slot name="note" />
    </div>
  </div>
</template>

<style scoped>
.row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: var(--space-2) var(--space-6);
  padding: var(--space-5) 0;
  border-bottom: 1px solid var(--hair);
}

.text {
  display: grid;
  gap: var(--space-1);
  min-width: 0;
}

.label {
  color: var(--ink);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
}

.description {
  color: var(--muted);
  font-size: var(--text-sm);
  line-height: 1.6;
}

.control {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
  min-width: 0;
}

.note {
  grid-column: 1 / -1;
}

@media (max-width: 640px) {
  /* The control drops under its label: a segmented control and a long label do not fit
     side by side on a phone. */
  .row {
    grid-template-columns: minmax(0, 1fr);
  }

  .control {
    justify-content: flex-start;
  }
}
</style>
