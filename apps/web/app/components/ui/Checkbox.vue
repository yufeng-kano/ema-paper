<script setup lang="ts">
/**
 * The platform's square check (docs/web.md § Design system, *Primitives*): an 18px box with
 * small rounded corners and an edge, filled with the accent under a white check when ticked.
 *
 * The box is drawn; the control is a native `<input type="checkbox">` laid over it, so focus,
 * Space, the form's semantics and assistive tech are the browser's own, and the app's one
 * focus ring is drawn around the box while the input has keyboard focus. Its name is the
 * visible text in the default slot, or `label` where there is none.
 *
 * `locked` is unavailable for a moment rather than for good — a box waiting on the save it
 * started: drawn and announced as disabled (`aria-disabled`) and deaf to a change, but still
 * focusable, since a natively disabled input drops the keyboard's focus to the page.
 */
const props = withDefaults(
  defineProps<{
    modelValue: boolean
    /** The accessible name where no visible text names the box. */
    label?: string
    disabled?: boolean
    locked?: boolean
  }>(),
  { label: undefined },
)

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

/** Controlled: the box shows what the caller holds, not what the click made of it — a caller
 *  that refuses the change, or answers it later, keeps the box honest. */
function onChange(event: Event) {
  const input = event.target as HTMLInputElement
  if (!props.locked) {
    emit('update:modelValue', input.checked)
  }
  void nextTick(() => {
    input.checked = props.modelValue
  })
}
</script>

<template>
  <label class="checkbox" :class="{ disabled: disabled || locked }">
    <span class="control">
      <input
        class="native"
        type="checkbox"
        :checked="modelValue"
        :disabled="disabled"
        :aria-disabled="locked && !disabled ? 'true' : undefined"
        :aria-label="label"
        @change="onChange"
      >
      <span class="box" :class="{ on: modelValue }" aria-hidden="true">
        <UiIcon v-if="modelValue" name="check" />
      </span>
    </span>
    <span v-if="$slots.default" class="text"><slot /></span>
  </label>
</template>

<style scoped>
.checkbox {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  cursor: pointer;
}

.checkbox.disabled {
  cursor: default;
}

.control {
  position: relative;
  display: inline-flex;
  flex-shrink: 0;
  width: 18px;
  height: 18px;
}

/* Over the box, invisible: it takes the click, the keyboard and the focus. */
.native {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  margin: 0;
  opacity: 0;
  cursor: inherit;
}

.box {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  border: 1.5px solid var(--edge);
  border-radius: var(--radius-xs);
  background: var(--paper);
  color: var(--accent-fg);
  transition:
    background var(--duration-fast) var(--ease),
    border-color var(--duration-fast) var(--ease);
}

.checkbox:not(.disabled):hover .box {
  border-color: var(--faint);
}

.box.on {
  border-color: var(--accent);
  background: var(--accent);
}

/* The app's one focus ring, around the drawn box rather than the invisible input. */
.native:focus-visible + .box {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.box :deep(svg) {
  width: 12px;
  height: 12px;
  stroke-width: 3;
}

.native:disabled + .box,
.native[aria-disabled='true'] + .box {
  border-color: var(--line);
  background: var(--paper-sunken);
  color: var(--faint);
}

.native:disabled + .box.on,
.native[aria-disabled='true'] + .box.on {
  border-color: var(--faint);
  background: var(--faint);
  color: var(--accent-fg);
}

.text {
  min-width: 0;
  color: var(--ink);
}

.disabled .text {
  color: var(--muted);
}
</style>
