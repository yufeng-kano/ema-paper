<script setup lang="ts">
/**
 * Text input. Pairs with `UiField`, which supplies `id` and `describedBy` through its slot
 * props. Height comes from `--control-height` so it matches any button on its row.
 */
withDefaults(
  defineProps<{
    modelValue: string
    id?: string
    type?: 'text' | 'password'
    placeholder?: string
    describedBy?: string
    invalid?: boolean
    disabled?: boolean
    required?: boolean
    maxlength?: number
    autocomplete?: string
    mono?: boolean
  }>(),
  {
    type: 'text',
    autocomplete: 'off',
    id: undefined,
    placeholder: undefined,
    describedBy: undefined,
    maxlength: undefined,
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
    :autocomplete="autocomplete"
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
