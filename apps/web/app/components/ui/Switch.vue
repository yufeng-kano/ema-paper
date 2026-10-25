<script setup lang="ts">
/**
 * The system's capsule switch on its own, for a settings row whose words are the row's
 * (docs/web.md § Pages, `/settings/<section>`): a `role="switch"` button carrying its name
 * as `aria-label`, since the row's label is a paragraph the button cannot be labelled by
 * without an id the row does not give. The agent panel's switches draw the same track
 * beside their own words.
 *
 * Not disabled while it saves — a disabled button drops the keyboard's focus — only busy;
 * the caller ignores a second click meanwhile.
 */
defineProps<{
  modelValue: boolean
  /** The accessible name: the words of the row it sits in. */
  label: string
  /** A save on its way. */
  busy?: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
</script>

<template>
  <button
    type="button"
    role="switch"
    class="switch"
    :aria-checked="modelValue"
    :aria-label="label"
    :aria-busy="busy || undefined"
    :disabled="disabled"
    @click="emit('update:modelValue', !modelValue)"
  >
    <span class="track" aria-hidden="true"><span class="knob" /></span>
  </button>
</template>

<style scoped>
.switch {
  display: inline-flex;
  align-items: center;
  height: var(--control-height-sm);
  padding: 0 var(--space-1);
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  cursor: pointer;
}

.switch[aria-busy='true'] {
  cursor: progress;
}

.switch:disabled {
  cursor: default;
  opacity: 0.5;
}

.track {
  position: relative;
  display: inline-block;
  flex-shrink: 0;
  width: 36px;
  height: 22px;
  border-radius: var(--radius-full);
  background: var(--fill-strong);
  transition: background var(--duration-fast) var(--ease);
}

.knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: var(--radius-full);
  background: var(--accent-fg);
  box-shadow: var(--shadow-segment);
  transition: left var(--duration-fast) var(--ease);
}

.switch[aria-checked='true'] .track {
  background: var(--accent);
}

.switch[aria-checked='true'] .knob {
  left: 16px;
}
</style>
