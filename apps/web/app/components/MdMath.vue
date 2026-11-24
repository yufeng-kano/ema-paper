<script setup lang="ts">
/**
 * One display-math block, typeset by KaTeX — `\begin{cases}` reads as the braced system
 * it is, not as source code (docs/web.md § Result viewer).
 *
 * `data-md` carries the block's `$$…$$` source, which is what copy-as-markdown hands
 * back. TeX KaTeX cannot parse falls back to a verbatim <pre> — a wrong formula is worse
 * than a visible one — and the <pre> path is what copy already serializes as `$$…$$`
 * (app/lib/copy.ts).
 */
import { renderTexHtml } from '~/lib/math'

const props = defineProps<{ tex: string }>()

const html = computed(() => renderTexHtml(props.tex, true))
const source = computed(() => `$$\n${props.tex}\n$$`)

/** Identifies this *occurrence*: a document can repeat a formula, and the copy handler's
 *  partial-selection set must not tar every twin with one boundary cut (docs/web.md). */
const blockId = useId()
</script>

<template>
  <!-- eslint-disable vue/no-v-html -- KaTeX escapes its input; `trust` is off -->
  <div
    v-if="html !== null"
    class="md-math-block"
    :data-md="source"
    :data-md-id="blockId"
    v-html="html"
  />
  <!-- eslint-enable vue/no-v-html -->
  <pre v-else class="md-math">{{ tex }}</pre>
</template>

<style scoped>
/* The formula keeps the body's colour; wider than the measure, it scrolls in place like a
   table rather than stretching the page.
   Sideways *only*. `overflow-y` is stated rather than left alone because CSS computes a
   `visible` axis to `auto` as soon as the other one is not — so `overflow-x: auto` alone
   hands a tall `\begin{cases}` a vertical scrollbar and shows half of it. The block has no
   height of its own, so hiding that axis expands to the formula instead of clipping it; the
   vertical padding is slack for the fraction bars and accents KaTeX draws a pixel or two
   outside its own box. */
.md-math-block {
  overflow-x: auto;
  overflow-y: hidden;
  overscroll-behavior-x: contain;
  padding-block: var(--space-1);
  color: var(--ink-soft);
}

.md-math-block :deep(.katex-display) {
  margin: 0;
}

/* The verbatim fallback, styled here rather than borrowed from the viewer's scope: the
   conditional root must not depend on parent scoped-style propagation to be readable. */
.md-math {
  margin: 0;
  overflow-x: auto;
  overflow-y: hidden;
  color: var(--ink-soft);
  font-family: var(--mono);
  font-size: var(--text-sm);
  line-height: 1.7;
  white-space: pre;
}
</style>
