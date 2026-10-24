<script setup lang="ts">
/**
 * A glass capsule holding a toolbar's icon buttons — the screens' toolbars and the result
 * viewer's header (docs/web.md § Design system, *Glass is for what floats*).
 *
 * The buttons inside are plain `UiButton`s, `ghost` and `icon-only`: the capsule is the one
 * glass surface, and nothing glass sits on it. A button that is a toggle carries
 * `aria-pressed`, and the pressed one takes the accent tint.
 */
defineProps<{
  /** What the group of controls is, for assistive tech. */
  label?: string
}>()
</script>

<template>
  <div class="capsule" role="group" :aria-label="label">
    <slot />
  </div>
</template>

<style scoped>
.capsule {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
  height: var(--control-height-lg);
  padding: 0 var(--space-1);
  border-radius: var(--radius-full);
  background: var(--glass);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  box-shadow: var(--shadow-glass);
}

@supports not (backdrop-filter: blur(1px)) {
  .capsule {
    background: var(--paper);
  }
}

/* The capsule's buttons are the toolbar's: a little wider than tall, in the ink. */
.capsule :deep(.btn) {
  width: 40px;
  color: var(--ink);
}

/* A button whose menu is open stays filled while the menu is up. */
.capsule :deep(.btn:hover),
.capsule :deep(.btn[aria-expanded='true']) {
  background: var(--fill);
  color: var(--ink);
}

.capsule :deep(.btn svg) {
  width: 18px;
  height: 18px;
}

.capsule :deep(.btn[aria-pressed='true']) {
  background: var(--accent-tint);
  color: var(--accent);
}

/* The rule between two groups of one capsule. */
.capsule :deep(.capsule-rule) {
  width: 1px;
  height: 18px;
  margin: 0 3px;
  background: var(--line-strong);
}
</style>
