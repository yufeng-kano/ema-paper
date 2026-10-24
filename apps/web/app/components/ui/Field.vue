<script setup lang="ts">
/**
 * Label + control + error, wired together.
 *
 * The point is the wiring: `for`/`id`, `aria-describedby` to the error, and `aria-invalid`.
 * Hand-rolled fields drift on exactly those, and the drift is invisible until someone uses
 * a screen reader.
 *
 * There is no hint prop. A field explained by a note underneath is usually a field whose
 * label is wrong; where guidance genuinely belongs to the whole form it is written once,
 * at readable size, beside the form.
 */
const props = defineProps<{
  label: string
  error?: string
  /**
   * Hides the label visually, for a control the design renders bare. The label stays in the
   * DOM rather than becoming an `aria-label`: a real `<label>` is also a click target and
   * survives translation, and the wiring is already here.
   */
  labelHidden?: boolean
}>()

const id = useId()
const errorId = computed(() => (props.error ? `${id}-error` : undefined))
</script>

<template>
  <div class="field">
    <label class="field-label" :class="{ 'sr-only': labelHidden }" :for="id">{{ label }}</label>
    <slot :id="id" :described-by="errorId" :invalid="!!error" />
    <p v-if="error" :id="errorId" class="field-error">{{ error }}</p>
  </div>
</template>

<style scoped>
.field {
  display: grid;
  /* Pins the track to the container: a long unbroken value must not inflate the grid and
     get clipped. */
  grid-template-columns: minmax(0, 1fr);
  gap: var(--space-2);
  min-width: 0;
}

/* The field's name over it: the system's form label, a step under the text it names. */
.field-label {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--ink-soft);
}

.field-error {
  font-size: var(--text-xs);
  color: var(--danger);
  overflow-wrap: anywhere;
}
</style>
