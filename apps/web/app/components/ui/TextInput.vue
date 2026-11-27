<script setup lang="ts">
/**
 * Text input. Pairs with `UiField`, which supplies `id` and `describedBy` through its slot
 * props. Height comes from `--control-height` so it matches any button on its row.
 *
 * `date` keeps the value a string, as the browser hands it over (`YYYY-MM-DD`); `min` is
 * the platform's. A whole number is a text field with `inputmode="numeric"` (the
 * numeric keypad), not `type="number"`: a number field hands over an empty string for what
 * it cannot parse (`-`, `1e`, `abc`), and the caller could not tell it from an empty field
 * to say what is wrong. The caller reads the number out of the text.
 *
 * `password` is for a PDF's password, which opens one file in this browser and is never
 * stored: `autocomplete="off"` stays, asking the browser neither to fill it nor to keep it.
 */
withDefaults(
  defineProps<{
    modelValue: string
    id?: string
    type?: 'text' | 'email' | 'date' | 'password'
    inputmode?: 'numeric'
    placeholder?: string
    describedBy?: string
    invalid?: boolean
    disabled?: boolean
    required?: boolean
    maxlength?: number
    mono?: boolean
    min?: string
  }>(),
  {
    type: 'text',
    id: undefined,
    inputmode: undefined,
    placeholder: undefined,
    describedBy: undefined,
    maxlength: undefined,
    min: undefined,
  },
)

defineEmits<{ 'update:modelValue': [string] }>()
</script>

<template>
  <input
    :id="id"
    class="control"
    :class="{ mono, invalid }"
    :type="type"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :required="required"
    :maxlength="maxlength"
    :min="min"
    :inputmode="inputmode"
    autocomplete="off"
    :aria-describedby="describedBy"
    :aria-invalid="invalid || undefined"
    spellcheck="false"
    @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
  >
</template>

<style scoped>
.control {
  width: 100%;
  min-width: 0;
  height: var(--control-height);
  padding: 0 var(--space-3);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink);
  font-size: var(--text-sm);
  transition: border-color var(--duration-fast) var(--ease);
}

.control:hover:not(:disabled) {
  border-color: var(--faint);
}

.control::placeholder {
  color: var(--faint);
}

/* Focus is the app's one ring, from main.css — nothing per-component. */
.control:disabled {
  background: var(--paper-sunken);
  border-color: var(--line);
  color: var(--muted);
  cursor: not-allowed;
}

.control.invalid {
  border-color: var(--danger-edge);
}

.mono {
  font-family: var(--mono);
  font-size: var(--text-xs);
}

/* 16px stops iOS Safari zooming the viewport on focus. */
@media (pointer: coarse) {
  .control,
  .mono {
    font-size: 16px;
  }
}
</style>
