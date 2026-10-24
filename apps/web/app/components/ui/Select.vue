<script setup lang="ts">
/**
 * Native `<select>` in the app's control skin — same box as `UiTextInput` and any button on
 * its row, driven by `--control-height`.
 *
 * The options are a slot rather than a prop: the model picker groups its options, and a
 * prop-driven list would have to re-invent `<optgroup>` and per-option `disabled` to say
 * the same thing the platform already says.
 */
defineProps<{
  modelValue: string
  id?: string
  describedBy?: string
  disabled?: boolean
}>()

defineEmits<{ 'update:modelValue': [string] }>()
</script>

<template>
  <select
    :id="id"
    class="control"
    :value="modelValue"
    :disabled="disabled"
    :aria-describedby="describedBy"
    @change="$emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
  >
    <slot />
  </select>
</template>

<style scoped>
.control {
  width: 100%;
  min-width: 0;
  max-width: 100%;
  height: var(--control-height);
  padding: 0 var(--space-3);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: border-color var(--duration-fast) var(--ease);
}

.control:hover:not(:disabled) {
  border-color: var(--faint);
}

/* Focus is the app's one ring, from main.css — nothing per-component. */
.control:disabled {
  background: var(--paper-sunken);
  color: var(--muted);
  cursor: not-allowed;
}

@media (pointer: coarse) {
  .control {
    font-size: 16px;
  }
}
</style>
