<script setup lang="ts">
/**
 * A key cap (docs/web.md § Design system, *Primitives*). Its text is always the shortcut
 * helper's — `⌘⇧↑` on Apple platforms, `Ctrl+Shift+↑` elsewhere — never typed at a call site,
 * so two spellings of one key cannot drift apart.
 */
const props = defineProps<{
  /** The shortcut, as `lib/shortcuts.ts` writes one: `mod+shift+up`, `esc`. */
  keys: string
}>()

const { label } = useShortcuts()
</script>

<template>
  <kbd class="kbd">{{ label(props.keys) }}</kbd>
</template>

<style scoped>
.kbd {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 var(--space-1);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--muted);
  font-family: var(--font);
  font-size: var(--text-2xs);
  line-height: 1;
  white-space: nowrap;
}
</style>
