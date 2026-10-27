<script setup lang="ts">
/**
 * A list's search (docs/web.md § Design system, *Primitives*): a capsule on the gray fill
 * with the search glyph inside its start, a native `type="search"` input, and the value
 * bound both ways. Its name is `label`, as an `aria-label`, since a toolbar has no room for
 * a visible one; the placeholder says what it matches.
 *
 * It searches nothing itself: the caller debounces the value and keeps it in the route
 * query, so Back and a reload return to the same results.
 */
withDefaults(
  defineProps<{
    modelValue: string
    /** The accessible name. */
    label: string
    placeholder?: string
    maxlength?: number
  }>(),
  { placeholder: undefined, maxlength: undefined },
)

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
</script>

<template>
  <span class="search">
    <UiIcon class="glyph" name="search" />
    <input
      class="input"
      type="search"
      :value="modelValue"
      :aria-label="label"
      :placeholder="placeholder"
      :maxlength="maxlength"
      autocomplete="off"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    >
  </span>
</template>

<style scoped>
.search {
  position: relative;
  display: inline-flex;
  align-items: center;
  min-width: 0;
  height: var(--control-height);
}

.glyph {
  position: absolute;
  left: var(--space-3);
  width: 15px;
  height: 15px;
  color: var(--muted);
  pointer-events: none;
}

.input {
  width: 100%;
  min-width: 0;
  height: 100%;
  padding: 0 var(--space-3) 0 34px;
  border: 0;
  border-radius: var(--radius-full);
  background: var(--fill);
  color: var(--ink);
  font-size: var(--text-sm);
  transition: background var(--duration-fast) var(--ease);
}

.input:hover {
  background: var(--fill-strong);
}

.input::placeholder {
  color: var(--muted);
}

/* The platform's clear button stays; its decoration does not reserve room the capsule does
   not have. */
.input::-webkit-search-decoration {
  appearance: none;
}

/* 16px stops iOS Safari zooming the viewport on focus. */
@media (pointer: coarse) {
  .input {
    font-size: 16px;
  }
}
</style>
